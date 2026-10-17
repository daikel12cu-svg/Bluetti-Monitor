"""
floating_widget.py - Widget Flotante Unificado (Gamer HUD + Barra Minimalista)
================================================================================
Reemplaza los antiguos modos separados "HUD Gamer" (que ocupaba toda la ventana
principal) y el script independiente "widget_minimalista.py" (que abría su
PROPIA conexión Bluetooth, entrando en conflicto con la app principal si ambos
se ejecutaban a la vez).

Este widget:
  1. Es una ventana Toplevel de verdad: puede estar abierta AL MISMO TIEMPO que
     el panel principal (no lo reemplaza ni lo oculta).
  2. Comparte la ÚNICA instancia de BluettiBleManager de la app -- nunca abre
     una segunda conexión Bluetooth.
  3. Tiene dos vistas intercambiables en la MISMA ventana: "gamer" (mini panel
     compacto) y "pill" (barra ultra-minimalista de una sola línea), con un
     botón para pasar de una a otra sin volver a pantalla completa.
  4. Transparencia SIMPLE de ventana única (atributo "-alpha" de Tk): afecta
     fondo y texto por igual. Se probó una variante con dos ventanas Win32 en
     capas (fondo + texto superpuesto) para que solo el fondo se transparentara
     dejando el texto siempre nítido, pero esa técnica resultó FRÁGIL: al ser
     dos HWND "topmost" independientes, Windows no garantiza cuál queda arriba
     en el z-order, así que la capa de fondo (sin ningún clic asociado) podía
     terminar tapando la capa de texto -- eso hacía que el widget se viera mal
     y no respondiera a clics, arrastre ni cierre. Se revirtió a una sola
     ventana por pedido explícito: prioridad a que funcione siempre por sobre
     el efecto visual de "solo el fondo se difumina".
"""

import collections
import faulthandler
import json
import os
import queue
import sys
import threading
import time
import traceback
import tkinter as tk
from ble_manager import alert_config, CHARGE_EFFICIENCY, CAP_DISCHARGE_FACTOR as DISCHARGE_LOSS, RATED_WH as BATTERY_CAPACITY_WH  # [alertas-patch] (fuente unica: ble_manager)
import tkinter.font as tkfont
from tkinter import Menu
from typing import Dict, Any, Optional

IS_WINDOWS = sys.platform.startswith("win")

# Atajo global para ocultar/mostrar el widget. Para cambiarlo basta con
# modificar estas tres constantes (por ejemplo Ctrl+Alt+B: _MOD_ALT | _MOD_CTRL).
_MOD_ALT = 0x0001
_MOD_CTRL = 0x0002
HOTKEY_MODIFIERS = _MOD_ALT
HOTKEY_VK = 0x42          # tecla "B"
HOTKEY_LABEL = "Alt+B"

# --- Modo OFFLINE (estimacion mientras no hay conexion) ---
OFFLINE_HISTORY_SECONDS = 300    # ventana de datos previos usada para la tendencia (5 min)
OFFLINE_MAX_DATA_AGE = 20        # si el ultimo dato tiene mas de esto, no hay base fiable
COLOR_OFFLINE = "#94a3b8"        # gris "Desconectado" (tiempo estimado offline)
COLOR_BT_CONNECTED = "#2563eb"   # azul (cliente)
COLOR_BT_CONNECTED_SERVER = "#22c55e"  # verde (servidor)
CHARGE_MODE_TRUST_SECONDS = 5.0    # tras conectar, el modo de carga leido aun puede ser el valor por defecto
DRAG_DEADZONE_PX = 8             # el arrastre no empieza hasta mover el mouse mas de esto
CONNECT_SETTLE_SECONDS = 1.0     # tras conectar, "Conectando..." hasta que lleguen las potencias reales
CURRENT_LIMIT_DC = 15.0           # A de entrada DC (solar) a partir de los cuales se avisa
CURRENT_LIMIT_AC = 12.0           # A de entrada AC (red) a partir de los cuales se avisa
CURRENT_HYSTERESIS = 0.5          # la alerta se apaga 0.5 A por debajo del limite (evita rebotes)
COLOR_OFFLINE_ARROW = "#60a5fa"   # flecha azul (claro, para que resalte) de tendencia offline
COLOR_BT_DISCONNECTED = "#ef4444"  # rojo (el ambar de "conectando" lo maneja _blink_tick)

# --- Alertas de TEMPERATURA de la bateria (promedio de los 4 sensores, clave "batt_temp") ---
TEMP_WARN_C = 43.0          # >= : "Atencion Bateria algo Caliente" (ambar)
TEMP_CRIT_C = 46.0          # >= : "Alerta Bateria muy Caliente" (rojo)
TEMP_HYST_C = 0.5           # histeresis: se sale de un nivel al bajar TEMP_HYST_C del umbral (rojo: <=45, ambar: <=42). 0 = sin histeresis
TEMP_REPEAT_S = 20 * 60     # aviso AMBAR cerrado a mano: no vuelve hasta pasados 20 min (el rojo no se repite por tiempo)
TEMP_AUTOCLEAR = True       # True = el aviso desaparece solo al bajar de umbral + histeresis


class TempAlertTracker:
    """Estado de las alertas de temperatura. UNA sola instancia compartida entre la app y el
    widget (cerrar el aviso en uno lo cierra en el otro). Reglas:
      AMBAR (>= 43 C): aparece y permanece hasta cerrarlo con la x. Mientras no se cierre, el
          triangulo parpadea en ambar; al cerrarlo el triangulo se apaga aunque siga caliente.
          Reaparece pasados 20 min desde el cierre si la condicion sigue.
      ROJO (>= 46 C): aparece y permanece hasta cerrarlo. El triangulo parpadea en rojo MIENTRAS
          dure la condicion (aunque se haya cerrado el aviso). El aviso NO se repite cada 20 min:
          solo vuelve a salir si la temperatura baja a 45 C (o menos) y despues vuelve a subir a 46.
      Si el ambar estaba cerrado y llega a rojo, el rojo sale de inmediato."""

    _RANK = {"ok": 0, "warning": 1, "critical": 2}
    _TEXT = {"warning": ("⚠ Atención: Batería algo Caliente", "#f59e0b"),
             "critical": ("⚠ ¡Alerta! Batería muy Caliente", "#ef4444")}

    def __init__(self):
        self.level = "ok"            # nivel actual segun la temperatura (con histeresis)
        self.visible = False         # hay un aviso pendiente de cerrar
        self.shown_level = "ok"      # nivel que muestra el aviso
        self._last_dismiss_ts = 0.0
        self._crit_armed = True      # el aviso rojo puede salir (se "rearma" al bajar de nivel critico)
        self._fire = False           # "acaba de aparecer / empeorar" (lo consume el widget)
        self._last_temp = None       # ultima temperatura conocida para mostrar en el aviso

    def _classify(self, temp: float) -> str:
        """Histeresis: para ENTRAR en un nivel hace falta llegar al umbral; para SALIR hay que
        bajar TEMP_HYST_C por debajo (evita que 45.9/46.0/45.9 dispare y apague sin parar)."""
        try:
            warn_c, crit_c, hyst_c = alert_config.temp_limits()   # configurables en Alertas de batería
        except Exception:
            warn_c, crit_c, hyst_c = TEMP_WARN_C, TEMP_CRIT_C, TEMP_HYST_C
        if self.level == "critical":
            if temp > crit_c - hyst_c:
                return "critical"
        elif temp >= crit_c:
            return "critical"
        if self.level in ("warning", "critical"):
            if temp > warn_c - hyst_c:
                return "warning"
        elif temp >= warn_c:
            return "warning"
        return "ok"

    def update(self, temp, is_conn: bool) -> bool:
        """Devuelve True si el aviso acaba de aparecer o de empeorar (conviene abrir el banner)."""
        if not is_conn or not isinstance(temp, (int, float)):
            self.level = "ok"        # sin datos fiables no se avisa; cierre manual y rearme se conservan
            self.visible = False
            return False
        self._last_temp = float(temp)
        lvl = self._classify(self._last_temp)
        self.level = lvl
        if lvl != "critical":
            self._crit_armed = True  # bajo de 46 (con histeresis): el rojo vuelve a poder avisar
        rank = self._RANK
        fire = False
        if self.visible:
            if lvl == "ok":
                if TEMP_AUTOCLEAR:
                    self.visible = False
            elif rank[lvl] != rank[self.shown_level]:
                fire = rank[lvl] > rank[self.shown_level]
                self.shown_level = lvl
        elif lvl == "critical":
            if self._crit_armed:     # una vez por episodio; sin repeticion cada 20 min
                self._crit_armed = False
                self.visible = True
                self.shown_level = "critical"
                fire = True
        elif lvl == "warning":
            if self._last_dismiss_ts == 0.0 or time.time() - self._last_dismiss_ts >= TEMP_REPEAT_S:
                self.visible = True
                self.shown_level = "warning"
                fire = True
        if fire:
            self._fire = True
        return fire

    def consume_fire(self) -> bool:
        f, self._fire = self._fire, False
        return f

    def dismiss(self):
        if self.visible:
            self.visible = False
            self._last_dismiss_ts = time.time()

    def severity(self) -> str:
        """Aporte al triangulo: rojo mientras dure la temperatura critica; ambar solo mientras
        el aviso este sin cerrar."""
        if self.level == "critical":
            return "critical"
        if self.visible and self.level == "warning":
            return "warning"
        return "ok"

    def text_color(self, force: bool = False):
        """(texto, color) del aviso si esta visible. Con force=True (el usuario toca el triangulo)
        tambien lo da si la temperatura sigue critica aunque se haya cerrado el aviso.
        El texto incluye la temperatura actual en tiempo real (ej: '⚠ Atención: Batería algo Caliente (44.2 °C)')."""
        if self.visible:
            level = self.shown_level
        elif force and self.level in ("warning", "critical"):
            level = self.level
        else:
            return None
        base_text, color = self._TEXT[level]
        temp_str = f" ({self._last_temp:.1f} °C)" if self._last_temp is not None else ""
        return (base_text + temp_str, color)


class _GlobalHotkey:
    """Atajo de teclado GLOBAL de Windows (funciona aunque el widget no tenga
    el foco, y aunque esté oculto), usando RegisterHotKey de user32 vía
    ctypes: no requiere instalar nada extra (ni "keyboard" ni "pynput", que
    además engordarían el .exe y suelen disparar antivirus por parecer
    keyloggers). RegisterHotKey está atado al hilo que lo registra, así que
    vive en su propio hilo con un bucle de mensajes; ese hilo NO toca Tk
    (no es seguro): solo avisa por callback y el hilo principal lo procesa."""

    def __init__(self, modifiers: int, vk: int, on_press):
        self._modifiers = modifiers
        self._vk = vk
        self._on_press = on_press
        self._thread_id = None
        self._ready = threading.Event()
        self.registered = False

    def start(self) -> bool:
        """True si Windows aceptó el atajo; False si no es Windows o si otra
        aplicación ya lo tiene registrado (Windows solo permite un dueño)."""
        if not IS_WINDOWS:
            return False
        threading.Thread(target=self._run, name="widget-hotkey", daemon=True).start()
        self._ready.wait(2.0)
        return self.registered

    def _run(self):
        try:
            import ctypes
            from ctypes import wintypes
            user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
            self._thread_id = kernel32.GetCurrentThreadId()
            MOD_NOREPEAT = 0x4000   # mantener pulsado no dispara repeticiones
            self.registered = bool(user32.RegisterHotKey(None, 1, self._modifiers | MOD_NOREPEAT, self._vk))
            self._ready.set()
            if not self.registered:
                return
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == 0x0312:   # WM_HOTKEY
                    self._on_press()
            user32.UnregisterHotKey(None, 1)
        except Exception:
            self.registered = False
            self._ready.set()

    def stop(self):
        if self.registered and self._thread_id:
            try:
                import ctypes
                ctypes.windll.user32.PostThreadMessageW(self._thread_id, 0x0012, 0, 0)  # WM_QUIT
            except Exception:
                pass


def _config_path() -> str:
    """Ruta del archivo de configuración persistente del widget (posición,
    tamaño/escala, transparencia y vista). Se guarda en el perfil del
    usuario de Windows (%LOCALAPPDATA%) -- NO junto al .exe, porque un
    ejecutable compilado con PyInstaller puede vivir en una carpeta de solo
    lectura (Archivos de Programa) y, en modo --onefile, se descomprime en
    una carpeta temporal distinta cada vez que se abre, así que escribir ahí
    no serviría para persistir nada entre una apertura y la siguiente. Al
    ser una sola función compartida por FloatingHudWidget, tanto la versión
    embebida en hud_app.py como el ejecutable standalone (widget_minimalista.py)
    leen y escriben el mismo archivo -- no hace falta duplicar nada."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "BluettiElite100Widget")
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception:
        pass
    return os.path.join(folder, "widget_config.json")


_CONFIG_PATH = _config_path()

# --- Diagnostico: registro en %LOCALAPPDATA%\\BluettiElite100Widget\\widget.log ---
# Sin consola (--noconsole) los errores se pierden; esto deja constancia de
# excepciones, cierres/ocultamientos y caidas duras del proceso.
_LOG_PATH = os.path.join(os.path.dirname(_CONFIG_PATH), "widget.log")
_fault_file = None


def _log(msg: str):
    try:
        if os.path.exists(_LOG_PATH) and os.path.getsize(_LOG_PATH) > 200_000:
            os.replace(_LOG_PATH, _LOG_PATH + ".old")
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _setup_diagnostics():
    global _fault_file
    try:
        _fault_file = open(_LOG_PATH + ".crash", "a", buffering=1)
        faulthandler.enable(_fault_file)   # volcado si el proceso muere de golpe
    except Exception:
        pass
    try:
        threading.excepthook = lambda a: _log(
            "Excepcion en hilo %s:\n%s" % (a.thread.name if a.thread else "?",
                                           "".join(traceback.format_exception(a.exc_type, a.exc_value, a.exc_traceback)))
        )
    except Exception:
        pass


_setup_diagnostics()

# Nombre COMPARTIDO entre hud_app.py (app completa) y widget_minimalista.py
# (widget standalone): los dos abren su PROPIA conexión Bluetooth al mismo
# equipo, así que el candado tiene que cubrir "cualquiera de los dos ya está
# corriendo", no solo "otra copia del mismo programa" -- si no, podrían
# quedar las dos compitiendo por el mismo adaptador Bluetooth igual que dos
# widgets sueltos.
_SINGLE_INSTANCE_MUTEX_NAME = "Global\\BluettiElite100_SingleInstance"
_single_instance_mutex_handle = None  # referencia que hay que mantener viva:
                                       # si Python la recolecta, Windows
                                       # liberaría el mutex antes de tiempo


def acquire_single_instance_lock() -> bool:
    """True si este proceso consiguió ser el único dueño de la conexión
    Bluetooth con el equipo (ni otra copia de la app, ni del widget, están
    corriendo ya). Usa un mutex con nombre a nivel de todo Windows (prefijo
    "Global\\"): el sistema operativo lo libera en cuanto el proceso dueño
    termina, sea cual sea el motivo (cierre normal, "Finalizar tarea", o un
    cuelgue), así que nunca puede quedar "atascado" bloqueando arranques
    futuros por un cierre anormal de una sesión anterior -- no hace falta
    liberarlo a mano ni limpiar nada."""
    global _single_instance_mutex_handle
    if not IS_WINDOWS:
        return True  # fuera de Windows (pruebas, etc.) no hay nada que proteger
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        ERROR_ALREADY_EXISTS = 183
        handle = kernel32.CreateMutexW(None, False, _SINGLE_INSTANCE_MUTEX_NAME)
        already_running = (kernel32.GetLastError() == ERROR_ALREADY_EXISTS)
        if not handle or already_running:
            return False
        _single_instance_mutex_handle = handle
        return True
    except Exception:
        # Si algo falla al verificar (p.ej. ctypes bloqueado por una política
        # de seguridad rara), mejor dejar arrancar que bloquear el programa
        # entero por un problema en esta protección extra.
        return True


def notify_already_running():
    """Aviso visible (mensaje emergente) de que esta copia no va a arrancar
    porque ya hay otra conectada -- un mensaje de consola no serviría, estos
    programas corren sin ventana de consola una vez compilados."""
    try:
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showwarning(
            "Bluetti Elite 100 V2",
            "Ya hay otra copia de la app o del widget conectada al equipo "
            "Bluetti -- la app completa y el widget no pueden usar el "
            "Bluetooth al mismo tiempo (competirían por la misma conexión).\n\n"
            "Esta copia se va a cerrar sin abrir nada.",
        )
        root.destroy()
    except Exception:
        pass


_STARTUP_REG_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_STARTUP_VALUE_NAME = "BluettiElite100Widget"          # widget standalone (como antes)
_STARTUP_APP_VALUE_NAME = "BluettiElite100App"          # app completa (arranca minimizada + widget)


def _startup_command(extra_args: str = "") -> str:
    """Comando a escribir en el registro de arranque. Si esto ya corre
    compilado con PyInstaller (--onefile), sys.executable ES el propio .exe
    (no un intérprete de Python), así que se usa directo. Si corre desde
    código fuente (python widget_minimalista.py), sys.executable es
    python.exe -- ahí hace falta agregar también la ruta del script, o
    Windows intentaría abrir python.exe sin decirle qué ejecutar."""
    exe = sys.executable
    suffix = f" {extra_args}" if extra_args else ""
    if getattr(sys, "frozen", False):
        return f'"{exe}"{suffix}'
    # Desde codigo fuente: pythonw.exe evita que salga una consola al iniciar sesion
    if exe.lower().endswith("python.exe"):
        w = exe[:-len("python.exe")] + "pythonw.exe"
        if os.path.exists(w):
            exe = w
    script = os.path.abspath(sys.argv[0])
    return f'"{exe}" "{script}"{suffix}'


def _is_startup_enabled(name: str = _STARTUP_VALUE_NAME) -> bool:
    """HKEY_CURRENT_USER (no HKLM) a propósito: no requiere permisos de
    administrador y es exactamente el alcance correcto para "que se abra
    cuando ESTE usuario inicia sesión"."""
    if not IS_WINDOWS:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY, 0, winreg.KEY_READ) as key:
            winreg.QueryValueEx(key, name)
        return True
    except Exception:
        return False


def _set_startup_enabled(enabled: bool, name: str = _STARTUP_VALUE_NAME, extra_args: str = "") -> bool:
    """Intenta aplicar el cambio; devuelve False si no se pudo (por ejemplo,
    en un sistema que no sea Windows) para que quien llama pueda revertir
    el check del menú en vez de dar por hecho que se guardó."""
    if not IS_WINDOWS:
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_REG_KEY, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, name, 0, winreg.REG_SZ, _startup_command(extra_args))
            else:
                try:
                    winreg.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
        return True
    except Exception:
        return False


def is_app_startup_enabled() -> bool:
    return _is_startup_enabled(_STARTUP_APP_VALUE_NAME)


def set_app_startup(enabled: bool) -> bool:
    """Registra/quita el arranque de la APP COMPLETA (minimizada en bandeja + widget visible).
    Al activarlo se quita la entrada antigua del widget suelto: si quedaran las dos, al iniciar
    sesion la segunda copia chocaria con el candado de instancia unica y mostraria un aviso."""
    ok = _set_startup_enabled(enabled, _STARTUP_APP_VALUE_NAME, "--minimized")
    if ok and enabled:
        _set_startup_enabled(False, _STARTUP_VALUE_NAME)
    return ok


class FloatingHudWidget(tk.Toplevel):
    """Ventana flotante Always-on-Top compartiendo la conexión BLE de la app."""

    def __init__(self, master, ble, standalone: bool = False, temp_tracker=None, on_open_app=None):
        self.standalone = standalone
        self._implicit_root = None
        if master is None:
            # Modo independiente (widget_minimalista.py): no hay ventana
            # principal, así que se crea una raíz oculta solo para sostener
            # el bucle de eventos de Tkinter.
            self._implicit_root = tk.Tk()
            self._implicit_root.withdraw()
            master = self._implicit_root

        super().__init__(master)
        self.ble = ble
        # Alertas de temperatura: embebido en la app se comparte SU tracker (la app lo actualiza);
        # standalone crea el suyo y lo actualiza el propio widget.
        self._temp_own = temp_tracker is None
        self.temp_tracker = temp_tracker if temp_tracker is not None else TempAlertTracker()
        # Callback para traer la ventana principal (solo embebido): habilita el boton "abrir panel".
        self._on_open_app = on_open_app
        self._force_temp = False   # el usuario toco el triangulo: listar tambien la temperatura critica ya cerrada
        # Tkinter solo es seguro desde el hilo que lo creo. En modo standalone
        # el gestor BLE llama a update_from_telemetry desde SU hilo: esas
        # llamadas se encolan y las procesa _poll_telemetry en el hilo de Tk.
        self._ui_thread_id = threading.get_ident()
        self._tele_queue = queue.Queue()
        try:
            root = self._root()
            _orig_report = root.report_callback_exception

            def _report(exc, val, tb, _o=_orig_report):
                _log("Excepcion en callback de Tk:\n" + "".join(traceback.format_exception(exc, val, tb)))
                _o(exc, val, tb)
            root.report_callback_exception = _report
        except Exception:
            pass
        _log("Widget iniciado")
        self._saved_cfg = self._load_config()
        self.pinned = bool(self._saved_cfg.get("pinned", False))   # posicion fijada (sin arrastre)
        self.view_mode = "gamer"          # "gamer" | "pill"
        self.alpha_val = float(self._saved_cfg.get("alpha", 0.90))
        self.scale_factor = float(self._saved_cfg.get("scale", 1.0))
        # Modo a restaurar al apagar Turbo: se aprende de la telemetria real
        # (ultimo modo silencioso/estandar visto) y se guarda entre sesiones,
        # asi funciona aunque el Turbo se haya activado desde otro lado.
        _pm = self._saved_cfg.get("prev_charge_mode")
        self._prev_charge_mode = _pm if _pm in ("silent", "standard", "custom") else "standard"
        self._drag_x = 0
        self._drag_y = 0
        self._press_xy = (0, 0)
        self._drag_active = False
        self._last_click_ac = 0.0
        self._last_click_dc = 0.0
        self._last_click_turbo = 0.0
        self._last_click_grid = 0.0

        # 20% menos oscuro que el original (#0b1120), mezclando 20% hacia
        # blanco por canal: sigue siendo el mismo tono navy oscuro, solo un
        # poco más claro, para que al bajar la opacidad se note menos negro
        # de fondo. Es un cambio de UN SOLO color de una ventana única (no
        # se vuelve a tocar el mecanismo de transparencia en sí) -- nada que
        # ver con el intento de capas separadas que rompió la legibilidad.
        self.bg_card = "#3c414d"
        self.border_color = "#1e293b"

        self.overrideredirect(True)
        self.wm_attributes("-topmost", True)
        self.configure(bg=self.bg_card)
        self._apply_alpha()
        self._strip_invisible_border()

        # Variables que alimentan los checks (radiobutton/checkbutton) del
        # menú contextual, para que se vea de un vistazo cuál ajuste está
        # aplicado en cada categoría. Se actualizan desde cada setter
        # (set_opacity, set_scale, show_gamer_view/show_pill_view) y desde
        # update_from_telemetry (estado de conexión); la de posición se
        # recalcula al vuelo justo antes de abrir el menú, porque cualquier
        # arrastre manual puede sacarla de los 4 presets fijos.
        self._opacity_var = tk.IntVar(master=self, value=round(self.alpha_val * 100))
        self._scale_var = tk.DoubleVar(master=self, value=self.scale_factor)
        self._view_var = tk.StringVar(master=self, value=self.view_mode)
        self._conn_var = tk.BooleanVar(master=self, value=False)
        self._pin_var = tk.BooleanVar(master=self, value=self.pinned)

        # Ocultar/mostrar con atajo global. La cola es el puente entre el hilo
        # del atajo y el hilo de Tk (ver _poll_hotkey).
        self._hidden = False
        self._hotkey_active = True
        self._hotkey_queue = queue.Queue()
        self._hotkey = _GlobalHotkey(HOTKEY_MODIFIERS, HOTKEY_VK, lambda: self._hotkey_queue.put(1))
        self._hotkey_registered = self._hotkey.start()

        # Posición recordada de la sesión anterior (si existe): se aplica ACÁ,
        # antes de programar _resize_to_content, para que cuando este corra
        # ya encuentre winfo_x()/winfo_y() con estos valores. self._has_saved_position
        # es la ÚNICA señal de "primera vez" que usa _do_resize -- antes se
        # inferían de si x<=0 y y<=0 en el momento, lo cual disparaba el
        # reposicionamiento por defecto (esquina superior derecha) cada vez
        # que el usuario arrastraba el widget cerca de la esquina superior
        # izquierda, "teletransportándolo" al otro lado. Con un flag fijo,
        # eso ya no puede volver a pasar.
        saved_x, saved_y = self._saved_cfg.get("x"), self._saved_cfg.get("y")
        self._has_saved_position = isinstance(saved_x, int) and isinstance(saved_y, int)
        if self._has_saved_position:
            self.geometry(f"+{saved_x}+{saved_y}")
        self._positioned_once = False

        # Estas listas/atributos las usan tanto la vista Gamer como la vista
        # Pill (ambas dibujan un ícono Bluetooth propio) -- se inicializan
        # ACÁ, antes de construir cualquiera de las dos vistas, para que
        # _build_gamer_view() ya encuentre self._bt_icons/self._bt_color
        # listos al agregar su propio ícono.
        self._scalable = []        # (widget, familia, tamaño_base_pt, negrita) para set_scale() -- solo vista Pill
        self._scalable_icons = []  # (canvas, tamaño_base_px, "bt"|"clock") para set_scale() -- solo vista Pill
        self._bt_icons = []        # (canvas, item_id) -- ícono(s) Bluetooth activos (Gamer + Pill)
        self._clock_items = []     # (canvas, item_id, "outline"|"fill") -- ícono(s) reloj activos (solo Pill)
        self._bt_color = "#64748b"
        self._clock_color = "#94a3b8"
        self._battery_items = []   # (canvas, item_id, ("fill","outline")) -- ícono de batería (solo Pill)
        self._battery_color = "#94a3b8"
        self._warning_items = []   # (canvas, item_id, "fill"|None) -- ícono(s) de alerta de voltaje (Gamer + Pill)
        self._warning_severity = "ok"  # "ok" | "warning" | "critical" -- combina voltaje + corriente + celdas
        self._volt_severity = "ok"     # solo voltaje de red
        self._cur_dc_high = False      # corriente DC >= CURRENT_LIMIT_DC (con histeresis)
        self._cur_ac_high = False      # corriente AC >= CURRENT_LIMIT_AC (con histeresis)
        self._current_high = False     # cualquiera de las dos
        self._cell_alerts = []         # [(texto, color, severidad)] alertas de celdas/balance/calibracion activas
        self._cell_alert_keys = set()  # textos de las alertas de celdas ya conocidas (para avisar solo al aparecer una nueva)
        self._banner_visible = False
        self._warning_blink_on = True

        # Tres condiciones de parpadeo INDEPENDIENTES entre sí -- cada una
        # parpadea sola según su propia condición, sin importar el estado de
        # las otras dos (ver _blink_tick). Cada "_color" guarda el color
        # sólido "real" (calculado en update_from_telemetry) para que el
        # parpadeo pueda alternar entre ese color y bg_card sin tener que
        # recalcularlo cada 500ms.
        self._batt_low = False          # Batería < 15%
        self._batt_blink_on = True
        self._batt_color = "#f43f5e"

        self._power_extreme = False     # |Neto| >= 1000W (carga o descarga)
        self._power_blink_on = True
        self._power_color = "#94a3b8"

        self._time_low = False          # Descargando con < 20 min restantes
        self._time_blink_on = True
        self._time_color = "#94a3b8"
        self._banner_hide_job = None

        # --- Avisos de las protecciones automaticas de alta corriente ---
        # (la logica vive en ble_manager.py; aqui solo se leen las claves prot_*)
        self._prot_seen_init = False        # la primera telemetria no dispara eventos "viejos"
        self._prot_dc_active_ui = False     # cuenta atras DC en curso
        self._prot_dc_end_ts = None
        self._prot_dc_dismissed = False     # el usuario cerro (x) el aviso de la cuenta atras
        self._prot_restored_seen = 0.0
        self._prot_restored_until = 0.0     # "Alta corriente restaurada" visible hasta este instante
        self._prot_ac_ts_seen = 0.0
        self._prot_ac_acked = False         # el usuario cerro (x) el aviso "AC Reducida"
        self._prot_ac_announced = False
        self._prot_ac_pending_ui = False    # aviso "AC Reducida" pendiente de cerrar
        self._prot_cons_seen = 0.0
        self._spike_seen = 0.0               # ultimo pico de arranque ya avisado
        self._spike_until = 0.0              # "Pico de Arranque" visible hasta este instante
        self._spike_text = ""
        self._prot_consump_active = False   # "Alta corriente por Consumo" mientras dure la alerta
        self._prot_sev = "ok"               # severidad que aporta al triangulo
        self._banner_persistent = False
        self._banner_lines_shown = []
        self._banner_labels = []
        self._vis = alert_config.VisState()   # que avisos se ven (tiempos / cerrados)

        # "runtime_minutes" (Reg 104) lo calcula el propio equipo y no se
        # actualiza en el mismo instante que los registros de potencia: al
        # pasar de cargar a descargar (o viceversa), durante un rato corto
        # "net" ya cambió de signo pero runtime_minutes todavía es la
        # estimación vieja del modo anterior (p.ej. "8 min para completar
        # la carga", un número chico por sí mismo) -- si en ese lapso se
        # evaluara como si fuera autonomía de descarga, caería directo en
        # rojo+parpadeo por error. GRACE_AFTER_MODE_CHANGE es cuánto se
        # ignora esa estimación (mostrando solo el color base de modo, sin
        # rojo ni parpadeo) después de cada cambio de signo de "net", dándole
        # tiempo al equipo a recalcularla. No afecta nada más: mientras el
        # modo no cambie, el tiempo restante se sigue leyendo y coloreando
        # exactamente igual que antes.
        self._last_power_mode = None    # "charging" | "discharging" | "neutral" | None
        self._mode_change_time = 0.0
        self.GRACE_AFTER_MODE_CHANGE = 3.0

        # Estado del modo OFFLINE: historial de hasta 5 min de muestras reales
        # (solo mientras hay conexion) y, al desconectarse, la proyeccion
        # congelada (soc base + tasa %/min + instante de origen).
        self._hist = collections.deque()   # (ts, soc, net, rate_runtime|None, modo)
        self._was_conn = False
        self._conn_since = 0.0
        self._offline_active = False
        self._off_origin_ts = 0.0
        self._off_soc0 = 0.0
        self._off_rate = 0.0               # %/min (+ carga, - descarga)
        self._off_last_key = None
        self._off_mode = "neutral"          # ultimo estado antes de desconectar
        self._menu_open = False
        self._u32 = None
        self._arrow_key = None

        # Única ventana: la "tarjeta" visible y todo el contenido viven aquí.
        self.backdrop = tk.Frame(self, bg=self.bg_card, highlightbackground=self.border_color, highlightthickness=1)
        self.backdrop.pack(fill="both", expand=True)

        self.content = tk.Frame(self.backdrop, bg=self.bg_card)
        self.content.pack(fill="both", expand=True)

        # Banner de alerta de voltaje: hermano de self.content dentro de
        # self.backdrop, así que al empaquetarlo aparece DEBAJO de la vista
        # activa (Gamer o Pill) sin importar cuál sea, duplicando la altura
        # visible mientras está mostrado. Oculto por defecto (sin pack()) --
        # _show_banner()/_hide_banner() lo muestran/ocultan y
        # _resize_to_content() ya mide self.backdrop completo, así que el
        # tamaño de la ventana se ajusta solo en ambos casos.
        # Mismo fondo (bg_card) que el resto: como todo es UNA sola ventana
        # con una sola opacidad, el banner hereda solo la transparencia
        # elegida. Sin borde propio (antes duplicaba el borde del widget):
        # solo una línea fina arriba que lo separa del contenido. Márgenes
        # internos a la mitad de los originales, igual que el resto del
        # widget, y el tamaño de letra se ajusta en _apply_banner_metrics().
        self.banner_frame = tk.Frame(self.backdrop, bg=self.bg_card)
        tk.Frame(self.banner_frame, height=1, bg="#98a1b3").pack(fill="x")
        banner_row = tk.Frame(self.banner_frame, bg=self.bg_card)
        banner_row.pack(fill="both", expand=True, padx=4, pady=3)
        self.banner_text = tk.Frame(banner_row, bg=self.bg_card)
        self.banner_text.pack(side="left", expand=True)
        self.banner_close = tk.Label(banner_row, text="×", font=("Segoe UI", 13, "bold"), bg=self.bg_card, fg="#94a3b8", cursor="hand2")
        self.banner_close.pack(side="right")
        self.banner_close.bind("<Button-1>", lambda e: self._on_banner_close())

        self._build_gamer_view()
        self._build_pill_view()

        # Escala guardada: se aplica DESPUÉS de construir la vista pill (ahí
        # se llena self._scalable, que es lo que set_scale() recorre).
        if abs(self.scale_factor - 1.0) > 0.001:
            self.set_scale(self.scale_factor)

        # Vista guardada (gamer o pill). show_pill_view()/show_gamer_view()
        # ya llaman a _resize_to_content() por su cuenta.
        if self._saved_cfg.get("view") == "pill":
            self.show_pill_view()
        else:
            self.show_gamer_view()

        self._is_connecting = True
        self._blink_on = False
        self.after(50, self._resize_to_content)
        self.after(500, self._blink_tick)
        self.after(1000, self._offline_tick)
        self.after(150, self._poll_telemetry)
        self.after(300, self._watchdog_tick)
        self.bind("<Unmap>", lambda e: e.widget is self and _log("Evento Unmap: la ventana se oculto/minimizo"))
        self.bind("<Map>", lambda e: e.widget is self and _log("Evento Map: la ventana se mostro"))
        if self._hotkey_registered:
            self.after(100, self._poll_hotkey)

    def _load_config(self) -> Dict[str, Any]:
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            # Primer uso (archivo no existe todavía) o archivo corrupto:
            # se sigue con los valores por defecto de siempre, sin romper
            # el arranque del widget.
            return {}

    def _save_config(self):
        """Guarda posición, escala, transparencia y vista actuales. Se llama
        después de cada cambio del usuario (arrastre, rueda del mouse, menú
        de opciones) -- nunca durante la carga inicial, para no pisar el
        archivo con los valores por defecto antes de aplicar los guardados."""
        try:
            self.update_idletasks()
            cfg = {
                "x": self.winfo_x(),
                "y": self.winfo_y(),
                "alpha": self.alpha_val,
                "scale": self.scale_factor,
                "view": self.view_mode,
                "pinned": self.pinned,
                "prev_charge_mode": self._prev_charge_mode,
            }
            with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(cfg, f)
        except Exception:
            pass

    def _strip_invisible_border(self):
        """EXPERIMENTAL, sin poder probarla yo mismo en Windows real. Intenta
        explicar el margen "fantasma" que se viene reportando (posición
        restaurada con hueco respecto al borde; ahora, reducir el padding a
        la mitad tampoco se nota ni horizontal ni verticalmente): en
        Windows, incluso con overrideredirect(True), a veces queda un borde
        invisible de "resize" reservado por el propio SO alrededor de la
        ventana -- totalmente ajeno al padding interno de Tkinter, así que
        ningún padx/pady lo reduce nunca. Esto limpia explícitamente esos
        bits de estilo (WS_CAPTION/WS_THICKFRAME) a nivel Win32, algo que
        overrideredirect ya debería hacer solo, pero que en algunas
        combinaciones de Windows/Tk parece no quedar del todo limpio.
        Si no cambia nada, el margen que se ve no es esto -- avisame para
        buscar por otro lado."""
        if not IS_WINDOWS:
            return
        try:
            import ctypes
            GWL_STYLE = -16
            WS_CAPTION = 0x00C00000
            WS_THICKFRAME = 0x00040000
            hwnd = ctypes.windll.user32.GetParent(self.winfo_id())
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_STYLE)
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_STYLE, style & ~(WS_CAPTION | WS_THICKFRAME))
        except Exception:
            pass

    def _apply_alpha(self):
        try:
            self.attributes("-alpha", self.alpha_val)
        except Exception:
            pass

    def _blink_tick(self):
        """Hace parpadear el ícono Bluetooth en ámbar mientras se intenta
        conectar. Se detiene solo (deja de alternar color) en cuanto llega
        telemetría real o el usuario pausa la conexión."""
        if getattr(self, "_is_connecting", False):
            self._blink_on = not self._blink_on
            self._set_bt_color("#f59e0b" if self._blink_on else "#7a4f10")

        # Ícono de advertencia: parpadea entre su color activo (ámbar/rojo
        # según severidad) y "apagado" (color de fondo, se vuelve invisible)
        # mientras la condición siga activa. Se detiene solo (deja de
        # llamarse desde acá) en cuanto vuelve a "ok".
        if self._warning_severity != "ok":
            self._warning_blink_on = not self._warning_blink_on
            target = "#f59e0b" if self._warning_severity == "warning" else "#ef4444"
            self._set_warning_color(target if self._warning_blink_on else self.bg_card)

        # Las tres condiciones siguientes son INDEPENDIENTES: cada sección
        # parpadea sola según su propia condición, no todas a la vez ni
        # atadas a las otras dos.
        if self._batt_low:
            self._batt_blink_on = not self._batt_blink_on
            color = self._batt_color if self._batt_blink_on else self.bg_card
            self.hud_soc_val.configure(fg=color)
            self.pill_soc.configure(fg=color)
            self._set_battery_color(color)

        if self._power_extreme:
            self._power_blink_on = not self._power_blink_on
            color = self._power_color if self._power_blink_on else self.bg_card
            self.hud_net_power.configure(fg=color)
            self.pill_status.configure(fg=color)

        if self._time_low:
            self._time_blink_on = not self._time_blink_on
            color = self._time_color if self._time_blink_on else self.bg_card
            self.pill_time_est.configure(fg=color)
            self.hud_time_est.configure(fg=color)
            self._set_clock_color(color)

        self._prot_ui_tick()

        self.after(500, self._blink_tick)

    # ------------------------------------------------------------------
    # Íconos dibujados en Canvas (no dependen de ninguna fuente/emoji
    # especial -- así evitamos que vuelva a mostrarse un símbolo equivocado
    # o que se vea distinto según la fuente instalada). Se dibujan como
    # POLÍGONO RELLENO -- no como línea delgada -- para que se vean nítidos
    # y sólidos incluso a tamaños pequeños, en vez de un trazo fino
    # "hecho a mano" que se ve tosco.
    # ------------------------------------------------------------------
    def _draw_bt_icon(self, canvas: tk.Canvas, size: int, color: str):
        """Silueta del logo Bluetooth como TRAZO. Las versiones anteriores
        reconstruían el glifo "a ojo" y el orden de puntos no era exacto
        (se veía como un asterisco/estrella deforme). Estos puntos NO son
        inventados: son los del icono "bluetooth" del set open-source
        Feather Icons (MIT license) -- polyline
        "6.5 6.5 17.5 17.5 12 23 12 1 17.5 6.5 6.5 17.5" sobre un
        viewBox de 24x24 -- normalizados a 0..1 y reescalados aquí a
        cualquier tamaño de pixel. Al ser la geometría real del glifo
        (y no una reconstrucción de memoria), el trazo con esquinas
        redondeadas se ve limpio en vez de abultado."""
        s = size
        pts = [
            s * (6.5 / 24), s * (6.5 / 24),    # arriba-izquierda -> diagonal a...
            s * (17.5 / 24), s * (17.5 / 24),  # ...abajo-derecha
            s * (12.0 / 24), s * (23.0 / 24),  # punta inferior
            s * (12.0 / 24), s * (1.0 / 24),   # (salto vertical) punta superior
            s * (17.5 / 24), s * (6.5 / 24),   # diagonal a abajo-izquierda...
            s * (6.5 / 24), s * (17.5 / 24),   # ...
        ]
        width = max(1.5, s * 0.095)
        line_id = canvas.create_line(*pts, fill=color, width=width, joinstyle="round", capstyle="round")
        return line_id

    def _draw_clock_icon(self, canvas: tk.Canvas, size: int, color: str):
        """Reloj simple (esfera + dos manecillas) dibujado a mano, con el
        mismo criterio que el ícono Bluetooth: nítido a cualquier tamaño y
        sin depender de que el emoji de reloj se vea igual en todos lados."""
        s = size
        r = s * 0.42
        cx = cy = s / 2.0
        w = max(1.3, s * 0.09)
        oval_id = canvas.create_oval(cx - r, cy - r, cx + r, cy + r, outline=color, width=w)
        hour_id = canvas.create_line(cx, cy, cx, cy - r * 0.55, fill=color, width=w, capstyle="round")
        min_id = canvas.create_line(cx, cy, cx + r * 0.72, cy, fill=color, width=w, capstyle="round")
        return [(oval_id, "outline"), (hour_id, "fill"), (min_id, "fill")]

    def _draw_warning_icon(self, canvas: tk.Canvas, size: int, color: str):
        """Triángulo de advertencia con '!' -- geometría simple (triángulo +
        barra + punto), a propósito NADA parecida a la complejidad del glifo
        Bluetooth: acá SÍ hace falta poder cambiar el color de verdad (ámbar
        para inestable, rojo para crítico), y un ícono de texto/emoji de
        color fijo (como ⚠) no se puede recolorear con fg=. Dibujarlo entero
        es lo que permite ambos colores con el mismo ícono."""
        s = size
        tri = canvas.create_polygon(
            s * 0.5, s * 0.06,
            s * 0.05, s * 0.92,
            s * 0.95, s * 0.92,
            fill=color, outline=color,
        )
        bar = canvas.create_rectangle(
            s * 0.44, s * 0.36, s * 0.56, s * 0.62,
            fill=self.bg_card, outline=self.bg_card,
        )
        dot = canvas.create_oval(
            s * 0.44, s * 0.70, s * 0.56, s * 0.82,
            fill=self.bg_card, outline=self.bg_card,
        )
        return [(tri, "fill"), (bar, None), (dot, None)]

    @staticmethod
    def _battery_pixel_rects(size: int):
        """Geometría de la batería en PÍXELES ENTEROS: lista de rectángulos
        (x0, y0, x1, y1) rellenos, con x1/y1 excluidos. Todo son enteros y
        líneas de 1 px, así que el dibujo es idéntico y nítido a cualquier
        tamaño (Tk no tiene antialiasing: con coordenadas fraccionarias o
        trazos de 1.3 px los bordes salían irregulares y los signos +/− se
        borraban). Las esquinas redondeadas se logran recortando los píxeles
        de las esquinas del contorno, no con curvas."""
        s = int(size)
        nh = max(1, round(s * 0.10))                 # alto del borne
        w = int(s * 0.70)
        w = max(5, w if w % 2 else w - 1)            # ancho del cuerpo, impar (hay columna central)
        x0 = (s - w) // 2
        x1 = x0 + w
        y0, y1 = nh, s
        r = 2 if w >= 12 else 1                      # radio de esquina (px)
        rects = []

        nw = max(3, (round(w * 0.4) // 2) * 2 + 1)   # borne, ancho impar y centrado
        nx = x0 + (w - nw) // 2
        rects.append((nx, 0, nx + nw, nh))

        rects.append((x0 + r, y0, x1 - r, y0 + 1))   # borde superior
        rects.append((x0 + r, y1 - 1, x1 - r, y1))   # borde inferior
        rects.append((x0, y0 + r, x0 + 1, y1 - r))   # borde izquierdo
        rects.append((x1 - 1, y0 + r, x1, y1 - r))   # borde derecho
        if r == 2:                                   # píxel diagonal de cada esquina
            for cx, cy in ((x0 + 1, y0 + 1), (x1 - 2, y0 + 1), (x0 + 1, y1 - 2), (x1 - 2, y1 - 2)):
                rects.append((cx, cy, cx + 1, cy + 1))

        # Signos: "+" arriba, "−" abajo, largo impar, trazo de 1 px, centrados.
        ih, iw = y1 - y0 - 2, w - 2
        length = 3 if ih <= 8 else (5 if ih <= 13 else 7)
        length = min(length, iw - 2)
        margin = 1 if ih <= 12 else 2
        cx = x0 + w // 2
        top = y0 + 1 + margin
        mid = top + length // 2
        left = cx - length // 2
        rects.append((left, mid, left + length, mid + 1))          # "+" horizontal
        rects.append((cx, top, cx + 1, top + length))              # "+" vertical
        row = y1 - 1 - margin - 1
        rects.append((left, row, left + length, row + 1))          # "−"
        return rects

    def _draw_battery_icon(self, canvas: tk.Canvas, size: int, color: str):
        """Batería vertical vacía (contorno de esquinas redondeadas + borne +
        signos +/−) dibujada píxel a píxel para poder recolorearla igual que
        el número y el %: el emoji 🔋 que había antes tiene el verde "cocido"
        en la propia fuente y fg= no lo cambia."""
        items = []
        for x0, y0, x1, y1 in self._battery_pixel_rects(size):
            item = canvas.create_rectangle(x0, y0, x1, y1, fill=color, outline="")
            items.append((item, ("fill",)))
        return items

    def _set_battery_color(self, color: str):
        self._battery_color = color
        for cv, item_id, opts in getattr(self, "_battery_items", []):
            try:
                cv.itemconfig(item_id, **{o: color for o in opts})
            except Exception:
                pass

    def _set_bt_color(self, color: str):
        self._bt_color = color
        for cv, item_id in getattr(self, "_bt_icons", []):
            try:
                cv.itemconfig(item_id, fill=color)
            except Exception:
                pass

    def _set_clock_color(self, color: str):
        self._clock_color = color
        for cv, item_id, opt in getattr(self, "_clock_items", []):
            try:
                cv.itemconfig(item_id, **{opt: color})
            except Exception:
                pass

    def _set_warning_color(self, color: str):
        """Solo recolorea el triángulo (opt=='fill'); la barra y el punto
        del '!' quedan siempre del color de fondo (son el hueco negativo del
        signo), sin importar si el triángulo está en ámbar, rojo o "apagado"
        (color de fondo, durante el parpadeo)."""
        for cv, item_id, opt in getattr(self, "_warning_items", []):
            if opt != "fill":
                continue
            try:
                cv.itemconfig(item_id, fill=color, outline=color)
            except Exception:
                pass

    def set_opacity(self, alpha: float):
        # Se redondea a multiplos de 5% para que siempre coincida con una opcion del menu
        self.alpha_val = max(0.25, min(1.0, round(alpha * 20) / 20))
        self._apply_alpha()
        self._opacity_var.set(round(self.alpha_val * 100))
        self._save_config()

    def set_scale(self, scale: float):
        """Escala la fuente de todos los elementos de la vista minimalista.
        0.5 (50%) es el tamaño mínimo y 1.5 (150%) el máximo."""
        self.scale_factor = round(max(0.5, min(1.5, scale)), 2)
        self._scale_var.set(self.scale_factor)
        for widget, family, base_size, bold in getattr(self, "_scalable", []):
            new_size = max(5, round(base_size * self.scale_factor))
            widget.configure(font=(family, new_size, "bold" if bold else "normal"))
        new_bt_icons, new_clock_items, new_battery_items = [], [], []
        for cv, base_px, kind in getattr(self, "_scalable_icons", []):
            new_px = max(8, round(base_px * self.scale_factor))
            cv.configure(width=new_px, height=new_px)
            cv.delete("all")
            if kind == "bt":
                item_id = self._draw_bt_icon(cv, new_px, self._bt_color)
                new_bt_icons.append((cv, item_id))
            elif kind == "battery":
                items = self._draw_battery_icon(cv, new_px, self._battery_color)
                new_battery_items.extend((cv, iid, opts) for iid, opts in items)
            else:
                items = self._draw_clock_icon(cv, new_px, self._clock_color)
                new_clock_items.extend((cv, iid, opt) for iid, opt in items)
        if new_bt_icons:
            # Solo los íconos Bluetooth "escalables" (vista Pill) se
            # reconstruyeron arriba -- el de la vista HUD es de tamaño fijo
            # y no pasa por _scalable_icons, así que hay que conservarlo acá
            # en vez de reemplazar toda la lista (si no, quedaría sin
            # sincronizar color tras el primer cambio de escala).
            scalable_canvases = {cv for cv, _, _ in getattr(self, "_scalable_icons", [])}
            kept = [(cv, iid) for cv, iid in self._bt_icons if cv not in scalable_canvases]
            self._bt_icons = kept + new_bt_icons
        if new_clock_items:
            self._clock_items = new_clock_items
        if new_battery_items:
            self._battery_items = new_battery_items
        self._refresh_offline_arrow()
        self._resize_to_content()
        self._save_config()

    # ------------------------------------------------------------------
    # Banner de alerta de voltaje de red (aparece debajo de la vista activa,
    # se cierra solo a los 5s o con la cruz)
    # ------------------------------------------------------------------
    def _banner_is_persistent(self) -> bool:  # [alertas-patch]
        ns = alert_config.notices()
        return bool(self._banner_is_persistent_impl()) or bool(ns.any_active() and self._notices_visible())

    def _banner_is_persistent_impl(self) -> bool:
        """True si hay un aviso de proteccion que NO debe ocultarse solo."""
        return ((self._prot_dc_active_ui and not self._prot_dc_dismissed) or self._prot_ac_pending_ui
                or self.temp_tracker.visible)

    def _render_banner_lines(self, lines):
        while len(self._banner_labels) < len(lines):
            self._banner_labels.append(
                tk.Label(self.banner_text, text="", font=("Segoe UI", 10, "bold"), bg=self.bg_card, justify="center")
            )
        for lb in self._banner_labels:
            lb.pack_forget()
        for lb, (text, color) in zip(self._banner_labels, lines):
            lb.configure(text=text, fg=color)
            lb.pack(anchor="center")
        self._banner_lines_shown = list(lines)

    def _show_banner(self, lines, persistent: bool = False):
        """lines = [(texto, color), ...]. Se oculta solo a los 5 s salvo que
        persistent=True (proteccion en curso): entonces solo cierra la x."""
        if self._banner_hide_job is not None:
            try:
                self.after_cancel(self._banner_hide_job)
            except Exception:
                pass
            self._banner_hide_job = None
        self._render_banner_lines(lines)
        self._apply_banner_metrics()
        self.banner_frame.pack(fill="x")
        self._banner_visible = True
        self._banner_persistent = persistent
        self._resize_to_content()
        if not persistent:
            self._banner_hide_job = self.after(5000, self._hide_banner)

    def _apply_banner_metrics(self):
        """Ajusta la letra del banner a la escala de la barra y limita su
        ancho al del contenido. El texto es 1 pt mas grande que antes, pero
        SOLO si sigue cabiendo en una linea dentro del ancho disponible: asi
        la ventana del aviso no crece (si no cabe, se usa el tamano anterior)."""
        scale = self.scale_factor if self.view_mode == "pill" else 1.0
        old_size = max(8, round(10 * scale))
        size = max(8, round(11 * scale))
        self.banner_close.configure(font=("Segoe UI", max(9, round(13 * scale)), "bold"))
        try:
            used = self._banner_labels[:len(self._banner_lines_shown)]
            for lb in used:
                lb.configure(font=("Segoe UI", old_size, "bold"))
            self.update_idletasks()
            available = self.content.winfo_reqwidth() - self.banner_close.winfo_reqwidth() - 14
            texts = [lb.cget("text") for lb in used]
            while size > old_size:
                f = tkfont.Font(family="Segoe UI", size=size, weight="bold")
                if all(f.measure(tx) <= available - 6 for tx in texts):
                    break
                size -= 1
            for lb in used:
                lb.configure(font=("Segoe UI", size, "bold"), wraplength=max(90, available))
        except Exception:
            pass

    def _hide_banner(self):
        if self._banner_hide_job is not None:
            try:
                self.after_cancel(self._banner_hide_job)
            except Exception:
                pass
            self._banner_hide_job = None
        self._banner_visible = False
        self._force_temp = False
        self._banner_persistent = False
        self.banner_frame.pack_forget()
        self._resize_to_content()

    def _notices_visible(self):  # [alertas-patch]
        # igual que la barra de estado de la app: los avisos solo se ven con conexión real
        try:
            return bool(self.ble.state.get("connected", False)) and not self.ble.is_paused
        except Exception:
            return False

    def _on_banner_close(self):  # [alertas-patch]
        self._vis.close(self._banner_lines_shown)
        return self._on_banner_close_impl()

    def _on_banner_close_impl(self):
        """Cruz del banner. Cierra el aviso y, segun lo que estuviera activo:
        - "Alta corriente AC Reducida": queda reconocido (no vuelve a salir).
        - Cuenta atras DC: se oculta, pero la proteccion sigue; se puede volver
          a ver tocando el triangulo.
        - Batería Crítica (5% o 4%): se pospone al siguiente nivel."""
        self.temp_tracker.dismiss()   # la x cierra tambien el aviso de temperatura (reaparece a los 30 min)
        if self._prot_ac_pending_ui:
            self._prot_ac_pending_ui = False
            self._prot_ac_acked = True
            try:
                self.ble.ack_ac_reduced_notice()
            except Exception:
                _log("No se pudo reconocer el aviso de AC reducida:\n" + traceback.format_exc())
        if self._prot_dc_active_ui:
            self._prot_dc_dismissed = True
        # Batería Crítica: detectar si se cerró un aviso de 5% o 4%
        for it in self._banner_lines_shown:
            text = str(it[0]).lower()
            if "batería crítica (5%)" in text or "bateria critica (5%)" in text:
                try:
                    self.ble.ack_bat_crit_notice(5)
                except Exception:
                    _log("No se pudo posponer batería crítica 5%:\n" + traceback.format_exc())
                break
            if "batería crítica (4%)" in text or "bateria critica (4%)" in text:
                try:
                    self.ble.ack_bat_crit_notice(4)
                except Exception:
                    _log("No se pudo posponer batería crítica 4%:\n" + traceback.format_exc())
                break
        self._hide_banner()
        try:
            self.update_from_telemetry(dict(self.ble.state))   # refresca el triangulo ya
        except Exception:
            pass

    def _prot_dc_countdown_text(self) -> str:
        end = self._prot_dc_end_ts
        if not end:
            rem = 60            # el cambio a "Otros" aun no esta confirmado
        else:
            rem = max(0, int(end - time.time() + 0.999))
        return f"{rem // 60}:{rem % 60:02d}"

    def _active_alerts(self):  # [alertas-patch]
        lines = list(alert_config.filter_alert_lines(self._active_alerts_impl()))
        if self._notices_visible():
            lines.extend((x[0], x[1]) for x in alert_config.notices().lines())
        return lines

    def _active_alerts_impl(self):
        """Lista (texto, color) de las alertas activas. Primero las protecciones
        automaticas, luego las rojas."""
        alerts = []
        now = time.time()
        # Protecciones automaticas de alta corriente
        if self._prot_dc_active_ui:
            alerts.append((f"Protección de Alta corriente DC {self._prot_dc_countdown_text()}", "#38bdf8"))
        if now < self._prot_restored_until:
            alerts.append(("Alta corriente restaurada", "#10b981"))
        if now < self._spike_until:
            alerts.append((self._spike_text, "#fb923c"))
        if self._prot_ac_pending_ui:
            alerts.append(("Alta corriente AC Reducida", "#f59e0b"))
        temp_alert = self.temp_tracker.text_color(True)   # la visibilidad la decide VisState
        if temp_alert:
            alerts.append(temp_alert)
        # Celdas criticas (voltaje por celda fuera de rango seguro): lo mas grave
        for text, color, sev in self._cell_alerts:
            if sev == "critical":
                alerts.append((text, color))
        if self._volt_severity == "critical":
            alerts.append((self._grid_alarm_text(), "#f87171"))
        if self._cur_dc_high:
            alerts.append(("⚠ ¡Precaución Alta Corriente DC!", "#ef4444"))
        if self._cur_ac_high:
            if self._prot_consump_active:
                # Entrada AC alta con el modo Silencioso ya activo: es consumo, no carga
                alerts.append(("Alta corriente por Consumo", "#ef4444"))
            else:
                alerts.append(("⚠ ¡Precaución Alta Corriente AC!", "#ef4444"))
        if self._volt_severity == "warning":
            alerts.append(("⚠ ¡Precaución Voltaje Inestable!", "#f59e0b"))
        # Avisos de batería no criticos: desbalance de celdas / posible descalibración
        for text, color, sev in self._cell_alerts:
            if sev != "critical":
                alerts.append((text, color))
        return alerts

    def _visible_alerts(self):
        """Avisos que toca ver ahora (tiempos configurados; cerrados fuera; triangulo tocado = tambien los que dejan triangulo)."""
        return self._vis.visible(self._active_alerts(), self._force_temp)

    def _show_active_alerts(self):
        """Muestra el banner con todas las alertas activas ahora mismo (5 s, o
        hasta cerrarlo si hay una proteccion automatica pendiente)."""
        alerts = self._visible_alerts()
        if not alerts:
            return
        # El banner se cierra solo cuando vencen sus avisos (_prot_ui_tick); tocar el triangulo lo muestra 5 s
        self._show_banner(alerts, persistent=not self._force_temp)

    def _prot_ui_read(self, t: Dict[str, Any], is_conn: bool) -> bool:
        """Lee las claves prot_* de la telemetria. Devuelve True si hay un evento
        nuevo que merece abrir el banner (inicio de proteccion DC, restauracion,
        AC reducida, alta corriente por consumo)."""
        now = time.time()
        fire = False

        # Cuenta atras DC
        dc_active = bool(t.get("prot_dc_active")) and is_conn
        if dc_active and not self._prot_dc_active_ui:
            self._prot_dc_dismissed = False
            fire = True
        if not dc_active:
            self._prot_dc_dismissed = False
        self._prot_dc_active_ui = dc_active
        self._prot_dc_end_ts = t.get("prot_dc_end_ts") if dc_active else None

        # Restauracion a Panel
        rts = float(t.get("prot_dc_restored_ts") or 0.0)
        if self._prot_seen_init and rts > self._prot_restored_seen:
            self._vis.reset("dc_restored")
            self._prot_restored_until = now + alert_config.event_secs("dc_restored")
            fire = True
        self._prot_restored_seen = rts

        # AC reducida (permanece hasta que el usuario la cierre)
        ats = float(t.get("prot_ac_reduced_ts") or 0.0)
        if ats > self._prot_ac_ts_seen:
            self._prot_ac_acked = False
            self._prot_ac_announced = False
        self._prot_ac_ts_seen = ats
        backend_pending = bool(t.get("prot_ac_reduced_pending"))
        if not backend_pending:
            self._prot_ac_acked = False
            self._prot_ac_announced = False
        pending = backend_pending and not self._prot_ac_acked
        if pending and not self._prot_ac_announced:
            self._prot_ac_announced = True
            fire = True
        self._prot_ac_pending_ui = pending

        # Alta corriente por consumo
        cts = float(t.get("prot_ac_consumption_ts") or 0.0)
        if self._prot_seen_init and cts > self._prot_cons_seen:
            self._prot_consump_active = True
            fire = True
        self._prot_cons_seen = cts

        # Pico de arranque (evento puntual; lo detecta ble_manager.py)
        sts = float(t.get("spike_ts") or 0.0)
        if self._prot_seen_init and sts > self._spike_seen:
            self._spike_text = f"Pico de Arranque de {int(t.get('spike_w') or 0)} W"
            self._vis.reset("spike")
            self._spike_until = now + alert_config.event_secs("spike")
            fire = True
        self._spike_seen = sts

        self._prot_seen_init = True
        self._prot_sev = "warning" if (dc_active or pending) else "ok"
        return fire

    def _prot_ui_tick(self):
        """Cada 500 ms: mientras un aviso persistente este visible, actualiza su
        contenido (cuenta atras, fin de "restaurada", etc.)."""
        try:
            if not (self._banner_visible and self._banner_persistent):
                return
            alerts = self._visible_alerts()
            if not alerts:
                self._hide_banner()
                return
            if alerts != self._banner_lines_shown:
                self._render_banner_lines(alerts)
                self._apply_banner_metrics()
                self._resize_to_content()
        except Exception:
            _log("Error en _prot_ui_tick:\n" + traceback.format_exc())

    @staticmethod
    def _build_cell_alerts(*args, **kwargs):  # [alertas-patch]
        return alert_config.filter_cell_alerts(FloatingHudWidget._build_cell_alerts_impl(*args, **kwargs))

    @staticmethod
    def _build_cell_alerts_impl(t: Dict[str, Any], is_conn: bool):
        """Alertas de la batería a partir de la telemetría: celda crítica
        (rojo), desbalance de celdas y posible descalibración del % (ámbar).
        Devuelve [(texto, color, severidad)]. Sin conexión no se avisa nada
        (los datos serían viejos)."""
        alerts = []
        if not is_conn:
            return alerts
        cells = ", ".join(f"#{c}" for c in (t.get("cell_alert_cells") or [])[:4])
        if t.get("cell_alert") == "low":
            alerts.append((f"⚠ ¡Celda crítica BAJA ({cells})!", "#ef4444", "critical"))
        elif t.get("cell_alert") == "high":
            alerts.append((f"⚠ ¡Celda crítica ALTA ({cells})!", "#ef4444", "critical"))

        # Aviso de voltaje de celda (ambar): fuera de lo normal pero aun lejos del limite critico.
        # ble_manager lo deja en "ok" mientras haya una alerta critica activa.
        warn = t.get("cell_warn")
        if warn in ("low", "high"):
            wcells = ", ".join(f"#{c}" for c in (t.get("cell_warn_cells") or [])[:4])
            alerts.append((f"⚠ Celda {'baja' if warn == 'low' else 'alta'} fuera de rango ({wcells})",
                           "#f59e0b", "warning"))

        bal = t.get("cell_balance")
        reliable = bool(t.get("cell_balance_reliable"))
        plateau = t.get("cell_zone", "plateau") == "plateau"
        if bal == "unbalanced":
            # En la meseta un delta alto es dispersion (resistencia/conexion/temperatura), no falta de carga
            alerts.append(("⚠ Dispersión alta entre celdas" if plateau else "⚠ Batería desbalanceada (calibrar)",
                           "#f59e0b", "warning"))
        elif bal == "slight" and reliable:
            alerts.append(("⚠ Dispersión leve entre celdas" if plateau else "⚠ Desbalance leve entre celdas",
                           "#f59e0b", "warning"))

        if t.get("soc_calibration") == "suspect":
            alerts.append(("⚠ % de carga posiblemente descalibrado", "#f59e0b", "warning"))
        return alerts

    def _grid_low_alarm_active(self) -> bool:
        """True si el EQUIPO reporta la alarma de bajo voltaje de red (con conexion real).
        Solo sirve para ESCALAR a critica cuando el voltaje ya esta bajo; nunca crea una
        alarma por si sola (ver _update_alerts)."""
        try:
            if self.ble.is_paused or not self.ble.state.get("connected", False):
                return False
            return bool(alert_config.mask_telemetry(self.ble.state).get("grid_low_voltage_alarm", False))
        except Exception:
            return False

    def _grid_alarm_text(self) -> str:
        """Texto de la alarma critica de red, con el voltaje que la disparo (si es > 0)."""
        try:
            v = float(getattr(self, "_alert_volts", 0.0) or 0.0)
        except Exception:
            v = 0.0
        base = "⚠ ¡Alarma Bajo Voltaje de Red!"
        return f"{base} {v:.1f}V" if v > 0 else base

    def _update_alerts(self, ac_input_volts, dc_amps, ac_amps, *args, **kwargs):  # [alertas-patch]
        real_volts = ac_input_volts
        ac_input_volts, dc_amps, ac_amps = alert_config.widget_gate(
            globals(), self.ble.state.get("charge_mode"), ac_input_volts, dc_amps, ac_amps)
        result = self._update_alerts_impl(ac_input_volts, dc_amps, ac_amps, *args, **kwargs)
        self._alert_volts = real_volts   # el texto de la alarma muestra el voltaje REAL, no el traducido
        return result

    def _update_alerts_impl(self, ac_input_volts: float, dc_amps: float, ac_amps: float, cell_alerts=()):
        """Alertas de entrada: VOLTAJE de red (106V inestable / 101V bajo) y
        CORRIENTE (DC >= 15 A o AC >= 12 A, con histeresis de 0.5 A), más las
        alertas de la batería (celda crítica, desbalance, descalibración).
        El banner emergente solo aparece al EMPEORAR (cruzar a un umbral peor,
        entrar en alta corriente o aparecer una alerta de batería NUEVA); el
        icono persistente refleja siempre el estado actual combinado, parpadea
        mientras dure y se puede tocar para volver a ver/ocultar las
        notificaciones."""
        rank = {"ok": 0, "warning": 1, "critical": 2}

        # Voltaje de red (0V = red no conectada, no cuenta como falla)
        self._alert_volts = ac_input_volts   # ALARMA_RED_V2: la alarma la manda el VOLTAJE
        if ac_input_volts <= 0:
            volt_sev = "ok"   # apagon / sin red: no hay alarma, aunque el equipo conserve su aviso
        elif ac_input_volts < 101 or (ac_input_volts < 106 and self._grid_low_alarm_active()):
            volt_sev = "critical"
        elif ac_input_volts < 106:
            volt_sev = "warning"
        else:
            volt_sev = "ok"

        # Corriente de entrada con histeresis
        dc_amps = dc_amps or 0.0
        ac_amps = ac_amps or 0.0
        if self._cur_dc_high:
            self._cur_dc_high = dc_amps >= CURRENT_LIMIT_DC - CURRENT_HYSTERESIS
        else:
            self._cur_dc_high = dc_amps >= CURRENT_LIMIT_DC
        if self._cur_ac_high:
            self._cur_ac_high = ac_amps >= CURRENT_LIMIT_AC - CURRENT_HYSTERESIS
        else:
            self._cur_ac_high = ac_amps >= CURRENT_LIMIT_AC
        current_high = self._cur_dc_high or self._cur_ac_high

        # Alertas de batería: solo "nueva" si su texto no estaba ya activo
        cell_alerts = list(cell_alerts)
        new_keys = {text for text, _, _ in cell_alerts}
        cell_new = bool(new_keys - self._cell_alert_keys)
        self._cell_alerts = cell_alerts
        self._cell_alert_keys = new_keys
        cell_sev = "ok"
        for _, _, sev in cell_alerts:
            if rank[sev] > rank[cell_sev]:
                cell_sev = sev

        volt_worsened = rank[volt_sev] > rank[self._volt_severity]
        current_rose = current_high and not self._current_high
        self._volt_severity = volt_sev
        self._current_high = current_high

        severity = volt_sev
        if current_high:
            severity = "critical"   # alta corriente = rojo
        if rank[cell_sev] > rank[severity]:
            severity = cell_sev
        if rank[self._prot_sev] > rank[severity]:
            severity = self._prot_sev   # proteccion automatica en curso / aviso sin cerrar
        temp_sev = self.temp_tracker.severity()   # ambar: solo con el aviso sin cerrar; rojo: mientras dure
        if rank[temp_sev] > rank[severity]:
            severity = temp_sev
        _all_alerts = self._active_alerts()
        self._vis.update(_all_alerts)
        severity = alert_config.triangle_severity(_all_alerts)   # solo cuentan los avisos que dejan triangulo

        if volt_worsened or current_rose or cell_new:
            self._show_active_alerts()

        severity_changed = severity != self._warning_severity
        self._warning_severity = severity
        if severity == "ok":
            self._warning_blink_on = True
            self.hud_warn_icon.configure(width=0)
            self.pill_warn_icon.configure(width=0)
        else:
            # Ancho 0 -> tamaño real (en vez de pack()/pack_forget(), que
            # reordenaría el ícono al final de la fila la primera vez que
            # se muestre -- ver comentarios en _build_gamer_view/_build_pill_view).
            self.hud_warn_icon.configure(width=self._hud_warn_size)
            self.pill_warn_icon.configure(width=self._pill_warn_size)
            # El color sólido solo se fija UNA VEZ, justo al entrar o escalar
            # de severidad -- de ahí en más, es _blink_tick() el único que lo
            # toca (cada 500ms).
            if severity_changed:
                color = "#f59e0b" if severity == "warning" else "#ef4444"
                self._set_warning_color(color)
                self._warning_blink_on = True
        self._resize_to_content()

    # ------------------------------------------------------------------
    # MODO OFFLINE: al desconectarse (voluntaria o no) se conserva el % y el
    # tiempo, se proyectan con la tendencia de los ultimos 5 min y avanzan
    # como cronometro hasta que se vuelva a conectar (ahi se limpia todo).
    # ------------------------------------------------------------------
    def _record_sample(self, soc, net, runtime_min, mode, in_grace):
        now = time.time()
        if self._hist and now - self._hist[-1][0] < 1.0:
            return
        rate_rt = None
        if (isinstance(runtime_min, (int, float)) and runtime_min > 0
                and not in_grace and mode in ("charging", "discharging")):
            if mode == "charging" and soc < 100:
                rate_rt = (100.0 - soc) / runtime_min     # %/min estimado por el equipo
            elif mode == "discharging" and soc > 0:
                rate_rt = -soc / runtime_min
        self._hist.append((now, float(soc), float(net), rate_rt, mode))
        while self._hist and now - self._hist[0][0] > OFFLINE_HISTORY_SECONDS:
            self._hist.popleft()

    def _begin_offline(self):
        if self._offline_active:
            return
        now = time.time()
        samples = [h for h in self._hist if now - h[0] <= OFFLINE_HISTORY_SECONDS]
        if not samples or now - samples[-1][0] > OFFLINE_MAX_DATA_AGE:
            return  # sin datos recientes fiables: se mantiene el comportamiento de siempre
        last_ts, last_soc = samples[-1][0], samples[-1][1]

        recent = [h[2] for h in samples if last_ts - h[0] <= 10.0]
        avg_net = sum(recent) / len(recent)
        mode = "charging" if avg_net > 5 else ("discharging" if avg_net < -5 else "neutral")

        self._off_mode = mode
        rate = 0.0
        soc0 = last_soc
        if mode != "neutral":
            sign = 1.0 if mode == "charging" else -1.0
            same = [h for h in samples if h[4] == mode]
            t0 = same[0][0]
            span = max(last_ts - t0, 1.0)
            wts = [1.0 + 3.0 * (h[0] - t0) / span for h in same]   # mas peso a lo reciente

            def wmean(vals):
                pairs = [(v, w) for v, w in zip(vals, wts) if v is not None]
                if not pairs:
                    return None
                return sum(v * w for v, w in pairs) / sum(w for _, w in pairs)

            # 1) Tasa segun el tiempo restante que calcula el propio equipo
            est_rt = wmean([h[3] for h in same])
            # 2) Tasa segun la potencia neta real (con perdidas del inversor/cargador)
            net_avg = wmean([h[2] for h in same])
            factor = CHARGE_EFFICIENCY if mode == "charging" else DISCHARGE_LOSS
            est_net = net_avg * factor / (BATTERY_CAPACITY_WH * 0.6)
            # 3) Tendencia real del % en la ventana (regresion lineal)
            est_soc = None
            if len(same) >= 2 and span >= 120:
                socs = [h[1] for h in same]
                if max(socs) - min(socs) >= 2:
                    xs = [h[0] - t0 for h in same]
                    mx, my = sum(xs) / len(xs), sum(socs) / len(socs)
                    den = sum((x - mx) ** 2 for x in xs)
                    if den > 0:
                        slope = sum((x - mx) * (y - my) for x, y in zip(xs, socs)) / den * 60.0
                        if slope * sign > 0:
                            est_soc = slope

            parts = [(est_rt, 0.5), (est_soc, 0.3), (est_net, 0.2)]
            parts = [(v, w) for v, w in parts if v is not None and v * sign > 0]
            if parts:
                rate = sum(v * w for v, w in parts) / sum(w for _, w in parts)

            # % base suavizado con la tendencia (el % del equipo es entero)
            if rate != 0.0:
                near = [h for h in same if last_ts - h[0] <= 120.0]
                ests = [h[1] + rate * (last_ts - h[0]) / 60.0 for h in near]
                if ests:
                    mean_soc = sum(ests) / len(ests)
                    if abs(mean_soc - last_soc) <= 1.0:
                        soc0 = mean_soc

        self._off_soc0 = min(100.0, max(0.0, soc0))
        self._off_rate = rate
        self._off_origin_ts = last_ts
        self._off_last_key = None
        self._offline_active = True

    def _end_offline(self):
        self._offline_active = False
        self._off_last_key = None

    def _offline_state(self):
        """(soc_actual_estimado, minutos_restantes|None) segun el cronometro offline."""
        if not self._offline_active:
            return None, None
        elapsed = max(0.0, time.time() - self._off_origin_ts) / 60.0
        soc = min(100.0, max(0.0, self._off_soc0 + self._off_rate * elapsed))
        rate = self._off_rate
        if rate < -1e-4:
            rem = soc / (-rate)
        elif rate > 1e-4:
            rem = (100.0 - soc) / rate
        else:
            rem = None
        return soc, rem

    def _offline_tick(self):
        try:
            if self._offline_active and not self._hidden:
                soc, rem = self._offline_state()
                key = (int(round(soc)), None if rem is None else int(rem))
                if key != self._off_last_key:
                    self._off_last_key = key
                    self.update_from_telemetry(dict(self.ble.state))
        except Exception:
            pass
        try:
            self.after(1000, self._offline_tick)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Telemetria segura entre hilos + vigilante de la ventana
    # ------------------------------------------------------------------
    def _poll_telemetry(self):
        latest = None
        try:
            while True:
                latest = self._tele_queue.get_nowait()
        except queue.Empty:
            pass
        if latest is not None:
            try:
                self.update_from_telemetry(latest)
            except Exception:
                _log("Error en update_from_telemetry:\n" + traceback.format_exc())
        try:
            self.after(150, self._poll_telemetry)
        except Exception:
            pass

    def _is_topmost(self) -> bool:
        if not IS_WINDOWS:
            return True
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(self.winfo_id()) or self.winfo_id()
            return bool(user32.GetWindowLongW(hwnd, -20) & 0x00000008)   # WS_EX_TOPMOST
        except Exception:
            return True

    def _reassert_topmost(self):
        """Reafirma 'siempre encima' con SetWindowPos(HWND_TOPMOST) sobre la
        ventana real de Windows, sin activarla ni robar el foco. Es mas
        fiable que el atributo -topmost de Tk, que Windows puede dejar de
        respetar (p. ej. al abrirse una consola de comandos)."""
        if not IS_WINDOWS:
            self.wm_attributes("-topmost", True)
            return
        import ctypes
        from ctypes import wintypes
        if self._u32 is None:
            u = ctypes.WinDLL("user32", use_last_error=True)   # instancia propia: no toca argtypes globales
            u.GetParent.restype = wintypes.HWND
            u.GetParent.argtypes = [wintypes.HWND]
            u.GetWindowLongW.restype = ctypes.c_long
            u.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
            u.SetWindowPos.restype = wintypes.BOOL
            u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                                       ctypes.c_int, ctypes.c_int, wintypes.UINT]
            u.IsIconic.argtypes = [wintypes.HWND]
            u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
            u.GetForegroundWindow.restype = wintypes.HWND
            u.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            self._u32 = u
        u = self._u32
        hwnd = u.GetParent(self.winfo_id()) or self.winfo_id()
        if u.IsIconic(hwnd):
            _log("La ventana estaba minimizada (p. ej. Mostrar escritorio); se restaura")
            u.ShowWindow(hwnd, 4)   # SW_SHOWNOACTIVATE
        if not (u.GetWindowLongW(hwnd, -20) & 0x00000008):   # WS_EX_TOPMOST
            try:
                fg = u.GetForegroundWindow()
                buf = ctypes.create_unicode_buffer(256)
                u.GetWindowTextW(fg, buf, 256)
                title = buf.value
                u.GetClassNameW(fg, buf, 256)
                _log(f"Perdio always-on-top; se reaplica. Ventana en primer plano: clase='{buf.value}' titulo='{title}'")
            except Exception:
                _log("Perdio always-on-top; se reaplica")
        # HWND_TOPMOST=-1 ; NOSIZE|NOMOVE|NOACTIVATE|SHOWWINDOW
        u.SetWindowPos(hwnd, wintypes.HWND(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0040)

    def _watchdog_tick(self):
        """Cada 2 s comprueba que el widget siga visible, 'siempre encima' y
        dentro de la pantalla; si no, lo corrige y deja constancia en el log."""
        try:
            if not self._hidden and self._hotkey_active:
                st = self.state()
                if st != "normal":
                    _log(f"Estado de ventana inesperado ({st}); se restaura")
                    self.deiconify()
                    self.wm_attributes("-topmost", True)
                    self._apply_alpha()
                    self._resize_to_content()
                elif not self._menu_open:
                    self._reassert_topmost()
                # Totalmente fuera de pantalla (p. ej. creció el banner y quedó
                # abajo del todo): traerlo de vuelta. Parcialmente fuera se respeta.
                x, y, w, h = self.winfo_x(), self.winfo_y(), self.winfo_width(), self.winfo_height()
                sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
                if x + w <= 0 or x >= sw or y + h <= 0 or y >= sh:
                    _log(f"Ventana fuera de pantalla ({x},{y} {w}x{h}); se reposiciona")
                    self.geometry(f"+{max(0, sw - w - 24)}+20")
                    self._save_config()
        except Exception:
            _log("Error en watchdog:\n" + traceback.format_exc())
        try:
            self.after(300, self._watchdog_tick)
        except Exception:
            pass

    def _draw_arrow_icon(self, canvas: tk.Canvas, size: int, up: bool, color: str):
        """Flecha de TRAZO fino (asta + punta en V, extremos redondeados)."""
        s = size
        w = max(2.0, s * 0.15)

        def pt(x, y):
            return (2 + s * x, s * (y if up else 1.0 - y))

        stem = [*pt(0.5, 0.94), *pt(0.5, 0.08)]
        head = [*pt(0.12, 0.46), *pt(0.5, 0.06), *pt(0.88, 0.46)]
        canvas.create_line(*stem, fill=color, width=w, capstyle="round")
        canvas.create_line(*head, fill=color, width=w, capstyle="round", joinstyle="round")

    def _refresh_offline_arrow(self):
        """Flecha azul entre la bateria y 'Desconectado': arriba si lo ultimo
        fue cargando, abajo si fue descargando. Solo mientras dura el modo
        offline; desaparece al reconectar."""
        direction = None
        if self._offline_active and self._off_mode in ("charging", "discharging"):
            direction = "up" if self._off_mode == "charging" else "down"
        pill_size = max(7, round(14 * self.scale_factor))
        key = (direction, pill_size)
        if key == self._arrow_key:
            return
        self._arrow_key = key
        for cv, size in ((self.pill_arrow_icon, pill_size), (self.hud_arrow_icon, self._hud_arrow_size)):
            cv.delete("all")
            if direction is None:
                cv.configure(width=0)
                continue
            cv.configure(width=size + 4, height=size)
            self._draw_arrow_icon(cv, size, direction == "up", COLOR_OFFLINE_ARROW)
        self._resize_to_content()

    def _on_mouse_wheel(self, event):
        if event.delta > 0:
            self.set_opacity(self.alpha_val + 0.05)
        else:
            self.set_opacity(self.alpha_val - 0.05)

    # ------------------------------------------------------------------
    # Tamaño de la ventana a partir del contenido real
    # ------------------------------------------------------------------
    def _resize_to_content(self):
        # Cada telemetria pedia 1-3 redimensionados (update_idletasks + geometry): se funden en uno
        if getattr(self, "_resize_job", None) is None:
            self._resize_job = self.after(10, self._do_resize)

    def _do_resize(self):
        self._resize_job = None
        try:
            self.update_idletasks()
            w = self.backdrop.winfo_reqwidth()
            h = self.backdrop.winfo_reqheight()
            x, y = self.winfo_x(), self.winfo_y()
            screen_w = self.winfo_screenwidth()

            # Posición por defecto SOLO la primerísima vez que corre esto en
            # toda la vida del widget, y solo si no había ninguna posición
            # guardada de antes -- nunca más se vuelve a inferir "primera
            # vez" a partir de dónde esté la ventana en ese momento (eso era
            # lo que causaba el "teletransporte" al arrastrar cerca de la
            # esquina superior izquierda).
            if not self._has_saved_position and not self._positioned_once:
                x = screen_w - w - 24
                y = 20
            self._positioned_once = True

            # Clamping SOLO horizontal: si el contenido cambia de tamaño (un
            # valor más largo, cambio de vista/escala, banner de alerta) y
            # eso empuja el borde izquierdo o derecho fuera de pantalla, se
            # reempuja hacia adentro desde el lado que corresponda -- pero
            # nunca al revés (nunca se "corrige" una posición horizontal que
            # ya estaba bien). El eje vertical queda completamente libre a
            # propósito: el usuario puede dejarlo parcialmente fuera de
            # pantalla arriba o abajo a propósito para aprovechar espacio, y
            # eso se respeta y se restaura tal cual, sin ningún ajuste.
            x = max(0, min(x, screen_w - w))
            # Solo se toca la geometria si de verdad cambia (evita repintados y que pelee con el arrastre)
            if (w, h, x, y) != (self.winfo_width(), self.winfo_height(), self.winfo_x(), self.winfo_y()):
                self.geometry(f"{w}x{h}+{x}+{y}")
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Arrastre libre por pantalla
    # ------------------------------------------------------------------
    def _bind_drag(self, widget):
        widget.bind("<ButtonPress-1>", self._start_drag)
        widget.bind("<B1-Motion>", self._do_drag)
        widget.bind("<ButtonRelease-1>", self._end_drag)
        widget.bind("<Button-3>", self._show_context_menu)
        widget.bind("<MouseWheel>", self._on_mouse_wheel)

    def _start_drag(self, event):
        self._drag_x = event.x_root - self.winfo_x()
        self._drag_y = event.y_root - self.winfo_y()
        self._press_xy = (event.x_root, event.y_root)
        self._drag_active = False

    def _do_drag(self, event):
        # Zona muerta: un clic con un pequeno temblor del mouse no mueve nada.
        if not self._drag_active:
            sx, sy = self._press_xy
            if max(abs(event.x_root - sx), abs(event.y_root - sy)) < DRAG_DEADZONE_PX:
                return
            self._drag_active = True
        if self.pinned:
            return   # posicion fijada: se detecta el arrastre pero no se mueve
        new_x = event.x_root - self._drag_x
        new_y = event.y_root - self._drag_y
        self.geometry(f"+{new_x}+{new_y}")

    def _end_drag(self, _event):
        if self._drag_active and not self.pinned:
            self._save_config()
        self._drag_active = False

    def _toggle_pin(self):
        # add_checkbutton ya invirtio la variable antes de llamar a este command
        self.pinned = bool(self._pin_var.get())
        self._save_config()

    # ------------------------------------------------------------------
    # Ícono Bluetooth: necesita ARRASTRE y CLIC A LA VEZ. "<Button-1>" y
    # "<ButtonPress-1>" son el MISMO evento en Tkinter, así que enlazar
    # "click para conectar/desconectar" (_bind_bt_icon) por un lado y
    # "arrastre" (_bind_drag) por otro sobre el mismo widget hace que el
    # segundo bind pise al primero -- esa es la causa exacta de que el
    # ícono nunca desconectara. Aquí se combinan ambos en un único
    # manejador: si sueltas el botón sin mover el mouse, es un clic
    # (conectar/desconectar); si lo mueves, fue un arrastre.
    # ------------------------------------------------------------------
    def _bind_bt_icon(self, widget):
        widget.bind("<ButtonPress-1>", self._bt_press)
        widget.bind("<B1-Motion>", self._bt_motion)
        widget.bind("<ButtonRelease-1>", self._bt_release)
        widget.bind("<Button-3>", self._show_context_menu)
        widget.bind("<MouseWheel>", self._on_mouse_wheel)

    def _bt_press(self, event):
        self._drag_x = event.x_root - self.winfo_x()
        self._drag_y = event.y_root - self.winfo_y()
        self._press_xy = (event.x_root, event.y_root)
        self._drag_active = False
        self._bt_dragged = False

    def _bt_motion(self, event):
        self._do_drag(event)
        if self._drag_active:
            self._bt_dragged = True   # fue un arrastre real, no un clic

    def _bt_release(self, event):
        if not getattr(self, "_bt_dragged", False):
            self._toggle_connection()
        else:
            if not self.pinned:
                self._save_config()
            self._drag_active = False

    # ------------------------------------------------------------------
    # Icono de advertencia (triangulo con "!"): arrastre + clic a la vez,
    # igual que el icono Bluetooth. Clic sin mover = mostrar/ocultar las
    # notificaciones activas (voltaje, corriente y/o batería).
    # ------------------------------------------------------------------
    def _bind_warn_icon(self, widget):
        widget.bind("<ButtonPress-1>", self._bt_press)
        widget.bind("<B1-Motion>", self._bt_motion)
        widget.bind("<ButtonRelease-1>", self._warn_release)
        widget.bind("<Button-3>", self._show_context_menu)
        widget.bind("<MouseWheel>", self._on_mouse_wheel)

    def _warn_release(self, event):
        if not getattr(self, "_bt_dragged", False):
            self._toggle_alert_banner()
        else:
            if not self.pinned:
                self._save_config()
            self._drag_active = False

    def _toggle_alert_banner(self):
        if self._banner_visible:
            self._vis.close(self._banner_lines_shown)
            self.temp_tracker.dismiss()   # ocultar a mano cuenta como cerrar el aviso de temperatura
            self._hide_banner()
        else:
            self._prot_dc_dismissed = False   # el usuario quiere verla: vuelve a ser persistente
            self._force_temp = True
            self._show_active_alerts()

    # ------------------------------------------------------------------
    # VISTA "GAMER" (mini panel compacto)
    # ------------------------------------------------------------------
    def _build_gamer_view(self):
        self.gamer_frame = tk.Frame(self.content, bg=self.bg_card)

        bar = tk.Frame(self.gamer_frame, bg=self.bg_card)
        bar.pack(fill="x", padx=4, pady=(4, 1))

        # Ícono Bluetooth (conectar/desconectar), mismo dibujo y mismo manejo
        # combinado de arrastre+clic que en la vista Pill -- antes esta vista
        # no tenía forma de conectar/desconectar sin cambiar a la vista Pill.
        # Al agregarlo a self._bt_icons, _set_bt_color() (usado por el
        # parpadeo de "conectando" y por update_from_telemetry) ya lo
        # mantiene sincronizado con el de la vista Pill sin tocar nada más.
        hud_bt_size = 18
        self.hud_bt_icon = tk.Canvas(bar, width=hud_bt_size, height=hud_bt_size, bg=self.bg_card, highlightthickness=0, cursor="hand2")
        self.hud_bt_icon.pack(side="left", padx=(0, 6))
        hud_bt_item = self._draw_bt_icon(self.hud_bt_icon, hud_bt_size, self._bt_color)
        self._bt_icons.append((self.hud_bt_icon, hud_bt_item))
        self._bind_bt_icon(self.hud_bt_icon)

        title = tk.Label(bar, text="⚡ BLUETTI HUD", font=("Segoe UI", 11, "bold"), bg=self.bg_card, fg="#00f0ff")
        title.pack(side="left")
        self._bind_drag(bar)
        self._bind_drag(title)

        btn_pill = tk.Label(bar, text="▭", font=("Segoe UI", 12, "bold"), bg=self.bg_card, fg="#94a3b8", cursor="hand2")
        btn_pill.pack(side="right", padx=(4, 0))
        btn_pill.bind("<Button-1>", lambda e: self.show_pill_view())
        if self._on_open_app is not None:
            btn_app = tk.Label(bar, text="▣", font=("Segoe UI Symbol", 11), bg=self.bg_card, fg="#94a3b8", cursor="hand2")
            btn_app.pack(side="right", padx=(4, 0))
            btn_app.bind("<Button-1>", lambda e: self._on_open_app())

        btn_close = tk.Label(bar, text="×", font=("Segoe UI", 14, "bold"), bg=self.bg_card, fg="#94a3b8", cursor="hand2")
        btn_close.pack(side="right", padx=(4, 6))
        btn_close.bind("<Button-1>", lambda e: self.close_widget())

        soc_card = tk.Frame(self.gamer_frame, bg=self.bg_card)
        soc_card.pack(fill="x", padx=5, pady=2)
        self.hud_soc_val = tk.Label(soc_card, text="--%", font=("Segoe UI", 30, "bold"), bg=self.bg_card, fg="#94a3b8")
        self.hud_soc_val.pack()

        # Fila del estado (Cargando/Descargando/...) con el ícono de
        # advertencia de voltaje de red ANTES del texto, oculto por defecto
        # (solo aparece cuando hay una alerta activa -- ver
        # _update_voltage_alert()).
        net_row = tk.Frame(soc_card, bg=self.bg_card)
        net_row.pack()
        self._hud_warn_size = 13
        self.hud_warn_icon = tk.Canvas(net_row, width=0, height=self._hud_warn_size, bg=self.bg_card, highlightthickness=0, cursor="hand2")
        self.hud_warn_icon.pack(side="left", padx=(0, 0))
        self._bind_warn_icon(self.hud_warn_icon)
        warn_items = self._draw_warning_icon(self.hud_warn_icon, self._hud_warn_size, "#f59e0b")
        self._warning_items.extend((self.hud_warn_icon, iid, opt) for iid, opt in warn_items)
        # Arranca con width=0 (oculto, sin ocupar espacio): queda EMPAQUETADO
        # ya en su lugar correcto desde el inicio -- _update_voltage_alert()
        # solo cambia su ancho (0 <-> self._hud_warn_size) para mostrar/
        # ocultar, en vez de pack()/pack_forget(), que lo reordenaría al
        # final de la fila la primera vez que se muestre.
        self._hud_arrow_size = 15
        self.hud_arrow_icon = tk.Canvas(net_row, width=0, height=self._hud_arrow_size, bg=self.bg_card, highlightthickness=0)
        self.hud_arrow_icon.pack(side="left")
        self.hud_net_power = tk.Label(net_row, text="Conectando...", font=("Segoe UI", 10, "bold"), bg=self.bg_card, fg="#f59e0b")
        self.hud_net_power.pack(side="left")

        hud_time_row = tk.Frame(soc_card, bg=self.bg_card)
        hud_time_row.pack(pady=(2, 4))
        tk.Label(hud_time_row, text="🕐", font=("Segoe UI Emoji", 9), bg=self.bg_card, fg="#94a3b8").pack(side="left", padx=(0, 3))
        self.hud_time_est = tk.Label(hud_time_row, text="--h--m", font=("Consolas", 9, "bold"), bg=self.bg_card, fg="#94a3b8")
        self.hud_time_est.pack(side="left")

        # Dos columnas: ENTRADAS (Red AC arriba, Solar DC debajo) a la
        # izquierda, SALIDAS (AC Out arriba, DC Out debajo) a la derecha.
        # Los switches AC y DC van DEBAJO del valor de su salida, en el mismo
        # sitio donde las entradas muestran voltaje/corriente.
        grid = tk.Frame(self.gamer_frame, bg=self.bg_card)
        grid.pack(fill="x", padx=4, pady=2)
        grid.columnconfigure(0, weight=1)
        grid.columnconfigure(1, weight=1)
        tk.Label(grid, text="ENTRADAS", font=("Segoe UI", 8, "bold"), bg=self.bg_card, fg="#475569").grid(row=0, column=0, sticky="w", padx=6)
        tk.Label(grid, text="SALIDAS", font=("Segoe UI", 8, "bold"), bg=self.bg_card, fg="#475569").grid(row=0, column=1, sticky="w", padx=6)
        self.hud_grid_w, self.hud_grid_v, self.hud_grid_a = self._create_hud_metric(
            grid, "RED AC", "0 W", "#38bdf8", 1, 0, with_volts=True, with_amps=True
        )
        self.hud_ac_out_w, self.hud_bt_ac = self._create_hud_metric(
            grid, "AC OUT", "0 W", "#ff3366", 1, 1, switch_text="AC ⭘"
        )
        self.hud_bt_ac.bind("<Button-1>", lambda e: self._toggle_ac())
        self.hud_solar_w, self.hud_solar_v, self.hud_solar_a = self._create_hud_metric(
            grid, "SOLAR DC", "0 W", "#fbbf24", 2, 0, with_volts=True, with_amps=True
        )
        self.hud_dc_out_w, self.hud_bt_dc = self._create_hud_metric(
            grid, "DC OUT", "0 W", "#c084fc", 2, 1, switch_text="DC ⭘"
        )
        self.hud_bt_dc.bind("<Button-1>", lambda e: self._toggle_dc())

        sw_row = tk.Frame(self.gamer_frame, bg=self.bg_card)
        sw_row.pack(fill="x", padx=5, pady=(2, 5))

        # Fila inferior: solo los switches de configuración (TURBO y REFUERZO).
        # AC y DC viven ahora bajo sus valores de salida (ver más arriba).
        # TURBO: modo de carga (perfil de carga AC más rápido).
        # Es un simple ON/OFF -- no un selector de 3 modos como en la app
        # principal -- porque al apagarlo debe volver exactamente al modo
        # que estaba activo antes (silencioso o estándar), no forzar
        # siempre "estándar". Ver _toggle_turbo().
        self.hud_bt_turbo = tk.Label(sw_row, text="TURBO ⭘", font=("Segoe UI", 10, "bold"), bg=self.bg_card, fg="#64748b", cursor="hand2")
        self.hud_bt_turbo.pack(side="left", padx=(0, 10))
        self.hud_bt_turbo.bind("<Button-1>", lambda e: self._toggle_turbo())

        # REFUERZO DE RED (Grid Enhancement, Reg 2225): simple ON/OFF.
        self.hud_bt_grid = tk.Label(sw_row, text="REFUERZO ⭘", font=("Segoe UI", 10, "bold"), bg=self.bg_card, fg="#64748b", cursor="hand2")
        self.hud_bt_grid.pack(side="left")
        self.hud_bt_grid.bind("<Button-1>", lambda e: self._toggle_grid())

    def _create_hud_metric(self, parent, title, val, color, r, c, with_volts=False, with_amps=False, switch_text=None):
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=1)
        f = tk.Frame(parent, bg=self.bg_card)
        f.grid(row=r, column=c, padx=6, pady=3, sticky="w")
        tk.Label(f, text=title, font=("Segoe UI", 8, "bold"), bg=self.bg_card, fg="#64748b").pack(anchor="w")
        lbl_v = tk.Label(f, text=val, font=("Segoe UI", 14, "bold"), bg=self.bg_card, fg=color)
        lbl_v.pack(anchor="w")
        if switch_text is not None:
            # Switch (AC/DC de salida) debajo del valor, donde las entradas
            # muestran voltaje/corriente. Devuelve (valor, etiqueta_switch).
            lbl_sw = tk.Label(f, text=switch_text, font=("Segoe UI", 10, "bold"), bg=self.bg_card, fg="#64748b", cursor="hand2")
            lbl_sw.pack(anchor="w")
            return lbl_v, lbl_sw
        if with_volts and with_amps:
            # Voltaje y corriente pequeños debajo de la potencia, uno al lado
            # del otro (los usan SOLAR DC y RED AC).
            va_row = tk.Frame(f, bg=self.bg_card)
            va_row.pack(anchor="w")
            lbl_volts = tk.Label(va_row, text="--V", font=("Segoe UI", 8, "bold"), bg=self.bg_card, fg="#64748b")
            lbl_volts.pack(side="left")
            lbl_amps = tk.Label(va_row, text="--A", font=("Segoe UI", 8, "bold"), bg=self.bg_card, fg="#64748b")
            lbl_amps.pack(side="left", padx=(6, 0))
            return lbl_v, lbl_volts, lbl_amps
        if with_volts:
            # Voltaje pequeño debajo de la potencia -- de momento solo lo usa
            # RED AC, para poder avisar de inestabilidad de la red (ver
            # update_from_telemetry / _update_voltage_alert).
            lbl_volts = tk.Label(f, text="0V", font=("Segoe UI", 8, "bold"), bg=self.bg_card, fg="#64748b")
            lbl_volts.pack(anchor="w")
            return lbl_v, lbl_volts
        return lbl_v

    # ------------------------------------------------------------------
    # VISTA "PILL" (barra ultra-minimalista, ex widget_minimalista.py)
    # ------------------------------------------------------------------
    def _build_pill_view(self):
        self.pill_frame = tk.Frame(self.content, bg=self.bg_card)
        # self._scalable / self._scalable_icons / self._bt_icons / self._bt_color /
        # self._clock_items / self._clock_color ya se inicializaron en __init__,
        # antes de construir la vista Gamer -- si se reinicializaran acá se
        # perdería el ícono Bluetooth que esa vista ya agregó a self._bt_icons.

        # Todas las secciones de texto comparten el MISMO tamaño (11pt) que
        # el % de batería, para que nada se vea "achicando" en comparación.
        PILL_FONT_PX = 11

        row = tk.Frame(self.pill_frame, bg=self.bg_card)
        row.pack(padx=0, pady=0)
        self._bind_drag(row)

        # Los separadores conservan su hueco (1px + 2px a cada lado) en TODAS
        # las posiciones, así el ancho y la posición de cada elemento no
        # cambian; solo los de los EXTREMOS (Bluetooth|batería y
        # tiempo|redimensionar) se dibujan visibles, los del medio quedan del
        # color del fondo. Antes eran #334155, que con el fondo aclarado a
        # #3c414d prácticamente desaparecieron -- por eso "no se veían".
        SEP_VISIBLE = "#98a1b3"

        def sep(visible=False):
            s = tk.Frame(row, width=1, height=14, bg=SEP_VISIBLE if visible else self.bg_card)
            s.pack(side="left", padx=2)
            self._bind_drag(s)

        # Ícono Bluetooth, A LA IZQUIERDA DEL TODO: botón único de conectar/
        # desconectar. Azul = conectado, Gris = desconectado, parpadea en
        # ámbar mientras intenta conectar. Dibujado en Canvas (sin depender
        # de ninguna fuente/emoji).
        bt_size = 17
        self.pill_bt_icon = tk.Canvas(row, width=bt_size, height=bt_size, bg=self.bg_card, highlightthickness=0, cursor="hand2")
        self.pill_bt_icon.pack(side="left", padx=(2, 2))
        bt_item = self._draw_bt_icon(self.pill_bt_icon, bt_size, self._bt_color)
        self._bt_icons.append((self.pill_bt_icon, bt_item))
        self._scalable_icons.append((self.pill_bt_icon, bt_size, "bt"))
        self._bind_bt_icon(self.pill_bt_icon)

        sep(True)  # separador visible en el extremo izquierdo (Bluetooth | batería)

        batt_size = 16
        self.pill_soc_icon = tk.Canvas(row, width=batt_size, height=batt_size, bg=self.bg_card, highlightthickness=0)
        self.pill_soc_icon.pack(side="left", padx=(2, 1))
        batt_items = self._draw_battery_icon(self.pill_soc_icon, batt_size, self._battery_color)
        self._battery_items.extend((self.pill_soc_icon, iid, opts) for iid, opts in batt_items)
        self._scalable_icons.append((self.pill_soc_icon, batt_size, "battery"))
        self._bind_drag(self.pill_soc_icon)

        self.pill_soc = tk.Label(row, text="--%", font=("Segoe UI", PILL_FONT_PX, "bold"), bg=self.bg_card, fg="#94a3b8")
        self.pill_soc.pack(side="left", padx=(0, 3))
        self._bind_drag(self.pill_soc)
        self._scalable.append((self.pill_soc, "Segoe UI", PILL_FONT_PX, True))

        # Flecha de tendencia offline (oculta con width=0 hasta que haga falta)
        self.pill_arrow_icon = tk.Canvas(row, width=0, height=14, bg=self.bg_card, highlightthickness=0)
        self.pill_arrow_icon.pack(side="left")
        self._bind_drag(self.pill_arrow_icon)

        sep()

        # Ícono de advertencia de voltaje de red, oculto por defecto (mismo
        # criterio que en la vista Gamer) -- ANTES del texto de estado.
        self._pill_warn_size = 12
        self.pill_warn_icon = tk.Canvas(row, width=0, height=self._pill_warn_size, bg=self.bg_card, highlightthickness=0, cursor="hand2")
        self.pill_warn_icon.pack(side="left", padx=(0, 0))
        self._bind_warn_icon(self.pill_warn_icon)
        warn_items = self._draw_warning_icon(self.pill_warn_icon, self._pill_warn_size, "#f59e0b")
        self._warning_items.extend((self.pill_warn_icon, iid, opt) for iid, opt in warn_items)
        # Igual que en la vista Gamer: arranca con width=0 (empaquetado ya en
        # su lugar, sin ocupar espacio) -- ver comentario allá. No entra en
        # _scalable_icons (ese mecanismo redibuja con cv.delete("all") y
        # asume que el ancho ya es el de destino, lo que chocaría con el
        # ancho dinámico 0<->self._pill_warn_size); su tamaño base ya es
        # chico y no hace falta que escale con el resto de la barra.
        self._pill_warn_padx = 3

        # Sin la palabra "Estado": solo el mensaje de flujo neto directo.
        # Mismo tamaño y familia de fuente que el % de batería.
        self.pill_status = tk.Label(row, text="Conectando...", font=("Segoe UI", PILL_FONT_PX, "bold"), bg=self.bg_card, fg="#f59e0b")
        self.pill_status.pack(side="left", padx=(3, 3))
        self._bind_drag(self.pill_status)
        self._scalable.append((self.pill_status, "Segoe UI", PILL_FONT_PX, True))

        sep()

        # Ícono de reloj (dibujado a mano, mismo criterio que el Bluetooth)
        # + estimación de tiempo. Misma familia/tamaño de fuente que el
        # resto (Segoe UI, no Consolas): a igual tamaño en puntos, una
        # fuente monoespaciada como Consolas se ve más chica/delgada que
        # Segoe UI Bold, por eso se veía "achicado" en comparación aunque
        # el número de puntos fuera el mismo. Verde = cargando, Rojo =
        # descargando.
        clock_size = 13
        self.pill_clock_icon = tk.Canvas(row, width=clock_size, height=clock_size, bg=self.bg_card, highlightthickness=0)
        self.pill_clock_icon.pack(side="left", padx=(3, 2))
        clock_items = self._draw_clock_icon(self.pill_clock_icon, clock_size, self._clock_color)
        self._clock_items.extend((self.pill_clock_icon, iid, opt) for iid, opt in clock_items)
        self._scalable_icons.append((self.pill_clock_icon, clock_size, "clock"))
        self._bind_drag(self.pill_clock_icon)

        self.pill_time_est = tk.Label(row, text="--h--m", font=("Segoe UI", PILL_FONT_PX, "bold"), bg=self.bg_card, fg="#94a3b8")
        self.pill_time_est.pack(side="left", padx=(0, 2))
        self._bind_drag(self.pill_time_est)
        self._scalable.append((self.pill_time_est, "Segoe UI", PILL_FONT_PX, True))

        sep(True)  # separador visible en el extremo derecho (tiempo | redimensionar)

        # Sin cruz de cerrar (se cierra con clic derecho -> "Cerrar Widget
        # Flotante") y sin ícono de ajustes aparte (el menú de clic derecho
        # ya está disponible en toda la barra). Queda solo el cambio de
        # tamaño/versión (pasa a la vista Gamer).
        expand_box = tk.Frame(row, bg=self.bg_card, width=18, height=18)
        expand_box.pack(side="left", padx=(2, 1))
        expand_box.pack_propagate(False)
        self.pill_expand = tk.Label(expand_box, text="⤢", font=("Segoe UI", 12), bg=self.bg_card, fg="#94a3b8", cursor="hand2")
        self.pill_expand.place(relx=0.5, rely=0.5, anchor="center")
        self.pill_expand.bind("<Button-1>", lambda e: self.show_gamer_view())
        self._bind_drag(expand_box)

        self._build_context_menu()

    def _build_context_menu(self):
        self.context_menu = Menu(self, tearoff=0, bg="#0f172a", fg="#f8fafc", activebackground="#0284c7", activeforeground="#ffffff")
        # Checkbutton en vez de command: además de conectar/desconectar al
        # hacer clic, el propio ítem del menú queda marcado ✓ mientras esté
        # conectado, así se sabe de un vistazo sin tener que fijarse en el
        # color del ícono.
        self.context_menu.add_checkbutton(
            label="Conectado", variable=self._conn_var, onvalue=True, offvalue=False,
            command=self._toggle_connection, selectcolor="#00f0ff",
        )
        self.context_menu.add_separator()
        if self._on_open_app is not None:
            self.context_menu.add_command(label="Abrir panel principal", command=self._on_open_app)
            self.context_menu.add_separator()

        # Los 4 grupos de abajo (Transparencia, Tamaño, Vista, Posición) son
        # elecciones EXCLUYENTES (una sola aplica a la vez) -- add_radiobutton
        # con una variable compartida por grupo es lo que hace que Tkinter
        # dibuje el punto/check junto a la opción actualmente activa.
        opacity_menu = Menu(self.context_menu, tearoff=0, bg="#0f172a", fg="#f8fafc", activebackground="#0284c7")
        for op in range(100, 24, -5):   # 100%..25% de 5 en 5 (mismos pasos que la rueda del mouse)
            opacity_menu.add_radiobutton(
                label=f"{op}%", variable=self._opacity_var, value=op,
                command=lambda v=op / 100.0: self.set_opacity(v), selectcolor="#00f0ff",
            )
        self.context_menu.add_cascade(label="Transparencia", menu=opacity_menu)

        size_menu = Menu(self.context_menu, tearoff=0, bg="#0f172a", fg="#f8fafc", activebackground="#0284c7")
        for sc, name in [(pc / 100.0, f"{pc}%" + {50: " (Mínimo)", 100: " (Normal)", 150: " (Máximo)"}.get(pc, "")) for pc in range(50, 151, 10)]:
            size_menu.add_radiobutton(
                label=name, variable=self._scale_var, value=sc,
                command=lambda v=sc: self.set_scale(v), selectcolor="#00f0ff",
            )
        self.context_menu.add_cascade(label="Tamaño (Vista Minimalista)", menu=size_menu)

        view_menu = Menu(self.context_menu, tearoff=0, bg="#0f172a", fg="#f8fafc", activebackground="#0284c7")
        view_menu.add_radiobutton(
            label="Vista Gamer (mini panel)", variable=self._view_var, value="gamer",
            command=self.show_gamer_view, selectcolor="#00f0ff",
        )
        view_menu.add_radiobutton(
            label="Vista Minimalista (barra)", variable=self._view_var, value="pill",
            command=self.show_pill_view, selectcolor="#00f0ff",
        )
        self.context_menu.add_cascade(label="Vista", menu=view_menu)

        # Sin submenú de "Posición en Pantalla": con arrastre libre, esos 4
        # presets fijos no cumplían ningún objetivo -- la posición real
        # siempre queda donde el usuario lo soltó (y se restaura sola,
        # ajustada para no quedar fuera de pantalla, en _do_resize()).
        self.context_menu.add_separator()

        self.context_menu.add_checkbutton(
            label="Fijar posición", variable=self._pin_var, onvalue=True, offvalue=False,
            command=self._toggle_pin, selectcolor="#00f0ff",
        )

        self.startup_var = tk.BooleanVar(master=self, value=_is_startup_enabled(self._startup_spec()[0]))
        self.context_menu.add_checkbutton(
            label="Iniciar con Windows", variable=self.startup_var, onvalue=True, offvalue=False,
            command=self._toggle_startup, selectcolor="#00f0ff",
        )

        self.context_menu.add_separator()
        if self._hotkey_registered:
            self.context_menu.add_command(label=f"Ocultar widget ({HOTKEY_LABEL})", command=self._hide_widget)
        self.context_menu.add_command(label="Cerrar Widget Flotante", command=self.close_widget)

    def _show_context_menu(self, event):
        self._menu_open = True
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()
            self._menu_open = False

    # ------------------------------------------------------------------
    # Alternar entre vistas (reemplaza el antiguo "volver a pantalla completa")
    # ------------------------------------------------------------------
    def show_gamer_view(self):
        self.view_mode = "gamer"
        self._view_var.set("gamer")
        self.pill_frame.pack_forget()
        self.gamer_frame.pack(fill="both", expand=True)
        self._resize_to_content()
        self._save_config()

    def show_pill_view(self):
        self.view_mode = "pill"
        self._view_var.set("pill")
        self.gamer_frame.pack_forget()
        self.pill_frame.pack(fill="both", expand=True)
        self._resize_to_content()
        self._save_config()

    # ------------------------------------------------------------------
    # Ocultar / mostrar con atajo global (Alt+B)
    # ------------------------------------------------------------------
    def _poll_hotkey(self):
        """Corre en el hilo de Tk: vacía la cola que llena el hilo del atajo.
        (Tkinter no es seguro entre hilos, por eso el hilo del atajo no toca
        la interfaz directamente.)"""
        if not self._hotkey_active:
            return
        try:
            while True:
                self._hotkey_queue.get_nowait()
                self._toggle_visibility()
        except queue.Empty:
            pass
        try:
            self.after(100, self._poll_hotkey)
        except Exception:
            pass

    def _toggle_visibility(self):
        if self._hidden:
            self._show_widget()
        else:
            self._hide_widget()

    def _hide_widget(self):
        # Sin atajo registrado no se puede volver a mostrar: no ocultar.
        if self._hidden or not self._hotkey_registered:
            return
        self._hidden = True
        _log("Widget oculto a proposito (atajo o menu)")
        # Solo en modo standalone el widget es dueño de la conexión BLE. Si
        # está embebido en hud_app.py, la conexión es de la ventana principal
        # (que tiene su propio botón "Liberar BLE"), así que ocultar el widget
        # no debe cortársela.
        if self.standalone:
            self._is_connecting = False
            self._begin_offline()
            if not self.ble.is_paused:
                self.ble.pause_and_release()
            self._conn_var.set(False)
            self._set_bt_color(COLOR_BT_DISCONNECTED)
        self.withdraw()

    def _show_widget(self):
        if not self._hidden:
            return
        self._hidden = False
        self.deiconify()
        self.wm_attributes("-topmost", True)   # deiconify puede perder el "siempre visible"
        self._apply_alpha()
        if self.standalone and self.ble.is_paused:
            self._is_connecting = True
            self._conn_var.set(False)
            self.pill_status.configure(text="Conectando...", fg="#f59e0b")
            self.ble.resume_connection()
        self._resize_to_content()

    def close_widget(self):
        _log("close_widget() llamado: el widget se cierra")
        self._hotkey_active = False
        self._hotkey.stop()
        self._save_config()
        if self._banner_hide_job is not None:
            try:
                self.after_cancel(self._banner_hide_job)
            except Exception:
                pass
        try:
            self.destroy()
        except Exception:
            pass
        if self._implicit_root is not None:
            try:
                self._implicit_root.quit()
                self._implicit_root.destroy()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Acciones (comparten self.ble con la ventana principal)
    # ------------------------------------------------------------------
    def _toggle_ac(self):
        self._last_click_ac = time.time()
        new_state = not bool(self.ble.state.get("ac_output_on", False))
        self.ble.set_ac_output(new_state)

    def _toggle_dc(self):
        self._last_click_dc = time.time()
        new_state = not bool(self.ble.state.get("dc_output_on", False))
        self.ble.set_dc_output(new_state)

    def _toggle_grid(self):
        self._last_click_grid = time.time()
        new_state = not bool(self.ble.state.get("grid_enhancement_on", False))
        self.ble.set_grid_enhancement(new_state)

    def _toggle_turbo(self):
        self._last_click_turbo = time.time()
        current_mode = self.ble.state.get("charge_mode", "standard")
        if current_mode == "turbo":
            # Restaurar el modo que estaba activo antes de encender Turbo.
            # Si por algún motivo se guardó "custom" (perfil manual desde la
            # app oficial), no es uno de los dos modos que este switch
            # conoce -- se cae a "standard" en vez de reenviarlo tal cual.
            restore = self._prev_charge_mode if self._prev_charge_mode in ("silent", "standard", "custom") else "standard"
            self.ble.set_charge_mode(restore)
        else:
            # Antes de activar Turbo, recordar el modo actual para poder
            # volver exactamente a él al apagarlo.
            trusted = self._was_conn and time.time() - self._conn_since >= CHARGE_MODE_TRUST_SECONDS
            if trusted and current_mode in ("silent", "standard", "custom"):
                self._prev_charge_mode = current_mode
                self._save_config()
            self.ble.set_charge_mode("turbo")

    def _startup_spec(self):
        """(nombre en el registro, argumentos). Embebido en la app => se registra la APP COMPLETA."""
        if self.standalone:
            return _STARTUP_VALUE_NAME, ""
        return _STARTUP_APP_VALUE_NAME, "--minimized"

    def _toggle_startup(self):
        # add_checkbutton ya invirtió self.startup_var ANTES de llamar a este
        # command, así que acá adentro ya refleja el estado que el usuario
        # quiere aplicar.
        want_enabled = bool(self.startup_var.get())
        name, args = self._startup_spec()
        ok = _set_startup_enabled(want_enabled, name, args)
        if ok and want_enabled and not self.standalone:
            _set_startup_enabled(False, _STARTUP_VALUE_NAME)   # evita doble arranque (app + widget suelto)
        if not ok:
            # No se pudo escribir el registro: revertir el check en vez de
            # mostrar una opción marcada que en realidad no quedó aplicada.
            self.startup_var.set(not want_enabled)

    def _toggle_connection(self):
        connected_now = bool(self.ble.state.get("connected", False)) and not self.ble.is_paused
        # Conectado O intentando conectar -> el clic CORTA. Antes solo se
        # cortaba si ya había conexión real; si el intento no prosperaba
        # (Bluetooth ocupado, estación apagada), el clic caía en la rama de
        # "conectar" otra vez, no hacía nada nuevo y el widget quedaba en
        # "Conectando..." indefinidamente. Ahora cancela el intento, pausa
        # los reintentos automáticos (is_paused) y pasa a Desconectado hasta
        # que se vuelva a pulsar.
        if self.ble.is_paused:
            self._is_connecting = True
            self.ble.resume_connection()
            self._conn_var.set(False)  # Todavía intentando, no conectado de verdad.
            # Empujón manual: mientras se escanea/reconecta, el gestor BLE no
            # llama a on_telemetry en absoluto (solo notifica texto de estado
            # por otro canal) -- sin esto, este texto solo se veía la
            # primera vez que se abría el widget (con el texto inicial
            # hardcodeado de _build_pill_view), nunca en una reconexión
            # posterior, porque no había ningún update_from_telemetry en el
            # medio para mostrarlo.
            self.pill_status.configure(text="Conectando...", fg="#f59e0b")
            self.hud_net_power.configure(text="Conectando...", fg="#f59e0b")
        else:
            self._is_connecting = False
            self._begin_offline()   # congelar tendencia ANTES de soltar el BLE
            self.ble.pause_and_release()
            self._set_bt_color(COLOR_BT_DISCONNECTED)
            self._conn_var.set(False)
            # Mismo motivo: no esperar a que el desconectado asíncrono
            # termine y dispare telemetría para reflejarlo. La vista Gamer
            # ahora refleja el mismo estado de conexión que la Pill (antes
            # solo mostraba Cargando/Descargando/Neutro sin importar si
            # había conexión real o no).
            self.pill_status.configure(text="Desconectado", fg="#94a3b8")
            self.hud_net_power.configure(text="Desconectado", fg="#94a3b8")
            self._off_last_key = None
            self.update_from_telemetry(dict(self.ble.state))   # mostrar el estimado offline ya
        sync = getattr(self.master, "_on_ble_toggled", None)   # embebido: el botón de la app se pone en espejo
        if callable(sync):
            sync()

    def set_connecting(self, on: bool):
        """Lo llama la app (botón Conectar/Desconectar BLE) para que el widget refleje el cambio igual que
        su propio botón: "Conectando..." al conectar, "Desconectado" al desconectar."""
        if on:
            self._is_connecting = True
            self._conn_var.set(False)
            self.pill_status.configure(text="Conectando...", fg="#f59e0b")
            self.hud_net_power.configure(text="Conectando...", fg="#f59e0b")
        else:
            self._is_connecting = False
            self._begin_offline()
            self._set_bt_color(COLOR_BT_DISCONNECTED)
            self._conn_var.set(False)
            self.pill_status.configure(text="Desconectado", fg="#94a3b8")
            self.hud_net_power.configure(text="Desconectado", fg="#94a3b8")
            self._off_last_key = None
            self.update_from_telemetry(dict(self.ble.state))

    # ------------------------------------------------------------------
    # Actualización de telemetría (llamada desde BluettiDesktopApp, que ya
    # está en el hilo principal de Tk -- no se registra un segundo callback
    # en self.ble para no pisar al de la ventana principal)
    # ------------------------------------------------------------------
    def update_from_telemetry(self, t):  # [alertas-patch]
        t = alert_config.mask_telemetry(t)
        fired = False
        if threading.get_ident() == self._ui_thread_id:
            try:
                ns = alert_config.notices()
                is_conn = bool(t.get("connected", False)) and not self.ble.is_paused
                ns.update(t, is_conn)
                fired = ns.fire_seq != getattr(self, "_notice_seen", 0)
                self._notice_seen = ns.fire_seq
            except Exception:
                _log("Error en avisos de bateria/consumo:\n" + traceback.format_exc())
        result = self.update_from_telemetry_impl(t)
        if fired:
            try:
                self._show_active_alerts()
            except Exception:
                pass
        return result

    def update_from_telemetry_impl(self, t: Dict[str, Any]):
        if threading.get_ident() != self._ui_thread_id:
            # Llamada desde el hilo BLE: no tocar Tk desde aqui, encolar.
            self._tele_queue.put(dict(t))
            return
        is_conn = bool(t.get("connected", False)) and not self.ble.is_paused
        self._conn_var.set(is_conn)

        # Transiciones de conexion para el modo offline: al conectar se limpia
        # todo (historial y proyeccion); al desconectar (voluntaria o no) se
        # congela la ultima tendencia y arranca el cronometro.
        if is_conn:
            if not self._was_conn:
                self._conn_since = time.time()
                self._hist.clear()
                self._end_offline()
        elif self._was_conn:
            self._begin_offline()
        self._was_conn = is_conn

        # Recordar el ultimo modo de carga NO turbo realmente leido del equipo
        # (se ignora justo tras conectar: ahi aun puede ser el valor por defecto).
        _cm = t.get("charge_mode")
        if (is_conn and _cm in ("silent", "standard", "custom") and _cm != self._prev_charge_mode
                and time.time() - self._conn_since >= CHARGE_MODE_TRUST_SECONDS):
            self._prev_charge_mode = _cm
            self._save_config()

        # Solo se apaga el parpadeo "conectando" cuando la telemetría
        # confirma una conexión REAL -- antes se limpiaba con cualquier
        # telemetría, incluida la que anuncia una desconexión (connected=False),
        # lo que apagaba el parpadeo justo cuando el equipo se acababa de caer.
        if t.get("connected", False):
            self._is_connecting = False

        # "soc" es None hasta la primera lectura real del registro de batería
        # (ver ble_manager.py) -- se muestra "--%" en vez de un número
        # inventado mientras tanto.
        soc = t.get("soc")
        if self._offline_active:
            off_soc = self._offline_state()[0]
            if off_soc is not None:
                soc = int(round(off_soc))
        dc_out_w = t.get("dc_output_watts", 0) if t.get("dc_output_on", False) else 0
        in_w = t.get("dc_input_watts", 0) + t.get("ac_input_watts", 0)
        out_w = t.get("ac_output_watts", 0) + dc_out_w
        net = in_w - out_w

        if soc is None:
            soc_text, soc_color = "--%", "#94a3b8"
            self._batt_low = False
        else:
            soc_text = f"{soc}%"
            # Verde 65-100%, ámbar 26-64%, rojo 25% o menos
            soc_color = "#10b981" if soc >= 65 else ("#fbbf24" if soc >= 26 else "#f43f5e")
            # Condición 1 (independiente): batería por debajo del 15% -> parpadeo.
            self._batt_low = alert_config.blink_soc_low(soc)  # [alertas-patch]
            if self._batt_low:
                self._batt_color = soc_color

        # Texto de flujo neto (usado tal cual por la vista Gamer -- "Flujo
        # Neto: ...W" es una etiqueta fija ahí, no se confunde con el estado
        # de conexión). IMPORTANTE: "net" es entrada TOTAL menos salida TOTAL
        # -- o sea, la potencia que realmente llega/sale de la batería, no la
        # salida bruta. Es el mismo número que ya se muestra en el widget y
        # del que depende el tiempo estimado (Registro 104 del equipo se basa
        # en ese mismo flujo real) -- deben coincidir siempre, por eso el
        # umbral de color/parpadeo usa esta "net" y no la potencia de salida
        # directa.
        if net > 5:
            net_status_text = f"Cargando +{net}W"
            net_status_color = "#10b981"  # siempre verde mientras cargue, sin importar cuánto
            # Condición 2 (independiente): velocidad de carga/descarga >= 1000W.
            self._power_extreme = net >= 1000
        elif net < -5:
            net_status_text = f"Descargando {net}W"
            if net <= -1000:
                net_status_color = "#ef4444"
                self._power_extreme = True
            elif net <= -800:
                net_status_color = "#ef4444"
                self._power_extreme = False
            else:
                net_status_color = "#f59e0b"
                self._power_extreme = False
        else:
            # Batería llena + recibiendo AC + flujo neto ~0 = el equipo no
            # está ni cargando ni descargando la batería, solo dejando pasar
            # la corriente de la red -- se avisa como "UPS" en vez de
            # "Neutro 0W", que sonaba a que no hay nada conectado.
            if soc == 100 and t.get("ac_input_watts", 0) > 0:
                net_status_text = "Modo UPS Activo"
                net_status_color = "#38bdf8"
            else:
                net_status_text = "Neutro 0W"
                net_status_color = "#cbd5e1"
            self._power_extreme = False
        if self._power_extreme:
            self._power_color = net_status_color

        # Texto del espacio central de la vista Pill: mientras no haya
        # conexión real, reemplaza el dato de carga/descarga (que quedaría
        # obsoleto/engañoso) por palabras clave sobre el estado del enlace.
        # Este texto/color se usa IGUAL en ambas vistas (Gamer y Pill): antes
        # la vista Gamer mostraba Cargando/Descargando/Neutro sin importar si
        # había conexión real o no, mientras que la Pill sí distinguía
        # Conectando.../Desconectado -- ahora las dos se comportan igual.
        # Justo al conectar, el primer bloque que llega (bateria) trae las
        # potencias todavia en 0 y se vería "Neutro 0W" unos instantes: se
        # mantiene "Conectando..." hasta que llegue una potencia real o pasen
        # CONNECT_SETTLE_SECONDS (lo que ocurra primero).
        settling = is_conn and net == 0 and (time.time() - self._conn_since) < CONNECT_SETTLE_SECONDS
        if settling:
            conn_status_text, conn_status_color = "Conectando...", "#f59e0b"
        elif is_conn:
            conn_status_text, conn_status_color = net_status_text, net_status_color
        elif self._is_connecting:
            conn_status_text, conn_status_color = "Conectando...", "#f59e0b"
        else:
            conn_status_text, conn_status_color = "Desconectado", "#94a3b8"

        # Ícono de reloj: verde cargando, ámbar descargando (autonomía normal),
        # rojo cuando quedan 2h o menos, parpadeo cuando quedan menos de 20 min.
        # Formato "10h32m". Dato real del equipo (Registro 104).
        # Detectar cambio de modo (carga/descarga/neutro) para la ventana de
        # gracia de arriba. Usa los mismos umbrales (net > 5 / net < -5) que
        # ya se usan en todo el resto de esta función para clasificar el modo.
        current_power_mode = "charging" if net > 5 else ("discharging" if net < -5 else "neutral")
        if current_power_mode != self._last_power_mode:
            self._mode_change_time = time.time()
            self._last_power_mode = current_power_mode
        in_grace_period = (time.time() - self._mode_change_time) < self.GRACE_AFTER_MODE_CHANGE

        runtime_min = t.get("runtime_minutes")
        if is_conn and soc is not None:
            self._record_sample(soc, net, runtime_min, current_power_mode, in_grace_period)
        if self._offline_active:
            # Offline: tiempo estimado en cuenta regresiva, color de desconectado, sin parpadeo.
            rem = self._offline_state()[1]
            if rem is None:
                time_txt = "--h--m"
            else:
                time_txt = f"{int(rem) // 60}h{int(rem) % 60:02d}m"
            time_color = COLOR_OFFLINE
            self._time_low = False
        elif isinstance(runtime_min, (int, float)) and runtime_min > 0 and (net > 5 or net < -5):
            hh = int(runtime_min) // 60
            mm = int(runtime_min) % 60
            time_txt = f"{hh}h{mm:02d}m"
            if net > 5:
                time_color = "#10b981"  # verde: cargando
            elif in_grace_period:
                # Acabamos de pasar a descargar: runtime_min todavía puede
                # ser la estimación vieja de "cargando" -- se muestra el
                # ámbar base (sin escalar a rojo) hasta que el equipo
                # recalcule, en vez de arriesgarse a un rojo falso.
                time_color = "#f59e0b"
            elif runtime_min <= 120:
                time_color = "#ef4444"  # rojo: 2h o menos de autonomía
            else:
                time_color = "#f59e0b"  # ámbar: descargando, autonomía normal
            # Condición 3 (independiente): descargando con menos de 20 min
            # restantes. Misma protección: no dispara dentro de la ventana
            # de gracia recién iniciada un cambio de modo.
            self._time_low = (net < -5) and (alert_config.blink_time_low(runtime_min)) and not in_grace_period
            if self._time_low:
                self._time_color = time_color
        else:
            time_txt = "--h--m"
            time_color = "#94a3b8"
            self._time_low = False
        self.pill_time_est.configure(text=time_txt)
        self.hud_time_est.configure(text=time_txt)
        if not self._time_low:
            self.pill_time_est.configure(fg=time_color)
            self._set_clock_color(time_color)
            self.hud_time_est.configure(fg=time_color)

        # Vista Gamer
        self.hud_soc_val.configure(text=soc_text)
        if not self._batt_low:
            self.hud_soc_val.configure(fg=soc_color)
        self.hud_net_power.configure(text=conn_status_text)
        if not self._power_extreme:
            self.hud_net_power.configure(fg=conn_status_color)
        self.hud_solar_w.configure(text=f"{t.get('dc_input_watts', 0)} W")
        # Voltaje (Reg 1213) y corriente (Reg 1214) solares leídos del equipo;
        # ble_manager ya deja W / V de respaldo en dc_input_current si falla.
        solar_volts = t.get("dc_input_volts", 0.0) or 0.0
        solar_amps = t.get("dc_input_current", 0.0) or 0.0
        if solar_volts > 0:
            self.hud_solar_v.configure(text=f"{solar_volts:.1f}V")
            self.hud_solar_a.configure(text=f"{solar_amps:.1f}A")
        else:
            self.hud_solar_v.configure(text="--V")
            self.hud_solar_a.configure(text="--A")
        self.hud_grid_w.configure(text=f"{t.get('ac_input_watts', 0)} W")
        ac_input_volts = t.get("ac_input_volts", 0.0)
        ac_input_amps = t.get("ac_input_current", 0.0) or 0.0
        self.hud_grid_v.configure(text=f"{ac_input_volts:.0f}V" if ac_input_volts > 0 else "--V")
        self.hud_grid_a.configure(text=f"{ac_input_amps:.1f}A" if ac_input_volts > 0 else "--A")
        # Alertas: voltaje de red, corriente de entrada y salud de la batería
        # (celda crítica, desbalance, descalibración del %).
        prot_fire = self._prot_ui_read(t, is_conn)
        if self._temp_own:
            self.temp_tracker.update(t.get("batt_temp"), is_conn)
        temp_fire = self.temp_tracker.consume_fire()
        if temp_fire:
            self._vis.reset("temp_warn")
            self._vis.reset("temp_crit")
        self._update_alerts(ac_input_volts, solar_amps, ac_input_amps, self._build_cell_alerts(t, is_conn))
        if not self._current_high:
            self._prot_consump_active = False   # "por Consumo" solo mientras dure la alerta
        if prot_fire or temp_fire or (self.temp_tracker.visible and not self._banner_visible):
            self._show_active_alerts()
        self.hud_ac_out_w.configure(text=f"{t.get('ac_output_watts', 0)} W")
        self.hud_dc_out_w.configure(text=f"{dc_out_w} W")

        ac_on = bool(t.get("ac_output_on", False))
        dc_on = bool(t.get("dc_output_on", False))
        if time.time() - self._last_click_ac > 2.0:
            self.hud_bt_ac.configure(text="AC ⬤" if ac_on else "AC ⭘", fg="#ff3366" if ac_on else "#64748b")
        if time.time() - self._last_click_dc > 2.0:
            self.hud_bt_dc.configure(text="DC ⬤" if dc_on else "DC ⭘", fg="#00f0ff" if dc_on else "#64748b")

        turbo_on = t.get("charge_mode") == "turbo"
        if time.time() - self._last_click_turbo > 2.0:
            self.hud_bt_turbo.configure(text="TURBO ⬤" if turbo_on else "TURBO ⭘", fg="#fbbf24" if turbo_on else "#64748b")

        grid_on = bool(t.get("grid_enhancement_on", False))
        if time.time() - self._last_click_grid > 2.0:
            self.hud_bt_grid.configure(text="REFUERZO ⬤" if grid_on else "REFUERZO ⭘", fg="#38bdf8" if grid_on else "#64748b")

        # Vista Pill
        self.pill_soc.configure(text=soc_text)
        if not self._batt_low:
            self.pill_soc.configure(fg=soc_color)
            self._set_battery_color(soc_color)
        self.pill_status.configure(text=conn_status_text)
        if not self._power_extreme:
            self.pill_status.configure(fg=conn_status_color)

        # Color del ícono Bluetooth según modo de la app:
        #   Servidor = verde, Cliente = azul. Rojo = desconectado.
        #   Mientras se intenta conectar, el color lo maneja _blink_tick (ambar).
        if is_conn:
            is_server = getattr(self.master, "_remote", None) is None
            self._set_bt_color(COLOR_BT_CONNECTED_SERVER if is_server else COLOR_BT_CONNECTED)
        elif not self._is_connecting:
            self._set_bt_color(COLOR_BT_DISCONNECTED)

        self._refresh_offline_arrow()
        self._resize_to_content()
