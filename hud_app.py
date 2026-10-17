"""
hud_app.py - Panel de Control Oficial & Gamer HUD para Bluetti Elite 100 V2 (Windows 10/11)
============================================================================================
v2:
  * Ventana de tamaño FIJO (ancho y alto), medido automáticamente para que quepa todo.
  * Tarjeta de batería rediseñada (el doble de ancha que Entrada / Salida):
      - cabecera, nivel, capacidad estimada + salud, estado de carga en una sola línea.
      - sub-tarjeta "Banco de Baterías": voltaje del pack, temperatura promedio,
        16 celdas en 4x4 (cada una en su recuadro), Min/Max/Dif y estado destacado
        con icono "i" que abre una ventana flotante con el detalle.
  * Cola de cambios DC, resaltado optimista de la fuente DC, etc. (sin cambios de comportamiento).
"""

import collections
import functools
import json
import logging
import math
import os
import queue
import statistics
import sys
import time
import tkinter as tk
from weather_provider import WeatherManager, provider_choices
from tkinter import messagebox
from typing import Dict, Any, List, Optional
import customtkinter as ctk
from ble_manager import BluettiBleManager, GRID_LIMIT_MIN_A, GRID_LIMIT_MAX_A, RATED_WH as _RATED_WH_F
from floating_widget import (FloatingHudWidget, acquire_single_instance_lock, notify_already_running,
                             TempAlertTracker, TEMP_WARN_C, TEMP_CRIT_C, is_app_startup_enabled, set_app_startup)
from tray_icon import TrayController, TRAY_AVAILABLE
from telemetry_server import ServerUiMixin
import floating_widget as _fw_mod
from ble_manager import alert_config  # [alertas-patch]

logger = logging.getLogger(__name__)

ctk.set_appearance_mode("Dark")
ctk.set_default_color_theme("blue")

# Decimales con que se muestran los voltajes de celda. Con 3 se ven diferencias de 1 mV
# (3.308V vs 3.310V); con 2 todas las celdas aparecerían idénticas (3.31V).
CELL_DECIMALS = 3

RATED_WH = int(_RATED_WH_F)   # fuente unica: ble_manager
ZOOM_MIN, ZOOM_MAX = 0.6, 3.0   # límites del zoom proporcional de la ventana
# En pantallas menores que REF_SCREEN (p. ej. 1366x768) la app se reduce de forma proporcional para ocupar
# la misma fraccion de pantalla que ocupa en Full HD (la escala de Windows tambien se compensa).
AUTO_FIT_SMALL_SCREENS = True
REF_SCREEN = (1920, 1080)
# Boost para pantallas pequeñas: +20 % en el factor de zoom base (antes de ajustar a pantalla).
SMALL_SCREEN_BOOST = 1.20
# Fracción máxima de la pantalla que puede ocupar el contenido al aplicar el boost (seguridad).
MAX_SCREEN_FRACTION = 0.95
# Al maximizar, customtkinter reescala y redibuja TODOS los widgets (set_widget_scaling): es lo que hacía
# lenta y errática la ventana. Con False el contenido se queda a su tamaño natural, centrado, y no se
# recalcula nada (maximizar/restaurar es instantáneo). True = comportamiento anterior (zoom proporcional).
ZOOM_ON_MAXIMIZE = True
ZOOM_SETTLE_MS = 50   # el zoom solo se aplica cuando el tamaño lleva este tiempo sin cambiar (fin de la animación)
HIDE_ON_MINIMIZE = False   # False: minimizar deja la app en la barra de tareas (acceso rápido); True: la manda a la bandeja
CLOSE_TO_TRAY = True       # True: la x de la ventana solo la oculta en la bandeja; se cierra con "Salir" del icono de la bandeja
DC_UI_HOLD_S = 7.0   # tiempo que se mantiene el resaltado optimista tras pulsar la fuente DC

# grado de equilibrio -> (texto, color de texto, fondo)
GRADE_STYLE = {
    "excellent":  ("Equilibrada",     "#00ff88", "#0b2e1f"),
    "good":       ("Buen equilibrio", "#4ade80", "#0f2d1c"),
    "acceptable": ("Aceptable",       "#bef264", "#26300f"),
    "fair":       ("Regular",         "#fbbf24", "#3a2d0a"),
    "bad":        ("Desequilibrio",   "#ff3366", "#3a0f1a"),
}
NEUTRAL_STYLE = ("Sin datos", "#94a3b8", "#1e293b")
UNRELIABLE_STYLE = ("Δ incierto", "#fbbf24", "#2a2410")   # solo con potencia muy alta
# En la meseta un Δ alto no es desequilibrio de carga sino dispersión (resistencia/conexión/temperatura)
PLATEAU_TEXT = {"fair": "Dispersión leve", "bad": "Dispersión alta"}
WARN_STYLE = {"high": ("Celda alta", "#fb923c", "#3a2210"), "low": ("Celda baja", "#fb923c", "#3a2210")}
ZONE_NAME = {"plateau": "meseta", "top": "codo superior", "bottom": "codo inferior"}

C_BG = "#080b11"
C_CARD = "#0e141f"
C_BORDER = "#1e293b"


# Cada cuantos segundos rota la alerta mostrada en la barra de estado
ALERT_CYCLE_S = 3.0
# La telemetria llega varias veces por ciclo; la pantalla se actualiza como mucho cada UI_MIN_INTERVAL_S
# y no mientras se mueve/redimensiona la ventana (hasta UI_DRAG_QUIET_S despues del ultimo movimiento)
UI_MIN_INTERVAL_S = 0.25
UI_DRAG_QUIET_S = 0.35
SPIKE_NOTICE_S = 6.0


class AppAlertTracker:
    """Evalua en la app las mismas alertas que el widget muestra en su banner
    (para poder avisar aunque el widget este cerrado u oculto). Los umbrales de
    corriente se leen de floating_widget.py; los de voltaje (106 V inestable /
    101 V bajo) y los textos/colores son los del widget."""

    @property
    def VOLT_WARN(self):  # [alertas-patch]
        return float(alert_config.get("ac_in_voltage", "warn_v"))

    @property
    def VOLT_CRIT(self):  # [alertas-patch]
        return float(alert_config.get("ac_in_voltage", "crit_v"))

    def __init__(self, ble, temp_tracker=None):
        self.ble = ble
        self.temp = temp_tracker   # alertas de temperatura (compartido con el widget)
        self.is_conn = False
        self.volt_sev = "ok"
        self.dc_high = False
        self.ac_high = False
        self.cell_alerts: List[Any] = []
        self.lim_dc = float(getattr(_fw_mod, "CURRENT_LIMIT_DC", 15.0))
        self.lim_ac = float(getattr(_fw_mod, "CURRENT_LIMIT_AC", 12.0))
        self.hyst = float(getattr(_fw_mod, "CURRENT_HYSTERESIS", 0.5))
        # Protecciones automaticas (claves prot_* de ble_manager.py)
        self.seen_init = False
        self.dc_active = False
        self.dc_end_ts: Optional[float] = None
        self.dc_dismissed = False
        self.restored_seen = 0.0
        self.restored_until = 0.0
        self.ac_ts_seen = 0.0
        self.ac_acked = False
        self.ac_pending = False
        self.cons_seen = 0.0
        self.consump_active = False
        self.spike_seen = 0.0
        self.spike_until = 0.0
        self.spike_text = ""
        self._vis = alert_config.VisState()   # que avisos se ven en la barra de estado (tiempos / cerrados)
        self.last_shown: List[Any] = []
        self.smart = None   # SmartCharge (la app lo asigna): aviso azul «Carga inteligente Activa»

    def update(self, t: Dict[str, Any], is_conn: bool):  # [alertas-patch]
        t = alert_config.mask_telemetry(t)
        try:
            alert_config.notices().update(t, is_conn)
            self.lim_dc, self.lim_ac, _d, _a = alert_config.current_limits(t.get("charge_mode"))
        except Exception:
            pass
        return self.update_impl(alert_config.tracker_view(t), is_conn)

    def update_impl(self, t: Dict[str, Any], is_conn: bool):
        now = time.time()
        self.is_conn = is_conn
        tv = bool(self.temp is not None and self.temp.visible)
        if tv and not getattr(self, "_temp_vis_prev", False):
            self._vis.reset("temp_warn")
            self._vis.reset("temp_crit")
        self._temp_vis_prev = tv

        # Voltaje de red (0 V = red no conectada, no cuenta como falla)
        v = t.get("ac_input_volts", 0.0) or 0.0
        self.grid_alarm = bool(t.get("grid_low_voltage_alarm")) and is_conn   # ALARMA_RED_V2
        self.grid_alarm_v = v
        if v <= 0:
            self.volt_sev = "ok"   # apagon / sin red: no hay alarma
        elif v < self.VOLT_CRIT or (v < self.VOLT_WARN and self.grid_alarm):
            self.volt_sev = "critical"
        elif v < self.VOLT_WARN:
            self.volt_sev = "warning"
        else:
            self.volt_sev = "ok"

        # Corriente de entrada con histeresis (igual que el widget)
        dc_a = t.get("dc_input_current", 0.0) or 0.0
        ac_a = t.get("ac_input_current", 0.0) or 0.0
        self.dc_high = dc_a >= self.lim_dc - self.hyst if self.dc_high else dc_a >= self.lim_dc
        self.ac_high = ac_a >= self.lim_ac - self.hyst if self.ac_high else ac_a >= self.lim_ac

        # Salud de la bateria (celda critica, desbalance, descalibracion)
        try:
            self.cell_alerts = FloatingHudWidget._build_cell_alerts(t, is_conn)
        except Exception:
            self.cell_alerts = []

        # Protecciones automaticas
        dc_active = bool(t.get("prot_dc_active")) and is_conn
        if not dc_active or not self.dc_active:
            self.dc_dismissed = False
        self.dc_active = dc_active
        self.dc_end_ts = t.get("prot_dc_end_ts") if dc_active else None

        rts = float(t.get("prot_dc_restored_ts") or 0.0)
        if self.seen_init and rts > self.restored_seen:
            self._vis.reset("dc_restored")
            self.restored_until = now + alert_config.event_secs("dc_restored")
        self.restored_seen = rts

        ats = float(t.get("prot_ac_reduced_ts") or 0.0)
        if ats > self.ac_ts_seen:
            self.ac_acked = False
        self.ac_ts_seen = ats
        backend_pending = bool(t.get("prot_ac_reduced_pending"))
        if not backend_pending:
            self.ac_acked = False
        self.ac_pending = backend_pending and not self.ac_acked

        cts = float(t.get("prot_ac_consumption_ts") or 0.0)
        if self.seen_init and cts > self.cons_seen:
            self.consump_active = True
        self.cons_seen = cts
        if not (self.dc_high or self.ac_high):
            self.consump_active = False   # "por Consumo" solo mientras dure la alerta
        # Pico de arranque (evento puntual que detecta ble_manager.py)
        sts = float(t.get("spike_ts") or 0.0)
        if self.seen_init and sts > self.spike_seen:
            self.spike_text = f"Pico de Arranque de {int(t.get('spike_w') or 0)} W"
            self._vis.reset("spike")
            self.spike_until = now + alert_config.event_secs("spike")
        self.spike_seen = sts
        self.seen_init = True

    def countdown_text(self, now: float) -> str:
        if not self.dc_end_ts:
            rem = 60            # el cambio a "Otros" aun no esta confirmado
        else:
            rem = max(0, int(self.dc_end_ts - now + 0.999))
        return f"{rem // 60}:{rem % 60:02d}"

    def dc_row(self, now: float):
        """(texto, color, cerrable) para la tarjeta Solar (cuenta atras o 'restaurada')."""
        if not self.is_conn:
            return None
        if self.dc_active and not self.dc_dismissed:
            return (f"Protección de Alta corriente DC {self.countdown_text(now)}", "#38bdf8", True)
        if now < self.restored_until:
            return ("Alta corriente restaurada", "#10b981", False)
        return None

    def _all_alerts(self, now: float):
        out = list(alert_config.filter_alert_lines(self.alerts_impl(now, True)))
        if self.is_conn:
            out.extend(alert_config.notices().lines())
        return out

    def alerts(self, now: float, force_temp: bool = False):
        """Avisos a mostrar ahora (tiempos configurados; force = triangulo tocado)."""
        out = self._all_alerts(now)
        self._vis.update(out)
        self.last_shown = self._vis.visible(out, force_temp)
        sl = self.smart_line(now)
        return self.last_shown + [sl] if sl else self.last_shown   # el aviso de Carga inteligente no pasa por _vis ni da triangulo

    def smart_line(self, now: float):
        """(texto, color, 'smart') mientras la Carga inteligente esta activa y su aviso no se haya cerrado."""
        sm = self.smart
        if sm is None or not self.is_conn:
            return None
        try:
            return sm.notice(now)
        except Exception:
            return None

    def dismiss_smart(self):
        if self.smart is not None:
            self.smart.dismiss_notice()

    def alerts_impl(self, now: float, force_temp: bool = False):
        """[(texto, color, tipo)] con las alertas activas; tipo 'dc'/'ac' = se pueden
        cerrar con la x, None = dependen solo de su condicion."""
        if not self.is_conn:
            return []
        out = []
        if self.dc_active and not self.dc_dismissed:
            out.append((f"Protección de Alta corriente DC {self.countdown_text(now)}", "#38bdf8", "dc"))
        if now < self.restored_until:
            out.append(("Alta corriente restaurada", "#10b981", None))
        if now < self.spike_until:
            out.append((self.spike_text, "#fb923c", None))
        if self.ac_pending:
            out.append(("Alta corriente AC Reducida", "#f59e0b", "ac"))
        if self.temp is not None:
            temp_alert = self.temp.text_color(True)
            if temp_alert:
                out.append((temp_alert[0], temp_alert[1], "temp"))
        for text_, color, sev in self.cell_alerts:
            if sev == "critical":
                out.append((text_, color, None))
        if self.volt_sev == "critical":
            _gv = getattr(self, "grid_alarm_v", 0.0) or 0.0
            _gt = "⚠ ¡Alarma Bajo Voltaje de Red!" + (f" {_gv:.1f}V" if _gv > 0 else "")
            out.append((_gt, "#f87171", None))
        if self.dc_high:
            out.append(("⚠ ¡Precaución Alta Corriente DC!", "#ef4444", None))
        if self.ac_high:
            if self.consump_active:
                out.append(("Alta corriente por Consumo", "#ef4444", None))
            else:
                out.append(("⚠ ¡Precaución Alta Corriente AC!", "#ef4444", None))
        if self.volt_sev == "warning":
            out.append(("⚠ ¡Precaución Voltaje Inestable!", "#f59e0b", None))
        for text_, color, sev in self.cell_alerts:
            if sev != "critical":
                out.append((text_, color, None))
        return out

    def severity(self) -> str:
        if not self.is_conn:
            return "ok"
        _out = self._all_alerts(time.time())
        self._vis.update(_out)
        return alert_config.triangle_severity(_out)   # solo cuentan los avisos que dejan triangulo

    def dismiss_dc(self):
        if self.dc_active:
            self.dc_dismissed = True

    def dismiss_temp(self):
        if self.temp is not None:
            self.temp.dismiss()
        self._vis.close([it for it in self.last_shown if "caliente" in str(it[0]).lower()])

    def dismiss_all(self):  # [alertas-patch]
        self.dismiss_smart()
        self._vis.close([it for it in self.last_shown
                         if "salida ac apagada" not in str(it[0]).lower()
                         and "batería crítica" not in str(it[0]).lower()
                         and "bateria critica" not in str(it[0]).lower()
                         and "apagado inmediato por bater" not in str(it[0]).lower()])
        return self.dismiss_all_impl()

    def dismiss_all_impl(self):
        """x de la barra de estado: reconoce 'AC Reducida' y oculta la cuenta atras."""
        if self.ac_pending:
            self.ac_pending = False
            self.ac_acked = True
            ack = getattr(self.ble, "ack_ac_reduced_notice", None)
            if callable(ack):
                try:
                    ack()
                except Exception:
                    pass
        # Batería Crítica: detectar si se cerró un aviso de 5% o 4%
        for it in self.last_shown:
            text = str(it[0]).lower()
            if "batería crítica (5%)" in text or "bateria critica (5%)" in text:
                ack = getattr(self.ble, "ack_bat_crit_notice", None)
                if callable(ack):
                    try:
                        ack(5)
                    except Exception:
                        pass
                break
            if "batería crítica (4%)" in text or "bateria critica (4%)" in text:
                ack = getattr(self.ble, "ack_bat_crit_notice", None)
                if callable(ack):
                    try:
                        ack(4)
                    except Exception:
                        pass
                break
        self.dismiss_dc()


def _pixel_exact_dimensions_event(self, event):
    """customtkinter solo redibuja si el tamaño cambia en unidades LÓGICAS redondeadas; con zoom > 1 un
    cambio de 1-2 px reales pasa desapercibido y el borde queda más corto que el widget (líneas que faltan).
    Aquí se compara en píxeles reales."""
    if (round(self._apply_widget_scaling(self._current_width)) != event.width
            or round(self._apply_widget_scaling(self._current_height)) != event.height):
        self._current_width = self._reverse_widget_scaling(event.width)
        self._current_height = self._reverse_widget_scaling(event.height)
        self._draw(no_color_updates=True)


from customtkinter.windows.widgets.core_widget_classes import CTkBaseClass as _CTkBaseClass
_CTkBaseClass._update_dimensions_event = _pixel_exact_dimensions_event   # antes de crear ningún widget

OFFLINE_STATUS_TEXT ="Desconectado, actualizaciones imprecisas de Batería y Tiempo"
OFFLINE_ARROW_COLOR = "#60a5fa"
OFFLINE_TEXT_COLOR = "#94a3b8"


class _OfflineModel:
    """Estimador offline propio de la app (para cuando el widget está cerrado). Reutiliza tal cual
    los métodos del widget, así que el algoritmo es exactamente el mismo."""
    def __init__(self):
        self._hist = collections.deque()
        self._offline_active = False
        self._off_origin_ts = 0.0
        self._off_soc0 = 0.0
        self._off_rate = 0.0
        self._off_last_key = None
        self._off_mode = "neutral"

    _record_sample = FloatingHudWidget._record_sample
    _begin_offline = FloatingHudWidget._begin_offline
    _end_offline = FloatingHudWidget._end_offline
    _offline_state = FloatingHudWidget._offline_state


# =============================================================================
# [sky-patch] ESTADO DEL CIELO Y RENDIMIENTO DEL PANEL (Boviet 615 W, Cuba)
# Compara la potencia solar real (dc_input_watts, ya medida tras el cable) con la esperada con cielo
# despejado para la hora/epoca/ubicacion y deduce el estado del cielo. Guarda el historial en
# %LOCALAPPDATA%\BluettiElite100Widget\sky_log.json. Ajustes: constantes de "parametros".
# =============================================================================
import datetime  # [sky-patch]

# ---------------------------------------------------------------- parametros
PANEL_WP = 615.0              # potencia maxima del panel (W)
# Etiqueta del panel: Mono-Si, 2382x1134x33 mm, 32.5 kg (formato 66 celdas media celda de 615 W). La etiqueta no trae la tabla
# electrica; se toman los valores tipicos de modulos 615 W del mismo formato (Imp ~15 A, Vmp ~41 V, Pmax -0.29..-0.30 %/K).
PANEL_VMP = 41.0              # tension de trabajo (V): para estimar la corriente/pérdida del cable
PANEL_TEMP_COEF = -0.0030     # coeficiente de potencia por °C
PANEL_TILT_DEG = 18.0         # inclinacion del panel (dato del usuario)
PANEL_AZIMUTH_DEG = 190.0     # ajustado a tus datos reales (curva 8/10 y 13-14 sept); la brujula daba 214 (magnetico)
IAM_B0 = 0.05                 # perdida por reflexion del vidrio con luz muy inclinada (ASHRAE)
ALBEDO = 0.20                 # reflexion del suelo
DIFFUSE_FRAC = 0.15           # fraccion difusa de la irradiancia con cielo despejado (tropico humedo)
CELL_RISE_K = 20.0            # °C que sube la celda sobre el ambiente a 800 W/m2 (panel ventilado)
VMP_TEMP_COEF = -0.0027       # la tension de trabajo baja al calentarse la celda -> sube la corriente
CABLE_TEMP_COEF = 0.00393     # resistencia del cobre por °C (el cable al sol se calienta y pierde mas)
MODEL_VER = 9                 # nueva version: solo ajuste matutino/crepuscular, no toca mediodia
LAT_DEG, LON_DEG = 22.097425, -78.625940   # ubicacion exacta (salida/puesta del sol se calculan con esto)
TAMB_C = [23, 24, 25, 26.5, 27.5, 28.5, 29, 29, 28.5, 27.5, 26, 24]   # temperatura ambiente media diurna por mes
MONTH_CLEARNESS = [1.0] * 12   # ajuste fino de la transparencia del cielo por mes (1.0 = modelo base)
CABLE_OHM_PER_M = 0.008286    # 14 AWG
CABLE_RUN_M = 20.0
CABLE_ROUNDTRIP = True
CABLE_R20_OHM = CABLE_OHM_PER_M * CABLE_RUN_M * (2.0 if CABLE_ROUNDTRIP else 1.0)   # a 20 °C
SAHARA_MONTHS = (5, 6, 7, 8, 9)   # temporada de polvo del Sahara (solo cambia el nombre del estado)

MIN_EXPECTED_W = 25.0         # por debajo no se clasifica (amanecer / atardecer)
LOWSUN_ELEV = 4.5             # ni mientras el sol este mas bajo que esto ("Sol bajo": ~22-26 min tras salir / antes de ponerse)
HAZE_MAX_S = 2400.0           # neblina / niebla: solo se declara hasta 40 min despues de la salida del sol
HAZE_HIGH = 0.85              # Neblina: rendimiento estable entre HAZE_LOW y HAZE_HIGH (similar al cielo velado y hasta ~10 pts mas bajo)
HAZE_LOW = 0.68               # Niebla (mas densa): entre STRATUS_MIN (0.30) y HAZE_LOW
HAZE_CV = 0.15                # variacion maxima (desv./media) para considerarla estable
HIGH_A = 15.0                 # corriente solar (A) por encima de la cual el trazo de potencia se pinta de rojo
AC_CHG_W = 20.0               # entrada AC (W) por encima de la cual se considera carga por AC (trazo verde de la bateria)
HIGH_T = 0.75                 # >= : sin bajada (una bajada = nube); relativo a la referencia despejada aprendida
SUN_T = 0.89                  # a partir de esto el cielo es Soleado; entre HIGH_T y SUN_T es Brumoso (nunca Brumoso por encima de Soleado)
DENSE_T = 0.30                # nublado denso: media de la bajada por debajo de esto
SAHARA_LOW = 0.78             # polvo: rendimiento estable entre SAHARA_LOW y SUN_T (medido: 10-18 % menos que un dia limpio)
SAHARA_CV = 0.10              # ...con variacion (desv./media) menor que esto
STRATUS_CV = 0.15
STRATUS_GAP = 0.12            # estratos: rendimiento estable AL MENOS 12 puntos (10-15 %) por debajo del piso del polvo
STRATUS_MAX = SAHARA_LOW - STRATUS_GAP   # 0.66
STRATUS_MIN = DENSE_T         # por debajo de 0.30 ya es nublado denso
SMOOTH_TAU_S = 20.0           # suavizado de la potencia
GAP_TOL_S = 60.0              # una subida menor que esto no corta una bajada
DIP_MIN_S = 20.0              # bajada minima para contar como nube pasajera
CLOUD_S = 300.0               # bajada > 5 min = Nublado
DENSE_S = 600.0               # bajada > 10 min y mas baja = Nublado denso
RISE_S = 180.0                # potencia alta > 3 min: de Nublado pasa a Ocasionales
OCC_AFTER_CLOUD_S = 600.0     # ...y tras 10 min despejado vuelve a Soleado
OCC_AFTER_DIP_S = 300.0       # tras una bajada corta, 5 min despejado
INT_MIN_S = 20.0              # nublados intermitentes: cada subida/bajada dura entre 20 s...
INT_MAX_S = 260.0             # ...y ~4 min, repetido (al menos 4 tramos seguidos en los ultimos 15 min)
BIN_S = 60                    # resolucion de los puntos guardados (s); los dias guardados antes conservan 120
KEEP_DAYS = 14
SEED_K = 1.02                 # real/modelo mientras no haya franja aprendida (tus dias limpios: 0.95-1.13)
K_FLOOR = 0.80                # suelo del factor total; permite que la curva BAJE si el modelo sobreestima (antes 0.90 lo impedia)
K_MAX = 1.20
K_SYS_MIN = 0.70              # k_sistema (suciedad del panel) nunca baja de esto; 1.0 = panel limpio confirmado
SLOT_S = 900                  # franjas de 15 min
SUSTAIN_MIN = 5               # minutos seguidos para aprender una franja
SLOT_DECAY = 0.995            # por dia
FULL_SOC = 100                # bateria "llena" = 100 % Y sin entrar potencia a la bateria (el equipo limita lo que acepta: no se clasifica ni se aprende)
FULL_HOLD_S = 60.0            # y se sigue tratando como limitado este tiempo tras bajar de FULL_SOC (se recupera con retraso)
WARMUP_S = 90.0               # tras abrir la app / amanecer: "Determinando..." hasta tener estos segundos de datos validos
LAST_VALID_S = 900.0          # el ultimo estado guardado solo se reutiliza si tiene menos de 15 min
SUN_H0 = -1.0                # altura del sol a la salida/puesta: -0.833 (refraccion+disco) y ~-0.18 por tus 36 m de altitud
SUN_ERR_S = 180.0            # margen de error de la salida/puesta (s): horizonte real, arboles/edificios, refraccion
BAT_NEUTRAL_W = 10.0         # |entrada - salida| menor que esto = bateria equilibrada (DC in = DC out)
DENSE_HOLD_S = 180.0          # Lluvioso: debe llevar mas de 3 min por debajo de DENSE_T (antes se mantiene Nublado)
SAHARA_HOLD_S = 120.0         # Brumoso: debe mantenerse 2 min para declararse (evita nubes de paso ligeras)
DWELL_S = 45.0               # un cambio de cielo debe mantenerse este tiempo para declararse (nubes de paso <45 s no cuentan)
SUN_BACK = 0.80              # para volver a Soleado desde Nublado/Lluvioso hace falta superar esto (histeresis)
LOW_MAX_S = 1500.0           # "Sol bajo" dura como maximo 25 min tras la salida / antes de la puesta (despues se clasifica aunque el esperado sea pequeño)
TW_W = 12.0                  # W esperados al ras del horizonte: luz difusa antes de salir y despues de ponerse el sol (extremos de la curva menos abruptos)
LEARN_MIN = 0.90              # la referencia solo aprende rendimientos sostenidos >= 90 % del modelo (no con nubes)
LEARN_CV = 0.05               # ...y estables: variacion (desv./media) de las medias de 1 min menor que esto (sin picos entre nubes)
LEARN_DOWN_W = 0.12          # peso diario con que una franja BAJA hacia un rendimiento estable algo menor
LEARN_UP_W = 0.50            # peso con que SUBE hacia un rendimiento estable mayor (rapido: lo mayor es casi siempre cielo limpio)
LEARN_ELEV_MIN = 10.0        # elevacion minima para aprender (antes 20): la manana tambien se corrige con datos
LEARN_DAYS = 3                # un primer valor por debajo de la referencia inicial solo se acepta si se repite en estos dias distintos

# clave: (nombre, rendimiento, color, icono)
STATES = {
    "na":     ("Determinando...", "", "#94a3b8", "☀"),
    "night":  ("Sin sol", "", "#64748b", "🌙"),
    "lowsun": ("Sol bajo", "", "#ef4444", "🌅"),
    "sun":    ("Soleado", "Rendimiento máximo", "#fde047", "☀"),
    "sahara": ("Brumoso", "Rendimiento reducido estable", "#fb923c", "🌫"),
    "occ":    ("Nublado", "Rendimiento bajo", "#38bdf8", "⛅"),
    "cloud":  ("Nublado", "Rendimiento bajo", "#94a3b8", "☁"),
    "dense":  ("Lluvioso", "Rendimiento mínimo", "#818cf8", "🌧"),
    "stratus": ("Nublado", "Rendimiento bajo", "#6ee7b7", "🌥"),   # al final: no cambia los indices guardados
    "interm": ("Nublado", "Rendimiento bajo", "#22d3ee", "🌤"),
    "neblina": ("Neblina", "Rendimiento reducido", "#d9c9a3", "🌁"),
    "niebla": ("Niebla", "Rendimiento bajo", "#b8a98c", "🌫"),
}
STATE_KEYS = list(STATES)
ALIAS = {"occ": "cloud", "interm": "cloud", "stratus": "cloud"}   # estados antiguos -> los 4 actuales (los puntos ya guardados se conservan)
BAT_COL = {"chg": "#2dd4bf", "dis": "#f97316", "bal": "#94a3b8", "full": "#4dff88"}
BAT_NAME = {"chg": "cargando", "dis": "descargando", "bal": "equilibrada", "full": "completa"}


RIB_PX = 50.0                 # grosor de la cinta de bateria hacia abajo (px): se desvanece hasta desaparecer
RIB_N = 20                     # capas del degradado de la cinta
PEAK_H0 = 9                   # los picos de potencia (max/min) solo se buscan entre estas horas
PEAK_H1 = 17
BAT_TAG_HOURS = (10, 16)          # horas a las que se rotula el % de bateria en la grafica
PEAK_MIN_W = 10               # el pico minimo debe ser superior a esta potencia (W)
RIB_A = 0.50                  # intensidad de la cinta junto a la linea (0-1)
BAT_LINE = "#c084fc"          # linea y cinta de la bateria (violeta); pasa a verde solo mientras carga por AC
BAT_AC = "#22c55e"
RIB_CW = 3                    # ancho (px) de las columnas con que se dibuja la cinta (sin solapes ni costuras)
# casillas de customtkinter: borde claro (el por defecto casi no se ve sobre el fondo oscuro) y tamaño que cabe aun con zoom < 1
_CK_STYLE = dict(checkbox_width=20, checkbox_height=20, border_width=2, corner_radius=4, border_color="#94a3b8",
                 fg_color="#0ea5e9", hover_color="#38bdf8", checkmark_color="#ffffff")
LIME_BG = "#0a4752"           # fondo (promedio del mallado cian) del area "supuesto con bateria llena"
PANEL_FILL = "#0b3b46"        # color del relleno bajo la curva del panel (la cinta se funde con el)
GAP_FILL = "#2a2a2a"          # gris para relleno de huecos (sin datos BLE)
GAP_LINE = "#555555"          # gris de la malla y del borde superior de los huecos (sin datos BLE)
MESH_STIPPLE = "gray25"       # malla de los estimados (batería llena / sin datos): MISMO patron en ambos
MESH_LINE_W = 2               # grosor del borde superior discontinuo de la malla (antes 1)
MESH_DASH = (3, 2)            # guiones del borde superior de la malla
GAP_BAR_STIPPLE = "gray50"    # barras de Cielo / Batería en los huecos (estimadas): mismo color, punteadas
GAP_RIB_ALPHA = 0.6           # la cinta de batería estimada en un hueco es algo mas tenue que la medida
WX_PRECIP_DENSE = 60.0        # pronostico -> "Lluvioso": prob. de precipitacion >= esto %
WX_CLOUD_DENSE = 90.0         #                           o nubosidad >= esto % con humedad >= WX_RH_DENSE
WX_RH_DENSE = 90.0
WX_CLOUD_CLOUDY = 50.0        # pronostico -> "Nublado": nubosidad >= esto %
WX_AOD_HAZE = 0.2             # pronostico -> "Brumoso": AOD (polvo) >= esto
WX_CLOUD_ATTEN = 0.75         # curva "esperado segun clima": atenuacion por nubes (Kasten-Czeplak): 1 - 0.75 * nubosidad^3.4
WX_CLOUD_EXP = 3.4
WX_PRECIP_ATTEN = 0.30        # ... y hasta -30 % extra con 100 % de probabilidad de precipitacion


@functools.lru_cache(maxsize=8192)
def _mix(c1: str, c2: str, a: float) -> str:
    """Color c1 con opacidad a sobre c2 (Tk no tiene transparencia: se mezcla a mano)."""
    p = [int(c1[i:i + 2], 16) for i in (1, 3, 5)]
    q = [int(c2[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(int(round(y + (x - y) * a)) for x, y in zip(p, q))


def _smooth_kref_curve(tracker, day: str, x0: float, x1: float, step: int = 60) -> List[tuple]:
    """
    Genera curva suave de kref (referencia aprendida) usando interpolación Catmull-Rom.
    Evita los escalones cada 15 min de la interpolación lineal simple.
    """
    # Rango de franjas que cubren [x0, x1]
    i0 = int(x0 // SLOT_S) - 1
    i1 = int(x1 // SLOT_S) + 2

    # Puntos de control: el MISMO kref que usa el clasificador (tracker.kref: franja suavizada con las vecinas
    # x k_sistema, con sus limites), en el centro de cada franja de 15 min. Sin franjas aprendidas devuelve SEED_K,
    # asi la curva "esperado" tambien se dibuja en una instalacion nueva.
    ctrl_pts = []  # [(segundo, kref)]
    for i in range(i0, i1 + 1):
        mid = i * SLOT_S + SLOT_S / 2.0
        ctrl_pts.append((mid, tracker.kref(mid)))

    if len(ctrl_pts) < 4:
        return []  # No hay suficientes puntos para spline

    # Interpolación Catmull-Rom (suave, pasa por todos los puntos de control)
    def catmull_rom(p0, p1, p2, p3, t):
        # p0, p1, p2, p3 son valores kref; t ∈ [0,1] entre p1 y p2
        return 0.5 * (
            (2 * p1) +
            (-p0 + p2) * t +
            (2*p0 - 5*p1 + 4*p2 - p3) * t**2 +
            (-p0 + 3*p1 - 3*p2 + p3) * t**3
        )

    # Generar puntos cada `step` segundos
    out = []
    s = int(x0 // step * step)
    while s <= x1 + step:
        # Encontrar segmento entre puntos de control
        for j in range(len(ctrl_pts) - 3):
            x1_ctrl, y1_ctrl = ctrl_pts[j + 1]
            x2_ctrl, y2_ctrl = ctrl_pts[j + 2]
            if x1_ctrl <= s <= x2_ctrl:
                t = (s - x1_ctrl) / (x2_ctrl - x1_ctrl) if x2_ctrl != x1_ctrl else 0
                k_smooth = catmull_rom(
                    ctrl_pts[j][1], ctrl_pts[j + 1][1],
                    ctrl_pts[j + 2][1], ctrl_pts[j + 3][1],
                    t
                )
                # Clamp al rango válido
                k_smooth = max(K_FLOOR, min(K_MAX, k_smooth))
                out.append((s, k_smooth))
                break
        else:
            # Fuera de rango: usar valor más cercano
            if s < ctrl_pts[1][0]:
                out.append((s, ctrl_pts[1][1]))
            elif s > ctrl_pts[-2][0]:
                out.append((s, ctrl_pts[-2][1]))
        s += step

    return out


def _sk(i) -> str:
    k = STATE_KEYS[int(i)]
    return ALIAS.get(k, k)


def _bat_state(p):
    """cargando / descargando / equilibrada / completa a partir de un punto guardado (None si no hay datos)."""
    n = p[10] if len(p) > 10 else None
    if len(p) > 7 and p[7] >= FULL_SOC and (n is None or -BAT_NEUTRAL_W < n <= BAT_NEUTRAL_W):
        return "full"
    if n is None:
        return None
    return "chg" if n > BAT_NEUTRAL_W else ("dis" if n < -BAT_NEUTRAL_W else "bal")


def state_label(key: str, month: int) -> str:
    return STATES[ALIAS.get(key, key)][0]


# ---------------------------------------------------------------- fisica
def _solar_pos(dtl: datetime.datetime):
    """(altura, azimut desde el norte) del sol en grados."""
    n = dtl.timetuple().tm_yday
    decl = math.radians(23.45 * math.sin(math.radians(360.0 * (284 + n) / 365.0)))
    bb = math.radians(360.0 * (n - 81) / 364.0)
    eot = 9.87 * math.sin(2 * bb) - 7.53 * math.cos(bb) - 1.5 * math.sin(bb)
    off = (dtl.utcoffset() or datetime.timedelta(0)).total_seconds() / 3600.0
    hrs = dtl.hour + dtl.minute / 60.0 + dtl.second / 3600.0
    solar = hrs + (4.0 * (LON_DEG - 15.0 * off) + eot) / 60.0
    ha = math.radians(15.0 * (solar - 12.0))
    lat = math.radians(LAT_DEG)
    se = math.sin(lat) * math.sin(decl) + math.cos(lat) * math.cos(decl) * math.cos(ha)
    el = math.asin(max(-1.0, min(1.0, se)))
    if math.cos(el) > 1e-6:
        ca = (math.sin(decl) - se * math.sin(lat)) / (math.cos(el) * math.cos(lat))
        az = math.degrees(math.acos(max(-1.0, min(1.0, ca))))
        if ha > 0:
            az = 360.0 - az
    else:
        az = 180.0
    return math.degrees(el), az


def _poa(elev: float, az: float, month: int = 1, wx: Optional[Dict] = None) -> float:
    """Irradiancia (W/m2) sobre el plano del panel (inclinacion/orientacion) con cielo despejado.
    Si wx tiene cloud_cover, ajusta diffuse_frac dinámicamente."""
    s = math.sin(math.radians(elev))
    ghi = 1098.0 * s * math.exp(-0.057 / s) * MONTH_CLEARNESS[month - 1]   # Haurwitz (horizontal)
    diffuse_frac = DIFFUSE_FRAC
    if wx and "cloud_cover" in wx:
        # cloud_cover 0-100% -> diffuse_frac 0.10 a 0.35
        diffuse_frac = 0.10 + 0.25 * (wx["cloud_cover"] / 100.0)
    dhi = diffuse_frac * ghi
    dni = min(1100.0, (ghi - dhi) / s)
    b = math.radians(PANEL_TILT_DEG)
    cos_t = s * math.cos(b) + math.cos(math.radians(elev)) * math.sin(b) * math.cos(math.radians(az - PANEL_AZIMUTH_DEG))
    iam = 1.0 if cos_t <= 0.05 else max(0.0, 1.0 - IAM_B0 * (1.0 / cos_t - 1.0))
    return dni * max(0.0, cos_t) * iam + dhi * (1.0 + math.cos(b)) / 2.0 + ghi * ALBEDO * (1.0 - math.cos(b)) / 2.0


def _twilight_w(elev: float) -> float:
    """W esperados con el sol al ras del horizonte (luz difusa del crepusculo).
    Transición exponencial suave en lugar de lineal: más realista al amanecer/atardecer.
    -6° a 0°: curva exponencial que empieza suave y acelera cerca del horizonte."""
    if elev >= 0.0:
        return TW_W
    if elev <= -6.0:
        return 0.0
    # Exponencial: en -6° -> ~0, en -3° -> ~0.4*TW_W, en 0° -> TW_W
    # t = (elev + 6) / 6  va de 0 a 1
    t = (elev + 6.0) / 6.0
    # Curva exponencial suave: t^1.5 da inicio más lento, final más pronunciado
    return TW_W * (t ** 1.5)


def _model(elev: float, az: float, month: int, i_lim: float, wx: Optional[Dict] = None):
    """(W esperados A LA ENTRADA DEL EQUIPO con cielo despejado, resistencia del cable caliente en ohm).
    Celda caliente -> baja Vmp -> sube la corriente; cable al sol -> sube su resistencia: pierde I^2*R.
    Si wx tiene temp, usa esa temperatura ambiente en vez de la tabla mensual."""
    tamb = wx.get("temp") if wx and "temp" in wx else TAMB_C[month - 1]
    if elev <= 1.0:
        return _twilight_w(elev), CABLE_R20_OHM
    g = _poa(elev, az, month, wx)
    # Correccion de forma, continua y sin quiebros (sustituye a la rampa lineal + campana a trozos):
    # 1) Rampa matutina (sol del este vs panel a 190 deg): 0.60 -> 1.00 con pendiente nula al final (smoothstep)
    if az < 150.0:
        # igual que antes (lineal) hasta 15 deg; de 15 a 30 deg enlaza con Hermite (pendiente continua) hasta 1.0
        if elev <= 15.0:
            f = elev / 25.0
        elif elev < 30.0:
            t = (elev - 15.0) / 15.0
            f = 0.6 * (2*t**3 - 3*t**2 + 1) + 0.6 * (t**3 - 2*t**2 + t) + (-2*t**3 + 3*t**2)
        else:
            f = 1.0
        g *= 0.60 + 0.40 * f
    # 2) Valle suave de hasta -5 % entre ~14.5 y ~28 deg (aprox. 8:20-9:20 en octubre), nulo en los extremos
    if 14.5 < elev < 28.0:
        g *= 1.0 - 0.05 * math.sin(math.pi * (elev - 14.5) / 13.5) ** 2
    # 3) Campana global centrada en 58 deg (+5 % hacia el mediodia), para todas las elevaciones
    g *= 1.0 + 0.05 * math.exp(-((elev - 58.0) / 12.0) ** 2)
    tcell = tamb + 2.0 + CELL_RISE_K * g / 800.0
    p = PANEL_WP * g / 1000.0 * (1.0 + PANEL_TEMP_COEF * (tcell - 25.0))
    vmp = PANEL_VMP * (1.0 + VMP_TEMP_COEF * (tcell - 25.0)) * (1.0 + 0.04 * math.log(max(g, 50.0) / 1000.0))
    r = CABLE_R20_OHM * (1.0 + CABLE_TEMP_COEF * (tamb + 8.0 + 22.0 * g / 1000.0 - 20.0))
    i = p / vmp
    return max(_twilight_w(elev), max(0.0, min(p - i * i * r, i_lim * vmp * 0.95))), r


def _expected_w(elev: float, az: float, month: int, i_lim: float, wx: Optional[Dict] = None) -> float:
    return _model(elev, az, month, i_lim, wx)[0]


def _wx_atten(cache: Dict, dtl: datetime.datetime) -> float:
    """Factor (0-1) de atenuacion por nubes / lluvia del pronostico, interpolado entre horas. SOLO para dibujar la curva
    "esperado segun clima": el modelo (_poa) con cloud_cover solo cambia la fraccion difusa, no reduce la irradiancia total,
    y el clasificador usa ese modelo, asi que no se toca para no alterar el aprendizaje."""
    def f(d):
        wx = cache.get(d.strftime("%Y-%m-%d_%H"))
        if not wx:
            return None
        c = max(0.0, min(1.0, float(wx.get("cloud_cover", 0) or 0) / 100.0))
        pop = max(0.0, min(1.0, float(wx.get("precip_prob", 0) or 0) / 100.0))
        return (1.0 - WX_CLOUD_ATTEN * c ** WX_CLOUD_EXP) * (1.0 - WX_PRECIP_ATTEN * pop)
    f0 = f(dtl)
    if f0 is None:
        return 1.0
    f1 = f(dtl.replace(minute=0, second=0, microsecond=0) + datetime.timedelta(hours=1))
    if f1 is None:
        return f0
    return f0 + (f1 - f0) * (dtl.minute * 60 + dtl.second) / 3600.0


def _sun_times(day: str):
    """(salida, puesta) del sol en segundos del dia local para la ubicacion exacta (altura -0.833 grados)."""
    try:
        d0 = datetime.datetime.strptime(day, "%Y-%m-%d")
    except Exception:
        return None
    rise = sset = None
    prev = None
    for m in range(0, 1441, 2):
        el = _solar_pos((d0 + datetime.timedelta(minutes=m)).astimezone())[0]
        if prev is not None:
            if prev < SUN_H0 <= el and rise is None:
                rise = ((m - 2) + 2.0 * (SUN_H0 - prev) / (el - prev)) * 60.0
            elif prev >= SUN_H0 > el:
                sset = ((m - 2) + 2.0 * (prev - SUN_H0) / (prev - el)) * 60.0
        prev = el
    return (rise, sset) if rise is not None and sset is not None else None


def _sky_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "BluettiElite100Widget")
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception:
        pass
    return os.path.join(folder, "sky_log.json")


# =============================================================================
# CARGA INTELIGENTE
# Si hay carga solar (DC) y de red (AC) a la vez, limita la corriente AC (modo Personalizado) para que la potencia
# que RECIBE la bateria (DC + AC - consumo directo del inversor/UPS) no pase del limite configurado.
# La decision se toma con cada telemetria; los comandos los ejecuta la app (_smart_exec).
# =============================================================================
SC_RANGES = {"limit_w": (100.0, 1200.0), "decide_s": (3.0, 300.0), "hyst_w": (0.0, 300.0), "notice_secs": (5.0, 3600.0)}
_SC_RANGES = SC_RANGES
SC_DEFAULTS = {"enabled": False, "limit_w": 650, "decide_s": 20, "hyst_w": 40, "notice_permanent": True, "notice_secs": 60}
SC_DC_MIN_W = 30.0       # entrada DC minima para considerar que hay carga solar
SC_AC_MIN_W = 30.0       # entrada AC minima para considerar que hay carga de red
SC_ACT_HOLD_S = 3.0      # la coincidencia DC + AC debe mantenerse este tiempo para activarse
SC_WRITE_GAP_S = 8.0     # intervalo minimo entre dos escrituras al equipo
SC_VERIFY_S = 25.0       # tras escribir, este tiempo antes de comparar con lo que lee el equipo (deteccion de cambio manual)
SC_END_MIN_S = 10.0      # tiempo minimo sin coincidencia antes de devolver el modo anterior


def _smart_cfg_path() -> str:
    return os.path.join(os.path.dirname(_sky_path()), "smart_charge.json")


class SmartCharge:
    """update(t, now, conectado) -> [('mode', m) | ('amps', n)]. No toca el equipo: solo decide."""

    RANGES = SC_RANGES

    def __init__(self, min_a: int = 1, max_a: int = 15):
        self.min_a, self.max_a = int(min_a), int(max_a)
        self.cfg: Dict[str, Any] = dict(SC_DEFAULTS)
        self._load()
        self.active = False
        self.overridden = False        # el usuario (o una proteccion) cambio el modo/limite: no se vuelve a tocar en esta carga
        self.prev: Optional[Dict[str, Any]] = None   # {"mode", "amps"} de antes de limitar (solo si se llego a tocar)
        self.applied: Optional[int] = None
        self.paused = False            # la proteccion de carga del equipo manda
        self._coinc_since: Optional[float] = None
        self._gone_since: Optional[float] = None
        self._ac_gone_since: Optional[float] = None
        self._cand_dir = 0
        self._cand_since = 0.0
        self._last_write = 0.0
        self._immediate = False
        self._notice_ts = 0.0
        self._dismissed = False
        self._last: Dict[str, float] = {}

    # ---- persistencia / ajustes
    def _load(self):
        try:
            with open(_smart_cfg_path(), "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                for k, dv in SC_DEFAULTS.items():
                    if k in d and isinstance(d[k], type(dv)):
                        self.cfg[k] = d[k]
                for k, (lo, hi) in SC_RANGES.items():
                    self.cfg[k] = max(lo, min(hi, self.cfg[k]))
        except Exception:
            pass

    def _save(self):
        try:
            tmp = _smart_cfg_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.cfg, f, separators=(",", ":"))
            os.replace(tmp, _smart_cfg_path())
        except Exception:
            pass

    def apply_settings(self, values: Dict[str, Any]):
        """Valida y guarda. Devuelve (ok, mensaje_de_error)."""
        labels = {"limit_w": "Potencia máxima de entrada", "decide_s": "Tiempo para decidir un cambio",
                  "hyst_w": "Histéresis", "notice_secs": "Duración del aviso"}
        new = dict(self.cfg)
        for k, (lo, hi) in SC_RANGES.items():
            if k not in values:
                continue
            try:
                v = float(str(values[k]).strip().replace(",", "."))
            except ValueError:
                return False, f"'{labels[k]}': escribe un número."
            if not lo <= v <= hi:
                return False, f"'{labels[k]}' debe estar entre {int(lo)} y {int(hi)}."
            new[k] = int(round(v))
        if "notice_permanent" in values:
            new["notice_permanent"] = bool(values["notice_permanent"])
        self.cfg = new
        self._save()
        return True, ""

    def set_enabled(self, on: bool):
        """Activa/desactiva la funcion. Al apagarla estando activa devuelve el modo de carga anterior."""
        self.cfg["enabled"] = bool(on)
        self._save()
        return [] if on else self._finish()

    # ---- avisos
    def notice(self, now: float):
        if not (self.cfg["enabled"] and self.active) or self._dismissed:
            return None
        if not self.cfg["notice_permanent"] and now - self._notice_ts > self.cfg["notice_secs"]:
            return None
        return ("Carga inteligente Activa", "#60a5fa", "smart")

    def dismiss_notice(self):
        self._dismissed = True

    def status_text(self) -> str:
        if not self.cfg["enabled"]:
            return "desactivada."
        if self.overridden:
            return "en pausa: se cambió el modo/límite de carga a mano (se reanuda en la próxima carga AC)."
        if self.paused:
            return "en pausa: la protección de carga del equipo tiene el control."
        if not self.active:
            return "esperando a que coincidan la carga solar y la red AC."
        if self.applied is None:
            lv = self._last
            return f"activa, vigilando (batería recibe {lv.get('net', 0):.0f} W)."
        lv = self._last
        return f"activa: AC limitada a {self.applied} A (batería recibe {lv.get('net', 0):.0f} W)."

    # ---- control
    def _finish(self):
        """Fin de la limitacion: devuelve el modo (y limite) de antes, solo si se llego a tocar."""
        cmds = []
        prev = self.prev
        if prev:
            a = prev.get("amps")
            if isinstance(a, (int, float)) and a > 0 and int(a) != self.applied:
                cmds.append(("amps", int(a)))   # tambien se devuelve el limite personalizado que tenia el usuario
            if prev.get("mode") and prev["mode"] != "custom":
                cmds.append(("mode", prev["mode"]))
        self.active = False
        self.prev = None
        self.applied = None
        self._cand_dir = 0
        self._coinc_since = self._gone_since = None
        return cmds

    def restore_on_exit(self):
        return self._finish() if self.active else []

    def update(self, t: Dict[str, Any], now: float, is_conn: bool):
        if not self.cfg["enabled"] or not is_conn:
            return []
        dc = float(t.get("dc_input_watts") or 0.0)
        ac = float(t.get("ac_input_watts") or 0.0)
        loads = float(t.get("ac_output_watts") or 0.0) + float(t.get("dc_output_watts") or 0.0)
        volts = float(t.get("ac_input_volts") or 0.0)
        wpa = volts if volts >= 80.0 else 110.0              # W por amperio de la entrada AC
        net = dc + ac - loads                                # potencia que recibe la bateria
        self._last = {"net": net, "dc": dc, "ac": ac}
        lim, hyst, dec = float(self.cfg["limit_w"]), float(self.cfg["hyst_w"]), float(self.cfg["decide_s"])
        coinc = dc >= SC_DC_MIN_W and ac >= SC_AC_MIN_W

        # fin de la carga AC: se olvida un cambio manual anterior
        if ac < SC_AC_MIN_W:
            if self._ac_gone_since is None:
                self._ac_gone_since = now
            elif now - self._ac_gone_since >= SC_END_MIN_S:
                self.overridden = False
        else:
            self._ac_gone_since = None

        # la proteccion de carga del equipo (temperatura / bateria llena) tiene prioridad
        if t.get("chg_lim_a") is not None:
            self.paused = True
            self._cand_dir = 0
            return []
        if self.paused:                       # la proteccion termino: se vuelve a evaluar desde cero
            self.paused = False
            self.active, self.prev, self.applied = False, None, None
            self._coinc_since = None

        if not self.active:
            if self.overridden or not coinc:
                self._coinc_since = None
                return []
            if self._coinc_since is None:
                self._coinc_since = now
                return []
            if now - self._coinc_since < SC_ACT_HOLD_S:
                return []
            self.active, self._immediate = True, True
            self._notice_ts, self._dismissed = now, False
            self.prev, self.applied, self._cand_dir = None, None, 0
            self._gone_since = None

        # ---- activa
        if not coinc:
            if self._gone_since is None:
                self._gone_since = now
            if now - self._gone_since >= max(SC_END_MIN_S, min(dec, 60.0)):
                return self._finish()
            return []
        self._gone_since = None

        # cambio manual (o de otra proteccion) mientras limitamos: se respeta y no se vuelve a tocar
        if self.prev is not None and now - self._last_write > SC_VERIFY_S:
            mode, gm = t.get("charge_mode"), t.get("grid_max_current")
            if mode != "custom" or (gm is not None and self.applied is not None and int(gm) != self.applied):
                self.active, self.overridden, self.prev, self.applied = False, True, None, None
                return []

        target = int(max(self.min_a, min(self.max_a, math.floor((lim + loads - dc) / wpa))))
        cur = self.applied
        if cur is None:
            go = net > lim + hyst
            dirn = -1 if go else 0
        else:
            if target < cur and net > lim + hyst:
                dirn = -1
            elif target > cur and net < lim - hyst:
                dirn = 1
            else:
                dirn = 0
        immediate, self._immediate = self._immediate, False
        if dirn == 0:
            self._cand_dir = 0
            return []
        if dirn != self._cand_dir:
            self._cand_dir, self._cand_since = dirn, now
        if not (immediate and cur is None) and now - self._cand_since < dec:
            return []
        if now - self._last_write < SC_WRITE_GAP_S:
            return []
        if cur is not None and target == cur:
            self._cand_dir = 0
            return []
        cmds = []
        if self.prev is None:
            self.prev = {"mode": t.get("charge_mode"), "amps": t.get("grid_max_current")}
        if t.get("charge_mode") != "custom":
            cmds.append(("mode", "custom"))
        cmds.append(("amps", target))
        self.applied, self._last_write, self._cand_dir = target, now, 0
        return cmds



# ---------------------------------------------------------------- tracker
class SkyTracker:
    """Se alimenta con feed(telemetria, conectado) en cada actualizacion. No toca el equipo."""

    def __init__(self):
        self.data: Dict[str, Any] = {"k": SEED_K, "k_sys": 1.0, "k_sys_days": [], "days": {}, "slots": {}, "decay_day": None, "daily": {}, "model": MODEL_VER}
        self._load()
        self.state = "na"
        self.reason = "esperando datos"
        self.watts = 0.0
        self.exp_w = 0.0
        self.elev = 0.0
        self.pct: Optional[int] = None
        self.k_eff = SEED_K
        self.day: Optional[str] = None
        self.rec: Optional[Dict[str, Any]] = None
        self._last_ts: Optional[float] = None
        self._raw_s: Optional[float] = None
        self._clk = 0.0
        self._warm = 0.0
        self._full_ts = 0.0
        self._first_roll = True
        self._since_rise = None   # s desde la salida del sol (para la neblina matinal)
        self._last_logged = None
        self._r_hold: Optional[float] = None   # ultimo rendimiento valido (para estimar con la bateria llena)
        self._bs = BIN_S
        self._sun_cache: Dict[str, Any] = {}
        self._clear_done = None   # dia cuya curva "esperado despejado" ya se reviso/fijo (ver feed)
        self._bin: Optional[Dict[str, Any]] = None
        self._dirty = False
        self._last_save = time.time()
        self._occ_need = OCC_AFTER_DIP_S
        self._mbs = collections.deque(maxlen=SUSTAIN_MIN)   # medias de 60 s para aprender la referencia
        self._mb_s = 0.0
        self._mb_t = 0.0
        self._subtype = ""
        self._weather_provider = WeatherManager(on_fetch_success=lambda: self._update_daily_curve_wx(self.day))
        self._reset_runs()

    # ---- persistencia
    def _load(self):
        try:
            with open(_sky_path(), "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get("days"), dict):
                self.data = {"k": SEED_K, "k_sys": d.get("k_sys", 1.0), "k_sys_days": d.get("k_sys_days", []), "k_sys_last_day": d.get("k_sys_last_day"),
                             "days": d["days"], "decay_day": d.get("decay_day"), "model": MODEL_VER,
                             "slots": d["slots"] if (isinstance(d.get("slots"), dict) and d.get("model") == MODEL_VER) else {},
                             "last": d.get("last") if isinstance(d.get("last"), dict) else None,
                             "daily": d["daily"] if isinstance(d.get("daily"), dict) else {},
                             "pend": d["pend"] if (isinstance(d.get("pend"), dict) and d.get("model") == MODEL_VER) else {},
                             "down_day": d["down_day"] if isinstance(d.get("down_day"), dict) else {}}
        except Exception:
            pass

    def save(self):
        try:
            self._flush_bin()
            if self.day and self.rec is not None:
                self.data["daily"][self.day] = self._summ(self.day)
            tmp = _sky_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, separators=(",", ":"))
            os.replace(tmp, _sky_path())
            self._dirty = False
        except Exception:
            pass
        self._last_save = time.time()

    # ---- dias / calibracion
    def kref(self, sec: float) -> float:
        """Referencia despejada (real/modelo) de la franja de 15 min, suavizada con las vecinas (sin escalones).
        k_total = k_sistema × k_atmósfera_slot"""
        sl = self.data["slots"]
        i = int(sec // SLOT_S)
        own, lo, hi = sl.get(str(i)), sl.get(str(i - 1)), sl.get(str(i + 1))
        if own is not None:
            v = 0.5 * own + 0.25 * (lo if lo is not None else own) + 0.25 * (hi if hi is not None else own)
        else:
            nb = [x for x in (lo, hi) if x is not None]
            v = sum(nb) / len(nb) if nb else SEED_K
        k_atm = max(K_FLOOR, min(K_MAX, float(v)))
        k_sys = self.data.get("k_sys", 1.0)
        return max(K_FLOOR, min(K_MAX, k_sys * k_atm))

    def _learn(self, sec: float, cand: float):
        """Aprende la referencia despejada de la franja. cand = real/modelo (sin k_sistema): se normaliza
        dividiendo por k_sistema para que franja y k_sistema no cuenten la suciedad dos veces.
        Sube rapido con rendimientos estables mayores y baja despacio con estables algo menores (nunca con nubes)."""
        cand = cand / max(K_SYS_MIN, self.data.get("k_sys", 1.0))
        sl = self.data["slots"]
        key = str(int(sec // SLOT_S))
        old = sl.get(key)
        ref = old if old is not None else SEED_K
        if cand < ref * LEARN_MIN and not (old is None and cand >= SEED_K * 0.80):   # con nubes no se aprende
            return
        new = min(K_MAX, cand)
        if old is None and new < SEED_K:
            # un primer valor por debajo de la referencia inicial solo se acepta si se repite en LEARN_DAYS dias distintos
            pend = self.data.setdefault("pend", {}).setdefault(key, {})
            pend[self.day or ""] = round(new, 4)
            for dk in sorted(pend)[:-LEARN_DAYS]:
                pend.pop(dk, None)
            self._dirty = True
            if len(pend) < LEARN_DAYS:
                return
            new = max(K_FLOOR, sum(pend.values()) / len(pend))
        self.data.get("pend", {}).pop(key, None)
        if old is None:
            val = new
        elif new >= old:
            val = old + LEARN_UP_W * (new - old)
        else:
            # baja como mucho una vez al dia por franja (evita que una manana brumosa la hunda)
            last = self.data.setdefault("down_day", {})
            if last.get(key) == self.day:
                return
            last[key] = self.day
            val = old + LEARN_DOWN_W * (new - old)
        sl[key] = round(max(K_FLOOR, min(K_MAX, val)), 4)
        self.invalidate_curves()   # la curva "esperado" cacheada ya no vale
        self._dirty = True

    def invalidate_curves(self):
        """Descarta las curvas 'esperado' cacheadas (cambió la referencia aprendida o k_sistema)."""
        self.__dict__.pop("_smooth_kref_cache", None)

    def _elev_at(self, sec: float, day: Optional[str] = None) -> float:
        """Elevación del sol (grados) en un segundo del día indicado (por defecto, el actual)."""
        try:
            d0 = datetime.datetime.strptime(day or self.day, "%Y-%m-%d")
            dtl = (d0 + datetime.timedelta(seconds=sec)).astimezone()
            return _solar_pos(dtl)[0]
        except Exception:
            return 0.0

    def _k_sys_day_estimate(self, day: str) -> Optional[float]:
        """k_sistema que se deduce de ese día (None si no hay datos fiables).
        k_sistema = 1.0 significa 'panel limpio' (lo confirma el usuario con el botón de reset) y baja con la
        suciedad. Como los puntos guardados llevan p[2] = esperado × (k_sistema × k_atmósfera) vigente ese día, el
        cociente r = p[1]/p[2] vale (k_sistema real / k_sistema usado): el k_sistema medido es k_usado × r.
        Se miden SOLO tramos de sol alto, sin estimados (fuente DC / batería llena) y muy estables (sin nubes ni
        bordes de nube); el rendimiento NO se exige alto: un panel sucio también debe poder bajar su k_sistema."""
        rec = self.data["days"].get(day)
        if not rec or not rec.get("pts"):
            return None
        try:
            month = int(day[5:7])
        except Exception:
            return None
        if month in SAHARA_MONTHS:   # el polvo del Sahara se confundiría con suciedad del panel
            return None
        bs = int(rec.get("bin", BIN_S))
        good = []
        for p in rec["pts"]:
            if p[1] <= 0 or p[2] <= 0:
                continue
            if len(p) > 11 and p[11]:        # estimado (protección DC / batería llena): no es medición
                continue
            if len(p) > 7 and p[7] >= FULL_SOC:   # batería llena: el equipo limita lo que acepta
                continue
            r = p[1] / p[2]
            if not (0.6 <= r <= 1.15):       # fuera de rango: nubes, sombra u otra cosa, no suciedad
                continue
            if self._elev_at(p[0] + bs / 2.0, day) <= 40.0:   # sol alto = poca atmósfera
                continue
            good.append((p[0], r))
        # tramos consecutivos -> ventanas de 5 puntos con variación < 3 %
        meds = []
        run: List[tuple] = []
        for q in good + [None]:
            if q is not None and (not run or q[0] - run[-1][0] <= 1.5 * bs):
                run.append(q)
                continue
            for i in range(0, len(run) - 4, 5):
                vals = [v for _, v in run[i:i + 5]]
                m = sum(vals) / 5.0
                if m > 0 and math.sqrt(sum((x - m) ** 2 for x in vals) / 5.0) / m < 0.03:
                    meds.append(statistics.median(vals))
            run = [q] if q is not None else []
        if len(meds) < 2:                    # al menos 2 ventanas estables (≈10 min en total)
            return None
        return self.data.get("k_sys", 1.0) * statistics.median(meds)

    def _maybe_update_k_sys(self, day: str):
        """Al terminar un día: acerca k_sistema al valor medido ese día (suavizado exponencial ~30 días)."""
        last = self.data.get("k_sys_last_day")
        if last and day <= last:             # ya procesado (p. ej. tras reiniciar la app)
            return
        self.data["k_sys_last_day"] = day
        self._dirty = True
        est = self._k_sys_day_estimate(day)
        if est is None:
            return
        target = max(K_SYS_MIN, min(1.0, est))   # 1.0 = limpio: nunca se supera la referencia confirmada
        old = self.data.get("k_sys", 1.0)
        new_k_sys = round(0.967 * old + 0.033 * target, 4)
        if abs(new_k_sys - old) > 0.0002:
            self.data["k_sys"] = new_k_sys
            self.data.setdefault("k_sys_days", []).append({"day": day, "k": round(est, 4), "auto": True, "ts": time.time()})
            if len(self.data["k_sys_days"]) > 60:   # podar > 60 entradas
                self.data["k_sys_days"] = self.data["k_sys_days"][-60:]
            self.invalidate_curves()

    def reset_k_sys_manual(self):
        """Reset manual de k_sistema (usuario limpió el panel)."""
        self.data["k_sys"] = 1.0
        self.data.setdefault("k_sys_days", []).append({"day": self.day or datetime.date.today().isoformat(), "k": 1.0, "auto": False, "ts": time.time()})
        self.invalidate_curves()
        self._dirty = True

    def maybe_auto_reset_k_sys(self, wx: Optional[Dict]):
        """Auto-reset k_sistema si hay lluvia intensa confirmada (ESTRICTO):
        1. Tenemos datos del tiempo (wx no es None)
        2. Probabilidad lluvia >= 80%
        3. Temperatura pronosticada baja >= 2°C en la próxima hora
        4. Estado actual 'Lluvioso' (dense) sostenido > 30 min
        5. El reset anterior NO fue automático (evita bucles)
        """
        if not wx:
            return  # Sin datos del tiempo = no reseteamos
        if wx.get("precip_prob", 0) < 80:
            return
        if wx.get("temp_drop_1h", 0) < 2.0:
            return
        if self.state != "dense" or self._low_t < 1800:
            return
        # Evitar bucles: si el último reset ya fue auto, no repetir
        k_days = self.data.get("k_sys_days", [])
        if k_days and "prev_k" in k_days[-1]:   # el ultimo evento ya fue un auto-reset por lluvia
            return
        # Reset
        old_k = self.data.get("k_sys", 1.0)
        self.data["k_sys"] = 1.0
        self.data.setdefault("k_sys_days", []).append({
            "day": self.day or datetime.date.today().isoformat(),
            "k": 1.0,
            "auto": True,
            "ts": time.time(),
            "prev_k": round(old_k, 4),  # Guardamos valor anterior para deshacer
            "reason": f"Lluvia: precip={wx.get('precip_prob',0)}% temp_drop={wx.get('temp_drop_1h',0):.1f}°C"
        })
        self.invalidate_curves()
        self._dirty = True

    def _summ(self, day: str) -> Dict[str, Any]:
        """Resumen de un dia (se conserva siempre; los puntos detallados solo KEEP_DAYS dias)."""
        v = self.view(day)
        pts = v["pts"]
        bs = v["bin"]
        st: Dict[str, int] = {}
        for p in pts:
            kx = _sk(p[4])
            st[kx] = st.get(kx, 0) + max(1, bs // 60)
        k = bs / 3600.0
        # Incluir la curva de pronóstico final si existe guardada
        daily_entry = self.data.get("daily", {}).get(day, {})
        out = {"wh": round(v["wh_in"], 1), "whp": round(v["wh_panel"], 1),
                "pk": round(max([p[1] for p in pts] + [0.0]), 0),
                "act": round(sum(p[1] for p in pts) * k, 1), "pot": round(sum(p[2] for p in pts) * k, 1),
                "lw": round(sum(p[3] for p in pts) * k, 1),
                "ner": round(sum(max((p[6] if len(p) > 6 else p[1]) - p[1], 0.0) for p in pts) * k, 1),
                "full": int(sum(1 for p in pts if len(p) > 7 and p[7] >= FULL_SOC) * bs // 60),
                "ac": int(sum(1 for p in pts if len(p) > 8 and p[8] > AC_CHG_W) * bs // 60),
                "hi": int(sum(1 for p in pts if len(p) > 9 and p[9] > HIGH_A) * bs // 60), "st": st}
        if "curve_wx" in daily_entry:
            out["curve_wx"] = daily_entry["curve_wx"]
        if "curve_clear" in daily_entry:   # curva "esperado despejado" fijada al acabar las horas de sol
            out["curve_clear"] = daily_entry["curve_clear"]
        return out

    def summaries(self) -> Dict[str, Any]:
        out = dict(self.data["daily"])
        for dk in out:
            if dk in self.data["days"]:
                out[dk] = self._summ(dk)   # recalcula con los puntos guardados (sin restos del supuesto antiguo)
        if self.day and self.rec is not None:
            out[self.day] = self._summ(self.day)
        return out

    def _rollover(self, day: str):
        self._flush_bin()
        days = self.data["days"]
        daily = self.data["daily"]
        for dk in list(days):
            if dk != day and dk not in daily:
                daily[dk] = self._summ(dk)
        if len(daily) > 800:
            for dk in sorted(daily)[:len(daily) - 800]:
                daily.pop(dk, None)
        if self.data.get("decay_day") != day:
            sl = self.data["slots"]
            if self.data.get("decay_day") is not None:
                for kx in list(sl):
                    sl[kx] = round(sl[kx] * SLOT_DECAY, 4)
            self.data["decay_day"] = day
        for dk in sorted(days)[:-KEEP_DAYS + 1] if len(days) >= KEEP_DAYS else []:
            if dk != day:
                days.pop(dk, None)
        # Guardar curva_wx final del día que termina (antes de cambiar self.day)
        prev_day = sorted(days.keys())[-1] if days else None
        if prev_day and prev_day != day:
            # Asegurar que la curva_wx del día anterior quede guardada en daily
            self._update_daily_curve_wx(prev_day)
            self._maybe_update_k_sys(prev_day)
        self.day = day
        self.rec = days.setdefault(day, {"pts": [], "wh_in": 0.0, "wh_panel": 0.0, "rmax": 0.0, "bin": BIN_S})
        self.rec.setdefault("rmax", 0.0)
        if "bin" not in self.rec:
            self.rec["bin"] = 120   # dia guardado con la version anterior
        self._bs = int(self.rec["bin"])
        self._r_hold = None
        self._raw_s = None
        self._bin = None
        self.state = "na"
        self._warm = 0.0
        last = self.data.get("last")
        if (self._first_roll and isinstance(last, dict) and last.get("day") == day
                and last.get("state") in STATES and last["state"] not in ("na", "night", "lowsun")
                and time.time() - float(last.get("ts", 0.0)) <= LAST_VALID_S):
            self.state = last["state"]   # reabre la app poco despues: recupera el ultimo estado
        self._first_roll = False
        self._reset_runs()
        self._dirty = True

    def _reset_runs(self):
        self.low_start: Optional[float] = None
        self.last_low: Optional[float] = None
        self.run_sum = 0.0
        self.run_dt = 0.0
        self.high_s = 0.0
        self.win = collections.deque(maxlen=30)   # medias de 30 s (15 min)
        self._wacc_s = 0.0
        self._wacc_t = 0.0
        self._segs = collections.deque(maxlen=12)   # tramos alto/bajo completados: (tipo, duracion, fin)
        self._seg_kind: Optional[str] = None
        self._seg_t = 0.0
        self._pend_t = 0.0
        self._cand: Optional[str] = None
        self._cand_t = 0.0
        self._low_t = 0.0
        self._subtype = ""

    # ---- puntos guardados
    def _row(self, b) -> List[float]:
        t = b["t"]
        return [b["key"] * self._bs, round(b["w"] / t, 1), round(b["e"] / t, 1), round(b["l"] / t, 1),
                b["st"], round(b["wh"], 2), round(b["x"] / t, 1),
                round(b["s"] / b["sn"], 1) if b["sn"] > 0 else -1,
                round(b["a"] / t, 1), round(b["am"], 1), round(b["n"] / t, 1),
                1 if b.get("est_src") == "dc" else (2 if b.get("est_src") == "full" else 0),
                b.get("subtype", "")]

    def _flush_bin(self):
        b, self._bin = self._bin, None
        if b and b["t"] > 0 and self.rec is not None:
            self.rec["pts"].append(self._row(b))
            self._dirty = True

    def _bin_add(self, sec, w, e, l, dt, x, soc=None, acw=0.0, amp=0.0, net=0.0, est_src=None):
        key = int(sec // self._bs)
        if self._bin is not None and self._bin["key"] != key:
            self._flush_bin()
        if self._bin is None:
            self._bin = {"key": key, "w": 0.0, "e": 0.0, "l": 0.0, "x": 0.0, "t": 0.0, "st": 0, "wh": 0.0, "s": 0.0, "sn": 0.0, "a": 0.0, "am": 0.0, "n": 0.0, "est_src": None, "subtype": ""}
        b = self._bin
        b["w"] += w * dt
        b["e"] += e * dt
        b["l"] += l * dt
        b["x"] += x * dt
        b["a"] += acw * dt
        b["n"] += net * dt
        b["am"] = max(b["am"], amp)
        if soc is not None:
            b["s"] += soc * dt
            b["sn"] += dt
        b["t"] += dt
        b["st"] = STATE_KEYS.index(self.state)
        b["wh"] = self.rec["wh_in"]
        if est_src is not None:
            b["est_src"] = est_src
        if self._subtype:
            b["subtype"] = self._subtype

    # ---- alimentacion
    def feed(self, t: Dict[str, Any], ok: bool):
        now = time.time()
        dt = 0.0 if self._last_ts is None else now - self._last_ts
        self._last_ts = now
        if dt < 0 or dt > 5.0:
            dt = 0.0
        dtl = datetime.datetime.now().astimezone()
        day = dtl.strftime("%Y-%m-%d")
        if day != self.day:
            self._rollover(day)
        rec = self.rec
        sec = dtl.hour * 3600 + dtl.minute * 60 + dtl.second
        if self._clear_done != day:   # al acabar las horas de sol, la curva "esperado despejado" del dia queda fija y guardada
            try:
                if sec >= self._clear_range(day)[1]:
                    if self._clear_saved(day) is None:
                        self._freeze_curve_clear(day)
                    self._clear_done = day
            except Exception:
                pass
        elev, az = _solar_pos(dtl)
        wx = None
        if self._weather_provider:
            self._weather_provider.fetch()
            wx = self._weather_provider.get_hourly(dtl)
        exp_w, rcab = _model(elev, az, dtl.month, 20.0 if t.get("dc_high_current_on") else 15.0, wx)
        w = float(t.get("dc_input_watts") or 0.0)
        a = float(t.get("dc_input_current") or 0.0)
        loss = a * a * rcab if w > 0 else 0.0
        self.watts, self.exp_w, self.elev = w, exp_w, elev
        _sun = self.sun_times(day)
        self._since_rise = (sec - _sun[0]) if _sun else None
        if ok and dt > 0:
            rec["wh_in"] += w * dt / 3600.0
            rec["wh_panel"] += (w + loss) * dt / 3600.0
            self._dirty = True

        k = self.k_eff = self.kref(sec)
        soc = t.get("soc")
        bnet = (float(t.get("dc_input_watts") or 0.0) + float(t.get("ac_input_watts") or 0.0)
                - float(t.get("ac_output_watts") or 0.0) - float(t.get("dc_output_watts") or 0.0))
        full_now = isinstance(soc, (int, float)) and soc >= FULL_SOC and bnet <= BAT_NEUTRAL_W   # 100 % y ya no entra potencia a la bateria
        if full_now:
            self._full_ts = now
        est = w   # SUPOSICION con la bateria llena (el equipo no mide lo que podria dar el panel): se mantiene el ultimo rendimiento valido
        est_src = None
        low = exp_w < MIN_EXPECTED_W or elev < LOWSUN_ELEV
        if low and _sun and elev > SUN_H0 and sec - _sun[0] > LOW_MAX_S and _sun[1] - sec > LOW_MAX_S:
            low = False   # ya pasaron 25 min desde la salida y falta mucho para la puesta: se clasifica
        if low:
            self.state = "lowsun" if elev > SUN_H0 else "night"
            self._warm = 0.0
            self.reason = ""
            self.pct = None
            self._raw_s = None
            self._mbs.clear()
            self._mb_s = self._mb_t = 0.0
            self._reset_runs()
        else:
            reason = ""
            # wx ya se obtuvo al inicio de feed()
            if self._weather_provider:
                self.maybe_auto_reset_k_sys(wx)
            if not ok:
                reason = "sin conexión"
            elif t.get("prot_dc_active") or t.get("dc_source") == "other":
                reason = "fuente DC / protección"
                est_src = "dc"
            elif now - float(t.get("prot_dc_restored_ts") or 0.0) < 60.0:
                reason = "fuente DC / protección"
                est_src = "dc"
            elif (full_now or now - self._full_ts < FULL_HOLD_S) \
                    and w < HIGH_T * exp_w * k:
                reason = "batería llena"
                est_src = "full"
            self.reason = reason
            if reason:
                self._mbs.clear()
                self._mb_s = self._mb_t = 0.0
                # Estimación con kref de la franja actual × último rendimiento válido
                k_slot = self.kref(sec)
                r_hold = self._r_hold if self._r_hold is not None else 1.0
                est = max(w, exp_w * k_slot * min(r_hold, 1.2))
            else:
                self._clk += dt
                raw = w / max(exp_w, 5.0)
                if self._raw_s is None:
                    self._raw_s = raw
                elif dt > 0:
                    self._raw_s += (1.0 - math.exp(-dt / SMOOTH_TAU_S)) * (raw - self._raw_s)
                if elev > LEARN_ELEV_MIN:
                    # Aprende la referencia con rendimiento SOSTENIDO (media de SUSTAIN_MIN min), no con picos: los picos
                    # cortos por reflejo en bordes de nubes (>500 W) no deben inflarla.
                    self._mb_s += raw * dt
                    self._mb_t += dt
                    if self._mb_t >= 60.0:
                        self._mbs.append(self._mb_s / self._mb_t)
                        self._mb_s = self._mb_t = 0.0
                        if len(self._mbs) >= SUSTAIN_MIN:
                            _v = list(self._mbs)
                            _m = sum(_v) / len(_v)
                            if _m > 0 and math.sqrt(sum((x - _m) ** 2 for x in _v) / len(_v)) / _m < LEARN_CV:   # estable: sin nubes ni picos
                                self._learn(sec, _m)
                k = self.k_eff = self.kref(sec)
                r = self._raw_s / k
                self.pct = int(round(min(r, 1.5) * 100))
                self._r_hold = min(r, 1.2)
                if dt > 0:
                    self._classify(r, dt, wx)
        if self.state != "na":
            if self.state != self._last_logged:
                self._last_logged = self.state
                self._dirty = True
            self.data["last"] = {"day": day, "state": self.state, "ts": now}
        if ok and dt > 0 and (exp_w > 0 or w > 0):
            self._bin_add(sec, w, exp_w * k, loss, dt, est, float(soc) if isinstance(soc, (int, float)) else None,
                          float(t.get("ac_input_watts") or 0.0), a,
                          w + float(t.get("ac_input_watts") or 0.0) - float(t.get("ac_output_watts") or 0.0) - float(t.get("dc_output_watts") or 0.0),
                          est_src)
        if self._dirty and now - self._last_save >= 300.0:
            self.save()

    def _band_ok(self, lo: float, hi: float, cv: float = SAHARA_CV) -> bool:
        """Rendimiento ESTABLE (ultimos <=15 min, minimo 5) con media en [lo, hi)."""
        if len(self.win) < 10:
            return False
        v = list(self.win)
        m = sum(v) / len(v)
        if not (lo <= m < hi) or m <= 0:
            return False
        sd = math.sqrt(sum((x - m) ** 2 for x in v) / len(v))
        return sd / m < cv

    def _sahara_ok(self) -> bool:
        return self._band_ok(SAHARA_LOW, SUN_T)

    def _stratus_ok(self) -> bool:
        return self._band_ok(STRATUS_MIN, STRATUS_MAX, STRATUS_CV)

    def _haze_kind(self):
        """'neblina' / 'niebla' / None: velo matinal estable, solo hasta HAZE_MAX_S tras salir el sol."""
        sr = self._since_rise
        if sr is None or not (0.0 <= sr <= HAZE_MAX_S) or len(self.win) < 3:
            return None
        v = list(self.win)[-6:]
        m = sum(v) / len(v)
        if m <= 0:
            return None
        sd = math.sqrt(sum((x - m) ** 2 for x in v) / len(v))
        if sd / m >= HAZE_CV:
            return None
        if HAZE_LOW <= m < HAZE_HIGH:
            return "neblina"
        if STRATUS_MIN <= m < HAZE_LOW:
            return "niebla"
        return None

    def _interm_ok(self) -> bool:
        """Sube y baja repetidamente en intervalos de ~1-4 min: >=4 tramos seguidos recientes, todos de 45-260 s."""
        clk = self._clk
        recent = [sg for sg in self._segs if clk - sg[2] <= 900.0]
        if len(recent) < 4:
            return False
        last = recent[-4:]
        if any(not (INT_MIN_S <= d <= INT_MAX_S) for _, d, _ in last):
            return False
        if self._seg_t > INT_MAX_S:
            return False
        return sum(1 for kd, _, _ in last if kd == "low") >= 2

    def _detect_subtype(self, r: float, state: str, wx: Optional[Dict] = None) -> str:
        """Devuelve subtipo interno para cloud/dense/sahara. No cambia estado visible."""
        if state == "sahara":
            # Brumoso: distinguir polvo Sahariano vs velo por humedad
            # Usar AOD del wx si disponible, si no mes
            if wx and wx.get("aod_550", 0) > 0.2:
                return "haze_saharan"
            month = datetime.datetime.now().month
            if month in SAHARA_MONTHS:
                return "haze_saharan"
            return "haze_humid"
        if state == "dense":
            # Lluvioso: distinguir nimbostratus (estable) vs cumulonimbus (con picos de borde)
            if self._has_edge_spikes():
                return "cumulonimbus"
            return "stratus_low"
        # state == "cloud": Nublado
        # CV en ventana corta (2 min = 4 muestras) vs larga (15 min)
        cv_short = self._cv_window(4)   # últimos 2 min
        cv_long = self._cv_window(30)   # últimos 15 min
        elev = self.elev
        if cv_short > 0.25 and cv_long > 0.15:
            return "cumulus_fast"       # fluctuación rápida = cúmulos de paso
        if cv_long < 0.08 and elev > 30.0:
            return "altostratus"        # estable, sol alto = velo medio/alto
        return "stratus_low"            # default: estratos bajos / nimbostratus

    def _cv_window(self, n: int) -> float:
        """Coeficiente de variación de las últimas n muestras de self.win (30 s c/u)."""
        if len(self.win) < n:
            return 0.0
        v = list(self.win)[-n:]
        m = sum(v) / len(v)
        if m <= 0:
            return 0.0
        sd = math.sqrt(sum((x - m) ** 2 for x in v) / len(v))
        return sd / m

    def _has_edge_spikes(self) -> bool:
        """Detecta picos > 1.1×esperado tras bajada (cloud edge enhancement)."""
        if len(self.win) < 6:
            return False
        v = list(self.win)[-6:]
        # Buscar: bajada seguida de pico > 1.1
        for i in range(1, len(v)):
            if v[i-1] < 0.5 and v[i] > 1.1:
                return True
        return False

    def _classify(self, r: float, dt: float, wx: Optional[Dict] = None):
        """Cielo en tiempo real con 4 condiciones segun el rendimiento (potencia / esperada): Soleado, Nublado,
        Lluvioso (muy bajo), Brumoso (velo estable) + Neblina/Niebla matinal. Un cambio debe mantenerse DWELL_S."""
        self._wacc_s += r * dt
        self._wacc_t += dt
        if self._wacc_t >= 30.0:
            self.win.append(self._wacc_s / self._wacc_t)
            self._wacc_s = self._wacc_t = 0.0
        if self.state in ("na", "night", "lowsun"):
            self._warm += dt
            if self._warm < WARMUP_S:
                self.state = "na"
                return
        st = ALIAS.get(self.state, self.state)
        fresh = st in ("na", "night", "lowsun", "neblina", "niebla")
        hz = self._haze_kind()
        if hz:
            self.state = hz
            self._cand, self._cand_t = None, 0.0
            self._subtype = hz  # neblina/niebla son su propio subtipo
            return
        # Lluvioso solo tras DENSE_HOLD_S seguidos por debajo de DENSE_T; mientras tanto es Nublado
        self._low_t = self._low_t + dt if r < DENSE_T else 0.0
        if r < DENSE_T:
            cand = "dense" if self._low_t >= DENSE_HOLD_S else "cloud"
        elif r < HIGH_T or (st in ("cloud", "dense") and r < SUN_BACK):
            cand = "cloud"
        elif r >= SUN_T:
            cand = "sun"
        else:
            cand = "sahara"   # HIGH_T (o SUN_BACK) <= r < SUN_T
        # Si wx indica AOD alto (polvo), forzar sahara aunque r >= SUN_T
        if wx and wx.get("aod_550", 0) > 0.3 and r >= 0.85:
            cand = "sahara"
        need = SAHARA_HOLD_S if cand == "sahara" else (0.0 if cand == "dense" else DWELL_S)   # dense: ya espero DENSE_HOLD_S
        if fresh:
            st = cand
            self._cand, self._cand_t = None, 0.0
        elif cand == st:
            self._cand, self._cand_t = None, 0.0
        else:
            self._cand_t = self._cand_t + dt if cand == self._cand else dt
            self._cand = cand
            if self._cand_t >= need:
                st = cand
                self._cand, self._cand_t = None, 0.0
        self.state = st
        # Detectar subtipo solo para estados nublados/brumosos
        if st in ("cloud", "dense", "sahara"):
            self._subtype = self._detect_subtype(r, st, wx)
        else:
            self._subtype = ""

    # ---- consulta (UI)
    def row_parts(self):
        """(texto del estado, color, texto de produccion del dia) para la barra de la app."""
        label, perf, color, icon = STATES[self.state]
        label = state_label(self.state, datetime.datetime.now().month)
        if self.state == "na":
            txt = "Determinando..." + (f" ({self.reason})" if self.reason and self.reason != "esperando datos" else "")
        elif perf:
            txt = f"{label} con {perf}"
        else:
            txt = label + (f" · {int(self.watts)} W" if self.state == "lowsun" else "")
        # Badge opcional: subtipo de nube
        _badge = False
        try:   # la casilla de la ventana de clima guarda esta opcion en la config del proveedor, no en self.data
            _badge = bool(self._weather_provider.config.get("show_subtype_badge", False))
        except Exception:
            pass
        if self._subtype and _badge:
            sub_map = {
                "cumulus_fast": "Cúmulos rápidos",
                "stratus_low": "Estratos bajos",
                "altostratus": "Altostratus",
                "cumulonimbus": "Tormenta",
                "haze_saharan": "Polvo Sahariano",
                "haze_humid": "Velo húmedo",
                "neblina": "Neblina",
                "niebla": "Niebla",
            }
            badge = sub_map.get(self._subtype, self._subtype)
            txt += f" · {badge}"
        if self.reason and self.state != "na":
            txt += f" (en pausa: {self.reason})"
        wh = self.rec["wh_in"] if self.rec else 0.0
        return txt, color, f"Producción: {wh:.0f} Wh (hoy)"

    def dates(self) -> List[str]:
        # los dias sin ningun dato (p. ej. el dia en que se instalo la funcion y no se uso) no se ofrecen
        return sorted([d for d, r in self.data["days"].items()
                       if d == self.day or r.get("pts") or r.get("wh_in", 0) > 0], reverse=True)

    @staticmethod
    def _clean(p):
        """El "supuesto con bateria llena" solo vale con la bateria al 100 %: se descartan los restos guardados
        por versiones anteriores, que lo calculaban desde el 99 % sobre mediciones aun validas."""
        if len(p) > 7 and p[6] > p[1] and 0 <= p[7] < FULL_SOC and (len(p) <= 11 or not p[11]):   # los estimados por proteccion DC / bateria llena (p[11]) se conservan
            q = list(p)
            q[6] = q[1]
            return q
        return p

    def view(self, day: str) -> Dict[str, Any]:
        rec = self.data["days"].get(day)
        if rec is None:
            return {"pts": [], "wh_in": 0.0, "wh_panel": 0.0, "bin": BIN_S}
        pts = list(rec["pts"])
        if day == self.day and self._bin and self._bin["t"] > 0:
            pts.append(self._row(self._bin))
        pts = [self._clean(p) for p in pts]
        return {"pts": pts, "wh_in": rec["wh_in"], "wh_panel": rec["wh_panel"], "bin": int(rec.get("bin", 120))}

    def sun_times(self, day: str):
        if day not in self._sun_cache:
            self._sun_cache[day] = _sun_times(day)
        return self._sun_cache[day]

    def _clear_range(self, day: str):
        """Rango de la gráfica del día (amanecer-30 min .. atardecer+30 min), el mismo fx0/fx1 de la ventana."""
        sun = self.sun_times(day)
        return (max(0.0, sun[0] - 1800.0), min(86400.0, sun[1] + 1800.0)) if sun else (5 * 3600.0, 20 * 3600.0)

    def _calc_clear(self, day: str, d0, x0: float, x1: float, step: int):
        """W esperados despejado * referencia aprendida suave (con la referencia ACTUAL)."""
        out = []
        for s, k_smooth in _smooth_kref_curve(self, day, x0, x1, step):
            dtl = (d0 + datetime.timedelta(seconds=s)).astimezone()
            w_clear = _expected_w(*_solar_pos(dtl), d0.month, 15.0, None)
            out.append((s, w_clear * k_smooth))
        return out

    def _clear_saved(self, day: str):
        """Curva 'esperado despejado' ya fijada del día (daily[day].curve_clear) o None."""
        ent = self.data.get("daily", {}).get(day, {}).get("curve_clear")
        if not isinstance(ent, dict):
            return None
        try:
            s0, st = int(ent["x0"]), int(ent["step"])
            return [(s0 + i * st, w) for i, w in enumerate(ent["w"])]
        except Exception:
            return None

    def _freeze_curve_clear(self, day: str):
        """Calcula y guarda la curva 'esperado despejado' del día (paso 60 s, como la gráfica) y la devuelve."""
        try:
            d0 = datetime.datetime.strptime(day, "%Y-%m-%d")
        except Exception:
            return None
        daily = self.data.setdefault("daily", {})
        base = daily.get(day)
        if base is None:
            if day not in self.data.get("days", {}):
                return None   # sin datos de ese dia: nada que fijar
            base = self._summ(day)
        a, b = self._clear_range(day)
        pts = self._calc_clear(day, d0, a, b, 60)
        if not pts:
            return None
        daily[day] = {**base, "curve_clear": {"x0": int(pts[0][0]), "step": 60, "w": [round(w, 1) for _, w in pts]}}
        self._dirty = True
        return self._clear_saved(day)

    def expected_curve(self, day: str, x0: float = 5 * 3600, x1: float = 20 * 3600, step: int = 300):
        """[(segundo_del_dia, W esperados despejado * referencia aprendida SUAVE)].
        Curva base teórica (sin wx) con kref suavizado por spline Catmull-Rom.
        - Días pasados (y hoy una vez acabadas las horas de sol): curva fija guardada en daily[day].curve_clear.
          Un día pasado sin curva guardada se fija la primera vez que se pide (con la referencia actual).
        - Hoy, durante las horas de sol: se recalcula con la referencia que se va aprendiendo."""
        try:
            d0 = datetime.datetime.strptime(day, "%Y-%m-%d")
        except Exception:
            return []

        saved = self._clear_saved(day)
        if saved is None and day != datetime.date.today().isoformat():
            saved = self._freeze_curve_clear(day)
        if saved:
            return [(s, w) for s, w in saved if x0 <= s <= x1]

        # Cache de curva suave por día
        if not hasattr(self, "_smooth_kref_cache"):
            self._smooth_kref_cache = {}

        cache_key = (day, x0, x1, step)
        if cache_key not in self._smooth_kref_cache:
            # Convertir a curva completa: W_teórico * kref_suave
            out = self._calc_clear(day, d0, x0, x1, step)
            if len(self._smooth_kref_cache) > 300:   # el zoom/arrastre generan rangos nuevos: no crecer sin limite
                self._smooth_kref_cache.clear()
            self._smooth_kref_cache[cache_key] = out

        return self._smooth_kref_cache[cache_key]

    def expected_curve_wx(self, day: str, x0: float = 5 * 3600, x1: float = 20 * 3600, step: int = 300):
        """[(segundo_del_dia, W esperados CON pronóstico del tiempo)].
        - Para días pasados: devuelve la curva final guardada en daily[day].curve_wx
        - Para día actual: recalcula usando cache wx actual (se actualiza cada fetch de 15 min)"""
        try:
            d0 = datetime.datetime.strptime(day, "%Y-%m-%d")
        except Exception:
            return []

        today_iso = datetime.date.today().isoformat()

        # Para días pasados: devolver la curva guardada
        if day != today_iso:
            daily_entry = self.data.get("daily", {}).get(day, {})
            if "curve_wx" in daily_entry:
                # Filtrar por rango x0-x1
                return [(s, w) for s, w in daily_entry["curve_wx"] if x0 <= s <= x1]
            return []

        # Día actual: recalcular con cache wx actual
        if not self._weather_provider:
            return []

        wx_cache = getattr(self._weather_provider, "_mem_cache", None)
        if not wx_cache:
            try:
                wx_cache = self._weather_provider._providers[0]._load_cache()
            except Exception:
                wx_cache = None
        if not wx_cache:
            return []

        # la version de los datos del clima forma parte de la clave: cada descarga nueva (cada 15 min) recalcula la curva
        cache_key = ("wx", day, x0, x1, step, int(getattr(self._weather_provider, "_last_fetch", 0) or 0))
        if not hasattr(self, "_smooth_kref_cache"):
            self._smooth_kref_cache = {}

        if cache_key not in self._smooth_kref_cache:
            # Usar misma curva kref suave que expected_curve, pero con wx
            smooth_pts = _smooth_kref_curve(self, day, x0, x1, step)
            out = []
            for s, k_smooth in smooth_pts:
                dtl = (d0 + datetime.timedelta(seconds=s)).astimezone()
                key = dtl.strftime("%Y-%m-%d_%H")
                wx = wx_cache.get(key)
                elev_, az_ = _solar_pos(dtl)
                w_wx = max(_twilight_w(elev_), _expected_w(elev_, az_, d0.month, 15.0, wx) * _wx_atten(wx_cache, dtl))
                out.append((s, w_wx * k_smooth))
            if len(self._smooth_kref_cache) > 300:   # el zoom/arrastre generan rangos nuevos: no crecer sin limite
                self._smooth_kref_cache.clear()
            self._smooth_kref_cache[cache_key] = out

        return self._smooth_kref_cache[cache_key]

    def _update_daily_curve_wx(self, day: str, x0: float = 5 * 3600, x1: float = 20 * 3600, step: int = 300):
        """Actualiza la curva de pronóstico del día actual en data['daily'].
        Se llama tras cada fetch exitoso del clima para que la curva vaya mejorando."""
        today_iso = datetime.date.today().isoformat()
        if day != today_iso:
            return
        curve = self.expected_curve_wx(day, x0, x1, step)
        if curve:
            # Guardar en daily (se persiste con save())
            self.data.setdefault("daily", {})[day] = {**self.data.get("daily", {}).get(day, {}), "curve_wx": curve}
            self._dirty = True




# ---------------------------------------------------------------- estimados de huecos y extremos
# (los usan la ventana de la app y el servidor web: una sola logica para que las dos muestren lo mismo)
def _wx_cache_of(tr):
    """Cache horaria del proveedor del clima (memoria; si esta vacia, la de disco) o {}."""
    wp = getattr(tr, "_weather_provider", None)
    if not wp:
        return {}
    c = getattr(wp, "_mem_cache", None)
    if not c:
        try:
            c = wp._providers[0]._load_cache()
        except Exception:
            c = {}
    return c or {}


def wx_sky_key(wx):
    """Estado del cielo (soleado / nublado / lluvioso / brumoso) a partir de una hora del pronostico."""
    cloud = float(wx.get("cloud_cover", 0) or 0)
    rh = float(wx.get("rh", 0) or 0)
    if float(wx.get("precip_prob", 0) or 0) >= WX_PRECIP_DENSE or (cloud >= WX_CLOUD_DENSE and rh >= WX_RH_DENSE):
        return "dense"
    if cloud >= WX_CLOUD_CLOUDY:
        return "cloud"
    if float(wx.get("aod_550", 0) or 0) >= WX_AOD_HAZE:
        return "sahara"
    return "sun"


def gap_bat_key(a, b):
    """Estado de la bateria en un hueco, segun el % al inicio y al final: completa / cargando / descargando / equilibrada."""
    if a >= FULL_SOC and b >= FULL_SOC:
        return "full"
    return "chg" if b - a > 0.5 else ("dis" if b - a < -0.5 else "bal")


def adjacent_soc(tr, day, direction: int):
    """(segundo relativo a `day`, %) del ultimo dato del dia anterior (direction -1) o del primero del siguiente (+1),
    o None. Sirve para estimar la bateria entre el amanecer / atardecer y el primer / ultimo dato."""
    try:
        d2 = (datetime.date.fromisoformat(day) + datetime.timedelta(days=direction)).isoformat()
        v = tr.view(d2)
        q = [p for p in v["pts"] if len(p) > 7 and p[7] >= 0]
        if not q:
            return None
        pk = q[-1] if direction < 0 else q[0]
        return (pk[0] + v["bin"] / 2.0 + 86400.0 * direction, pk[7])
    except Exception:
        return None


def gap_soc_line(tr, day, g0, g1, pa, pb, bs):
    """Recta del % de bateria en un hueco: (t_a, %_a, t_b, %_b, desde, hasta) o None.
    Hueco normal: entre el dato anterior y el posterior. Extremo (pa o pb = None): hasta el dato del dia
    vecino si lo hay; si no, recta horizontal con el % del unico lado conocido."""
    if pa is not None and pb is not None:
        if len(pa) < 8 or len(pb) < 8 or pa[7] < 0 or pb[7] < 0:
            return None
        ta, tb = pa[0] + bs / 2.0, pb[0] + bs / 2.0
        return (ta, pa[7], tb, pb[7], ta, tb)
    pk = pb if pa is None else pa
    if len(pk) < 8 or pk[7] < 0:
        return None
    tk = pk[0] + bs / 2.0
    far = adjacent_soc(tr, day, -1 if pa is None else 1)
    if pa is None:
        ta, sa = far if far else (g0, pk[7])
        return (ta, sa, tk, pk[7], g0, tk)
    tb, sb = far if far else (g1, pk[7])
    return (tk, pk[7], tb, sb, tk, g1)


def lin_soc(ln, t):
    ta, sa, tb, sb = ln[:4]
    return max(0.0, min(100.0, sa if tb <= ta else sa + (sb - sa) * (t - ta) / (tb - ta)))


def gap_sky_runs(wxc, day, g0, g1, fallback_key, step=600.0):
    """Cielo estimado de un hueco: [[inicio, fin, clave]] segun el pronostico (o el ultimo estado conocido)."""
    d0g = datetime.datetime.strptime(day, "%Y-%m-%d")
    runs, t_ = [], g0
    while t_ < g1:
        t2 = min(g1, t_ + step)
        wx_ = None
        try:
            wx_ = wxc.get((d0g + datetime.timedelta(seconds=(t_ + t2) / 2.0)).astimezone().strftime("%Y-%m-%d_%H"))
        except Exception:
            pass
        k_ = wx_sky_key(wx_) if wx_ else fallback_key
        if runs and runs[-1][2] == k_:
            runs[-1][1] = t2
        else:
            runs.append([t_, t2, k_])
        t_ = t2
    return runs


def gap_estimates(tr, day, pts, bs, x0, x1, sun, fx0, fx1, curve_clear, curve_wx, step_c=60):
    """Huecos sin datos del dia (entre dos datos, y los extremos: del amanecer al primer dato y del ultimo al atardecer
    o a "ahora"). Devuelve [{s0, s1, pa, pb, pw, vis}]: pa / pb = puntos medidos antes / despues (None en un extremo),
    pw = [(segundo, W estimados)] y vis = el hueco toca el rango [x0, x1]."""
    out = []
    if not pts:
        return out
    clear_map = {sx: w for sx, w in curve_clear}
    wx_map = {sx: w for sx, w in curve_wx}

    def _wexp(sx, dflt):   # esperado (pronostico preferido) en el segundo sx
        k_ = round(sx / step_c) * step_c
        return wx_map.get(k_, clear_map.get(k_, dflt))

    def _add_gap(s_start, s_end, pa, pb):
        def _sc(pt, sx):
            we = _wexp(sx, 1.0)
            return max(0.2, min(2.0, pt[1] / we)) if we > 0 else 1.0
        sa = _sc(pa, s_start) if pa is not None else None
        sb = _sc(pb, s_end) if pb is not None else None
        if sa is None:
            sa = sb
        if sb is None:
            sb = sa
        pw = []
        s = s_start
        while s < s_end:
            if x0 <= s <= x1:
                t_ = (s - s_start) / max(1.0, s_end - s_start)
                pw.append((s, _wexp(s, 0.0) * (sa + (sb - sa) * t_)))
            s += step_c
        out.append({"s0": s_start, "s1": s_end, "pa": pa, "pb": pb, "pw": pw, "vis": s_end >= x0 and s_start <= x1})

    sorted_pts = sorted(pts, key=lambda p: p[0])
    lo_d, hi_d = (sun[0], sun[1]) if sun else (fx0, fx1)
    if day == datetime.date.today().isoformat():
        _nw = datetime.datetime.now()
        hi_d = min(hi_d, _nw.hour * 3600 + _nw.minute * 60 + _nw.second)
    if sorted_pts[0][0] - lo_d > 3 * bs:
        _add_gap(lo_d, sorted_pts[0][0], None, sorted_pts[0])
    for i in range(len(sorted_pts) - 1):
        p_curr, p_next = sorted_pts[i], sorted_pts[i + 1]
        if p_next[0] - (p_curr[0] + bs) > 3 * bs:   # mismo umbral que para romper segs
            _add_gap(p_curr[0] + bs, p_next[0], p_curr, p_next)
    if hi_d - (sorted_pts[-1][0] + bs) > 3 * bs:
        _add_gap(sorted_pts[-1][0] + bs, hi_d, sorted_pts[-1], None)
    return out


# ---------------------------------------------------------------- ventana
SKY_BG, SKY_CARD, SKY_GRID = "#080b11", "#0e141f", "#1e293b"
MODES = ("Día", "Semana", "Mes")
DOW = ["lun", "mar", "mié", "jue", "vie", "sáb", "dom"]
QUIET = ("na", "night", "lowsun")
HINT_DAY = "Rueda del ratón: zoom · arrastrar: mover · doble clic: restablecer · pasa el ratón sobre un punto para ver el detalle."
HINT_BARS = "Pasa el ratón o haz clic sobre una barra para ver el detalle del día."


def _dmy(iso: str) -> str:
    try:
        return datetime.datetime.strptime(iso, "%Y-%m-%d").strftime("%d/%m/%Y")
    except Exception:
        return iso


def _iso(dmy: str) -> str:
    try:
        return datetime.datetime.strptime(dmy, "%d/%m/%Y").strftime("%Y-%m-%d")
    except Exception:
        return dmy


def _fmt_e(wh: float) -> str:
    return f"{wh / 1000.0:.2f} kWh" if wh >= 1000 else f"{wh:.0f} Wh"


def _fmt_hm(sec: float) -> str:
    return f"{int(sec) // 3600:02d}:{(int(sec) % 3600) // 60:02d}"


def _merge(st: Dict[str, int]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for k, m in st.items():
        k = ALIAS.get(k, k)
        out[k] = out.get(k, 0) + m
    return out


def _dominant(st: Dict[str, int]):
    st = _merge(st)
    best = None
    for k, m in st.items():
        if k not in QUIET and (best is None or m > st[best]):
            best = k
    return best


def _states_text(mins: Dict[str, int], month: int) -> str:
    return " · ".join(f"{state_label(k, month)} {m // 60}h{m % 60:02d}m"
                      for k, m in sorted(_merge(mins).items(), key=lambda kv: -kv[1]) if k not in ("na", "night") and m > 0)


def _sky_win_path() -> str:   # [skywin-patch] tamano personalizado de la ventana de estadisticas
    return os.path.join(os.path.dirname(_sky_path()), "sky_window.json")


def _sky_win_load():
    try:
        with open(_sky_win_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        w, h = int(d["w"]), int(d["h"])
        return (w, h) if w >= 500 and h >= 300 else None
    except Exception:
        return None


def _sky_win_pos():   # [skywin-patch] ultima posicion en pantalla
    try:
        with open(_sky_win_path(), "r", encoding="utf-8") as f:
            d = json.load(f)
        return (int(d["x"]), int(d["y"])) if "x" in d and "y" in d else None
    except Exception:
        return None


def _sky_win_save(w: int, h: int, x=None, y=None):
    try:
        d = {"w": int(w), "h": int(h)}
        if x is not None and y is not None:
            d["x"], d["y"] = int(x), int(y)
        with open(_sky_win_path(), "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:
        pass


class _WxLocalCtl:
    """Controla el gestor de clima de ESTE equipo (servidor o equipo local)."""
    is_remote = False

    def __init__(self, tr):
        self.tr = tr
        self.wp = tr._weather_provider
        self._att0 = None

    def cfg(self):
        c = self.wp.config
        return {"enabled": bool(c["enabled"]), "provider": self.wp.preferred_name() or self.wp.current_provider_name(),
                "merge": self.wp.merge_enabled(), "interval": int(c["fetch_interval_min"]),
                "api_keys": dict(c["api_keys"])}

    def set_enabled(self, v):
        self.wp.set_enabled(bool(v))

    def set_provider(self, p):
        self.wp.set_provider_order([p] + [x for x in self.wp.config["providers_order"] if x != p])

    def set_merge(self, v):
        self.wp.set_merge_providers(bool(v))

    def set_interval(self, m):
        self.wp.set_fetch_interval(int(m))

    def set_keys(self, d):
        for k, v in d.items():
            self.wp.set_api_key(k, v)

    def refresh_begin(self):
        """'' si arranco la descarga; si no, el motivo."""
        self._att0 = self.wp._attempt_ts
        return "" if self.wp.fetch(force=True) else "Activa «Habilitar pronóstico meteorológico» primero"

    def refresh_poll(self):
        """(terminado, texto)"""
        return (self.wp._attempt_ts != self._att0, self.wp.status_text())


class _WxRemoteCtl:
    """Mando a distancia: controla el gestor de clima del SERVIDOR desde una copia cliente. Todo es asincrono (los
    mensajes van y vienen por la red); la ventana consulta los resultados con after()."""
    is_remote = True

    def __init__(self, remote):
        self.r = remote
        self._gq = self._rq = self._sq = None
        self._att0 = None

    def connected(self):
        return bool(self.r is not None and getattr(self.r, "ws_up", False))

    def role(self):
        return getattr(self.r, "role", None)

    def get_begin(self):
        self._gq = self.r.wx_cfg_send("get")
        return self._gq is not None

    def get_poll(self):
        return self.r.wx_cfg_result(self._gq) if self._gq is not None else None

    def _set(self, vals):
        self.r.wx_cfg_send("set", vals)

    def set_enabled(self, v):
        self._set({"enabled": bool(v)})

    def set_provider(self, p):
        self._set({"provider": p})

    def set_merge(self, v):
        self._set({"merge": bool(v)})

    def set_interval(self, m):
        self._set({"interval": int(m)})

    def set_keys(self, d):
        self._set({"api_keys": dict(d)})

    def refresh_begin(self):
        self._att0 = None
        self._sq = None
        self._rq = self.r.wx_cfg_send("refresh")
        return "" if self._rq is not None else "Sin conexión con el servidor"

    def refresh_poll(self):
        if self._att0 is None:   # esperando que el servidor confirme que arranco la descarga
            res = self.r.wx_cfg_result(self._rq)
            if res is None:
                return (False, "")
            if not res.get("ok"):
                return (True, res.get("error") or "Error del servidor")
            if not res.get("started"):
                return (True, "El pronóstico del servidor está deshabilitado")
            self._att0 = res.get("attempt")
            self._sq = self.r.wx_cfg_send("status")
            return (False, "")
        res = self.r.wx_cfg_result(self._sq) if self._sq is not None else None
        if res is None:
            return (False, "")
        if res.get("attempt") != self._att0:
            return (True, res.get("status") or "")
        self._sq = self.r.wx_cfg_send("status")
        return (False, "")


class SkyWindow(ctk.CTkToplevel):
    """Dia: curva del dia (zoom con la rueda hasta minuto a minuto). Semana / Mes: energia por dia + historial."""

    def __init__(self, app, tracker: SkyTracker, mode: str = "Día"):
        super().__init__(app)
        self.tr = tracker
        self.mode = mode if mode in MODES else "Día"
        self.day = tracker.day or (tracker.dates() or [datetime.date.today().isoformat()])[0]
        self._hx: Optional[int] = None
        self._geo: Optional[Dict[str, Any]] = None
        self._cfg_job = None
        self._xr: Optional[tuple] = None      # rango visible (s del dia) cuando hay zoom
        self._pan0 = None
        self._px = 0
        self.title("Condiciones y producción solar")
        self.configure(fg_color=SKY_CARD)
        self.transient(app)
        # Icono de la app
        try:
            import sys, os
            base_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            ico_path = os.path.join(base_dir, "bluetti.ico")
            self.iconbitmap(ico_path)
        except Exception:
            pass

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=16, pady=(12, 2))
        ctk.CTkLabel(head, text="☀ CONDICIONES Y ESTADÍSTICAS DEL PANEL", font=("Segoe UI", 12, "bold"),
                     text_color="#00f0ff").pack(side="left")
        self.seg = ctk.CTkSegmentedButton(head, values=list(MODES), command=self.set_mode, height=26)
        self.seg.set(self.mode)
        self.seg.pack(side="right")
        self.btn_exp = ctk.CTkButton(head, text="📷 Exportar", width=92, height=26, fg_color="#1e293b",
                                     hover_color="#334155", command=self._export)
        self.btn_exp.pack(side="right", padx=(0, 8))
        self.btn_wx = ctk.CTkButton(head, text="⚙️ Clima", width=80, height=26, fg_color="#1e293b",
                                    hover_color="#334155", command=self._open_wx_config)
        self.btn_wx.pack(side="right", padx=(0, 8))
        self.opt = ctk.CTkOptionMenu(head, values=[_dmy(d) for d in (tracker.dates() or [self.day])], width=120,
                                     height=26, command=self._on_day)
        self.opt.set(_dmy(self.day))
        if self.mode == "Día":
            self.opt.pack(side="right", padx=(0, 8))

        self.lbl_hover = ctk.CTkLabel(self, text="", font=("Consolas", 10, "bold"), text_color="#cbd5e1",
                                      anchor="w", justify="left")
        self.lbl_hover.pack(fill="x", padx=16)
        self.cv = tk.Canvas(self, bg=SKY_BG, highlightthickness=0, width=980, height=380)
        self.cv.pack(fill="both", expand=True, padx=12, pady=4)
        self.lbl_sum = ctk.CTkLabel(self, text="", font=("Consolas", 10), text_color="#cbd5e1",
                                    anchor="w", justify="left")
        self.lbl_sum.pack(fill="x", padx=16, pady=(0, 12))

        self.cv.bind("<Configure>", self._on_cfg)
        self.cv.bind("<Motion>", self._on_move)
        self.cv.bind("<ButtonPress-1>", self._on_press)
        self.cv.bind("<B1-Motion>", self._on_drag)
        self.cv.bind("<Double-Button-1>", self._on_reset)
        self.cv.bind("<MouseWheel>", self._on_wheel)
        self.cv.bind("<Leave>", self._on_leave)

        self.update_idletasks()
        try:
            sc = float(self._get_window_scaling())
        except Exception:
            sc = 1.0
        w, h = math.ceil(self.winfo_reqwidth() / sc), math.ceil(self.winfo_reqheight() / sc)
        saved = _sky_win_load()
        if saved:
            w, h = saved
        w = min(w, max(500, int(app.winfo_screenwidth() / sc)))
        h = min(h, max(300, int(app.winfo_screenheight() / sc) - 80))
        x = app.winfo_rootx() + app.winfo_width() + 8
        if x + w > app.winfo_screenwidth():
            x = max(0, app.winfo_rootx() - w - 8)
        y = max(0, app.winfo_rooty())
        pos = _sky_win_pos()
        if pos:   # recuerda la ultima posicion (ajustada para que quede visible)
            x = max(0, min(pos[0], app.winfo_screenwidth() - 120))
            y = max(0, min(pos[1], app.winfo_screenheight() - 80))
        self.geometry(f"{w}x{h}+{x}+{y}")
        self._sc = sc
        self._saved_custom = bool(saved)
        self._init_wh = None
        self._init_xy = None
        self._save_job = None
        # --- Leyenda interactiva: visibilidad por elemento ---
        self._legend_vis: Dict[str, tk.BooleanVar] = {}   # key -> BooleanVar
        self._legend_widgets: List[tk.Widget] = []        # widgets creados (para limpiar)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Configure>", self._on_win_cfg, add="+")
        self.lift()
        self.after(5000, self._tick)
        self.after(350, self._fit_once)

    def _fit_once(self):
        """Sin tamano guardado: tras el primer dibujo (ya con el resumen inferior escrito) ajusta el alto
        para que el texto de abajo se vea completo. Con tamano guardado se respeta tal cual."""
        try:
            if not self.winfo_exists():
                return
            if not self._saved_custom:
                self.update_idletasks()
                need = math.ceil(self.winfo_reqheight() / self._sc)
                cur = math.ceil(self.winfo_height() / self._sc)
                if need > cur:
                    maxh = max(300, int(self.winfo_screenheight() / self._sc) - 80)
                    self.geometry(f"{math.ceil(self.winfo_width() / self._sc)}x{min(need, maxh)}")
            self.after(450, self._mark_open)
        except Exception:
            pass

    def _mark_open(self):
        try:
            self._init_wh = (self.winfo_width(), self.winfo_height())
            self._init_xy = (self.winfo_x(), self.winfo_y())
        except Exception:
            pass

    def _on_win_cfg(self, e):
        if e.widget is not self or self._init_wh is None:
            return
        if self._save_job is not None:
            self.after_cancel(self._save_job)
        self._save_job = self.after(600, self._save_size)

    def _save_size(self):
        """Guarda el tamano solo si el usuario lo cambio respecto al de apertura."""
        self._save_job = None
        try:
            if self.winfo_exists() and self._init_wh is not None:
                wh = (self.winfo_width(), self.winfo_height())
                xy = (self.winfo_x(), self.winfo_y())
                if wh != self._init_wh or xy != self._init_xy:
                    _sky_win_save(math.ceil(wh[0] / self._sc), math.ceil(wh[1] / self._sc), xy[0], xy[1])
        except Exception:
            pass

    def _on_close(self):
        if self._save_job is not None:
            try:
                self.after_cancel(self._save_job)
            except Exception:
                pass
        self._save_size()
        self.destroy()

    def _sync_wrap(self):
        """Las lineas largas del resumen se parten al ancho de la ventana en vez de cortarse por la derecha."""
        try:
            wl = max(400, int((self.cv.winfo_width() - 8) / float(self._get_widget_scaling())))
            if wl != getattr(self, "_wrap_last", None):
                self._wrap_last = wl
                self.lbl_sum.configure(wraplength=wl)
        except Exception:
            pass

    def _legend_key(self, group: str, text: str) -> str:
        """Clave única para un elemento de leyenda."""
        return f"{group}:{text}"

    def _is_visible(self, group: str, text: str) -> bool:
        """Comprueba si un elemento de leyenda está visible (default True)."""
        key = self._legend_key(group, text)
        var = self._legend_vis.get(key)
        if var is None:
            var = tk.BooleanVar(value=True)
            self._legend_vis[key] = var
        return var.get()

    def _clear_legend_widgets(self):
        """Antes destruia los widgets en cada redibujado (parpadeo, y se perdia el foco). Ahora se reutilizan:
        cv.delete("all") ya quita sus ventanas del lienzo y _build_legend las vuelve a colocar."""
        return

    def _build_legend(self, items, y, group: str, x=50, max_x=None, dry=False, with_checkbox=True):
        """
        Coloca una leyenda; si no cabe en el ancho (max_x) sigue en otra fila.
        items: [(color, dash, texto)]; group: "curves", "states", "battery"...
        with_checkbox: True = muestras clicables (curvas), False = solo visual (cielo/batería)
        Devuelve la y de la fila siguiente. dry=True solo calcula la disposicion (para reservar el alto).
        Los widgets se crean una vez y se reutilizan en cada redibujado.
        """
        font = getattr(self, "_legend_font", None)
        if font is None:
            try:
                from tkinter import font as _tkf
                font = self._legend_font = _tkf.Font(family="Consolas", size=8)
            except Exception:
                font = self._legend_font = False
        meas = font.measure if font else (lambda s: 6 * len(s))
        cache = self.__dict__.setdefault("_legend_cache", {})
        x_start = x
        for color, dash, text in items:
            off = 46 if with_checkbox else 26   # ancho de la muestra (con casilla es mayor)
            if max_x is not None and x > x_start and x + off + 20 + meas(text) > max_x:
                x, y = x_start, y + 13          # no cabe: nueva fila
            if not dry:
                key = self._legend_key(group, text)
                var = self._legend_vis.get(key)
                if var is None:
                    var = tk.BooleanVar(value=True)
                    self._legend_vis[key] = var
                ck = (group, text, color, dash)
                widgets = cache.get(ck)
                if widgets is None:
                    if with_checkbox:
                        # Curvas: casilla + muestra clicables + label clicable
                        sample = tk.Canvas(self.cv, width=42, height=13, bg=SKY_BG, highlightthickness=0, bd=0, cursor="hand2")
                        sample.bind("<Button-1>", lambda e, g=group, t=text: self._on_legend_toggle(g, t))
                        lbl = tk.Label(self.cv, text=text, bg=SKY_BG, fg="#94a3b8", font=("Consolas", 8), cursor="hand2")
                        lbl.bind("<Button-1>", lambda e, g=group, t=text: self._on_legend_toggle(g, t))
                        widgets = cache[ck] = (sample, lbl, var)
                    else:
                        # Cielo/Batería: solo visual (rectángulo + label), sin interacción
                        sample = tk.Canvas(self.cv, width=22, height=12, bg=SKY_BG, highlightthickness=0, bd=0)
                        lbl = tk.Label(self.cv, text=text, bg=SKY_BG, fg="#94a3b8", font=("Consolas", 8))
                        widgets = cache[ck] = (sample, lbl, var)
                sample, lbl, _ = widgets
                # se repinta siempre: casilla marcada/vacia y muestra atenuada si esta desmarcada
                sample.delete("all")
                if with_checkbox:
                    on_ = bool(var.get())
                    sample.create_rectangle(1, 1, 12, 12, outline="#94a3b8" if on_ else "#64748b",
                                            fill="#1e293b" if on_ else SKY_BG, width=1)
                    if on_:
                        sample.create_line(3, 7, 5, 10, 10, 3, fill="#e2e8f0", width=2)
                    if group in ("states", "battery"):   # cielo / bateria: muestra de color (rectangulo) en vez de linea
                        sample.create_rectangle(17, 2, 39, 11, fill=color if on_ else "#334155", outline="")
                    else:
                        sample.create_line(17, 6, 39, 6, fill=color if on_ else "#334155", width=3,
                                           **({"dash": dash} if dash else {}))
                else:
                    sample.create_rectangle(2, 2, 20, 10, fill=color, outline="")
                self.cv.create_window(x, y, window=sample, anchor="w")
                self.cv.create_window(x + off, y, window=lbl, anchor="w")
            x += off + meas(text) + 16
        return y + 13

    def _on_legend_toggle(self, group: str, text: str):
        """Clic en la casilla / muestra / texto de la leyenda: alterna la casilla y redibuja."""
        key = self._legend_key(group, text)
        var = self._legend_vis.get(key)
        if var is None:
            var = tk.BooleanVar(value=True)
            self._legend_vis[key] = var
        var.set(not var.get())
        # Forzar redibujado del modo actual
        if self.mode == "Día":
            self._draw_day()
        else:
            self._draw_bars()

    def _wx_cache_now(self):
        return _wx_cache_of(self.tr)

    @staticmethod
    def _wx_sky_key(wx):
        return wx_sky_key(wx)

    def _adjacent_soc(self, direction: int):
        return adjacent_soc(self.tr, self.day, direction)

    def _gap_soc_line(self, g0, g1, pa, pb, bs):
        return gap_soc_line(self.tr, self.day, g0, g1, pa, pb, bs)

    @staticmethod
    def _lin(ln, t):
        return lin_soc(ln, t)

    @staticmethod
    def _gap_bat_key(a, b):
        return gap_bat_key(a, b)

    @staticmethod
    def _day_legend_groups():
        """Leyendas del modo Dia: [(items, grupo, con_checkbox)] en orden de dibujo.
        - Cielo y Batería: sin checkbox (solo referencia visual, siempre visibles)
        - Curvas: con checkboxes, se ajustan a varias filas según ancho
        """
        return [
            # Fila 1: Cielo (sin checkbox)
            ([(STATES[k][2], None, STATES[k][0]) for k in ("sun", "sahara", "cloud", "dense", "neblina", "niebla", "lowsun")], "states", False),
            # Fila 2: Batería (sin checkbox)
            ([(BAT_COL[k], None, "batería " + BAT_NAME[k]) for k in ("chg", "dis", "bal", "full")], "battery", False),
            # Filas 3+: Curvas (con checkboxes)
            ([("#fbbf24", (4, 3), "esperado despejado"), ("#22c55e", (4, 3), "según pronóstico"),
              ("#00f0ff", None, "medido"), ("#2dd4bf", (3, 2), "estimado (protección DC / batería llena)"),
              ("#555555", (4, 4), "huecos (sin datos BLE)"),
              (BAT_LINE, None, "batería % (línea y cinta)"), ("#22c55e", None, "carga por AC"),
              ("#ef4444", None, f"> {HIGH_A:.0f} A")], "curves", True),
        ]

    # ---- exportar (PNG de la ventana o CSV con los datos, segun la extension elegida)
    def _export(self):
        try:
            from tkinter import filedialog
            base = f"sky_{self.day}" if self.mode == "Día" else f"sky_{self.mode.lower()}_{self.tr.day or ''}"
            path = filedialog.asksaveasfilename(parent=self, defaultextension=".png", initialfile=base + ".png",
                                                filetypes=[("Imagen PNG", "*.png"), ("Datos CSV", "*.csv")])
            if not path:
                return
            if path.lower().endswith(".csv"):
                self._export_csv(path)
            else:
                self.after(300, lambda: self._export_png(path))
        except Exception as exc:
            self.lbl_hover.configure(text=f"No se pudo exportar: {exc}")

    def _export_png(self, path):
        try:
            self.lift()
            self.update()
            x, y, w, h = self.winfo_rootx(), self.winfo_rooty(), self.winfo_width(), self.winfo_height()
            try:
                from PIL import ImageGrab
                try:
                    img = ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)
                except TypeError:
                    img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
                img.save(path)
            except ImportError:   # sin Pillow: PostScript de la grafica
                path = os.path.splitext(path)[0] + ".ps"
                self.cv.postscript(file=path, colormode="color")
            self.lbl_hover.configure(text=f"Exportado: {path}")
        except Exception as exc:
            self.lbl_hover.configure(text=f"No se pudo exportar: {exc}")

    def _open_wx_config(self):
        """Abre modal de configuración de clima y k_sistema."""
        win = ctk.CTkToplevel(self)
        win.title("Configuración Clima / Panel")
        win.transient(self)
        try:   # _set_window_icon es de la ventana principal (antes se llamaba en SkyWindow y fallaba: la ventana quedaba vacia)
            self.master._set_window_icon(win)
        except Exception:
            pass

        def _grab():   # el grab solo funciona con la ventana ya visible; si falla no debe romper nada
            try:
                win.lift()
                win.focus_force()
                win.grab_set()
            except Exception:
                pass
        win.after(250, _grab)
        win.resizable(False, False)
        # Centrar sobre la ventana principal
        self.update_idletasks()
        x = self.winfo_rootx() + (self.winfo_width() - 460) // 2
        y = self.winfo_rooty() + (self.winfo_height() - 520) // 2
        win.geometry(f"460x520+{x}+{y}")
        win.configure(fg_color=SKY_CARD)

        ctk.CTkLabel(win, text="⚙️ CONFIGURACIÓN CLIMA / PANEL", font=("Segoe UI", 13, "bold"),
                     text_color="#00f0ff").pack(pady=(16, 8))
        ctk.CTkButton(win, text="Cerrar", width=100, command=win.destroy).pack(side="bottom", pady=(8, 16))   # siempre visible

        # --- k_sistema info ---
        k_sys = self.tr.data.get("k_sys", 1.0)
        k_sys_days = self.tr.data.get("k_sys_days", [])
        # solo cuentan los resets (manual o por lluvia), no las actualizaciones diarias automaticas
        last_reset = next((e for e in reversed(k_sys_days) if e.get("auto") is False or "prev_k" in e), None)
        lr_text = "Nunca"
        if last_reset:
            ts = last_reset.get("ts", 0)
            lr_text = datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
            lr_text += " (auto)" if last_reset.get("auto") else " (manual)"

        info_frame = ctk.CTkFrame(win, fg_color="#0e141f")
        info_frame.pack(fill="x", padx=16, pady=8)
        ctk.CTkLabel(info_frame, text=f"k_sistema actual:  {k_sys:.4f}", font=("Consolas", 11),
                     text_color="#2dd4bf").pack(anchor="w", padx=12, pady=(8, 2))
        ctk.CTkLabel(info_frame, text=f"Último reset:  {lr_text}", font=("Consolas", 10),
                     text_color="#94a3b8").pack(anchor="w", padx=12, pady=(0, 8))

        ctk.CTkButton(info_frame, text="🔄 Panel limpiado (reset manual k_sistema)",
                      fg_color="#2dd4bf", hover_color="#14b8a6", text_color="#080b11",
                      command=lambda: self._reset_k_sys_manual(win)).pack(fill="x", padx=12, pady=(0, 6))

        # Botón deshacer: solo si el último reset fue automático
        k_sys_days = self.tr.data.get("k_sys_days", [])
        last_was_auto = bool(k_sys_days) and "prev_k" in k_sys_days[-1]   # solo los auto-reset por lluvia llevan prev_k (las actualizaciones diarias no)
        if last_was_auto:
            prev_k = float(k_sys_days[-1].get("prev_k", 1.0))
            ctk.CTkButton(info_frame, text=f"↩️ Deshacer auto-reset (volver a {prev_k:.3f})",
                          fg_color="#f59e0b", hover_color="#d97706", text_color="#080b11",
                          command=lambda: self._undo_auto_reset_k_sys(win, prev_k)).pack(fill="x", padx=12, pady=(0, 12))

        # --- Config clima ---
        wx_frame = ctk.CTkFrame(win, fg_color="#0e141f")
        wx_frame.pack(fill="x", padx=16, pady=8)

        ctl = self._wx_ctl()
        if ctl.is_remote:
            self._wx_remote_open(win, wx_frame, ctl)
            return
        self._wx_build(win, wx_frame, ctl, ctl.cfg())


    def _wx_do(self, fn, *args):
        fn(*args)
        self.tr.invalidate_curves()

    def _wx_ctl(self):
        if self.tr._weather_provider.remote_mode():
            return _WxRemoteCtl(getattr(self.master, "_remote", None))
        return _WxLocalCtl(self.tr)

    def _wx_remote_open(self, win, wx_frame, ctl):
        """Cliente: la ventana es un mando a distancia del clima del servidor. Pide su configuracion y construye
        los mismos controles que en el servidor; cada cambio se envia al servidor, que lo aplica."""
        ctk.CTkLabel(wx_frame, text="☁ Modo cliente: aquí controlas el pronóstico del SERVIDOR.\n"
                                    "Los cambios se aplican allí y se reciben en este equipo, que no necesita\n"
                                    "internet ni claves.", font=("Segoe UI", 10), text_color="#e2e8f0",
                     justify="left").pack(anchor="w", padx=12, pady=(10, 4))
        st = ctk.CTkLabel(wx_frame, text="Cargando configuración del servidor…", font=("Consolas", 9),
                          text_color="#94a3b8", wraplength=400, justify="left")
        st.pack(anchor="w", padx=12, pady=(0, 8))

        def read_only(msg):
            st.configure(text=msg + "\n" + self.tr._weather_provider.status_text())
            badge_c = ctk.BooleanVar(value=self.tr._weather_provider.config.get("show_subtype_badge", False))
            ctk.CTkCheckBox(wx_frame, text="Mostrar detalle de nubes en barra (ej: 'Nublado · Cúmulos rápidos')",
                            variable=badge_c, font=("Segoe UI", 10), text_color="#e2e8f0",
                            command=lambda: self._wx_set_show_badge(badge_c.get())).pack(anchor="w", padx=12, pady=(4, 10))
            self._wx_fit(win)

        if not ctl.connected():
            return read_only("Sin conexión con el servidor: no se puede cambiar su configuración.")
        if ctl.role() != "controller":
            return read_only("Solo lectura: conéctate con la contraseña de «Controlar» para cambiar el clima del servidor.")
        ctl.get_begin()
        t0 = time.time()

        def poll():
            try:
                if not win.winfo_exists():
                    return
                res = ctl.get_poll()
                if res is None:
                    if time.time() - t0 < 8:
                        win.after(300, poll)
                    else:
                        st.configure(text="El servidor no respondió (¿tiene la versión nueva de la app?).")
                    return
                if not res.get("ok"):
                    st.configure(text=res.get("error") or "Error del servidor")
                    return
                st.configure(text=res.get("status") or "")
                self._wx_build(win, wx_frame, ctl, res["cfg"])
            except Exception:
                pass
        win.after(300, poll)

    def _wx_build(self, win, wx_frame, ctl, cfg):
        """Controles de la ventana de clima. cfg = configuracion a mostrar; ctl = quien la aplica (este equipo o el servidor)."""
        # Habilitado
        enabled_var = ctk.BooleanVar(value=cfg["enabled"])
        ctk.CTkCheckBox(wx_frame, text="Habilitar pronóstico meteorológico", variable=enabled_var,
                        font=("Segoe UI", 10), text_color="#e2e8f0",
                        command=lambda: self._wx_do(ctl.set_enabled, enabled_var.get())).pack(anchor="w", padx=12, pady=(8, 4))

        # Proveedor
        prov_frame = ctk.CTkFrame(wx_frame, fg_color="transparent")
        prov_frame.pack(fill="x", padx=12, pady=4)
        ctk.CTkLabel(prov_frame, text="Proveedor:", font=("Segoe UI", 10), text_color="#cbd5e1").pack(side="left")
        choices = provider_choices()
        prov_names = [f"{c['label']} ({c['free_tier']})" for c in choices]
        prov_values = [c["value"] for c in choices]
        current_prov = cfg["provider"]   # el ELEGIDO, no el de respaldo que respondio
        try:
            current_idx = prov_values.index(current_prov)
        except ValueError:
            current_idx = 0
        prov_var = ctk.StringVar(value=prov_names[current_idx])
        prov_menu = ctk.CTkOptionMenu(prov_frame, values=prov_names, variable=prov_var, width=280,
                                      command=lambda v: self._wx_do(ctl.set_provider, prov_values[prov_names.index(v)]))
        prov_menu.pack(side="left", padx=(8, 0))

        # Unificar proveedores (opcional): promedia los datos de los 3 para aproximar mejor la gráfica
        merge_var = ctk.BooleanVar(value=cfg["merge"])
        ctk.CTkSwitch(wx_frame, text="Unificar datos de los 3 proveedores (promedio)", variable=merge_var,
                      font=("Segoe UI", 10), text_color="#e2e8f0",
                      command=lambda: self._wx_do(ctl.set_merge, merge_var.get())).pack(anchor="w", padx=12, pady=(4, 4))

        # Intervalo
        int_frame = ctk.CTkFrame(wx_frame, fg_color="transparent")
        int_frame.pack(fill="x", padx=12, pady=4)
        ctk.CTkLabel(int_frame, text="Intervalo (min):", font=("Segoe UI", 10), text_color="#cbd5e1").pack(side="left")
        interval_var = ctk.StringVar(value=str(cfg["interval"]))
        interval_menu = ctk.CTkOptionMenu(int_frame, values=[str(i) for i in range(5, 31, 5)],
                                          variable=interval_var, width=80,
                                          command=lambda v: self._wx_do(ctl.set_interval, int(v)))
        interval_menu.pack(side="left", padx=(8, 0))

        # API Keys
        ctk.CTkLabel(wx_frame, text="API Keys (una por proveedor):", font=("Segoe UI", 10, "bold"),
                     text_color="#94a3b8").pack(anchor="w", padx=12, pady=(8, 4))

        key_entries = {}
        for c in choices:
            key_frame = ctk.CTkFrame(wx_frame, fg_color="transparent")
            key_frame.pack(fill="x", padx=12, pady=2)
            ctk.CTkLabel(key_frame, text=f"{c['label']}:", font=("Consolas", 9), text_color="#94a3b8",
                         width=160).pack(side="left")
            key_var = ctk.StringVar(value=cfg["api_keys"].get(c["value"], ""))
            entry = ctk.CTkEntry(key_frame, textvariable=key_var, width=260, show="•",
                                 placeholder_text="Opcional" if not c["requires_key"] else "Requerido")
            entry.pack(side="left", padx=(8, 0))
            key_entries[c["value"]] = key_var

        def save_keys():
            ctl.set_keys({prov: var.get() for prov, var in key_entries.items()})
            self.tr.invalidate_curves()
            self.lbl_hover.configure(text="API Keys enviadas al servidor" if ctl.is_remote else "API Keys guardadas")

        ctk.CTkButton(wx_frame, text="💾 Guardar API Keys", width=180,
                      fg_color="#1e293b", hover_color="#334155", command=save_keys).pack(pady=(8, 4))

        # Actualizar ahora (asincrono: muestra "Actualizando..." y consulta el resultado; en el cliente lo hace el servidor)
        def force_fetch():
            err = ctl.refresh_begin()
            if err:
                self.lbl_hover.configure(text=err)
                return
            t0 = time.time()
            btn_actualizar.configure(text="⏳ Actualizando...", state="disabled")

            def poll_fetch():
                try:
                    done, txt = ctl.refresh_poll()
                    if done:   # el intento termino (con exito o no): dice que proveedor se uso o por que fallo
                        self.tr.invalidate_curves()
                        self.lbl_hover.configure(text=txt)
                        btn_actualizar.configure(text="🔄 Actualizar ahora", state="normal")
                    elif time.time() - t0 < 40:
                        self.after(1000, poll_fetch)
                    else:
                        self.lbl_hover.configure(text="Timeout: sin respuesta del proveedor")
                        btn_actualizar.configure(text="🔄 Actualizar ahora", state="normal")
                except Exception:
                    pass   # la ventana se cerro
            self.after(1000, poll_fetch)

        btn_actualizar = ctk.CTkButton(wx_frame, text="🔄 Actualizar ahora", width=180,
                          fg_color="#2dd4bf", hover_color="#14b8a6", text_color="#080b11",
                          command=force_fetch)
        btn_actualizar.pack(pady=(0, 8))

        # Badge opcional: mostrar subtipo de nube en barra de estado
        badge_var = ctk.BooleanVar(value=self.tr._weather_provider.config.get("show_subtype_badge", False))
        ctk.CTkCheckBox(wx_frame, text="Mostrar detalle de nubes en barra (ej: 'Nublado · Cúmulos rápidos')",
                        variable=badge_var, font=("Segoe UI", 10), text_color="#e2e8f0",
                        command=lambda: self._wx_set_show_badge(badge_var.get())).pack(anchor="w", padx=12, pady=(8, 4))

        # Cache info
        cache_info = ctk.CTkLabel(wx_frame, text="Cache: 12h en %TEMP% (auto-purga)",
                                  font=("Consolas", 9), text_color="#64748b")
        cache_info.pack(anchor="w", padx=12, pady=(0, 8))

        self._wx_fit(win)

    def _wx_set_show_badge(self, show: bool):
        self.tr._weather_provider.config["show_subtype_badge"] = show
        self.tr._weather_provider.save_config()

    def _reset_k_sys_manual(self, parent_win):
        self.tr.reset_k_sys_manual()
        parent_win.destroy()
        self._open_wx_config()  # reabre para ver el cambio

    def _undo_auto_reset_k_sys(self, parent_win, prev_k: float):
        """Restaura k_sistema al valor anterior tras un auto-reset."""
        self.tr.data["k_sys"] = prev_k
        # Quitar la entrada automática del historial
        k_days = self.tr.data.get("k_sys_days", [])
        if k_days and k_days[-1].get("auto", False):
            k_days.pop()
        self.tr.invalidate_curves()
        self.tr._dirty = True
        parent_win.destroy()
        self._open_wx_config()

    def _export_csv(self, path):
        import csv
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                wr = csv.writer(f, delimiter=";")
                if self.mode == "Día":
                    wr.writerow(["hora", "estado", "potencia_w", "esperado_w", "cable_w", "energia_acum_wh",
                                 "estimado_w", "bateria_pct", "entrada_ac_w", "corriente_solar_max_a", "bateria_neta_w", "bateria_estado", "estimado_origen", "subtipo_nube"])
                    month = int(self.day[5:7])
                    for p in self.tr.view(self.day)["pts"]:
                        g = lambda i, d="": p[i] if len(p) > i else d
                        est_src = p[11] if len(p) > 11 else 0
                        est_origin = "protección DC" if est_src == 1 else ("batería llena" if est_src == 2 else "")
                        sub = p[12] if len(p) > 12 else ""
                        wr.writerow([_fmt_hm(p[0]), state_label(STATE_KEYS[int(p[4])], month), p[1], p[2], p[3],
                                     p[5], g(6), g(7), g(8), g(9), g(10), BAT_NAME.get(_bat_state(p), ""), est_origin, sub])
                else:
                    wr.writerow(["fecha", "energia_entregada_wh", "energia_panel_wh", "pico_w", "potencial_wh",
                                 "cable_wh", "no_aprovechado_supuesto_wh", "min_bateria_llena", "min_carga_ac",
                                 "min_corriente_alta", "estado_predominante"])
                    for d, r in sorted(self.tr.summaries().items()):
                        dom = _dominant(r["st"])
                        wr.writerow([_dmy(d), r["wh"], r["whp"], r["pk"], r.get("pot", ""), r.get("lw", ""),
                                     r.get("ner", ""), r.get("full", ""), r.get("ac", ""), r.get("hi", ""),
                                     state_label(dom, int(d[5:7])) if dom else ""])
            self.lbl_hover.configure(text=f"Exportado: {path}")
        except Exception as exc:
            self.lbl_hover.configure(text=f"No se pudo exportar: {exc}")

    def set_mode(self, mode: str):
        self.mode = mode if mode in MODES else "Día"
        try:
            self.seg.set(self.mode)
        except Exception:
            pass
        if self.mode == "Día":
            if not self.opt.winfo_manager():
                self.opt.pack(side="right", padx=(0, 8))
        else:
            self.opt.pack_forget()
        self._hx = None
        self._draw()

    def _tick(self):
        try:
            if not self.winfo_exists():
                return
            if self.mode != "Día" or self.day == self.tr.day:
                self._draw()
            self.after(5000, self._tick)
        except Exception:
            pass

    def _on_day(self, v):
        self.day = _iso(v)
        self._hx = None
        self._xr = None
        self._draw()

    def _on_cfg(self, _e=None):
        if self._cfg_job is not None:
            self.after_cancel(self._cfg_job)
        self._cfg_job = self.after(80, self._draw)

    def _on_move(self, e):
        self._hx = e.x
        self._draw_hover()

    def _on_leave(self, _e=None):
        self._hx = None
        self._draw_hover()

    def _on_press(self, e):
        self._px = e.x
        self._pan0 = self._xr
        self._on_move(e)

    def _on_reset(self, _e=None):
        self._xr = None
        self._draw()

    def _on_wheel(self, e):
        g = self._geo
        if self.mode != "Día" or not g or g.get("mode") != "Día":
            return
        x0, x1, fx0, fx1 = g["x0"], g["x1"], g["fx0"], g["fx1"]
        span, full = x1 - x0, fx1 - fx0
        new = min(full, max(600.0, span * (0.8 if e.delta > 0 else 1.25)))
        if new >= full * 0.995:
            self._xr = None
        else:
            tm = x0 + (e.x - g["L"]) / max(1.0, g["W"] - g["L"] - g["R"]) * span
            tm = min(max(tm, x0), x1)
            nx0 = tm - (tm - x0) / span * new
            nx0 = max(fx0, min(nx0, fx1 - new))
            self._xr = (nx0, nx0 + new)
        self._draw()

    def _on_drag(self, e):
        g = self._geo
        if self.mode == "Día" and g and g.get("mode") == "Día" and self._pan0 is not None:
            x0, x1 = self._pan0
            span = x1 - x0
            sh = -(e.x - self._px) / max(1.0, g["W"] - g["L"] - g["R"]) * span
            nx0 = max(g["fx0"], min(x0 + sh, g["fx1"] - span))
            self._xr = (nx0, nx0 + span)
            if self._cfg_job is None:
                self._cfg_job = self.after(15, self._draw)
        else:
            self._on_move(e)

    def _draw(self):
        self._sync_wrap()
        self._cfg_job = None
        try:
            if self.mode == "Día":
                self._draw_day()
            else:
                self._draw_bars()
        except Exception as exc:   # que un error al dibujar no deje el lienzo en blanco sin explicacion
            import traceback
            traceback.print_exc()
            try:
                self.lbl_hover.configure(text=f"Error al dibujar la gráfica: {type(exc).__name__}: {exc}")
            except Exception:
                pass

    def _ribbon_png(self, cv, rib):
        """Cinta como imagen PNG con CANAL ALFA: se mezcla con lo que haya debajo (relleno, malla, columnas del sol),
        asi puede dibujarse por encima de todo sin tapar nada ni depender del color de fondo."""
        import zlib, struct, base64
        xs = [r[0] for r in rib]
        x_min, x_max = int(min(xs)), int(max(xs)) + RIB_CW
        y_min = int(round(min(r[1] for r in rib)))
        y_max = int(round(max(r[2] for r in rib)))
        w_, h_ = x_max - x_min, y_max - y_min
        if w_ <= 0 or h_ <= 0:
            raise ValueError("cinta vacia")
        rows = [bytearray(w_ * 4) for _ in range(h_)]
        for cx_, e0_, e1_, _c, col_, al_ in rib:
            a_, b_ = int(round(e0_)) - y_min, int(round(e1_)) - y_min
            if b_ <= a_:
                continue
            rr, gg, bb = int(col_[1:3], 16), int(col_[3:5], 16), int(col_[5:7], 16)
            px = bytes((rr, gg, bb, max(0, min(255, int(al_ * 255))))) * RIB_CW
            x0_ = (int(cx_) - x_min) * 4
            for y_ in range(max(0, a_), min(b_, h_)):
                rows[y_][x0_:x0_ + RIB_CW * 4] = px
        raw = b"".join(b"\x00" + bytes(r) for r in rows)

        def chunk(t, d):
            c = struct.pack(">I", len(d)) + t + d
            return c + struct.pack(">I", zlib.crc32(t + d) & 0xffffffff)
        png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w_, h_, 8, 6, 0, 0, 0))
               + chunk(b"IDAT", zlib.compress(raw, 1)) + chunk(b"IEND", b""))
        img = tk.PhotoImage(master=cv, data=base64.b64encode(png), format="png")
        return img, x_min, y_min

    def _ribbon_draw(self, cv, rib):
        """Dibuja la cinta de bateria como UNA imagen (en vez de miles de rectangulos). Primero con transparencia real
        (PNG con alfa); si Tk no lo admite, cae a la imagen opaca antigua y, si tampoco, a rectangulos.
        rib: [(columna, y0, y1, color_mezclado_opaco, color_cinta, alfa)]. Todo lleva la etiqueta "bat"."""
        if not rib:
            return
        try:
            img, x_min, y_min = self._ribbon_png(cv, rib)
            cv.create_image(x_min, y_min, image=img, anchor="nw", tags=("bat",))
            self._rib_imgs.append(img)
            return
        except Exception:
            pass
        try:
            xs = [r[0] for r in rib]
            x_min, x_max = int(min(xs)), int(max(xs)) + RIB_CW
            y_min = int(round(min(r[1] for r in rib)))
            y_max = int(round(max(r[2] for r in rib)))
            if y_max <= y_min:
                return
            cols = {}
            for cx_, e0_, e1_, c_, _k, _a in rib:
                a_, b_ = int(round(e0_)), int(round(e1_))
                if b_ <= a_:
                    continue
                col = cols.get(cx_)
                if col is None:
                    col = cols[cx_] = [None] * (y_max - y_min)
                for y_ in range(a_ - y_min, min(b_ - y_min, len(col))):
                    col[y_] = c_
            img = tk.PhotoImage(master=cv, width=x_max - x_min, height=y_max - y_min)
            for cx_, col in cols.items():
                y_ = 0
                n_ = len(col)
                while y_ < n_:
                    if col[y_] is None:
                        y_ += 1
                        continue
                    y0_ = y_
                    rows = []
                    while y_ < n_ and col[y_] is not None:
                        c_ = col[y_]
                        rows.append("{%s %s %s}" % (c_, c_, c_) if RIB_CW == 3 else "{" + " ".join([c_] * RIB_CW) + "}")
                        y_ += 1
                    img.put(" ".join(rows), to=(int(cx_) - x_min, y0_))
            cv.create_image(x_min, y_min, image=img, anchor="nw", tags=("bat",))
            self._rib_imgs.append(img)   # Tk no mantiene la imagen viva por si solo
        except Exception:
            for cx_, e0_, e1_, c_, _k, _a in rib:
                cv.create_rectangle(cx_, e0_, cx_ + RIB_CW, e1_, fill=c_, outline="", tags=("bat",))

    def _mesh(self, cv, up, dn, color):
        """Malla de un area estimada (bateria llena / sin datos BLE): relleno punteado + borde superior discontinuo.
        up / dn: listas de (x, y) en pixeles (borde superior e inferior, de izquierda a derecha)."""
        if len(up) < 2 or len(dn) < 2:
            return
        flat = [c for q in up for c in q] + [c for q in reversed(dn) for c in q]
        cv.create_polygon(*flat, fill=color, stipple=MESH_STIPPLE, outline="")
        # contorno discontinuo CERRADO: arriba, lado derecho, abajo y lado izquierdo (antes solo arriba y los tramos
        # muy verticales quedaban solo con la malla, como incompletos)
        cv.create_line(*(flat + flat[:2]), fill=color, dash=MESH_DASH, width=MESH_LINE_W)

    # ---- modo Dia
    def _draw_day(self):
        cv = self.cv
        cv.delete("all")
        self._clear_legend_widgets()   # limpia checkboxes anteriores
        self._rib_imgs = []
        W, H = cv.winfo_width(), cv.winfo_height()
        if W < 150 or H < 120:
            return
        v = self.tr.view(self.day)
        pts, bs = v["pts"], v["bin"]
        sun = self.tr.sun_times(self.day)
        fx0, fx1 = ((max(0.0, sun[0] - 1800.0), min(86400.0, sun[1] + 1800.0)) if sun else (5 * 3600.0, 20 * 3600.0))
        x0, x1 = self._xr if self._xr else (fx0, fx1)
        span = x1 - x0
        L, R, B = 50, 44, 40
        # la leyenda pasa a otra fila si no cabe: se reserva su alto antes de fijar el margen superior T
        leg_groups = self._day_legend_groups()
        y_leg = 33
        for _items, _grp, _with_cb in leg_groups:
            y_leg = self._build_legend(_items, y_leg, _grp, L, W - 6, dry=True, with_checkbox=_with_cb)
        T = max(76, y_leg + 4)

        def X(sx):
            return L + (min(max(sx, x0), x1) - x0) / span * (W - L - R)

        vis = [p for p in pts if p[0] + bs >= x0 and p[0] <= x1]
        step_c = 60   # curva esperada punto a punto cada minuto

        # DOS curvas: despejado teórico (amarillo) + con pronóstico (verde)
        curve_clear = self.tr.expected_curve(self.day, x0, x1, step_c)
        try:
            curve_wx = self.tr.expected_curve_wx(self.day, x0, x1, step_c)
        except Exception:   # el pronostico es opcional: si falla (proveedor/cache), la grafica se dibuja igual
            curve_wx = []

        # ymax considera ambas curvas
        all_curves_y = [c[1] for c in curve_clear if x0 <= c[0] <= x1] + [c[1] for c in curve_wx if x0 <= c[0] <= x1]
        ymax = max([p[1] for p in vis] + [(p[6] if len(p) > 6 else p[1]) for p in vis] + [p[2] for p in vis]
                   + all_curves_y + [100.0])
        ymax = math.ceil(ymax * 1.08 / 100.0) * 100.0

        def Y(wv):
            return T + (1.0 - wv / ymax) * (H - T - B)

        def Ys(pc):
            return T + (1.0 - pc / 100.0) * (H - T - B)

        ystep = 50 if ymax <= 300 else (100 if ymax <= 600 else 200)
        for g in range(0, int(ymax) + 1, ystep):
            cv.create_line(L, Y(g), W - R, Y(g), fill=SKY_GRID)
            cv.create_text(L - 6, Y(g), text=f"{g}", fill="#64748b", anchor="e", font=("Consolas", 8))
        for pc in (0, 25, 50, 75, 100):
            cv.create_text(W - R + 6, Ys(pc), text=f"{pc}%", fill="#a78bfa", anchor="w", font=("Consolas", 8))
        cv.create_text(L - 6, T - 10, text="W", fill="#64748b", anchor="e", font=("Consolas", 8, "bold"))
        cv.create_text(W - R + 6, T - 10, text="Bat.", fill="#a78bfa", anchor="w", font=("Consolas", 8, "bold"))
        cv.create_text(L - 6, 9, text="Cielo", fill="#64748b", anchor="e", font=("Consolas", 7))
        cv.create_text(L - 6, 19, text="Batería", fill="#a78bfa", anchor="e", font=("Consolas", 7))
        tstep = next((s for s in (60, 120, 300, 600, 900, 1800, 3600, 7200) if span / s <= 14), 7200)
        t = math.ceil(x0 / tstep) * tstep
        while t <= x1:
            cv.create_line(X(t), T, X(t), H - B, fill="#131b29")
            cv.create_text(X(t), H - B + 10, text=_fmt_hm(t) if tstep < 3600 else f"{int(t // 3600)}h",
                           fill="#64748b", font=("Consolas", 8))
            t += tstep

        # Tramos continuos medidos (para construir segs y bpts)
        segs, cur = [], []
        bmap: Dict[float, Any] = {}
        bmarks: Dict[int, Any] = {}
        for p in vis:
            mid = p[0] + bs / 2.0
            bmap[mid] = _bat_state(p)
            if cur and mid - cur[-1][0] > 3 * bs:
                segs.append(cur)
                cur = []
            cur.append((mid, p[1], (p[6] if len(p) > 6 else p[1]), (p[7] if len(p) > 7 else -1),
                        (p[8] if len(p) > 8 else 0.0), (p[9] if len(p) > 9 else 0.0),
                        (p[11] if len(p) > 11 else 0)))
        if cur:
            segs.append(cur)
        base = Y(0)

        # ---- Detección de huecos (sin datos BLE) e interpolación ----
        # Usamos pts completo (no solo vis) para encontrar huecos en el día
        gap_segs = []   # lista de segmentos interpolados: [(sx, w_interp), ...]
        gap_info = []   # un elemento por hueco: (inicio, fin, punto_anterior, punto_siguiente) para estimar cielo y bateria
        for _e in gap_estimates(self.tr, self.day, pts, bs, x0, x1, sun, fx0, fx1, curve_clear, curve_wx, step_c):
            if _e["pw"]:
                gap_segs.append(_e["pw"])
            if _e["vis"]:
                gap_info.append((_e["s0"], _e["s1"], _e["pa"], _e["pb"]))

        # 1. Cinta de batería: se dibujará DENTRO del bucle de tramos medidos (después del relleno, antes de las líneas)
        # para que no quede tapada por el polígono de área medida. Aquí solo preparamos la flag.
        show_bat_ribbon = self._is_visible("curves", "batería % (línea y cinta)")
        show_gaps = self._is_visible("curves", "huecos (sin datos BLE)")   # tambien gobierna los estimados de los huecos (barras, linea y cinta)

        # 2. Amanecer / atardecer (AHORA encima de la cinta)
        if sun:
            for tt, lab in ((sun[0], "sale"), (sun[1], "se pone")):
                if x0 <= tt <= x1:
                    cv.create_rectangle(X(tt - SUN_ERR_S), T, X(tt + SUN_ERR_S), H - B, fill="#2a2208", outline="", tags=("sunbg",))
                    cv.create_line(X(tt), T, X(tt), H - B, fill="#fbbf24", dash=(2, 4), tags=("sun",))
                    cv.create_text(X(tt), H - B + 25, text=f"{lab} {_fmt_hm(tt)} ±{int(SUN_ERR_S // 60)}m", fill="#fbbf24", font=("Consolas", 8))

        # 3. Filas Cielo y Batería (barritas de estado)
        # cada casilla de la leyenda de estados / batería filtra de verdad su color en las barras
        rs = rb = None
        for p in vis:
            xa, xb = X(p[0]), max(X(p[0] + bs), X(p[0]) + 1)
            sk_ = _sk(p[4])
            col = STATES[sk_][2]
            if self._is_visible("states", STATES[sk_][0]):
                if rs is not None and rs[2] == col and xa <= rs[1] + 0.5:
                    rs[1] = max(rs[1], xb)
                else:
                    if rs is not None:
                        cv.create_rectangle(rs[0], 4, rs[1], 13, fill=rs[2], outline="")
                    rs = [xa, xb, col]
            elif rs is not None:
                cv.create_rectangle(rs[0], 4, rs[1], 13, fill=rs[2], outline="")
                rs = None
            bk = _bat_state(p)
            if bk and self._is_visible("battery", "batería " + BAT_NAME[bk]):
                if rb is not None and rb[2] == BAT_COL[bk] and xa <= rb[1] + 0.5:
                    rb[1] = max(rb[1], xb)
                else:
                    if rb is not None:
                        cv.create_rectangle(rb[0], 15, rb[1], 23, fill=rb[2], outline="")
                    rb = [xa, xb, BAT_COL[bk]]
            elif bk and rb is not None:
                cv.create_rectangle(rb[0], 15, rb[1], 23, fill=rb[2], outline="")
                rb = None
        if rs is not None:
            cv.create_rectangle(rs[0], 4, rs[1], 13, fill=rs[2], outline="")
        if rb is not None:
            cv.create_rectangle(rb[0], 15, rb[1], 23, fill=rb[2], outline="")

        # 3b. Huecos sin datos BLE: Cielo = pronostico del proveedor (o el ultimo estado conocido), Batería = tendencia
        # segun el % antes y despues del hueco (la misma que la linea / cinta estimada). Mismo color, punteado.
        if show_gaps and gap_info:
            wxc = self._wx_cache_now()
            for g0, g1, pa, pb in gap_info:
                runs = gap_sky_runs(wxc, self.day, g0, g1, _sk((pa if pa is not None else pb)[4]))
                for ta, tb, k_ in runs:
                    if tb >= x0 and ta <= x1 and self._is_visible("states", STATES[k_][0]):
                        cv.create_rectangle(X(ta), 4, max(X(tb), X(ta) + 1), 13, fill=STATES[k_][2],
                                            stipple=GAP_BAR_STIPPLE, outline=STATES[k_][2], dash=MESH_DASH)
                ln_ = self._gap_soc_line(g0, g1, pa, pb, bs)
                bk_ = self._gap_bat_key(self._lin(ln_, ln_[4]), self._lin(ln_, ln_[5])) if ln_ else None
                if bk_ and self._is_visible("battery", "batería " + BAT_NAME[bk_]):
                    cv.create_rectangle(X(g0), 15, max(X(g1), X(g0) + 1), 23, fill=BAT_COL[bk_],
                                        stipple=GAP_BAR_STIPPLE, outline=BAT_COL[bk_], dash=MESH_DASH)

        # 4. Curva "Esperado despejado" (amarillo discontinuo)
        show_clear = self._is_visible("curves", "esperado despejado")
        if show_clear and curve_clear:
            flat = []
            for sx, wv in curve_clear:
                flat += [X(sx), Y(wv)]
            if len(flat) >= 4:
                cv.create_line(*flat, fill="#fbbf24", dash=(4, 3), width=1, tags=("esp",))

        # 5. Curva "Según pronóstico" (verde discontinuo) - solo si hay datos
        show_wx = self._is_visible("curves", "según pronóstico")
        if show_wx and curve_wx:
            flat_wx = []
            for sx, wv in curve_wx:
                flat_wx += [X(sx), Y(wv)]
            if len(flat_wx) >= 4:
                cv.create_line(*flat_wx, fill="#22c55e", dash=(4, 3), width=1, tags=("wx",))

        # 5b. Relleno de huecos (sin datos BLE) - malla gris
        if show_gaps and gap_segs:
            for interp_pts in gap_segs:
                if len(interp_pts) >= 2:
                    # misma malla que el estimado con batería llena (relleno punteado + borde discontinuo), en gris
                    self._mesh(cv, [(X(sx), Y(wv)) for sx, wv in interp_pts],
                               [(X(interp_pts[0][0]), base), (X(interp_pts[-1][0]), base)], GAP_LINE)

        # 6. Tramos medidos, estimados, líneas de batería, etc.
        show_measured = self._is_visible("curves", "medido")
        show_est = self._is_visible("curves", "estimado (protección DC / batería llena)")
        show_bat_line = self._is_visible("curves", "batería % (línea y cinta)")
        show_ac = self._is_visible("curves", "carga por AC")
        show_hot = self._is_visible("curves", f"> {HIGH_A:.0f} A")
        def _ribbon(sg, amul=1.0):   # cinta de bateria de un tramo (medido, o estimado en un hueco con amul < 1)
            bpts = [(X(q[0]), Ys(q[3]), q[4] > AC_CHG_W, Y(q[1]), (Y(q[2]) if q[2] > q[1] + 10 else None))
                    for q in sg if q[3] >= 0]
            if len(bpts) >= 2:
                stp = RIB_PX / RIB_N
                rib = []
                j = 0
                cx = int(math.ceil(bpts[0][0]))
                while cx <= bpts[-1][0]:
                    while j < len(bpts) - 2 and bpts[j + 1][0] < cx:
                        j += 1
                    a_, b_ = bpts[j], bpts[j + 1]
                    f_ = 0.0 if b_[0] <= a_[0] else min(1.0, max(0.0, (cx - a_[0]) / (b_[0] - a_[0])))
                    yb_ = a_[1] + (b_[1] - a_[1]) * f_
                    yp_ = a_[3] + (b_[3] - a_[3]) * f_
                    ye_ = (a_[4] + (b_[4] - a_[4]) * f_) if (a_[4] is not None and b_[4] is not None) else None
                    col_ = BAT_AC if b_[2] else BAT_LINE
                    cuts = [(yp_, PANEL_FILL)] + ([(ye_, LIME_BG)] if ye_ is not None else [])
                    for k in range(RIB_N):
                        ya_, yz_ = yb_ + k * stp, min(base, yb_ + (k + 1) * stp)
                        if ya_ >= base:
                            break
                        al_ = RIB_A * amul * (1.0 - (k + 0.5) / RIB_N) ** 1.5
                        edges = sorted({ya_, yz_} | {c_ for c_, _bg in cuts if ya_ < c_ < yz_})
                        for e0, e1 in zip(edges, edges[1:]):
                            mid_ = (e0 + e1) / 2.0
                            bg_ = SKY_BG
                            if ye_ is not None and ye_ <= mid_ < yp_:
                                bg_ = LIME_BG
                            elif mid_ >= yp_:
                                bg_ = PANEL_FILL
                            rib.append((cx, e0, e1, _mix(col_, bg_, al_), col_, al_))
                    cx += RIB_CW
                self._ribbon_draw(cv, rib)
        for sg in segs:
            line = []
            for sx, wv, _e, _s, _a, _m, _est in sg:
                line += [X(sx), Y(wv)]
            # Polígono de relleno + línea medida: ambos respetan show_measured
            if show_measured and len(sg) >= 2:
                cv.create_polygon(*(line + [X(sg[-1][0]), base, X(sg[0][0]), base]), fill="#0b3b46", outline="")
            # Cinta de batería: después del relleno, antes de las líneas (para que se vea)
            if show_bat_ribbon:
                _ribbon(sg)
            # Levantar curvas y líneas sol sobre la cinta (la cinta se dibuja después del relleno pero antes de las líneas)
            cv.tag_raise("esp")
            cv.tag_raise("wx")
            cv.tag_raise("sun")
            # Estimado (protección DC / batería llena)
            if show_est:
                run_est = []
                for sx, wv, ex, _s, _a, _m, est_src in sg + [(None, 0, 0, -1, 0, 0, 0)]:
                    if est_src or ex > wv + 10:   # estimado marcado, o "supuesto" (mismo criterio que la cinta)
                        run_est.append((sx, wv, ex))
                        continue
                    if run_est:   # incluso una racha de 1 sola barra: se extiende media barra a cada lado
                        h_ = bs / 2.0
                        up = [(X(run_est[0][0] - h_), Y(run_est[0][2]))] + [(X(q[0]), Y(q[2])) for q in run_est] \
                            + [(X(run_est[-1][0] + h_), Y(run_est[-1][2]))]
                        dn = [(X(run_est[0][0] - h_), Y(run_est[0][1]))] + [(X(q[0]), Y(q[1])) for q in run_est] \
                            + [(X(run_est[-1][0] + h_), Y(run_est[-1][1]))]
                        self._mesh(cv, up, dn, "#2dd4bf")
                    run_est = []
            # Líneas de potencia medida (rojo si > HIGH_A)
            if show_measured and len(sg) >= 2:
                run, rc = [], None
                for i in range(1, len(sg)):
                    hot = sg[i][5] > HIGH_A or sg[i - 1][5] > HIGH_A
                    col = "#ef4444" if hot else "#00f0ff"
                    if show_hot or not hot:
                        if col != rc:
                            if len(run) >= 4:
                                cv.create_line(*run, fill=rc, width=2)
                            run, rc = [X(sg[i - 1][0]), Y(sg[i - 1][1])], col
                        run += [X(sg[i][0]), Y(sg[i][1])]
                    else:
                        if len(run) >= 4:
                            cv.create_line(*run, fill=rc, width=2)
                        run, rc = [], None
                if len(run) >= 4:
                    cv.create_line(*run, fill=rc, width=2)
            elif show_measured:
                cv.create_oval(line[0] - 2, line[1] - 2, line[0] + 2, line[1] + 2, fill="#00f0ff", outline="")
            # Línea de batería (verde si carga AC)
            if show_bat_line:
                run, rc = [], None
                for i in range(1, len(sg)):
                    if sg[i][3] >= 0 and sg[i - 1][3] >= 0:
                        col = BAT_AC if sg[i][4] > AC_CHG_W else BAT_LINE
                        if (show_ac and col == BAT_AC) or (col == BAT_LINE):
                            if col != rc or not run:
                                if len(run) >= 4:
                                    cv.create_line(*run, fill=rc, width=1.5, tags=("bat",))
                                run, rc = [X(sg[i - 1][0]), Ys(sg[i - 1][3])], col
                            run += [X(sg[i][0]), Ys(sg[i][3])]
                        else:
                            if len(run) >= 4:
                                cv.create_line(*run, fill=rc, width=1.5, tags=("bat",))
                            run, rc = [], None
                    else:
                        if len(run) >= 4:
                            cv.create_line(*run, fill=rc, width=1.5, tags=("bat",))
                        run, rc = [], None
                if len(run) >= 4:
                    cv.create_line(*run, fill=rc, width=1.5, tags=("bat",))
            # Marcadores hora en punto
            for hh in range(int(math.ceil(x0 / 3600.0)), int(x1 // 3600) + 1):
                tt = hh * 3600.0
                if sg[0][0] - 600 <= tt <= sg[-1][0] + 600:
                    cand = [q for q in sg if q[3] >= 0 and abs(q[0] - tt) <= 600]
                    if cand:
                        q = min(cand, key=lambda q_: abs(q_[0] - tt))
                        bmarks[hh] = (q[0], q[3], BAT_AC if q[4] > AC_CHG_W else BAT_LINE)
        # Filtrar marcadores hora en punto por leyenda de batería (checkbox "batería % (línea y cinta)")
        show_bat_marks = self._is_visible("curves", "batería % (línea y cinta)")
        if show_bat_marks:
            for mx, ms, mst in bmarks.values():
                cv.create_oval(X(mx) - 3, Ys(ms) - 3, X(mx) + 3, Ys(ms) + 3, fill=SKY_BG,
                               outline=mst, width=1.5, tags=("bat",))
        # Bateria estimada en los huecos: interpolacion entre el % anterior y el posterior (linea discontinua + cinta tenue)
        if show_gaps and (show_bat_line or show_bat_ribbon):
            for g0, g1, pa, pb in gap_info:
                ln = self._gap_soc_line(g0, g1, pa, pb, bs)
                if not ln:
                    continue
                d0_, d1_ = ln[4], ln[5]
                if d1_ <= d0_ or d1_ < x0 or d0_ > x1:
                    continue
                n_ = max(2, min(600, int((d1_ - d0_) // step_c)))
                gsg = [(d0_ + (d1_ - d0_) * i / n_, 0.0, 0.0, self._lin(ln, d0_ + (d1_ - d0_) * i / n_), 0.0, 0.0, 0)
                       for i in range(n_ + 1)]
                if show_bat_ribbon:
                    _ribbon(gsg, GAP_RIB_ALPHA)
                if show_bat_line:
                    cv.create_line(*[c for q in gsg for c in (X(q[0]), Ys(q[3]))], fill=BAT_LINE, width=1.5,
                                   dash=MESH_DASH, tags=("bat",))
        cv.tag_raise("bat")   # cinta y linea de bateria POR ENCIMA DE TODO (columnas del sol, malla, curvas); las etiquetas se dibujan despues

        def _tag(px, py, text, colm, up):   # etiqueta con recuadro de fondo (legible sobre cualquier capa)
            yy = min(max(py + (-14 if up else 14), T + 8), H - B - 8)
            it = cv.create_text(px, yy, text=text, fill=colm, font=("Consolas", 8, "bold"))
            bb = cv.bbox(it)
            if bb:
                cv.create_rectangle(bb[0] - 3, bb[1] - 1, bb[2] + 3, bb[3] + 1, fill=SKY_BG, outline=colm)
                cv.tag_raise(it)

        # valores de bateria: uno a las BAT_TAG_HOURS[0] h y otro a las BAT_TAG_HOURS[1] h (de toda la jornada, no cambian con el zoom)
        if show_bat_marks:
            allmk = []
            for hh in BAT_TAG_HOURS:
                c_ = [q for q in pts if len(q) > 7 and q[7] >= 0 and abs(q[0] + bs / 2.0 - hh * 3600.0) <= 600]
                if c_:
                    q = min(c_, key=lambda q_: abs(q_[0] + bs / 2.0 - hh * 3600.0))
                    allmk.append((q[0] + bs / 2.0, q[7], BAT_AC if (len(q) > 8 and q[8] > AC_CHG_W) else BAT_LINE))
            for mx, ms, mst in allmk:   # el valor mas alto con su etiqueta arriba y el mas bajo abajo
                if x0 <= mx <= x1:
                    _tag(X(mx), Ys(ms), f"{ms:.0f}%", mst, ms >= min(q_[1] for q_ in allmk))
        win = [p for p in pts if PEAK_H0 * 3600 <= p[0] + bs / 2.0 <= PEAK_H1 * 3600]
        if len(win) >= 2:   # picos de potencia: maxima y minima de la jornada (sin amanecer ni atardecer)
            pmx = max(win, key=lambda q_: q_[1])
            # el minimo no cuenta con la bateria llena (100 %: la potencia cae a 0) ni por debajo de PEAK_MIN_W
            _mn = [q_ for q_ in win if q_[1] > PEAK_MIN_W and not (len(q_) > 7 and q_[7] >= FULL_SOC)]
            pmn = min(_mn, key=lambda q_: q_[1]) if _mn else None
            if pmn is not None and pmx[1] - pmn[1] >= 20:
                for pk, up, sym in ((pmx, True, "▲"), (pmn, False, "▼")):
                    px_t = pk[0] + bs / 2.0
                    if x0 <= px_t <= x1:
                        qx, qy = X(px_t), Y(pk[1])
                        cv.create_polygon(qx, qy - 5, qx + 5, qy, qx, qy + 5, qx - 5, qy, fill="#00f0ff", outline="#e0f2fe")
                        _tag(qx, qy, f"{sym} {pk[1]:.0f} W · {_fmt_hm(pk[0])}", "#00f0ff", up)
        if span <= 7200:   # con zoom fuerte: cada medicion (minuto a minuto)
            for p in vis:
                px, py = X(p[0] + bs / 2.0), Y(p[1])
                cv.create_oval(px - 2, py - 2, px + 2, py + 2, fill="#00f0ff", outline="#080b11")
        # ya NO hacemos tag_raise: el orden de dibujo ya coloca las curvas en su capa correcta
        y_leg = 33
        for _items, _grp, _with_cb in leg_groups:
            y_leg = self._build_legend(_items, y_leg, _grp, L, W - 6, with_checkbox=_with_cb)
        self._geo = {"mode": "Día", "X": X, "Y": Y, "T": T, "B": B, "H": H, "L": L, "R": R, "W": W, "bs": bs,
                     "pts": vis, "x0": x0, "x1": x1, "fx0": fx0, "fx1": fx1, "clear": curve_clear}
        self._summary_day(v, sun)
        self._draw_hover()

    # ---- modos Semana / Mes
    def _draw_bars(self):
        cv = self.cv
        cv.delete("all")
        self._clear_legend_widgets()   # limpia checkboxes anteriores
        W, H = cv.winfo_width(), cv.winfo_height()
        if W < 150 or H < 100:
            return
        n = 7 if self.mode == "Semana" else 30
        try:
            today = datetime.date.fromisoformat(self.tr.day) if self.tr.day else datetime.date.today()
        except Exception:
            today = datetime.date.today()
        days = [(today - datetime.timedelta(days=i)).isoformat() for i in range(n - 1, -1, -1)]
        summ = self.tr.summaries()
        rows = [(d, summ.get(d)) for d in days]
        rows = [(d, r) for d, r in rows if (r and (r.get("wh", 0) > 0 or r.get("pot", 0) > 0)) or d == days[-1]] or rows   # sin dias vacios
        n = len(rows)
        L, R, B = 50, 14, 34
        bar_leg = [("#fbbf24", None, "potencial despejado"), ("#a3e635", (3, 2), "no aprovechado (supuesto, batería llena)"),
                   ("#64748b", None, "color de la barra = estado predominante")]
        T = max(46, self._build_legend(bar_leg, 22, "bars", L, W - 6, dry=True) + 11)   # alto reservado si la leyenda ocupa mas filas
        vals = [(r["wh"] / 1000.0 if r else 0.0) for _, r in rows]
        pots = [(r["pot"] / 1000.0 if r else 0.0) for _, r in rows]
        ners = [(r.get("ner", 0.0) / 1000.0 if r else 0.0) for _, r in rows]
        ymax = math.ceil(max([a + b for a, b in zip(vals, ners)] + pots + [1.0]) * 1.1 * 2) / 2.0
        slot = (W - L - R) / float(n)
        bw = min(slot * 0.7, 70.0)

        def Y(v):
            return T + (1.0 - v / ymax) * (H - T - B)

        step = 0.5 if ymax <= 3 else (1.0 if ymax <= 6 else 2.0)
        g = 0.0
        while g <= ymax + 1e-9:
            cv.create_line(L, Y(g), W - R, Y(g), fill=SKY_GRID)
            cv.create_text(L - 6, Y(g), text=f"{g:g}", fill="#64748b", anchor="e", font=("Consolas", 8))
            g += step
        cv.create_text(L - 6, T - 10, text="kWh", fill="#64748b", anchor="e", font=("Consolas", 8, "bold"))
        # Visibilidad para modo barras
        show_bars = self._is_visible("bars", "color de la barra = estado predominante")
        show_pot = self._is_visible("bars", "potencial despejado")
        show_ner = self._is_visible("bars", "no aprovechado (supuesto, batería llena)")
        bars = []
        for i, (d, r) in enumerate(rows):
            xc = L + (i + 0.5) * slot
            dom = _dominant(r["st"]) if r else None
            col = STATES[dom][2] if dom else "#334155"
            v = vals[i]
            if show_bars and v > 0:
                cv.create_rectangle(xc - bw / 2, Y(v), xc + bw / 2, Y(0), fill=col, outline="")
                if show_ner and ners[i] > 0.005:   # energia no aprovechada por bateria llena (estimada)
                    cv.create_rectangle(xc - bw / 2, Y(v + ners[i]), xc + bw / 2, Y(v), fill="#2a3b12", outline="#a3e635")
                if n <= 7:
                    cv.create_text(xc, Y(v + ners[i]) - 8, text=f"{v:.2f}", fill="#e2e8f0", font=("Consolas", 8, "bold"))
            if show_pot and pots[i] > 0:
                cv.create_line(xc - bw / 2, Y(pots[i]), xc + bw / 2, Y(pots[i]), fill="#fbbf24", width=2)
            dd = datetime.date.fromisoformat(d)
            if n <= 7:
                cv.create_text(xc, H - B + 12, text=f"{DOW[dd.weekday()]} {d[8:]}/{d[5:7]}", fill="#94a3b8", font=("Consolas", 8))
            elif i % 5 == 0 or i == n - 1:
                cv.create_text(xc, H - B + 12, text=f"{d[8:]}/{d[5:7]}", fill="#94a3b8", font=("Consolas", 8))
            bars.append((xc, d, r))
        self._build_legend(bar_leg, 22, "bars", L, W - 6)
        self._geo = {"mode": self.mode, "bars": bars, "slot": slot, "T": T, "B": B, "H": H}
        self._summary_bars(rows, summ)
        self._draw_hover()

    # ---- hover
    def _draw_hover(self):
        cv = self.cv
        cv.delete("hover")
        g = self._geo
        if g is None or self._hx is None:
            self.lbl_hover.configure(text=HINT_DAY if self.mode == "Día" else HINT_BARS)
            return
        if g["mode"] != "Día":
            if not g["bars"]:
                return
            xc, d, r = min(g["bars"], key=lambda q: abs(q[0] - self._hx))
            cv.create_rectangle(xc - g["slot"] / 2, g["T"], xc + g["slot"] / 2, g["H"] - g["B"],
                                outline="#e2e8f0", tags="hover")
            if not r:
                self.lbl_hover.configure(text=f"{_dmy(d)} · sin datos")
                return
            dom = _dominant(r["st"])
            pct = f" · {r['act'] / r['pot'] * 100:.0f}% del potencial despejado" if r.get("pot", 0) > 0 else ""
            ner = f" · no aprovechado (supuesto, batería llena) ≈ {_fmt_e(r['ner'])}" if r.get("ner", 0) > 1 else ""
            if r.get("full", 0) > 0:
                ner += f" · batería ≥{FULL_SOC}% {r['full'] // 60}h{r['full'] % 60:02d}m"
            self.lbl_hover.configure(
                text=f"{_dmy(d)} · {r['wh'] / 1000.0:.2f} kWh · pico {r['pk']:.0f} W{pct}{ner} · "
                     f"predomina: {state_label(dom, int(d[5:7])) if dom else '—'}")
            return
        if not g["pts"]:
            self.lbl_hover.configure(text=HINT_DAY)
            return
        X, Y, bs = g["X"], g["Y"], g["bs"]
        p = min(g["pts"], key=lambda q: abs(X(q[0] + bs / 2.0) - self._hx))
        px = X(p[0] + bs / 2.0)
        cv.create_line(px, g["T"], px, g["H"] - g["B"], fill="#e2e8f0", tags="hover")
        cv.create_oval(px - 4, Y(p[1]) - 4, px + 4, Y(p[1]) + 4, outline="#e2e8f0", fill="#00f0ff", tags="hover")
        month = int(self.day[5:7]) if len(self.day) >= 7 else 1
        name = state_label(STATE_KEYS[int(p[4])], month)
        _b = _bat_state(p)
        # "Esperado" = el de la curva amarilla que se ve (referencia aprendida ACTUAL), no el que se guardo en ese
        # instante (la referencia se corrige despues con los datos y la curva se recalcula entera, p[2] no)
        exp_w = p[2]
        _cc = g.get("clear") or []
        if len(_cc) >= 2:
            _t = p[0] + bs / 2.0
            _i = min(max(int(round((_t - _cc[0][0]) / max(1.0, _cc[1][0] - _cc[0][0]))), 0), len(_cc) - 1)
            if abs(_cc[_i][0] - _t) <= 150 and _cc[_i][1] > 0:
                exp_w = _cc[_i][1]
        pct = f" ({p[1] / exp_w * 100:.0f}%)" if exp_w > 0 else ""
        est_src = p[11] if len(p) > 11 else 0
        est_label = "protección DC" if est_src == 1 else ("batería llena" if est_src == 2 else "")
        est = f" · estimado ({est_label}) ≈ {p[6]:.0f} W" if len(p) > 6 and p[6] > p[1] + 10 and est_src else \
              (f" · supuesto con batería llena ≈ {p[6]:.0f} W" if len(p) > 6 and p[6] > p[1] + 10 else "")
        sub = p[12] if len(p) > 12 else ""
        sub_label = ""
        if sub:
            sub_map = {
                "cumulus_fast": "Cúmulos de paso rápido",
                "stratus_low": "Estratos bajos / Nimbostratus",
                "altostratus": "Altostratus / Velo medio",
                "cumulonimbus": "Cumulonimbus / Tormenta",
                "haze_saharan": "Polvo Sahariano",
                "haze_humid": "Velo por humedad",
                "neblina": "Neblina matinal",
                "niebla": "Niebla matinal",
            }
            sub_label = f" · {sub_map.get(sub, sub)}"
        bat = (f" · batería {p[7]:.0f}%" if len(p) > 7 and p[7] >= 0 else "") + (f" ({BAT_NAME[_b]}" + (f", {p[10]:+.0f} W)" if len(p) > 10 else ")") if _b else "")
        ac = f" · carga AC {p[8]:.0f} W" if len(p) > 8 and p[8] > AC_CHG_W else ""
        amp = (f" · {p[9]:.1f} A" + (" ⚠" if p[9] > HIGH_A else "")) if len(p) > 9 and p[9] > 0 else ""
        self.lbl_hover.configure(
            text=f"{_fmt_hm(p[0])} · {name}{sub_label} · {p[1]:.0f} W{pct} de {exp_w:.0f} W esperados{est}{bat}{ac}{amp} · "
                 f"cable ≈ {p[3]:.0f} W · acumulado {_fmt_e(p[5])}")

    # ---- resumenes
    def _summary_day(self, v, sun):
        pts, bs = v["pts"], v["bin"]
        month = int(self.day[5:7]) if len(self.day) >= 7 else 1
        k = bs / 3600.0
        L = [f"Día {_dmy(self.day)}" + (f" · ahora: {state_label(self.tr.state, month)}" if self.day == self.tr.day else "")]
        if sun:
            dl = int(sun[1] - sun[0]) // 60
            L.append(f"Sol: sale {_fmt_hm(sun[0])} · se pone {_fmt_hm(sun[1])} (±{int(SUN_ERR_S // 60)} min) · {dl // 60} h {dl % 60:02d} min de día "
                     f"({LAT_DEG:.3f}°N, {abs(LON_DEG):.3f}°O)")
        L.append(f"Energía entregada al equipo: {_fmt_e(v['wh_in'])} · estimada en el panel: {_fmt_e(v['wh_panel'])}")
        if pts:
            e_w = sum(p[1] for p in pts) * k
            e_x = sum(p[2] for p in pts) * k
            e_l = sum(p[3] for p in pts) * k
            e_n = sum(max((p[6] if len(p) > 6 else p[1]) - p[1], 0.0) for p in pts) * k
            pk = max(pts, key=lambda q: q[1])
            L.append(f"Pico: {pk[1]:.0f} W a las {_fmt_hm(pk[0])} · pérdida en cable (14 AWG, {CABLE_RUN_M:.0f} m): "
                     f"{_fmt_e(e_l)} (máx {max(p[3] for p in pts):.0f} W)")
            if e_x > 0:
                L.append(f"Frente al potencial despejado del tiempo monitoreado ({_fmt_e(e_x)}): {e_w / e_x * 100:.0f} %")
            socs = [p[7] for p in pts if len(p) > 7 and p[7] >= 0]
            if socs:
                fm = sum(1 for p in pts if len(p) > 7 and p[7] >= FULL_SOC) * bs // 60
                L.append(f"Batería: mín {min(socs):.0f}% · máx {max(socs):.0f}% · con ≥{FULL_SOC}% (limita la carga) "
                         f"{fm // 60}h{fm % 60:02d}m")
            am = sum(1 for p in pts if len(p) > 8 and p[8] > AC_CHG_W) * bs // 60
            hm = sum(1 for p in pts if len(p) > 9 and p[9] > HIGH_A) * bs // 60
            if am or hm:
                L.append(f"Carga por AC: {am // 60}h{am % 60:02d}m · corriente solar > {HIGH_A:.0f} A: {hm // 60}h{hm % 60:02d}m")
            bm: Dict[str, int] = {}
            for p in pts:
                bk = _bat_state(p)
                if bk:
                    bm[bk] = bm.get(bk, 0) + max(1, bs // 60)
            if bm:
                L.append("Batería: " + " · ".join(f"{BAT_NAME[k]} {bm[k] // 60}h{bm[k] % 60:02d}m" for k in ("chg", "bal", "dis", "full") if k in bm))
            if e_n > 0.5:
                L.append(f"Con batería llena (supuesto: último rendimiento válido, no es una medición): "
                         f"no aprovechada ≈ {_fmt_e(e_n)} · producción potencial ≈ {_fmt_e(v['wh_in'] + e_n)}")
            mins: Dict[str, int] = {}
            for p in pts:
                kx = _sk(p[4])
                mins[kx] = mins.get(kx, 0) + max(1, bs // 60)
            tx = _states_text(mins, month)
            if tx:
                L.append("Tiempo por estado: " + tx)
        else:
            L.append("Sin puntos registrados este día (se guardan solo con el equipo conectado y sol).")
        L.append(f"Referencia despejada: {len(self.tr.data['slots'])} franjas de 15 min aprendidas "
                 f"(mejora sola con los días despejados; el polvo no la baja)")
        self.lbl_sum.configure(text="\n".join(L))

    def _summary_bars(self, rows, summ):
        have = [(d, r) for d, r in rows if r and r["wh"] > 0]
        n = len(rows)
        L = []
        if have:
            tot = sum(r["wh"] for _, r in have)
            best = max(have, key=lambda q: q[1]["wh"])
            worst = min(have, key=lambda q: q[1]["wh"])
            L.append(f"Últimos {n} días: total {_fmt_e(tot)} · promedio {_fmt_e(tot / len(have))}/día "
                     f"({len(have)} con datos)")
            L.append(f"Mejor día: {_dmy(best[0])} ({_fmt_e(best[1]['wh'])}) · peor: {_dmy(worst[0])} ({_fmt_e(worst[1]['wh'])})")
            act = sum(r.get("act", 0) for _, r in have)
            pot = sum(r.get("pot", 0) for _, r in have)
            ner = sum(r.get("ner", 0) for _, r in have)
            L.append(f"Estimada en el panel: {_fmt_e(sum(r['whp'] for _, r in have))} · pérdida en cable: "
                     f"{_fmt_e(sum(r.get('lw', 0) for _, r in have))}"
                     + (f" · {act / pot * 100:.0f} % del potencial despejado" if pot > 0 else "")
                     + (f" · no aprovechado (supuesto) ≈ {_fmt_e(ner)}" if ner > 1 else ""))
            mins: Dict[str, int] = {}
            for _, r in have:
                for k, m in r["st"].items():
                    mins[k] = mins.get(k, 0) + m
            tx = _states_text(mins, datetime.date.today().month)
            if tx:
                L.append("Tiempo por estado: " + tx)
        else:
            L.append(f"Sin datos en los últimos {n} días.")
        if self.mode == "Mes" and have:
            wk: Dict[str, float] = {}
            for d, r in have:
                iso = datetime.date.fromisoformat(d).isocalendar()
                key = f"S{iso[1]:02d}/{iso[0]}"
                wk[key] = wk.get(key, 0.0) + r["wh"]
            L.append("Semanas: " + " · ".join(f"{k} {_fmt_e(v)}" for k, v in wk.items()))
        mo: Dict[str, List[float]] = {}
        for d, r in summ.items():
            if r and r["wh"] > 0:
                a = mo.setdefault(d[:7], [0.0, 0])
                a[0] += r["wh"]
                a[1] += 1
        if mo:
            L.append("Meses: " + " · ".join(f"{k[5:7]}/{k[:4]} {_fmt_e(a[0])} ({int(a[1])} d)" for k, a in sorted(mo.items())[-6:]))
        self.lbl_sum.configure(text="\n".join(L))


# [icon-patch] Iconos dibujados a mano en Canvas (nitidos a cualquier escala; no dependen de emojis/fuentes)
_ICON_BASE = {"panel": (20, 14), "tower": (20, 14), "plug": (20, 14), "battery": (20, 14), "sun": (20, 20)}


def _icon_mix(c: str, bg: str, t: float) -> str:
    """Mezcla el color c con el fondo bg (t = parte de bg)."""
    a = [int(c[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(bg[i:i + 2], 16) for i in (1, 3, 5)]
    return "#%02x%02x%02x" % tuple(round(x * (1 - t) + y * t) for x, y in zip(a, b))


def _draw_icon_on(cv, kind: str, c: str, sc: float, bg: str = "#0e141f"):
    bw, bh = _ICON_BASE[kind]
    w, h = max(12, round(bw * sc)), max(9, round(bh * sc))
    cv.configure(width=w, height=h)
    cv.delete("all")
    lw = max(1, round(sc))
    dark = _icon_mix(c, bg, 0.78)

    def P(x, y):
        return (x * w / float(bw), y * h / float(bh))

    def L(*pts, **kw):
        kw.setdefault("fill", c)
        kw.setdefault("width", lw)
        kw.setdefault("capstyle", "round")
        cv.create_line(*[v for p in pts for v in P(*p)], **kw)

    def poly(pts, **kw):
        cv.create_polygon(*[v for p in pts for v in P(*p)], **kw)

    if kind == "panel":
        poly([(4, 1), (19, 1), (16, 10), (1, 10)], fill="#1e3a8a", outline=c, width=lw)
        for t in (1 / 3.0, 2 / 3.0):
            L((4 + 15 * t, 1), (1 + 15 * t, 10), width=1)
        L((2.5, 5.5), (17.5, 5.5), width=1)
        L((9.5, 10), (9.5, 13))
        L((6, 13), (13, 13))
    elif kind == "tower":      # torre de alta tension
        L((10, 0.3), (10, 2))
        L((9, 2), (7, 13.5))
        L((11, 2), (13, 13.5))
        L((9, 2), (11, 2))
        L((5, 3.4), (15, 3.4))
        L((3, 7), (17, 7))
        for x, y in ((5, 3.4), (15, 3.4), (3, 7), (17, 7)):      # aisladores
            L((x, y), (x, y + 2))
        L((8.2, 7), (12.4, 10), width=1)
        L((11.8, 7), (7.6, 10), width=1)
        L((7.6, 10), (12.4, 10), width=1)
        L((7.6, 10), (13, 13.5), width=1)
        L((12.4, 10), (7, 13.5), width=1)
        L((6.2, 13.5), (13.8, 13.5))
    elif kind == "plug":       # punta de enchufe (dos clavijas planas)
        poly([(6.6, 0.4), (8.8, 0.4), (8.8, 5), (6.6, 5)], fill=c, outline=c)
        poly([(11.2, 0.4), (13.4, 0.4), (13.4, 5), (11.2, 5)], fill=c, outline=c)
        poly([(4.5, 4.8), (15.5, 4.8), (15.5, 9.2), (13.2, 11.5), (6.8, 11.5), (4.5, 9.2)],
             fill=dark, outline=c, width=lw)
        L((10, 11.5), (10, 13.8), width=max(lw, round(1.6 * sc)))
    elif kind == "battery":    # bateria horizontal con borne y 3 barras
        poly([(2.2, 2.4), (15.8, 2.4), (17, 3.6), (17, 10.4), (15.8, 11.6), (2.2, 11.6), (1, 10.4), (1, 3.6)],
             fill=dark, outline=c, width=lw)
        poly([(17, 5), (19.3, 5), (19.3, 9), (17, 9)], fill=c, outline=c)
        for x0 in (3, 7.4, 11.8):
            poly([(x0, 4.6), (x0 + 3.2, 4.6), (x0 + 3.2, 9.4), (x0, 9.4)], fill=c, outline="")
    elif kind == "sun":        # sol: rayos alternos largos/cortos + disco con brillo
        for i in range(8):
            a = math.radians(i * 45.0)
            r1 = 9.6 if i % 2 == 0 else 8.3
            L((10 + 6.4 * math.cos(a), 10 + 6.4 * math.sin(a)), (10 + r1 * math.cos(a), 10 + r1 * math.sin(a)),
              width=max(lw, round(1.7 * sc)))
        cv.create_oval(*P(5.4, 5.4), *P(14.6, 14.6), fill="#fde047", outline=c, width=lw)
        cv.create_oval(*P(7.3, 7.3), *P(9.7, 9.7), fill="#fef9c3", outline="")


def _make_tiny_power_icon(size: int = 16):
    """Simbolo de encendido (IEC 5009) limpio: arco + barra con extremos redondeados, sin fondo.
    Se dibuja sobremuestreado (x8) y customtkinter lo reduce al tamano logico * zoom."""
    from PIL import Image, ImageDraw
    import customtkinter as ctk
    ss = 8
    S = size * ss
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    col = (254, 202, 202, 255)                 # mismo color que el texto del boton (#fecaca)
    lw = max(ss, round(S * 0.15))
    r = S * 0.37
    # caja del glifo: arriba = cy - r - 0.65*lw, abajo = cy + r  ->  se centra en el lienzo
    cx = S / 2
    cy = S / 2 + (r + 0.65 * lw - r) / 2
    # arco abierto por arriba (hueco centrado en las 12)
    d.arc((cx - r, cy - r, cx + r, cy + r), start=-50, end=230, fill=col, width=lw)
    def cap(x, y):                              # extremo redondeado
        d.ellipse((x - lw / 2, y - lw / 2, x + lw / 2, y + lw / 2), fill=col)
    for ang in (-50, 230):
        a = math.radians(ang)
        cap(cx + (r - lw / 2) * math.cos(a), cy + (r - lw / 2) * math.sin(a))
    # barra vertical
    y0, y1 = cy - r - lw * 0.15, cy - r * 0.05
    d.line((cx, y0, cx, y1), fill=col, width=lw)
    cap(cx, y0)
    cap(cx, y1)
    bb = img.getchannel("A").getbbox()
    if bb:                                       # recorta el aire lateral: el hueco con el texto lo marca solo customtkinter
        img = img.crop((bb[0], 0, bb[2], S))
    w = max(1, round(size * img.size[0] / S))
    return ctk.CTkImage(light_image=img, dark_image=img, size=(w, size))


class BluettiDesktopApp(ServerUiMixin, ctk.CTk):
    def __init__(self, start_minimized: bool = False):
        super().__init__()
        self._base_zoom = self._screen_base_zoom()   # zoom "normal" (ventana sin maximizar) segun la pantalla
        if abs(self._base_zoom - 1.0) > 0.001:
            ctk.set_widget_scaling(self._base_zoom)
            try:
                self._set_scaled_min_max()   # igual que en _apply_zoom: libera el min/max que fija customtkinter al escalar
            except Exception:
                pass
        if start_minimized:
            self.withdraw()   # arranque con Windows: sin que llegue a verse la ventana

        self.title("Bluetti Elite 100 V2 • Centro de Control & Gamer HUD")
        self.configure(fg_color=C_BG)
        self.resizable(True, True)
        # Icono de la ventana (barra de tareas / titulo) - funciona en dev y en .exe compilado
        try:
            import sys
            base_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            ico_path = os.path.join(base_dir, "bluetti.ico")
            try:
                self.iconbitmap(ico_path, default=ico_path)   # "default": si Windows recrea la ventana (cambio de pantalla) hereda este, no la pluma de Tk
            except TypeError:
                self.iconbitmap(ico_path)
        except Exception:
            pass
        self.after(400, lambda: self._apply_app_icon(force=True))
        self.bind("<Map>", lambda e: self.after(250, self._apply_app_icon) if e.widget is self else None, add="+")

        self.floating_widget: Optional[FloatingHudWidget] = None
        self.temp_alert = TempAlertTracker()   # compartido con el widget flotante
        self._tray = None
        self._tray_hidden = False
        self._pre_min_state = "normal"
        self.discovered_devices: List[Dict[str, str]] = []
        self.info_window: Optional[ctk.CTkToplevel] = None
        self.info_label: Optional[ctk.CTkLabel] = None
        self._last_t: Dict[str, Any] = {}
        self._dc_override = None   # (fuente, instante) tras un clic del usuario
        # --- zoom proporcional ---
        self._zoom = self._base_zoom     # factor de escala actual de los widgets
        self._base_size = (1, 1)         # tamaño (px) del contenido a zoom 1.0
        self._zoom_job = None
        self._last_wh = (0, 0)
        self._zoom_enabled = False
        self._last_cfg_ts = 0.0          # ultimo <Configure> de la ventana (mover/redimensionar)

        self._tele_latest = None         # ultima telemetria recibida (la ve la pantalla)
        self._tele_scheduled = False
        self._tele_last_ui = 0.0
        self.sky = SkyTracker()   # [sky-patch] estado del cielo / rendimiento del panel
        self._sky_win = None
        self.smart = SmartCharge(min_a=GRID_LIMIT_MIN_A, max_a=GRID_LIMIT_MAX_A)   # Carga inteligente (limita la AC si coincide con el solar)
        self._smart_win = None
        self._off_model = _OfflineModel()   # estimación offline de batería/tiempo (independiente del widget)
        self._app_was_conn = False
        self._app_last_mode = None
        self._app_mode_t = 0.0
        self._status_offline = False        # True = desconectado: se muestra OFFLINE_STATUS_TEXT
        self._offline_since = None          # instante en que se perdio la conexion (contador del mensaje)

        self.ble = BluettiBleManager(
            on_telemetry_callback=self._handle_telemetry,
            on_status_callback=self._handle_status,
            on_devices_discovered=self._handle_devices_discovered,
            poll_interval=1.5
        )

        self._build_app_structure()
        self._fit_window()
        self.after(500, self._enable_zoom)
        self.after(1600, self._start_screen_poll)  # arranque tras primer zoom
        self.after(1000, self._offline_tick)
        self.after(700, self._redraw_all)
        self.after(2500, self._redraw_all)

        self.ble.start()
        self.protocol("WM_DELETE_WINDOW", self._on_window_close)
        self._setup_tray()
        self._server_init()
        if start_minimized:
            self.after(300, self._startup_minimized)
        else:
            self.after(800, self._restore_widget)   # el widget vuelve como se dejó (abierto/cerrado)

    def _apply_app_icon(self, force: bool = False):
        """Mantiene el icono de la app en la barra de tareas y la barra de titulo (Windows). Tk/Windows lo sustituyen
        por la pluma de Tk al cambiar la resolucion / DPI / monitor (se recrea la ventana). Se pone directamente con
        WM_SETICON (tamano segun el DPI actual) y se comprueba de forma barata; si ya no es el nuestro, se vuelve a poner."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes
            base_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            ico = os.path.join(base_dir, "bluetti.ico")
            if not os.path.exists(ico):
                return
            u32 = ctypes.windll.user32
            u32.SendMessageW.restype = ctypes.c_void_p
            u32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_void_p, ctypes.c_void_p]
            u32.LoadImageW.restype = ctypes.c_void_p
            u32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT, ctypes.c_int, ctypes.c_int, wintypes.UINT]
            u32.DestroyIcon.argtypes = [ctypes.c_void_p]
            u32.GetDpiForWindow.argtypes = [wintypes.HWND]
            u32.GetSystemMetricsForDpi.argtypes = [ctypes.c_int, ctypes.c_uint]
            hwnd = u32.GetParent(self.winfo_id()) or self.winfo_id()
            mine = getattr(self, "_app_icons", None)
            cur_big = u32.SendMessageW(hwnd, 0x007F, 1, 0)     # WM_GETICON, ICON_BIG
            cur_small = u32.SendMessageW(hwnd, 0x007F, 0, 0)   # ICON_SMALL
            if mine and not force and cur_big == mine[0] and cur_small == mine[1]:
                return
            try:
                dpi = int(u32.GetDpiForWindow(hwnd))
                cx_big, cx_small = u32.GetSystemMetricsForDpi(11, dpi), u32.GetSystemMetricsForDpi(49, dpi)   # SM_CXICON, SM_CXSMICON
            except Exception:
                cx_big, cx_small = u32.GetSystemMetrics(11), u32.GetSystemMetrics(49)
            big = u32.LoadImageW(None, ico, 1, cx_big, cx_big, 0x0010)       # IMAGE_ICON, LR_LOADFROMFILE
            small = u32.LoadImageW(None, ico, 1, cx_small, cx_small, 0x0010)
            if not big or not small:
                return
            u32.SendMessageW(hwnd, 0x0080, 1, big)     # WM_SETICON, ICON_BIG
            u32.SendMessageW(hwnd, 0x0080, 0, small)   # ICON_SMALL
            if mine:
                for h in mine:
                    try:
                        u32.DestroyIcon(h)
                    except Exception:
                        pass
            self._app_icons = (big, small)
        except Exception:
            pass

    def _set_window_icon(self, win):
        """Aplica el icono de la app a una ventana secundaria (Toplevel)."""
        try:
            import sys
            base_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            ico_path = os.path.join(base_dir, "bluetti.ico")
            win.iconbitmap(ico_path)
        except Exception:
            pass

    # ------------------------------------------------------------------
    def _win_scaling(self) -> float:
        try:
            return float(self._get_window_scaling())
        except Exception:
            return 1.0

    def _screen_base_zoom(self) -> float:
        """1.0 en Full HD o mayor; en pantallas menores, el factor que mantiene la fraccion de pantalla ocupada."""
        if not AUTO_FIT_SMALL_SCREENS:
            return 1.0
        try:
            ratio = min(self.winfo_screenwidth() / REF_SCREEN[0], self.winfo_screenheight() / REF_SCREEN[1])
            base = max(0.4, min(1.0, ratio / self._win_scaling()))
            # Boost del 20 % para pantallas menores que REF_SCREEN
            if ratio < 1.0:
                boosted = base * SMALL_SCREEN_BOOST
                # Limitar para que el contenido quepa en pantalla (95 % aprox)
                sc = self._win_scaling()
                max_w = int(self.winfo_screenwidth() * MAX_SCREEN_FRACTION / sc)
                max_h = int((self.winfo_screenheight() - 70 * sc) * MAX_SCREEN_FRACTION / sc)
                # _base_size se mide en _fit_window. Si aún es el valor inicial (1,1), saltamos el chequeo
                try:
                    bw, bh = self._base_size
                    if bw > 1 and bh > 1:
                        bw, bh = max(1, bw), max(1, bh)
                        max_boost_w = max_w / bw if bw > 0 else boosted
                        max_boost_h = max_h / bh if bh > 0 else boosted
                        boosted = min(boosted, max_boost_w, max_boost_h)
                except Exception:
                    pass
                return max(0.4, min(1.0, boosted))
            return base
        except Exception:
            return 1.0

    def _fit_window(self):
        """Mide el contenido a zoom 1.0 y ajusta la ventana a su tamaño natural."""
        try:
            if not self.winfo_exists():
                return
            self.update_idletasks()
            w = self.main_container.winfo_reqwidth()
            h = self.main_container.winfo_reqheight()
            if w <= 1 or h <= 1:
                return
            self._base_size = (max(1, w / self._zoom), max(1, h / self._zoom))
            sc = self._win_scaling()
            h = min(h, int(self.winfo_screenheight() - 70 * sc))
            self.geometry(f"{math.ceil(w / sc)}x{math.ceil(h / sc)}")
            zmin = min(ZOOM_MIN, self._base_zoom) if ZOOM_ON_MAXIMIZE else self._base_zoom   # sin zoom, nunca menos que el contenido
            self.minsize(math.ceil(self._base_size[0] * zmin / sc), math.ceil(self._base_size[1] * zmin / sc))
        except Exception:
            pass

    def _lock_edge_resize(self):
        """Impide redimensionar arrastrando bordes/esquinas (también maximizada). Maximizar y restaurar
        con el botón o doble clic en la barra siguen funcionando. Además escucha cambios de pantalla/DPI."""
        if sys.platform != "win32":
            return
        try:
            import ctypes
            from ctypes import wintypes
            u32 = ctypes.windll.user32
            hwnd = u32.GetParent(self.winfo_id()) or self.winfo_id()
            LRESULT = ctypes.c_ssize_t
            WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
            u32.GetWindowLongPtrW.restype = ctypes.c_void_p
            u32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
            u32.SetWindowLongPtrW.restype = ctypes.c_void_p
            u32.SetWindowLongPtrW.argtypes = [wintypes.HWND, wintypes.UINT, ctypes.c_void_p]
            u32.CallWindowProcW.restype = LRESULT
            u32.CallWindowProcW.argtypes = [ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
            old = u32.GetWindowLongPtrW(hwnd, -4)   # GWLP_WNDPROC

            # Flag thread-safe para solicitar recomprobación de pantalla en el mainloop
            self._screen_change_pending = False

            def proc(h, msg, wp, lp):
                if msg == 0x0112 and (wp & 0xFFF0) == 0xF000:   # WM_SYSCOMMAND SC_SIZE (Alt+Espacio > Tamaño)
                    return 0
                # WM_DISPLAYCHANGE (0x007E): resolución o profundidad de color
                # WM_DPICHANGED (0x02E0): cambio de DPI / monitor
                if msg in (0x007E, 0x02E0):
                    self._screen_change_pending = True
                r = u32.CallWindowProcW(old, h, msg, wp, lp)
                if msg == 0x0084 and 10 <= r <= 17:        # WM_NCHITTEST sobre un borde/esquina -> HTBORDER
                    return 18
                return r
            self._wndproc_ref = WNDPROC(proc)   # referencia viva: si se recolecta, la app se cierra
            u32.SetWindowLongPtrW(hwnd, -4, ctypes.cast(self._wndproc_ref, ctypes.c_void_p))

            # Procesar flag en el mainloop cada 200 ms (debounce incluido)
            def _poll_screen_change():
                try:
                    if getattr(self, "_screen_change_pending", False):
                        self._screen_change_pending = False
                        self._on_screen_change()
                except Exception:
                    pass
                self.after(200, _poll_screen_change)
            self.after(200, _poll_screen_change)

        except Exception:
            pass

    def _on_screen_change(self):
        """Reacciona a WM_DISPLAYCHANGE / WM_DPICHANGED: recalcula zoom base y reaplica si cambió."""
        try:   # el icono se pierde con el cambio de pantalla: se repone ya y de nuevo cuando Windows termina de reorganizar
            self._apply_app_icon(force=True)
            self.after(600, lambda: self._apply_app_icon(force=True))
        except Exception:
            pass
        try:
            new_base = self._screen_base_zoom()
            if abs(new_base - self._base_zoom) > 0.001:
                self._base_zoom = new_base
                # Reaplica zoom si no está en medio de una animación de maximizar/restaurar
                if not self._zoom_job and self._zoom_enabled:
                    self._fit_window()
                    self._apply_zoom()
                    self._redraw_all()
                # Ajusta posición si quedó fuera de pantalla
                self._clamp_to_screen()
        except Exception:
            pass

    def _start_screen_poll(self):
        """Sondeo de respaldo por si Tk no recibe los mensajes de Windows."""
        try:
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            scale = self._win_scaling()
            key = (sw, sh, round(scale, 3))
            if not hasattr(self, "_last_screen_key") or self._last_screen_key != key:
                self._last_screen_key = key
                self._on_screen_change()
            self._apply_app_icon()   # comprobacion barata: si el icono ya no es el nuestro, se repone
        except Exception:
            pass
        self.after(1500, self._start_screen_poll)

    def _clamp_to_screen(self):
        """Asegura que la ventana esté dentro de la pantalla visible."""
        try:
            sc = self._win_scaling()
            max_x = max(0, self.winfo_screenwidth() - self.winfo_width())
            max_y = max(0, self.winfo_screenheight() - self.winfo_height())
            x = max(0, min(self.winfo_x(), max_x))
            y = max(0, min(self.winfo_y(), max_y))
            if x != self.winfo_x() or y != self.winfo_y():
                self.geometry(f"+{x}+{y}")
        except Exception:
            pass

    def _enable_zoom(self):
        self._fit_window()
        self._zoom_enabled = True
        self.bind("<Configure>", self._on_configure)
        self._lock_edge_resize()

    def _on_configure(self, event):
        if event.widget is self:
            self._last_cfg_ts = time.time()   # mover/redimensionar: la telemetria espera a que pare
            try:
                st = self.state()
                if st in ("normal", "zoomed"):
                    self._pre_min_state = st   # para restaurar maximizada si se minimizó estando maximizada
            except Exception:
                pass
        if not ZOOM_ON_MAXIMIZE:
            return
        if event.widget is not self or not self._zoom_enabled:
            return
        # Mover la ventana también dispara <Configure>: solo importa si cambió el TAMAÑO
        # (maximizar / restaurar). Así arrastrarla no provoca ningún recálculo ni redibujado.
        key = (event.width, event.height)
        if key == self._last_wh:
            return
        self._last_wh = key
        if self._zoom_job is not None:
            self.after_cancel(self._zoom_job)
        self._zoom_job = self.after(ZOOM_SETTLE_MS, self._apply_zoom)   # espera a que cese la animación de maximizar/restaurar

    def _apply_zoom(self):
        """Ventana normal = tamaño original (el arrastre se deshace y vuelve a ese tamaño).
        Maximizada = zoom proporcional (menor factor ancho/alto), contenido centrado."""
        self._zoom_job = None
        changed = False
        hidden = False
        try:
            # Verificar que la ventana sigue viva y tiene handle válido
            if not self.winfo_exists():
                return
            W, H = self.winfo_width(), self.winfo_height()
            if W <= 1 or H <= 1:
                return
            bw, bh = self._base_size
            if bw <= 1 or bh <= 1:
                return
            was_zoomed = self.state() == "zoomed"
            z = max(min(ZOOM_MIN, self._base_zoom), min(ZOOM_MAX, min(W / bw, H / bh))) if was_zoomed else self._base_zoom
            if abs(z - self._zoom) > 0.01:
                changed = True
                self._zoom = z
                self._zoom_enabled = False
                # Se oculta el contenido mientras customtkinter reescala todos los widgets: así no se
                # pintan los pasos intermedios (era lo que se veía como redibujado errático).
                try:
                    self.main_container.place_forget()
                    hidden = True
                except Exception:
                    pass
                ctk.set_widget_scaling(z)
                # customtkinter fija min=max=tamaño actual durante 1 s al escalar: se libera ya
                try:
                    self._set_scaled_min_max()
                except Exception:
                    pass
                self.update_idletasks()
                if was_zoomed and self.state() != "zoomed":
                    self.state("zoomed")
                self._zoom_enabled = True
            if not was_zoomed:
                sc = self._win_scaling()
                w = self.main_container.winfo_reqwidth()
                h = self.main_container.winfo_reqheight()
                if abs(w - W) > 2 or abs(h - H) > 2:
                    changed = True
                    self._zoom_enabled = False
                    self.geometry(f"{math.ceil(w / sc)}x{math.ceil(h / sc)}")
                    self.update_idletasks()
                    self._zoom_enabled = True
        except Exception:
            self._zoom_enabled = True
        self._last_wh = (self.winfo_width(), self.winfo_height())
        if hidden:
            try:
                self.main_container.place(relx=0.5, rely=0.5, anchor="center")
            except Exception:
                pass
        if changed or hidden:
            # Una sola pasada de redibujado al terminar (bordes y esquinas con sus medidas reales),
            # más una de repaso por si algún evento <Configure> llega tarde.
            try:
                self.update_idletasks()
            except Exception:
                pass
            self._redraw_all()
            # Repasos solo de los marcos redondeados (cápsulas, tarjetas): son los que se quedaban
            # con el borde incompleto si su último <Configure> llegaba tarde; son baratos.
            self.after(200, lambda: self._redraw_all(frames_only=True))
            self.after(600, lambda: self._redraw_all(frames_only=True))

    def _redraw_all(self, widget=None, frames_only: bool = False):
        """customtkinter dibuja cada widget con las dimensiones de su último evento
        <Configure>; si ese evento llega tarde o se pierde (zoom, maximizar), los bordes
        redondeados quedan incompletos. Se sincronizan las dimensiones reales y se redibuja."""
        root = widget if widget is not None else self
        relax = abs(float(getattr(self, "_zoom", 1.0) or 1.0) - 1.0) > 0.001
        try:
            for c in root.winfo_children():
                try:
                    # customtkinter dibuja las formas redondeadas con ancho/alto REDONDEADO A PARES;
                    # con el zoom (tamaños en píxeles impares) eso deja el borde sin cerrar. Se desactiva
                    # con zoom y se restaura al volver a 1.0.
                    eng = getattr(c, "_draw_engine", None) or getattr(c, "draw_engine", None)
                    fn = getattr(eng, "set_round_to_even_numbers", None)
                    if callable(fn):
                        fn(not relax)
                    if (hasattr(c, "_draw") and hasattr(c, "_current_width") and c.winfo_width() > 1
                            and (not frames_only or isinstance(c, ctk.CTkFrame))):
                        rev = getattr(c, "_reverse_widget_scaling", None)
                        if rev is not None:
                            c._current_width = rev(c.winfo_width())
                            c._current_height = rev(c.winfo_height())
                        c._draw()
                except Exception:
                    pass
                if not isinstance(c, ctk.CTkToplevel):
                    self._redraw_all(c, frames_only)
        except Exception:
            pass

    # =========================================================================
    # BANDEJA DEL SISTEMA / MINIMIZAR / ARRANQUE CON WINDOWS
    # =========================================================================
    def _setup_tray(self):
        if not TRAY_AVAILABLE:
            return
        tray = TrayController("Bluetti Elite 100 V2", is_startup_enabled=is_app_startup_enabled)
        if tray.start():
            self._tray = tray
            if HIDE_ON_MINIMIZE:
                self.bind("<Unmap>", self._on_unmap)
            self.after(200, self._tray_poll)

    def _on_unmap(self, event):
        if event.widget is not self or self._tray is None or self._tray_hidden:
            return
        self.after(30, self._maybe_hide_to_tray)

    def _maybe_hide_to_tray(self):
        try:
            if self.state() == "iconic":   # minimizada por el usuario -> a la bandeja
                self._hide_to_tray()
        except Exception:
            pass

    def _on_window_close(self):
        """La x de la ventana: con bandeja disponible solo oculta la app (sigue corriendo con su icono);
        se cierra de verdad con "Salir" del menú de la bandeja. Sin bandeja (pystray no instalado)
        cierra la app, para que no quede corriendo sin forma de abrirla."""
        if CLOSE_TO_TRAY and self._tray is not None:
            self._hide_to_tray()
        else:
            self._on_app_close()

    def _hide_to_tray(self):
        self._tray_hidden = True
        self.withdraw()   # withdraw = fuera de la barra de tareas

    def _show_main_window(self):
        """Trae la app al frente (desde la bandeja, el widget o si estaba minimizada)."""
        try:
            self._tray_hidden = False
            self.deiconify()
            self.state("zoomed" if self._pre_min_state == "zoomed" else "normal")
            self.lift()
            self.attributes("-topmost", True)               # Windows limita robar el foco: se fuerza un instante
            self.after(200, lambda: self.attributes("-topmost", False))
            self.focus_force()
        except Exception:
            pass

    def _open_main_window(self):
        """Callback del boton/menu "Abrir panel" del widget."""
        self._show_main_window()

    def _widget_exists(self) -> bool:
        try:
            return self.floating_widget is not None and bool(self.floating_widget.winfo_exists())
        except Exception:
            return False

    def _tray_poll(self):
        if self._tray is None:
            return
        try:
            while True:
                ev = self._tray.events.get_nowait()
                if ev == "show":
                    self._show_main_window()
                elif ev == "widget":
                    self._toggle_floating_widget()
                elif ev == "startup":
                    set_app_startup(not is_app_startup_enabled())
                elif ev == "quit":
                    self._on_app_close()
                    return
        except queue.Empty:
            pass
        except Exception:
            pass
        self._tray.widget_open = self._widget_exists()
        getattr(self._tray, "refresh", lambda: None)()
        self.after(200, self._tray_poll)

    def _startup_minimized(self):
        """Arranque con Windows (--minimized): widget visible + app en la bandeja."""
        self._restore_widget()
        if self._tray is not None:
            self._tray_hidden = True          # ya está oculta (withdraw en __init__)
        else:
            self.iconify()                    # sin pystray: minimizada en la barra de tareas

    # ---- Recordar si el widget estaba abierto (archivo aparte: widget_config.json lo reescribe el widget) ----
    @staticmethod
    def _widget_pref_path() -> str:
        base = os.path.dirname(getattr(_fw_mod, "_CONFIG_PATH", "")) or os.path.expanduser("~")
        return os.path.join(base, "app_state.json")

    def _load_widget_pref(self) -> bool:
        try:
            with open(self._widget_pref_path(), "r", encoding="utf-8") as f:
                return bool(json.load(f).get("widget_open", True))
        except Exception:
            return True   # primera vez / archivo ausente: el widget se abre

    def _save_widget_pref(self, is_open: bool):
        try:
            with open(self._widget_pref_path(), "w", encoding="utf-8") as f:
                json.dump({"widget_open": bool(is_open)}, f)
        except Exception:
            pass

    def _restore_widget(self):
        """Al arrancar: abre el widget si estaba abierto la última vez (por defecto, sí)."""
        try:
            if self._load_widget_pref() and not self._widget_exists():
                self._toggle_floating_widget()
        finally:
            self._widget_saved = self._widget_exists()
            self._widget_state_ready = True
            self.after(1000, self._widget_sync_loop)

    def _widget_sync_loop(self):
        """Cada segundo: guarda si el widget está abierto (cubre cerrarlo con la x, el botón o la
        bandeja) y mantiene bien el texto del botón."""
        if not getattr(self, "_widget_state_ready", False):
            return
        try:
            cur = self._widget_exists()
            if cur != self._widget_saved:
                self._widget_saved = cur
                self._save_widget_pref(cur)
            self._cfg(self.btn_switch_hud,
                      text="🎮 Cerrar Widget" if cur else "🎮 Abrir Widget")
            self.after(1000, self._widget_sync_loop)
        except Exception:
            pass

    def _on_app_close(self):
        try:
            fn = getattr(self, '_server_shutdown', None)   # apaga servidor o cliente de red
            if fn is not None:
                fn()
        except Exception:
            pass
        if getattr(self, "_widget_state_ready", False):
            self._save_widget_pref(self._widget_exists())   # estado real al salir
            self._widget_state_ready = False                # el cierre del widget que sigue no cuenta
        try:
            self.sky.save()   # [sky-patch]
        except Exception:
            pass
        if self.info_window is not None:
            try:
                self.info_window.destroy()
            except Exception:
                pass
        if self.floating_widget is not None and self.floating_widget.winfo_exists():
            self.floating_widget.close_widget()
        try:   # Carga inteligente activa: devolver el modo de carga AC anterior antes de cerrar
            for c in self.smart.restore_on_exit():
                self._smart_exec(c)
        except Exception:
            pass
        try:
            self.ble.stop()
        except Exception:
            pass
        try:
            self.sky._weather_provider.stop()   # para hilo de fetch meteorológico
        except Exception:
            pass
        if getattr(self, "_tray", None) is not None:
            self._tray.stop()
        self.destroy()

    def _build_app_structure(self):
        self.main_container = ctk.CTkFrame(self, fg_color="transparent")
        self.main_container.place(relx=0.5, rely=0.5, anchor="center")
        self._build_full_dashboard()

    # =========================================================================
    # DASHBOARD
    # =========================================================================
    def _sub_card(self, parent, title, title_color, border, val_color, val_font=16, icon=False):
        fr = ctk.CTkFrame(parent, fg_color=C_CARD, corner_radius=10, border_width=1, border_color=border)
        fr.pack(fill="x", padx=10, pady=5, ipady=2)
        if icon:   # icono dibujado a mano: True = panel; o "tower" | "plug" | "battery"
            hd = ctk.CTkFrame(fr, fg_color="transparent")
            hd.pack(anchor="w", padx=10, pady=(4, 0))
            _cv = tk.Canvas(hd, width=20, height=14, bg=C_CARD, highlightthickness=0)
            _cv.pack(side="left", padx=(0, 4))
            self._icon_register(_cv, "panel" if icon is True else icon, title_color)
            ctk.CTkLabel(hd, text=title, font=("Segoe UI", 10, "bold"), text_color=title_color).pack(side="left")
        else:
            ctk.CTkLabel(fr, text=title, font=("Segoe UI", 10, "bold"), text_color=title_color).pack(anchor="w", padx=10, pady=(4, 0))
        val = ctk.CTkLabel(fr, text="0 W", font=("Segoe UI", val_font, "bold"), text_color=val_color)
        val.pack(anchor="w", padx=10)
        return fr, val

    def _build_full_dashboard(self):
        f = self.main_container

        # 1. Barra superior [sky-patch], en DOS lineas:
        #    linea 1: titulo (izq)  |  botones BLE y Widget (der)
        #    linea 2: triangulo + estado de conexion/alertas + x (izq)  |  selector de dispositivo (der)
        top_bar = ctk.CTkFrame(f, fg_color=C_CARD, height=58, corner_radius=16, border_width=1, border_color=C_BORDER)
        top_bar.pack(fill="x", side="top", padx=16, pady=(12, 0))
        top_bar.columnconfigure(0, weight=1)

        ctk.CTkLabel(top_bar, text="⚡ BLUETTI ELITE 100 V2", font=("Segoe UI", 14, "bold"),
                     text_color="#00f0ff").grid(row=0, column=0, sticky="w", padx=(16, 0), pady=(7, 0))

        btns = ctk.CTkFrame(top_bar, fg_color="transparent")
        btns.grid(row=0, column=1, sticky="e", padx=(0, 10), pady=(7, 0))

        # Fuente común para los 3 botones (tamaño 10, bold) - auto-size al contenido
        BTN_FONT = ("Segoe UI", 10, "bold")
        BTN_H, BTN_R = 24, 8      # alto y radio comunes; width=0 -> cada boton mide lo que su contenido

        # Icono del boton apagar (IEC 5009). Tamano logico FIJO: customtkinter lo escala con el zoom igual que
        # el texto, asi nunca supera el alto del boton (antes se agrandaba a zoom bajo y se recortaba abajo).
        self._power_icon_img = _make_tiny_power_icon(BTN_H - 6)

        # Botón "Apagar Bluetti" - usa imagen tiny en pantallas pequeñas, emoji en grandes
        self.btn_system_off = ctk.CTkButton(
            btns, text="Apagar Bluetti", font=BTN_FONT,     # misma fuente que los otros: alinea icono y texto
            fg_color="#7f1d1d", hover_color="#991b1b", text_color="#fecaca",
            width=0, height=BTN_H, corner_radius=BTN_R, command=self._system_power_off,
            image=self._power_icon_img,
            compound="left")
        self.btn_system_off.pack(side="right", padx=(4, 0))

        self.btn_switch_hud = ctk.CTkButton(
            btns, text="🎮 Abrir Widget", font=BTN_FONT,
            fg_color="#1d4ed8", hover_color="#2563eb", text_color="#ffffff",
            width=0, height=BTN_H, corner_radius=BTN_R, command=self._toggle_floating_widget)
        self.btn_switch_hud.pack(side="right", padx=4)
        self.btn_release_ble = ctk.CTkButton(
            btns, text="⏏ Desconectar BLE", font=BTN_FONT,
            fg_color="#1e293b", hover_color="#334155", text_color="#cbd5e1",
            width=0, height=BTN_H, corner_radius=BTN_R, command=self._toggle_ble_release)
        self.btn_release_ble.pack(side="right", padx=4)

        status_box = ctk.CTkFrame(top_bar, fg_color="transparent")
        status_box.grid(row=1, column=0, sticky="w", padx=(16, 0), pady=(0, 6))
        # Triangulo de advertencia (mismo dibujo que el widget): parpadea junto al estado de la conexion
        # mientras haya alguna alerta activa. Ancho 0 (oculto) hasta que haga falta. Es un Canvas de tk
        # (no escala solo con el zoom de customtkinter): _alert_icon_redraw lo hace.
        self.alert_icon = tk.Canvas(status_box, width=0, height=18, bg=C_CARD, highlightthickness=0, cursor="hand2")
        self.alert_icon.pack(side="left", padx=0, pady=2)
        self._alert_icon_redraw()
        self.alert_icon.bind("<Button-1>", lambda e: self._on_alert_icon_click())
        self.lbl_status = ctk.CTkLabel(status_box, text="◌ Negociando Enlace Seguro ECDH...",
                                       font=("Segoe UI", 11, "bold"), text_color="#fbbf24")
        self.lbl_status.pack(side="left", padx=(0, 8))
        # x para cerrar avisos persistentes (AC Reducida / cuenta atras DC); se muestra solo cuando
        # hay alguno activo (ver _refresh_status_label)
        self.btn_status_close = ctk.CTkButton(
            status_box, text="×", width=26, height=22, fg_color="transparent", hover_color="#1e293b",
            text_color="#94a3b8", font=("Segoe UI", 14, "bold"), command=self._on_status_alert_close
        )

        dev_box = ctk.CTkFrame(top_bar, fg_color="transparent")
        dev_box.grid(row=1, column=1, sticky="e", padx=(0, 10), pady=(0, 6))
        ctk.CTkLabel(dev_box, text="Dispositivo:", font=("Segoe UI", 10), text_color="#94a3b8").pack(side="left", padx=4)
        self.combo_devices = ctk.CTkComboBox(
            dev_box, values=["DC:B4:D9:54:78:1A (Por Defecto)"], width=240, height=24,
            font=("Segoe UI", 10), command=self._on_device_selected)
        self.combo_devices.pack(side="left", padx=4)

        # 2. Barra de condiciones del cielo y produccion (clic en cada seccion = su ventana de detalle e historial)
        sky_bar = ctk.CTkFrame(f, fg_color=C_CARD, height=40, corner_radius=16, border_width=1, border_color=C_BORDER)
        sky_bar.pack(fill="x", padx=16, pady=(8, 0))
        sky_left = ctk.CTkFrame(sky_bar, fg_color="transparent")
        sky_left.pack(side="left", padx=(16, 0), pady=3)
        self._sky_icon = tk.Canvas(sky_left, width=20, height=20, bg=C_CARD, highlightthickness=0, cursor="hand2")
        self._sky_icon.pack(side="left", padx=(0, 6))
        self._icon_register(self._sky_icon, "sun", "#fbbf24")
        self.lbl_sky_title = ctk.CTkLabel(sky_left, text="Condiciones y Estadísticas:", font=("Segoe UI", 12, "bold"),
                                          text_color="#94a3b8", cursor="hand2")
        self.lbl_sky_title.pack(side="left", padx=(0, 6))
        self.lbl_sky_state = ctk.CTkLabel(sky_left, text="Determinando...", font=("Segoe UI", 12, "bold"),
                                          text_color="#64748b", cursor="hand2")
        self.lbl_sky_state.pack(side="left")
        self.lbl_sky_prod = ctk.CTkLabel(sky_bar, text="Producción: -- Wh (hoy)", font=("Segoe UI", 12, "bold"),
                                         text_color="#34d399", cursor="hand2")
        self.lbl_sky_prod.pack(side="right", padx=(0, 16), pady=3)
        for _w in (self._sky_icon, self.lbl_sky_title, self.lbl_sky_state):
            _w.bind("<Button-1>", lambda e: self._open_sky_window("Día"))
        self.lbl_sky_prod.bind("<Button-1>", lambda e: self._open_sky_window("Semana"))

        # 3. Tarjeta de flujo (Entrada | Batería (doble) | Salida)
        flow_card = ctk.CTkFrame(f, fg_color=C_CARD, corner_radius=16, border_width=1, border_color=C_BORDER)
        flow_card.pack(fill="x", padx=16, pady=(8, 6))

        grid3 = ctk.CTkFrame(flow_card, fg_color="transparent")
        grid3.pack(fill="x", padx=8, pady=8)
        # Proporción 10 : 15 : 10  -> la batería sigue siendo la más ancha, pero más compacta
        for col, wgt in ((0, 10), (1, 15), (2, 10)):
            grid3.columnconfigure(col, weight=wgt, uniform="flow")

        # ---- ENTRADA ----
        col_in = ctk.CTkFrame(grid3, fg_color=C_BG, corner_radius=14, border_width=1, border_color=C_BORDER)
        col_in.grid(row=0, column=0, padx=5, pady=2, sticky="nsew")
        ctk.CTkLabel(col_in, text="ENTRADA TOTAL", font=("Segoe UI", 10, "bold"), text_color="#94a3b8").pack(anchor="w", padx=14, pady=(10, 2))
        self.lbl_in_total_w = ctk.CTkLabel(col_in, text="0 W", font=("Segoe UI", 26, "bold"), text_color="#00f0ff")
        self.lbl_in_total_w.pack(anchor="w", padx=14)

        sub_solar, self.lbl_solar_val = self._sub_card(col_in, "Solar MPPT (Máx 1000W)", "#fbbf24", "#332a10", "#fde047", icon=True)
        self.lbl_solar_sub = ctk.CTkLabel(sub_solar, text="--V • --A", font=("Consolas", 13, "bold"), text_color="#fcd34d")
        self.lbl_solar_sub.pack(anchor="w", padx=10, pady=(0, 4))
        self._add_alert_row(sub_solar, "dc_in_current", "#fbbf24")  # [alertas-patch]

        sub_ac_in, self.lbl_ac_in_val = self._sub_card(col_in, "Red AC (Máx 1200W)", "#38bdf8", "#102035", "#60a5fa", icon="tower")
        self.lbl_ac_in_sub = ctk.CTkLabel(sub_ac_in, text="Sin red", font=("Consolas", 13, "bold"), text_color="#7dd3fc")
        self.lbl_ac_in_sub.pack(anchor="w", padx=10, pady=(0, 4))
        self._add_alert_row(sub_ac_in, "ac_in_voltage", "#38bdf8")  # [alertas-patch]
        ctk.CTkLabel(col_in, text="", height=4).pack()

        # ---- BATERÍA ----
        col_b = ctk.CTkFrame(grid3, fg_color=C_BG, corner_radius=14, border_width=1, border_color=C_BORDER)
        col_b.grid(row=0, column=1, padx=5, pady=2, sticky="nsew")
        self._build_battery_card(col_b)

        # ---- SALIDA ----
        col_out = ctk.CTkFrame(grid3, fg_color=C_BG, corner_radius=14, border_width=1, border_color=C_BORDER)
        col_out.grid(row=0, column=2, padx=5, pady=2, sticky="nsew")
        ctk.CTkLabel(col_out, text="SALIDA TOTAL", font=("Segoe UI", 10, "bold"), text_color="#94a3b8").pack(anchor="w", padx=14, pady=(10, 2))
        self.lbl_out_total_w = ctk.CTkLabel(col_out, text="0 W", font=("Segoe UI", 26, "bold"), text_color="#ff3366")
        self.lbl_out_total_w.pack(anchor="w", padx=14)

        sub_ac_out, self.lbl_ac_out_val = self._sub_card(col_out, "Inversor AC (1800W)", "#f87171", "#35101a", "#fb7185", icon="plug")
        self.lbl_ac_out_sub = ctk.CTkLabel(sub_ac_out, text="120V • --Hz", font=("Consolas", 13, "bold"), text_color="#fda4af")
        self.lbl_ac_out_sub.pack(anchor="w", padx=10, pady=(0, 4))
        self._add_alert_row(sub_ac_out, "ac_out_current", "#f87171")  # [alertas-patch]

        sub_dc_out, self.lbl_dc_out_val = self._sub_card(col_out, "Salidas DC (USB/12V)", "#c084fc", "#2d1035", "#d8b4fe", icon="battery")
        ctk.CTkLabel(sub_dc_out, text="USB-C 140W+100W • 12V 10A\n2×USB-A 15W", font=("Consolas", 10), text_color="#c4b5fd",
                     justify="left", anchor="w").pack(anchor="w", padx=10, pady=(0, 4))
        self._add_alert_row(sub_dc_out, "dc_out_battery", "#c084fc")  # [alertas-patch]

        # 4. Conmutadores
        ctrl_card = ctk.CTkFrame(f, fg_color=C_CARD, corner_radius=16, border_width=1, border_color=C_BORDER)
        ctrl_card.pack(fill="x", padx=16, pady=(0, 12))

        ctrl_head = ctk.CTkFrame(ctrl_card, fg_color="transparent")
        ctrl_head.pack(fill="x", padx=16, pady=(6, 2))
        ctk.CTkLabel(ctrl_head, text="⚙️ CONTROL DE ACCESO FUNCIONES AVANZADAS", font=("Segoe UI", 11, "bold"), text_color="#00f0ff").pack(side="left")
        self._build_server_controls(ctrl_head)

        sw_grid = ctk.CTkFrame(ctrl_card, fg_color="transparent")
        sw_grid.pack(fill="x", padx=12, pady=(0, 2))
        for c in range(5):
            sw_grid.columnconfigure(c, weight=1, uniform="sw")

        def make_switch(col, label, cmd, color):
            fr = ctk.CTkFrame(sw_grid, fg_color=C_BG, corner_radius=12, border_width=1, border_color=C_BORDER)
            fr.grid(row=0, column=col, padx=4, pady=2, sticky="nsew")
            ctk.CTkLabel(fr, text=label, font=("Segoe UI", 11, "bold"), text_color="#ffffff").pack(pady=(5, 0), padx=10)
            sw = ctk.CTkSwitch(fr, text="", width=40, command=cmd, progress_color=color)   # width=40: sin texto, el boton queda centrado bajo su etiqueta
            sw.pack(pady=(1, 6))
            return sw

        self.sw_ac = make_switch(0, "Salida CA (1800W)", self._on_toggle_ac, "#ff3366")
        self.sw_dc = make_switch(1, "Salida CC (12V/USB)", self._on_toggle_dc, "#00f0ff")
        self.sw_eco = make_switch(2, "Modo ECO", self._on_toggle_eco, "#00ff88")
        self.sw_grid = make_switch(3, "Refuerzo Red", self._on_toggle_grid, "#f59e0b")
        self.sw_pl = make_switch(4, "Power Lifting (2700W)", self._on_toggle_pl, "#a855f7")

        # Fuente DC y modo de carga AC en una sola ventana, separados por una raya; selectores del tamaño
        # de los botones de la barra superior (alto 26, letra 10)
        mode_frame = ctk.CTkFrame(ctrl_card, fg_color=C_BG, corner_radius=12, border_width=1, border_color=C_BORDER)
        mode_frame.pack(fill="x", padx=16, pady=(4, 10))

        def mode_btn(parent, text, width, cmd, active=False):
            return ctk.CTkButton(parent, text=text, width=width, height=26, corner_radius=10, font=("Segoe UI", 10),
                                 fg_color="#1d4ed8" if active else "#1e293b",
                                 hover_color="#2563eb" if active else "#334155", command=cmd)

        row_dc = ctk.CTkFrame(mode_frame, fg_color="transparent")
        row_dc.pack(fill="x", pady=(7, 5))
        ctk.CTkLabel(row_dc, text="☀ ENTRADA DC (FUENTE):", width=190, anchor="w", font=("Segoe UI", 10, "bold"),
                     text_color="#94a3b8").pack(side="left", padx=(14, 0))
        self.btn_dc_other = mode_btn(row_dc, "Baterías (Máx 3A)", 128, lambda: self._set_dc_source("other"))
        self.btn_dc_other.pack(side="left", padx=5)
        self.btn_dc_pv = mode_btn(row_dc, "Panel Solar", 128, lambda: self._set_dc_source("pv"))
        self.btn_dc_pv.pack(side="left", padx=5)
        # Carga inteligente: entre «Panel Solar» y «Alta corriente» (switch verde + ⚙ con sus opciones)
        self.sw_smart = ctk.CTkSwitch(row_dc, text="Carga inteligente", font=("Segoe UI", 10, "bold"),
                                      command=self._on_toggle_smart, progress_color="#22c55e")
        self.sw_smart.pack(side="left", padx=(16, 0))
        if self.smart.cfg.get("enabled"):
            self.sw_smart.select()
        ctk.CTkButton(row_dc, text="⚙", width=28, height=26, corner_radius=10, font=("Segoe UI", 12),
                      fg_color="#1e293b", hover_color="#334155",
                      command=self._open_smart_window).pack(side="left", padx=(6, 0))
        self.sw_high_current =ctk.CTkSwitch(row_dc, text="Alta corriente", font=("Segoe UI", 10, "bold"),
                                             command=self._on_toggle_high_current, progress_color="#fbbf24", state="disabled")
        self.sw_high_current.pack(side="right", padx=14)

        tk.Frame(mode_frame, height=1, bg="#334155").pack(fill="x", padx=14)   # raya divisoria

        row_ac = ctk.CTkFrame(mode_frame, fg_color="transparent")
        row_ac.pack(fill="x", pady=(5, 7))
        ctk.CTkLabel(row_ac, text="⚡ MODO DE CARGA (RED AC):", width=190, anchor="w", font=("Segoe UI", 10, "bold"),
                     text_color="#94a3b8").pack(side="left", padx=(14, 0))
        self.btn_charge_silent = mode_btn(row_ac, "Silencioso (≤600W)", 128, lambda: self._set_charge_mode("silent"))
        self.btn_charge_silent.pack(side="left", padx=5)
        self.btn_charge_standard = mode_btn(row_ac, "Estándar (600W)", 128, lambda: self._set_charge_mode("standard"), active=True)
        self.btn_charge_standard.pack(side="left", padx=5)
        self.btn_charge_turbo = mode_btn(row_ac, "Turbo (1200W)", 128, lambda: self._set_charge_mode("turbo"))
        self.btn_charge_turbo.pack(side="left", padx=5)
        self.btn_charge_custom = mode_btn(row_ac, "Personalizado (--A Máx)", 152, self._on_custom_click)
        self.btn_charge_custom.pack(side="left", padx=5)

    # ------------------------------------------------------------------
    def _build_battery_card(self, col_b):
        ctk.CTkLabel(col_b, text="BATERÍA LiFePO4 • +4000 ciclos de carga y 10 años de uso",
                     font=("Segoe UI", 10, "bold"), text_color="#94a3b8").pack(pady=(10, 0))

        self.lbl_soc_big = ctk.CTkLabel(col_b, text="--%", font=("Segoe UI", 34, "bold"), text_color="#94a3b8")
        self.lbl_soc_big.pack(pady=(0, 0))
        # Flechas de tendencia offline (se colocan junto al %, sin alterar el layout): derecha = quedó cargando,
        # izquierda = quedó descargando
        self.lbl_soc_arrow_r = ctk.CTkLabel(col_b, text="▲", font=("Segoe UI", 20, "bold"), text_color=OFFLINE_ARROW_COLOR)
        self.lbl_soc_arrow_l = ctk.CTkLabel(col_b, text="▼", font=("Segoe UI", 20, "bold"), text_color=OFFLINE_ARROW_COLOR)
        self._soc_arrow = None

        self.progress_soc_big = ctk.CTkProgressBar(col_b, height=9, corner_radius=5, progress_color="#334155", fg_color="#1e293b")
        self.progress_soc_big.pack(fill="x", padx=40, pady=(2, 5))
        self.progress_soc_big.set(0)

        self.lbl_capacity = ctk.CTkLabel(col_b, text=f"Capacidad nominal {RATED_WH} Wh • Salud: midiendo…",
                                         font=("Segoe UI", 10, "bold"), text_color="#cbd5e1")
        self.lbl_capacity.pack()

        status_row = ctk.CTkFrame(col_b, fg_color="transparent")
        status_row.pack(pady=(3, 5))
        self.lbl_net_power = ctk.CTkLabel(status_row, text="Flujo Neto: 0 W", font=("Segoe UI", 11, "bold"), text_color="#94a3b8")
        self.lbl_net_power.pack(side="left", padx=(0, 8))
        self.lbl_batt_time = ctk.CTkLabel(status_row, text="⏱ Calculando autonomía...", font=("Segoe UI", 12), text_color="#38bdf8")
        self.lbl_batt_time.pack(side="left")

        # ---- Banco de baterías ----
        bank = ctk.CTkFrame(col_b, fg_color=C_CARD, corner_radius=10, border_width=1, border_color="#1e293b")
        bank.pack(fill="x", padx=10, pady=(0, 10))

        head = ctk.CTkFrame(bank, fg_color="transparent")
        head.pack(fill="x", padx=8, pady=(6, 2))
        self.lbl_bank_title = ctk.CTkLabel(head, text="BANCO DE BATERÍAS --V", font=("Segoe UI", 10, "bold"), text_color="#00f0ff")
        self.lbl_bank_title.pack(side="left")
        self.lbl_bank_temp = ctk.CTkLabel(head, text="Temp. celdas --°C", font=("Segoe UI", 10, "bold"), text_color="#fb923c")
        self.lbl_bank_temp.pack(side="right")

        cells_grid = ctk.CTkFrame(bank, fg_color="transparent")
        cells_grid.pack(padx=6, pady=0)
        for c in range(4):
            cells_grid.columnconfigure(c, weight=1, uniform="cell")
        self.cell_boxes: List[ctk.CTkFrame] = []
        self.cell_labels: List[ctk.CTkLabel] = []
        for i in range(16):
            # recuadro mínimo: borde de 1 px, esquinas poco redondeadas, sin relleno extra
            box = ctk.CTkFrame(cells_grid, fg_color=C_BG, corner_radius=6, border_width=1, border_color="#26324a")
            box.grid(row=i // 4, column=i % 4, padx=2, pady=2, sticky="nsew")
            lbl = ctk.CTkLabel(box, text=self._cell_text(i + 1, None), font=("Consolas", 10, "bold"),
                               text_color="#cbd5e1", height=16)
            lbl.pack(padx=4, pady=3)  # el relleno debe superar al borde, si no la etiqueta lo tapa
            self.cell_boxes.append(box)
            self.cell_labels.append(lbl)

        summary = ctk.CTkFrame(bank, fg_color="transparent")
        summary.pack(fill="x", padx=8, pady=(4, 7))

        # Estado destacado (clicable) con el icono "i" DENTRO de la misma cápsula
        self.state_pill = ctk.CTkFrame(summary, fg_color=NEUTRAL_STYLE[2], corner_radius=11, border_width=1,
                                       border_color=NEUTRAL_STYLE[1], cursor="hand2")
        self.state_pill.pack(side="right", padx=(0, 2), pady=1)   # margen: que el borde no toque el límite del contenedor
        self.lbl_state = ctk.CTkLabel(self.state_pill, text=NEUTRAL_STYLE[0] + " ⓘ", font=("Segoe UI", 10, "bold"),
                                      text_color=NEUTRAL_STYLE[1], height=18, cursor="hand2")
        self.lbl_state.pack(side="left", padx=(9, 7), pady=3)
        for w in (self.state_pill, self.lbl_state):
            w.bind("<Button-1>", lambda e: self._toggle_info_window())

        self.lbl_cell_summary = ctk.CTkLabel(
            summary, text="Min(-)--V • Max(-)--V • Dif --mV", font=("Consolas", 9, "bold"), text_color="#94a3b8")
        self.lbl_cell_summary.pack(side="left")

    def _set_state_pill(self, text: str, fg: str, bg: str):
        self._cfg(self.state_pill, fg_color=bg, border_color=fg)
        self._cfg(self.lbl_state, text=f"{text} ⓘ", text_color=fg)

    @staticmethod
    def _cell_text(idx: int, mv: Optional[int]) -> str:
        if mv is None:
            return f"({idx})-.{'-' * CELL_DECIMALS}V"
        return f"({idx}){mv / 1000.0:.{CELL_DECIMALS}f}V"

    # =========================================================================
    # VENTANA FLOTANTE DE INFORMACIÓN DEL BANCO
    # =========================================================================
    def _center_on_app(self, win):
        """Centra una ventana emergente sobre la ventana principal (en pixeles reales)."""
        win.update_idletasks()
        try:
            sc = float(win._get_window_scaling())
        except Exception:
            sc = 1.0
        pw, ph = win.winfo_reqwidth(), win.winfo_reqheight()
        x = self.winfo_rootx() + (self.winfo_width() - pw) // 2
        y = self.winfo_rooty() + (self.winfo_height() - ph) // 2
        x = max(0, min(x, self.winfo_screenwidth() - pw))
        y = max(0, min(y, self.winfo_screenheight() - ph))
        win.geometry("%dx%d+%d+%d" % (math.ceil(pw / sc), math.ceil(ph / sc), x, y))
        win.lift()

    def _toggle_info_window(self):
        if self.info_window is not None and self.info_window.winfo_exists():
            self.info_window.destroy()
            self.info_window = None
            self.info_label = None
            return
        win = ctk.CTkToplevel(self)
        win.title("Detalle del banco de baterías")
        win.configure(fg_color=C_CARD)
        win.resizable(False, False)
        win.transient(self)
        self._set_window_icon(win)
        win.protocol("WM_DELETE_WINDOW", self._toggle_info_window)
        ctk.CTkLabel(win, text="ℹ DETALLE DEL BANCO DE BATERÍAS", font=("Segoe UI", 12, "bold"),
                     text_color="#00f0ff").pack(anchor="w", padx=18, pady=(14, 4))
        self.info_label = ctk.CTkLabel(win, text="", font=("Consolas", 11), text_color="#cbd5e1",
                                       justify="left", anchor="w", wraplength=520)
        self.info_label.pack(anchor="w", padx=18, pady=(0, 10))
        ctk.CTkButton(win, text="Cerrar", width=90, height=28, fg_color="#1e293b", hover_color="#334155",
                      command=self._toggle_info_window).pack(pady=(0, 14))
        self.info_window = win
        self._refresh_info_window(self._last_t)
        self._center_on_app(win)

    @staticmethod
    def _temp1(x: float) -> str:
        """Un decimal, redondeando .x5 hacia arriba (40.25 -> 40.3)."""
        return f"{math.floor(x * 10 + 0.5) / 10:.1f}"

    @staticmethod
    def _fmt(v, spec="", suffix="", none="--"):
        if v is None:
            return none
        return f"{format(v, spec)}{suffix}"

    def _info_text(self, t: Dict[str, Any]) -> str:
        soc = t.get("soc")
        cells = t.get("cell_mv")
        L: List[str] = []

        # --- Equilibrio ---
        L.append("EQUILIBRIO DE CELDAS")
        if cells:
            delta = t.get("cell_delta_mv")
            L.append(f"  Diferencia máx-mín : {delta} mV")
            L.append(f"  Voltaje medio celda: {self._fmt(t.get('cell_avg_mv') and t['cell_avg_mv'] / 1000, '.3f', ' V')}")
            zone = t.get("cell_zone", "plateau")
            lim = t.get("cell_balance_limits")
            g = GRADE_STYLE.get(t.get("cell_grade"), ("--",))[0]
            L.append(f"  Equilibrio         : {g}")
            L.append(f"  Zona de la curva   : {ZONE_NAME.get(zone, zone)}")
            if lim:
                L.append(f"  Límites (mV)       : óptimo ≤{lim[0]} · bueno ≤{lim[1]} · aceptable ≤{lim[2]}")
                L.append(f"                       regular ≤{lim[3]} · malo >{lim[3]}")
            pk, tr = t.get("cell_peak_mv"), t.get("cell_trough_mv")
            if pk and tr:
                L.append(f"  Sesión             : celda más alta {pk/1000:.3f} V · más baja {tr/1000:.3f} V")
            if zone == "plateau":
                L.append("  En la meseta un Δ alto indica dispersión (resistencia,")
                L.append("  conexión o temperatura), no falta de equilibrio de carga.")
            else:
                L.append("  En el codo de la curva el Δ refleja desequilibrio real.")
            if not t.get("cell_balance_reliable"):
                L.append("  Fiabilidad BAJA: potencia muy alta distorsiona el Δ.")
        else:
            L.append("  Sin lectura de celdas todavía.")

        # --- Alertas ---
        L.append("")
        L.append("ALERTAS")
        alert = t.get("cell_alert", "ok")
        bad = ", ".join(str(i) for i in t.get("cell_alert_cells", []))
        warn = t.get("cell_warn", "ok")
        wbad = ", ".join(str(i) for i in t.get("cell_warn_cells", []))
        if alert == "low":
            L.append(f"  Celda CRÍTICA baja (≤2.60V): {bad}")
        elif alert == "high":
            L.append(f"  Celda CRÍTICA alta (≥3.65V): {bad}")
        elif warn == "low":
            L.append(f"  Aviso: celda baja (≤2.90V): {wbad}")
        elif warn == "high":
            L.append(f"  Aviso: celda alta (≥3.60V): {wbad}")
        else:
            L.append("  Celdas             : sin alertas")
            L.append("  (aviso ≤2.90 / ≥3.60 V · crítica ≤2.60 / ≥3.65 V)")
        cal = t.get("soc_calibration", "unknown")
        cal_txt = {"ok": "Coherente", "suspect": "SOSPECHOSA (el % no cuadra con el voltaje)"}.get(cal)
        if cal_txt is None:
            _s = t.get("soc")
            if _s is None:
                cal_txt = "Sin datos"
            elif 10 < _s < 98:
                cal_txt = "No evaluable ahora (solo se comprueba con ≤10 % o ≥98 %)"
            else:
                cal_txt = "Evaluando… (extremo del %, esperando condiciones estables)"
        L.append(f"  Calibración del %  : {cal_txt}")

        # --- Temperatura ---
        L.append("")
        L.append("TEMPERATURA")
        temps = t.get("batt_temps") or []
        if temps:
            L.append(f"  Promedio {self._temp1(t['batt_temp'])}°C · "
                     f"mín {t.get('batt_temp_min')}°C · máx {t.get('batt_temp_max')}°C")
        else:
            L.append("  Sin lectura todavía.")

        # --- Capacidad ---
        L.append("")
        L.append("CAPACIDAD Y SALUD (medida por energía)")
        n = t.get("cap_health_samples", 0)
        if n:
            rng = t.get("cap_range_wh")
            L.append(f"  Capacidad          : {self._fmt(t.get('cap_est_wh'), '', ' Wh')} de {RATED_WH} Wh"
                     f" · salud {self._fmt(t.get('cap_health_pct'), '', '%')}")
            L.append(f"  Muestras           : {n}" + (f" (rango {rng[0]}–{rng[1]} Wh)" if rng else ""))
        else:
            L.append("  Aún sin medir. Se mide al cargar o descargar 15 puntos de")
            L.append("  % seguidos (entre 8% y 95%) con más de 60 W de media.")
        prog = t.get("cap_window_progress")
        if prog is not None:
            L.append(f"  Ventana en curso   : {prog}/15 puntos de %")
        _cs, _ds = t.get("cap_chg_wh"), t.get("cap_dis_wh")
        if _cs or _ds:
            L.append(f"  Por sentido        : carga {self._fmt(_cs, '', ' Wh')} (n={t.get('cap_chg_n', 0)})"
                     f" · descarga {self._fmt(_ds, '', ' Wh')} (n={t.get('cap_dis_n', 0)})")
            L.append("  (carga incluye pérdidas internas: sale más alta que lo que se puede extraer)")
        L.append("  Precisión esperada ≈ ±10 %; mejora con cada muestra.")
        cd, cc_ = t.get("cap_counter_dis_wh"), t.get("cap_counter_chg_wh")
        if cd or cc_:
            L.append(f"  Contraste contador : descarga {self._fmt(cd, '', ' Wh')} (n={t.get('cap_counter_dis_n', 0)})"
                     f" · carga {self._fmt(cc_, '', ' Wh')} (n={t.get('cap_counter_chg_n', 0)})")
        L.append(f"  Nominal            : {self._fmt(t.get('cap_rated_ah'), '.1f', ' Ah')} × 51.2 V = {RATED_WH:.0f} Wh")

        # --- Ciclos de carga (contadores de energía del BMS, Reg 6019 / 6021, confirmados) ---
        L.append("")
        L.append("CICLOS DE CARGA (contadores del BMS)")
        ec, ed = t.get("energy_chg_kwh"), t.get("energy_dis_kwh")
        cy = t.get("cycles_eq")
        L.append(f"  Ciclos equivalentes: {self._fmt(cy, '.0f')}")
        L.append(f"  Energía acumulada  : cargada {self._fmt(ec, '.2f', ' kWh')} · descargada {self._fmt(ed, '.2f', ' kWh')}")

        L.append("")
        L.append("  Registro continuo en: %LOCALAPPDATA%\\BluettiElite100Widget\\registro_bateria.csv")
        return "\n".join(L)

    def _refresh_info_window(self, t: Dict[str, Any]):
        if self.info_window is None or self.info_label is None:
            return
        try:
            if not self.info_window.winfo_exists():
                self.info_window = None
                self.info_label = None
                return
            self.info_label.configure(text=self._info_text(t))
        except Exception:
            pass

    # =========================================================================
    # WIDGET FLOTANTE
    # =========================================================================
    def _toggle_floating_widget(self):
        if self.floating_widget is not None and self.floating_widget.winfo_exists():
            self.floating_widget.close_widget()
            self.floating_widget = None
            self.btn_switch_hud.configure(text="🎮 Abrir Widget")
        else:
            self.floating_widget = FloatingHudWidget(self, self.ble, temp_tracker=self.temp_alert,
                                                     on_open_app=self._open_main_window)
            self.btn_switch_hud.configure(text="🎮 Cerrar Widget")

    # =========================================================================
    # CONTROLADORES
    # =========================================================================
    def _on_toggle_ac(self):
        self.last_user_click_ac = time.time()
        self.ble.set_ac_output(bool(self.sw_ac.get()))

    def _on_toggle_dc(self):
        self.last_user_click_dc = time.time()
        self.ble.set_dc_output(bool(self.sw_dc.get()))

    def _on_toggle_eco(self):
        self.ble.set_eco_mode(bool(self.sw_eco.get()))

    def _on_toggle_grid(self):
        self.last_user_click_grid = time.time()
        self.ble.set_grid_enhancement(bool(self.sw_grid.get()))

    def _on_toggle_pl(self):
        self.ble.set_power_lifting(bool(self.sw_pl.get()))

    def _set_charge_mode(self, mode: str, internal: bool = False):
        self.ble.set_charge_mode(mode, _internal=internal)
        self._cfg(self.btn_charge_silent, fg_color="#1d4ed8" if mode == "silent" else "#1e293b")
        self._cfg(self.btn_charge_standard, fg_color="#1d4ed8" if mode == "standard" else "#1e293b")
        self._cfg(self.btn_charge_turbo, fg_color="#1d4ed8" if mode == "turbo" else "#1e293b")
        self._cfg(self.btn_charge_custom, fg_color="#1d4ed8" if mode == "custom" else "#1e293b")

    def _on_custom_click(self):
        if self.ble.state.get("charge_mode") == "custom":
            self._open_custom_limit_window()
        else:
            self._set_charge_mode("custom")

    def _open_custom_limit_window(self):
        """Ventana del límite AC (modo Personalizado) con el mismo diseño que las ventanas de alertas."""
        old = getattr(self, "_custom_win", None)
        try:
            if old is not None and old.winfo_exists():
                old.lift()
                old.focus()
                return
        except Exception:
            pass
        cur = self.ble.state.get("grid_max_current") or GRID_LIMIT_MIN_A
        cur = max(GRID_LIMIT_MIN_A, min(GRID_LIMIT_MAX_A, int(cur)))
        win = ctk.CTkToplevel(self)
        win.title("Límite AC (modo Personalizado)")
        win.configure(fg_color=C_CARD)
        win.resizable(False, False)
        win.transient(self)
        self._set_window_icon(win)
        self._custom_win = win
        ctk.CTkLabel(win, text="⚙ LÍMITE AC (MODO PERSONALIZADO)", font=("Segoe UI", 12, "bold"),
                     text_color="#00f0ff").pack(anchor="w", padx=18, pady=(14, 4))
        body = ctk.CTkFrame(win, fg_color="transparent", width=470)
        body.pack(fill="x", padx=12, pady=(0, 4))
        body.columnconfigure(0, weight=1)
        ctk.CTkLabel(body, text="Limita la corriente que el equipo toma de la red AC al cargar.", font=("Segoe UI", 9),
                     text_color="#64748b", justify="left", anchor="w", wraplength=430
                     ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 2))
        var = tk.StringVar(master=win, value=str(cur))
        ctk.CTkLabel(body, text="Corriente máxima de entrada", font=("Segoe UI", 10), text_color="#cbd5e1",
                     anchor="w").grid(row=1, column=0, sticky="w", pady=2)
        entry = ctk.CTkEntry(body, textvariable=var, width=78, height=26, justify="right")
        entry.grid(row=1, column=1, sticky="e", padx=(8, 4), pady=2)
        ctk.CTkLabel(body, text=f"A  ({GRID_LIMIT_MIN_A}-{GRID_LIMIT_MAX_A})", width=64, anchor="w",
                     text_color="#94a3b8").grid(row=1, column=2, sticky="w")
        msg = ctk.CTkLabel(win, text="", font=("Segoe UI", 10), text_color="#fbbf24", wraplength=450, justify="left")
        msg.pack(anchor="w", padx=18, pady=(2, 0))
        state = {"cur": cur}

        def on_save(_e=None):
            try:
                val = int(round(float(str(var.get()).strip().replace(",", "."))))
            except ValueError:
                msg.configure(text="⚠ 'Corriente máxima de entrada': escribe un número.", text_color="#f87171")
                return
            if not GRID_LIMIT_MIN_A <= val <= GRID_LIMIT_MAX_A:
                msg.configure(text=f"⚠ 'Corriente máxima de entrada' debe estar entre {GRID_LIMIT_MIN_A} y {GRID_LIMIT_MAX_A} A.",
                              text_color="#f87171")
                return
            if val != state["cur"]:
                self.ble.set_grid_max_current(val)
                state["cur"] = val
            var.set(str(val))
            msg.configure(text="✔ Guardado", text_color="#10b981")

        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.pack(pady=(8, 14))
        ctk.CTkButton(btns, text="Guardar", width=90, height=28, fg_color="#1d4ed8", hover_color="#2563eb",
                      command=on_save).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Cerrar", width=80, height=28, fg_color="#1e293b", hover_color="#334155",
                      command=win.destroy).pack(side="left", padx=4)
        entry.bind("<Return>", on_save)
        win.bind("<Escape>", lambda e: win.destroy())

        self._center_on_app(win)
        entry.focus()
        entry.select_range(0, "end")

    # ------------------------------------------------------------------
    # CARGA INTELIGENTE (clase SmartCharge, definida arriba en este mismo archivo)
    # ------------------------------------------------------------------
    def _smart_exec(self, cmd):
        """Ejecuta una orden de SmartCharge: ('mode', m) o ('amps', n)."""
        try:
            # _internal: la Carga inteligente no es un cambio manual (no debe cancelar la proteccion de carga del equipo)
            if cmd[0] == "mode":
                self._set_charge_mode(cmd[1], internal=True)
            elif cmd[0] == "amps":
                self.ble.set_grid_max_current(int(cmd[1]), _internal=True)
        except Exception as exc:
            logger.warning("Carga inteligente: no se pudo ejecutar %s: %s", cmd, exc)

    def _smart_update(self, t: Dict[str, Any], is_conn: bool):
        """Con cada telemetria (hilo de Tk): deja que la Carga inteligente decida y ejecuta sus ordenes."""
        try:
            for c in self.smart.update(t, time.time(), is_conn):
                self._smart_exec(c)
        except Exception as exc:
            logger.warning("Carga inteligente: %s", exc)
        self._smart_publish()

    def _smart_publish(self):
        """Dice al gestor BLE que limite puso la Carga inteligente y cual era el original del usuario: la proteccion
        de carga no debe tomar el limite temporal de la Carga inteligente como 'lo que ya tenia configurado'."""
        try:
            sm = self.smart
            on = bool(sm.cfg.get("enabled") and sm.active and sm.applied is not None)
            self.ble.state["smart_applied"] = sm.applied if on else None
            self.ble.state["smart_prev"] = dict(sm.prev) if (on and sm.prev) else None
        except Exception:
            pass

    def _on_toggle_smart(self):
        on = bool(self.sw_smart.get())
        try:
            for c in self.smart.set_enabled(on):   # al apagarla estando activa, devuelve el modo de carga anterior
                self._smart_exec(c)
        except Exception as exc:
            logger.warning("Carga inteligente: %s", exc)
        self._smart_publish()
        self._refresh_alert_ui()

    def _open_smart_window(self):
        """Opciones de la Carga inteligente (mismo diseño que la ventana del limite AC)."""
        old = getattr(self, "_smart_win", None)
        try:
            if old is not None and old.winfo_exists():
                old.lift()
                old.focus()
                return
        except Exception:
            pass
        cfg = self.smart.cfg
        win = ctk.CTkToplevel(self)
        win.title("Carga inteligente")
        win.configure(fg_color=C_CARD)
        win.resizable(False, False)
        win.transient(self)
        self._set_window_icon(win)
        self._smart_win = win
        ctk.CTkLabel(win, text="⚙ CARGA INTELIGENTE", font=("Segoe UI", 12, "bold"),
                     text_color="#22c55e").pack(anchor="w", padx=18, pady=(14, 4))
        body = ctk.CTkFrame(win, fg_color="transparent", width=470)
        body.pack(fill="x", padx=12, pady=(0, 4))
        body.columnconfigure(0, weight=1)
        ctk.CTkLabel(body, text="Cuando coinciden la carga solar y la red AC, ajusta el modo Personalizado de la carga AC "
                                "para que la batería no reciba más del límite (solar + red − consumo directo). "
                                "Al terminar la carga AC devuelve el modo anterior.",
                     font=("Segoe UI", 9), text_color="#64748b", justify="left", anchor="w", wraplength=430
                     ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        rows = [("limit_w", "Potencia máxima de entrada", "W"), ("decide_s", "Tiempo para decidir un cambio", "s"),
                ("hyst_w", "Histéresis", "W")]
        vars_: Dict[str, Any] = {}
        for i, (key, label, unit) in enumerate(rows, start=1):
            ctk.CTkLabel(body, text=label, font=("Segoe UI", 10), text_color="#cbd5e1", anchor="w"
                         ).grid(row=i, column=0, sticky="w", pady=2)
            vars_[key] = tk.StringVar(master=win, value=str(cfg[key]))
            ctk.CTkEntry(body, textvariable=vars_[key], width=78, height=26, justify="right"
                         ).grid(row=i, column=1, sticky="e", padx=(8, 4), pady=2)
            lo, hi = _SC_RANGES[key]
            ctk.CTkLabel(body, text=f"{unit}  ({int(lo)}-{int(hi)})", width=96, anchor="w", text_color="#94a3b8"
                         ).grid(row=i, column=2, sticky="w")
        perm_var = tk.BooleanVar(master=win, value=bool(cfg["notice_permanent"]))
        ctk.CTkCheckBox(body, text="Aviso «Carga inteligente Activa» permanente (hasta cerrarlo)", variable=perm_var,
                        font=("Segoe UI", 10), text_color="#cbd5e1"
                        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(6, 2))
        vars_["notice_secs"] = tk.StringVar(master=win, value=str(cfg["notice_secs"]))
        ctk.CTkLabel(body, text="Si no es permanente, duración del aviso", font=("Segoe UI", 10), text_color="#cbd5e1",
                     anchor="w").grid(row=5, column=0, sticky="w", pady=2)
        ctk.CTkEntry(body, textvariable=vars_["notice_secs"], width=78, height=26, justify="right"
                     ).grid(row=5, column=1, sticky="e", padx=(8, 4), pady=2)
        lo, hi = _SC_RANGES["notice_secs"]
        ctk.CTkLabel(body, text=f"s  ({int(lo)}-{int(hi)})", width=96, anchor="w", text_color="#94a3b8"
                     ).grid(row=5, column=2, sticky="w")
        status = ctk.CTkLabel(win, text=f"Estado: {self.smart.status_text()}", font=("Segoe UI", 10),
                              text_color="#94a3b8", wraplength=450, justify="left")
        status.pack(anchor="w", padx=18, pady=(4, 0))
        msg = ctk.CTkLabel(win, text="", font=("Segoe UI", 10), text_color="#fbbf24", wraplength=450, justify="left")
        msg.pack(anchor="w", padx=18, pady=(2, 0))

        def on_save(_e=None):
            values = {k: v.get() for k, v in vars_.items()}
            values["notice_permanent"] = bool(perm_var.get())
            ok, err = self.smart.apply_settings(values)
            if not ok:
                msg.configure(text="⚠ " + err, text_color="#f87171")
                return
            msg.configure(text="✔ Guardado", text_color="#10b981")
            status.configure(text=f"Estado: {self.smart.status_text()}")

        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.pack(pady=(8, 14))
        ctk.CTkButton(btns, text="Guardar", width=90, height=28, fg_color="#1d4ed8", hover_color="#2563eb",
                      command=on_save).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Cerrar", width=80, height=28, fg_color="#1e293b", hover_color="#334155",
                      command=win.destroy).pack(side="left", padx=4)
        win.bind("<Return>", on_save)
        win.bind("<Escape>", lambda e: win.destroy())
        self._center_on_app(win)

    def _set_dc_source(self, source: str):
        if self._effective_dc_source(self.ble.state.get("dc_source")) == source:
            return
        now = time.time()
        self.last_user_click_hc = now
        self._dc_override = (source, now)
        # Resaltado inmediato; los cambios se encolan en el gestor BLE (último gana, cada 5 s)
        self._cfg(self.btn_dc_pv, fg_color="#1d4ed8" if source == "pv" else "#1e293b")
        self._cfg(self.btn_dc_other, fg_color="#1d4ed8" if source == "other" else "#1e293b")
        self.sw_high_current.configure(state="normal" if source == "pv" else "disabled")
        self.ble.set_dc_source(source)

    def _effective_dc_source(self, real):
        ov = self._dc_override
        if ov is not None:
            if real == ov[0] or time.time() - ov[1] > DC_UI_HOLD_S:
                self._dc_override = None
            else:
                return ov[0]
        return real

    def _on_toggle_high_current(self):
        want = bool(self.sw_high_current.get())
        self.last_user_click_hc = time.time()
        if want and not messagebox.askyesno(
                "Alta corriente",
                "¿Activar la alta corriente de entrada del panel solar?\n\n"
                "Confirma que tu panel/cableado soporta la corriente elevada.",
                parent=self):
            self.sw_high_current.deselect()
            return
        self.ble.set_dc_high_current(want)

    def _toggle_ble_release(self):
        w = self.floating_widget
        try:
            w_ok = w is not None and bool(w.winfo_exists())
        except Exception:
            w_ok = False
        if not self.ble.is_paused:
            if self._app_was_conn:
                self._off_model._begin_offline()   # congelar la tendencia ANTES de soltar el BLE
            self.ble.pause_and_release()
            if w_ok:
                w.set_connecting(False)
        else:
            self.ble.resume_connection()
            if w_ok:
                w.set_connecting(True)   # el widget muestra "Conectando..." en vez de saltar a Conectado
        self._on_ble_toggled()

    def _system_power_off(self):
        """Apagado total del sistema (registro 2013) - botón de la barra superior."""
        if hasattr(self.ble, "set_system_power_off"):
            try:
                self.ble.set_system_power_off()
            except Exception as exc:
                logger.warning("Error al apagar sistema: %s", exc)

    def _sync_ble_button(self):
        """El botón refleja SIEMPRE el estado real (ble.is_paused = desconectado por el usuario), venga el
        clic de aquí o del widget: así los dos botones son espejos."""
        if self.ble.is_paused:
            self._cfg(self.btn_release_ble, text=self._ble_btn_text(0), fg_color="#1d4ed8")
        else:
            self._cfg(self.btn_release_ble, text=self._ble_btn_text(1), fg_color="#1e293b")

    def _on_ble_toggled(self):
        """Tras conectar/desconectar (desde la app o desde el widget)."""
        self._sync_ble_button()
        self._update_ui_telemetry(dict(self.ble.state))

    def _offline_src(self):
        """Quien lleva la cuenta offline: el widget si la tiene activa (así ambos muestran lo mismo) o, si
        no, el estimador propio de la app. None = sin estimación."""
        w = self.floating_widget
        try:
            if w is not None and w.winfo_exists() and w._offline_active:
                return w
        except Exception:
            pass
        return self._off_model if self._off_model._offline_active else None

    def _offline_tick(self):
        try:
            self._sync_ble_button()
        except Exception:
            pass
        try:
            if (not (bool(self.ble.state.get("connected", False)) and not self.ble.is_paused)
                    and self._offline_src() is not None
                    and time.time() - self._last_cfg_ts >= UI_DRAG_QUIET_S):
                self._update_ui_telemetry(dict(self.ble.state))
        except Exception:
            pass
        try:
            self.after(1000, self._offline_tick)
        except Exception:
            pass

    def _set_soc_arrow(self, direction):
        """direction: 'up' (quedó cargando, a la derecha del %), 'down' (descargando, a la izquierda) o None."""
        if direction == self._soc_arrow:
            return
        self._soc_arrow = direction
        self.lbl_soc_arrow_r.place_forget()
        self.lbl_soc_arrow_l.place_forget()
        if direction == "up":
            self.lbl_soc_arrow_r.place(in_=self.lbl_soc_big, relx=1.0, x=6, rely=0.5, anchor="w")
        elif direction == "down":
            self.lbl_soc_arrow_l.place(in_=self.lbl_soc_big, relx=0.0, x=-6, rely=0.5, anchor="e")

    def _on_device_selected(self, choice: str):
        if " [" in choice and "]" in choice:
            addr = choice.split("[")[-1].replace("]", "").strip()
            self.ble.set_device_address(addr)

    def _handle_status(self, msg: str):
        # El texto de conexion se guarda como "base": la barra de estado lo muestra
        # cuando no hay alertas que enseñar (ver _refresh_status_label).
        def _apply():
            if msg.startswith("Desconectado"):
                if not self._status_offline:
                    self._offline_since = time.time()
                self._status_offline = True
                self._status_base_text = f"● {OFFLINE_STATUS_TEXT}"
            elif msg.startswith(("Enlace Seguro Activo", "Conectado")) or msg == "Reconectando...":
                self._status_offline = False   # conectado o reconexión pedida a mano: vuelven los mensajes normales
                self._offline_since = None
                self._status_base_text = f"● {msg}"
            elif self._status_offline and msg.startswith(("Buscando", "Estación no detectada", "Reconectando (", "Reintentando")):
                self._status_base_text = f"● {OFFLINE_STATUS_TEXT}"   # reintentos en segundo plano
            else:
                self._status_base_text = f"● {msg}"
            self._refresh_status_label()
        self.after(0, _apply)

    # =========================================================================
    # ALERTAS: barra de estado dinamica + cuenta atras DC en la tarjeta Solar
    # =========================================================================
    def _get_alert_tracker(self) -> AppAlertTracker:
        tr = getattr(self, "_alert_tracker", None)
        if tr is None:
            tr = AppAlertTracker(self.ble, self.temp_alert)
            tr.smart = self.smart
            self._alert_tracker = tr
        return tr

    def _widget_visible(self) -> bool:
        """True si el widget flotante esta abierto y a la vista (no oculto con Alt+B)."""
        w = self.floating_widget
        try:
            if w is None or not w.winfo_exists():
                return False
            if getattr(w, "_hidden", False):
                return False
            return w.state() == "normal"
        except Exception:
            return False

    def _app_alerts_update(self, t: Dict[str, Any]):
        """Se llama con cada telemetria (hilo de Tk)."""
        is_conn = bool(t.get("connected", False)) and not self.ble.is_paused
        self._get_alert_tracker().update(t, is_conn)
        if not getattr(self, "_alert_tick_started", False):
            self._alert_tick_started = True
            self.after(500, self._alert_tick)
        self._refresh_alert_ui()

    def _alert_tick(self):
        """Cada 500 ms: avanza la cuenta atras, rota la alerta mostrada y hace
        parpadear el triangulo."""
        try:
            if time.time() - self._last_cfg_ts >= UI_DRAG_QUIET_S:   # moviendo/redimensionando: no pintar
                self._refresh_alert_ui(blink=True)
        except Exception:
            pass
        try:
            self.after(500, self._alert_tick)
        except Exception:
            pass

    def _refresh_alert_ui(self, blink: bool = False):
        self._refresh_status_label()
        self._refresh_alert_icon(blink)

    def _alert_icon_scale_now(self) -> float:
        """Misma escala que el resto de widgets: zoom de la ventana x escala de
        pantalla (DPI). Un Canvas de tk no la aplica solo, asi que se calcula aqui."""
        z = float(getattr(self, "_zoom", 1.0) or 1.0)
        try:
            sc = float(self._win_scaling())
        except Exception:
            sc = 1.0
        return max(0.5, z * sc)

    def _alert_icon_redraw(self):
        """Dibuja el triangulo con '!' al tamaño que toca (18 px x escala) y ajusta
        su hueco en la barra. Se llama al crearlo y cada vez que cambia el zoom."""
        scale = self._alert_icon_scale_now()
        size = max(10, round(18 * scale))
        self._alert_icon_drawn = scale
        self._alert_icon_px = size
        on = getattr(self, "_alert_icon_on", False)
        cv = self.alert_icon
        cv.delete("all")
        cv.configure(width=size if on else 0, height=size)
        cv.pack_configure(padx=(round(12 * scale), 0) if on else 0, pady=round(6 * scale))
        color = getattr(self, "_alert_color", "#f59e0b")
        bg = C_CARD
        self._alert_tri = cv.create_polygon(
            size * 0.5, size * 0.06, size * 0.05, size * 0.92, size * 0.95, size * 0.92, fill=color, outline=color)
        cv.create_rectangle(size * 0.44, size * 0.36, size * 0.56, size * 0.62, fill=bg, outline=bg)
        cv.create_oval(size * 0.44, size * 0.70, size * 0.56, size * 0.82, fill=bg, outline=bg)

    def _icon_register(self, cv, kind: str, color: str):  # [icon-patch]
        if not hasattr(self, "_icons"):
            self._icons = []
            self._icons_scale = 0.0
        self._icons.append((cv, kind, color))
        _draw_icon_on(cv, kind, color, self._alert_icon_scale_now())
        self._icons_scale = self._alert_icon_scale_now()

    def _icons_redraw(self):  # [icon-patch]
        sc = self._alert_icon_scale_now()
        for cv, kind, color in self._icons:
            _draw_icon_on(cv, kind, color, sc)
        self._icons_scale = sc

    def _refresh_alert_icon(self, blink: bool = False):
        if getattr(self, "_icons", None) and abs(self._alert_icon_scale_now() - self._icons_scale) > 0.01:
            self._icons_redraw()
        """Triangulo parpadeante junto al estado: visible mientras haya alguna alerta
        (este o no el widget a la vista); rojo si es critica, ambar si es precaucion.
        Se reescala solo cuando cambia el zoom de la ventana."""
        if abs(self._alert_icon_scale_now() - getattr(self, "_alert_icon_drawn", 0.0)) > 0.01:
            self._alert_icon_redraw()
        tr = getattr(self, "_alert_tracker", None)
        sev = tr.severity() if tr is not None else "ok"
        if sev == "ok":
            if getattr(self, "_alert_icon_on", False):
                self._alert_icon_on = False
                self.alert_icon.configure(width=0)
                self.alert_icon.pack_configure(padx=0)
            return
        if not getattr(self, "_alert_icon_on", False):
            self._alert_icon_on = True
            self.alert_icon.configure(width=self._alert_icon_px)
            self.alert_icon.pack_configure(padx=(round(12 * self._alert_icon_scale_now()), 0))
        if blink:
            self._alert_blink = not getattr(self, "_alert_blink", False)
        solid = "#ef4444" if sev == "critical" else "#f59e0b"
        self._alert_color = solid
        color = solid if getattr(self, "_alert_blink", True) else C_CARD
        self.alert_icon.itemconfig(self._alert_tri, fill=color, outline=color)

    def _on_alert_icon_click(self):
        """Tocar el triangulo: la barra de estado muestra las alertas 10 s aunque el
        widget este visible (tocar otra vez lo cierra)."""
        now = time.time()
        self._alert_force_until = 0.0 if now < getattr(self, "_alert_force_until", 0.0) else now + 10.0
        self._refresh_status_label()

    def _offline_status_text(self) -> str:
        t0 = self._offline_since
        sec = 0 if t0 is None else max(0, int(time.time() - t0))
        if sec < 60:
            ago = f"{sec}s"
        elif sec < 3600:
            ago = f"{sec // 60}m"
        else:
            ago = f"{sec // 3600}h:{(sec % 3600) // 60:02d}m"
        return f"● Desconectado (hace {ago}) · Actualizaciones imprecisas de Batería y Tiempo"

    def _refresh_status_label(self):
        base = getattr(self, "_status_base_text", "◌ Negociando Enlace Seguro ECDH...")
        if base == f"● {OFFLINE_STATUS_TEXT}":
            base = self._offline_status_text()
        text, color, has_close = base, "#fbbf24", False
        self._status_kind = None
        tr = getattr(self, "_alert_tracker", None)
        now = time.time()
        forced = now < getattr(self, "_alert_force_until", 0.0)
        dc = tr.dc_row(now) if tr is not None else None
        if dc is not None and not forced:
            # Cuenta atras de la proteccion DC: fija junto al triangulo hasta que termine (o se cierre con la x);
            # tocar el triangulo muestra el resto de alertas 10 s y vuelve a tocarlo para regresar.
            d_text, color, has_close = dc
            text = d_text if d_text.startswith("⚠") else f"⚠ {d_text}"
            self._status_kind = "dc" if has_close else None
        elif tr is not None and (forced or not self._widget_visible()):
            alerts = tr.alerts(now, forced)
            if alerts:
                idx = int(now // ALERT_CYCLE_S) % len(alerts)
                a_text, color, _kind = alerts[idx]
                self._status_kind = _kind
                prefix = "" if (a_text.startswith("⚠") or _kind == "smart") else "⚠ "
                suffix = f"  ({idx + 1}/{len(alerts)})" if len(alerts) > 1 else ""
                text = f"{prefix}{a_text}{suffix}"
                a_low = a_text.lower()
                has_close = ("salida ac apagada" not in a_low
                             and "batería crítica" not in a_low
                             and "bateria critica" not in a_low
                             and "apagado inmediato por bater" not in a_low)
        elif tr is not None:
            # widget a la vista: el aviso azul de Carga inteligente alterna con el estado normal (la x lo cierra)
            sl = tr.smart_line(now)
            if sl and int(now // ALERT_CYCLE_S) % 2 == 1:
                text, color, has_close = sl[0], sl[1], True
                self._status_kind = "smart"
        if (text, color) != getattr(self, "_status_shown", None):
            self._status_shown = (text, color)
            self.lbl_status.configure(text=text, text_color=color)
        shown = bool(self.btn_status_close.winfo_manager())
        if has_close and not shown:
            self.btn_status_close.pack(side="left", padx=(0, 6))
        elif not has_close and shown:
            self.btn_status_close.pack_forget()

    def _on_status_alert_close(self):
        tr = self._get_alert_tracker()
        if getattr(self, "_status_kind", None) == "dc":
            tr.dismiss_dc()     # x sobre la cuenta atras de la proteccion DC
        elif getattr(self, "_status_kind", None) == "temp":
            tr.dismiss_temp()   # x sobre el aviso de temperatura: solo cierra ese (reaparece a los 30 min)
        elif getattr(self, "_status_kind", None) == "smart":
            tr.dismiss_smart()  # x sobre «Carga inteligente Activa»: solo cierra ese aviso (la funcion sigue activa)
        else:
            tr.dismiss_all()
        self._refresh_alert_ui()

    def _handle_devices_discovered(self, devices: List[Dict[str, str]]):
        opts = [f"{d['name']} [{d['address']}]" for d in devices]
        if opts:
            self.after(0, lambda: self.combo_devices.configure(values=opts))

    def _handle_telemetry(self, t: Dict[str, Any]):
        # Lo llama el hilo BLE VARIAS veces por ciclo (una por registro leido). Antes cada llamada
        # repintaba toda la pantalla (~100 configure de customtkinter): era lo que hacia lenta y
        # erratica la ventana. Ahora solo se guarda la ultima y se programa UNA actualizacion.
        self._tele_latest = t
        srv = getattr(self, 'srv', None)   # servidor de red (puede no existir aun)
        if srv is not None:
            srv.publish(t)
        if not self._tele_scheduled:
            self._tele_scheduled = True
            self.after(0, self._flush_telemetry)

    def _flush_telemetry(self):
        now = time.time()
        wait = UI_MIN_INTERVAL_S - (now - self._tele_last_ui)
        quiet = UI_DRAG_QUIET_S - (now - self._last_cfg_ts)
        if quiet > 0:                      # se esta moviendo o redimensionando la ventana: esperar
            wait = max(wait, quiet)
        if wait > 0.01:
            self.after(int(wait * 1000) + 1, self._flush_telemetry)
            return
        self._tele_scheduled = False
        self._tele_last_ui = now
        t = self._tele_latest
        if t is not None:
            self._update_ui_telemetry(t)

    # =========================================================================
    # ACTUALIZACIÓN DE LA INTERFAZ
    # =========================================================================
    def _update_bank(self, t: Dict[str, Any], na: bool = False):
        if na:
            self._cfg(self.lbl_bank_title, text="BANCO DE BATERÍAS --V")
            self._cfg(self.lbl_bank_temp, text_color="#fb923c", text="Temp. celdas --°C")
            for i in range(16):
                self._cfg(self.cell_labels[i], text=self._cell_text(i + 1, None), text_color="#cbd5e1")
                self._cfg(self.cell_boxes[i], border_color="#26324a")
            self._cfg(self.lbl_cell_summary, text="Min(--) --V • Max(--) --V • Dif --mV", text_color="#cbd5e1")
            self._set_state_pill(*NEUTRAL_STYLE)
            return
        pv = t.get("pack_volts")
        self._cfg(self.lbl_bank_title, text=f"BANCO DE BATERÍAS {pv:.1f}V" if pv else "BANCO DE BATERÍAS --V")
        tmp = t.get("batt_temp")
        tcol = "#fb923c"
        if tmp is not None:
            _tw, _tc, _th = alert_config.temp_limits()
            tcol = "#ef4444" if tmp >= _tc else ("#fbbf24" if tmp >= _tw else "#fb923c")
        self._cfg(self.lbl_bank_temp, text_color=tcol, text=f"Temp. celdas {self._temp1(tmp)}°C" if tmp is not None
                                     else "Temp. celdas --°C")

        cells = t.get("cell_mv")
        if not cells or len(cells) < 16:
            return
        crit = set(t.get("cell_alert_cells", []) or [])
        warn_cells = set(t.get("cell_warn_cells", []) or [])
        mn_i, mx_i = t.get("cell_min_idx"), t.get("cell_max_idx")
        delta = t.get("cell_delta_mv") or 0
        for i in range(16):
            idx = i + 1
            color, border = "#cbd5e1", "#26324a"
            if idx in crit:
                color, border = "#ff3366", "#ff3366"
            elif idx in warn_cells:
                color, border = "#fb923c", "#fb923c"
            elif delta >= 3 and idx == mn_i:
                color, border = "#38bdf8", "#38bdf8"
            elif delta >= 3 and idx == mx_i:
                color, border = "#fbbf24", "#fbbf24"
            self._cfg(self.cell_labels[i], text=self._cell_text(idx, cells[i]), text_color=color)
            self._cfg(self.cell_boxes[i], border_color=border)

        d = CELL_DECIMALS
        self._cfg(self.lbl_cell_summary, 
            text=(f"Min({mn_i}){t['cell_min_mv'] / 1000:.{d}f}V • "
                  f"Max({mx_i}){t['cell_max_mv'] / 1000:.{d}f}V • Dif {delta}mV"),
            text_color="#cbd5e1")

        # Estado destacado
        alert = t.get("cell_alert", "ok")
        bal = t.get("cell_balance")
        if alert == "low":
            text, fg, bg = "Crítica baja", "#ff3366", "#3a0f1a"
        elif alert == "high":
            text, fg, bg = "Crítica alta", "#ff3366", "#3a0f1a"
        elif t.get("cell_warn", "ok") in WARN_STYLE:
            text, fg, bg = WARN_STYLE[t["cell_warn"]]
        elif t.get("cell_grade") in GRADE_STYLE:
            grade = t["cell_grade"]
            if grade in ("fair", "bad") and not t.get("cell_balance_reliable"):
                text, fg, bg = UNRELIABLE_STYLE
            else:
                text, fg, bg = GRADE_STYLE[grade]
                if t.get("cell_zone") == "plateau" and grade in PLATEAU_TEXT:
                    text = PLATEAU_TEXT[grade]
        else:
            text, fg, bg = NEUTRAL_STYLE
        self._set_state_pill(text, fg, bg)

    def _update_ui_telemetry(self, t: Dict[str, Any]):
        self._last_t = t
        soc = t.get("soc")
        in_w = t.get("dc_input_watts", 0) + t.get("ac_input_watts", 0)
        out_w = t.get("ac_output_watts", 0) + t.get("dc_output_watts", 0)
        net = in_w - out_w

        is_conn_t = bool(t.get("connected", False)) and not self.ble.is_paused
        na = not is_conn_t   # sin enlace: solo se estiman batería y tiempo; el resto se muestra como --
        self.temp_alert.update(t.get("batt_temp"), is_conn_t)

        # Estimación offline propia de la app (el widget lleva la suya; si la tiene activa se usa esa)
        mode = "charging" if net > 5 else ("discharging" if net < -5 else "neutral")
        if mode != self._app_last_mode:
            self._app_mode_t = time.time()
            self._app_last_mode = mode
        if is_conn_t:
            if not self._app_was_conn:
                self._off_model._hist.clear()
                self._off_model._end_offline()
            if soc is not None:
                self._off_model._record_sample(soc, net, t.get("runtime_minutes"), mode,
                                               (time.time() - self._app_mode_t) < 3.0)
        elif self._app_was_conn:
            self._off_model._begin_offline()
        self._app_was_conn = is_conn_t
        off_src = self._offline_src() if na else None
        off_rem = None
        if off_src is not None:
            off_soc, off_rem = off_src._offline_state()
            if off_soc is not None:
                soc = int(round(off_soc))
            self._set_soc_arrow({"charging": "up", "discharging": "down"}.get(off_src._off_mode))
        else:
            self._set_soc_arrow(None)

        if self._tray is not None:
            self._tray.set_tooltip(f"Bluetti • {soc}% • {net:+.0f} W" if soc is not None else "Bluetti Elite 100 V2")

        if soc is None:
            self._cfg(self.lbl_soc_big, text="--%", text_color="#94a3b8")
            self._set_progress(0)
            self._cfg(self.progress_soc_big, progress_color="#334155")
        else:
            soc_color = "#00ff88" if soc > 50 else ("#fbbf24" if soc > 20 else "#ff3366")
            self._cfg(self.lbl_soc_big, text=f"{soc}%", text_color=soc_color)
            self._set_progress(soc / 100.0)
            self._cfg(self.progress_soc_big, progress_color=soc_color)

        # Capacidad estimada + salud
        wh = t.get("cap_est_wh")
        health = t.get("cap_health_pct")
        if wh is not None and health is not None:
            self._cfg(self.lbl_capacity, text=f"Capacidad estimada {wh}/{RATED_WH} Wh • {health}% de Salud")
        else:
            self._cfg(self.lbl_capacity, text=f"Capacidad nominal {RATED_WH} Wh • Salud: midiendo…")

        # Entrada
        self._cfg(self.lbl_in_total_w, text="-- W" if na else f"{in_w} W")
        self._cfg(self.lbl_solar_val, text="-- W" if na else f"{t.get('dc_input_watts', 0)} W")
        solar_v = t.get("dc_input_volts", 0.0) or 0.0
        solar_a = t.get("dc_input_current", 0.0) or 0.0
        self._cfg(self.lbl_solar_sub, text=f"{solar_v:.1f}V • {solar_a:.1f}A" if solar_v > 0 and not na else "--V • --A")

        dc_src_real = t.get("dc_source")
        dc_src = self._effective_dc_source(dc_src_real)
        grid_lim = t.get("grid_max_current")
        self._cfg(self.btn_charge_custom, text=f"Personalizado ({grid_lim}A Máx)" if grid_lim else "Personalizado (--A Máx)")

        self._cfg(self.lbl_ac_in_val, text="-- W" if na else f"{t.get('ac_input_watts', 0)} W")
        v_grid = t.get("ac_input_volts", 0.0)
        i_grid = t.get("ac_input_current", 0.0)
        hz_in = t.get("ac_input_hz", 0.0) or 0.0
        if na:
            self._cfg(self.lbl_ac_in_sub, text="--V • --A • --Hz")
        elif v_grid and v_grid > 0:
            self._cfg(self.lbl_ac_in_sub, text=f"{v_grid:.0f}V • {i_grid:.1f}A • {hz_in:.0f}Hz" if hz_in > 0
                                         else f"{v_grid:.0f}V • {i_grid:.1f}A")
        else:
            self._cfg(self.lbl_ac_in_sub, text="Sin red")

        # Salida
        self._cfg(self.lbl_out_total_w, text="-- W" if na else f"{out_w} W")
        self._cfg(self.lbl_ac_out_val, text="-- W" if na else f"{t.get('ac_output_watts', 0)} W")
        v_out = t.get("ac_output_volts", 0.0) or 0.0
        hz_out = t.get("ac_output_hz", 0.0) or 0.0
        if na:
            self._cfg(self.lbl_ac_out_sub, text="--V • --Hz")
        elif v_out > 0:
            self._cfg(self.lbl_ac_out_sub, text=f"{v_out:.0f}V • {hz_out:.0f}Hz" if hz_out > 0 else f"{v_out:.0f}V")
        else:
            self._cfg(self.lbl_ac_out_sub, text="120V • --Hz")
        self._cfg(self.lbl_dc_out_val, text="-- W" if na else f"{t.get('dc_output_watts', 0)} W")

        # Estado de carga y tiempo en una sola línea
        net_text = f"Cargando (+{net}W)" if net > 5 else (f"Descargando ({net}W)" if net < -5 else "Flujo Neto: 0 W")
        if na:
            self._cfg(self.lbl_net_power, text="Desconectado", text_color=OFFLINE_TEXT_COLOR)
        else:
            self._cfg(self.lbl_net_power, text=net_text, text_color="#00f0ff" if net > 5 else ("#ff3366" if net < -5 else "#94a3b8"))

        runtime_min = t.get("runtime_minutes")
        if na:
            if off_rem is None:
                tiempo_off = "--h --m"
            else:
                tiempo_off = f"{int(off_rem) // 60}h {int(off_rem) % 60:02d}m"
            if off_src is not None and off_rem is not None:
                lead = "Carga completa en" if off_src._off_mode == "charging" else "Autonomía restante"
                self._cfg(self.lbl_batt_time, text=f"⏱ {lead} (est.): {tiempo_off}")
            else:
                self._cfg(self.lbl_batt_time, text=f"⏱ {tiempo_off}")
        elif isinstance(runtime_min, (int, float)) and runtime_min > 0:
            tiempo_txt = f"{int(runtime_min) // 60}h {int(runtime_min) % 60:02d}m"
            if net > 5:
                self._cfg(self.lbl_batt_time, text=f"⏱ Carga completa en {tiempo_txt}", text_color="#38bdf8")
            elif net < -5:
                self._cfg(self.lbl_batt_time, text=f"⏱ Autonomía restante: {tiempo_txt}", text_color="#38bdf8")
            else:
                self._cfg(self.lbl_batt_time, text="⏱ En reposo", text_color="#38bdf8")
        else:
            self._cfg(self.lbl_batt_time, text="⏱ Calculando autonomía...", text_color="#38bdf8")

        # Banco de baterías + ventana de detalle
        self._update_bank(t, na)
        self._refresh_info_window(t)

        if self.floating_widget is not None and self.floating_widget.winfo_exists():
            self.floating_widget.update_from_telemetry(t)

        # Sincronizar conmutadores (con antirrebote de clic)
        now = time.time()
        if "ac_output_on" in t and (now - getattr(self, "last_user_click_ac", 0) > 2.0):
            self._sync_switch(self.sw_ac, bool(t["ac_output_on"]))
        if "dc_output_on" in t and (now - getattr(self, "last_user_click_dc", 0) > 2.0):
            self._sync_switch(self.sw_dc, bool(t["dc_output_on"]))
        if "eco_mode_on" in t:
            self._sync_switch(self.sw_eco, bool(t["eco_mode_on"]))
        if "grid_enhancement_on" in t and (now - getattr(self, "last_user_click_grid", 0) > 2.0):
            self._sync_switch(self.sw_grid, bool(t["grid_enhancement_on"]))
        if "power_lifting_on" in t:
            self._sync_switch(self.sw_pl, bool(t["power_lifting_on"]))

        if "charge_mode" in t:
            m = t["charge_mode"]
            self._cfg(self.btn_charge_silent, fg_color="#1d4ed8" if m == "silent" else "#1e293b")
            self._cfg(self.btn_charge_standard, fg_color="#1d4ed8" if m == "standard" else "#1e293b")
            self._cfg(self.btn_charge_turbo, fg_color="#1d4ed8" if m == "turbo" else "#1e293b")
            self._cfg(self.btn_charge_custom, fg_color="#1d4ed8" if m == "custom" else "#1e293b")

        self._cfg(self.btn_dc_pv, fg_color="#1d4ed8" if dc_src == "pv" else "#1e293b")
        self._cfg(self.btn_dc_other, fg_color="#1d4ed8" if dc_src == "other" else "#1e293b")
        if now - getattr(self, "last_user_click_hc", 0) > 4.0:
            hc = bool(t.get("dc_high_current_on", False)) and dc_src == "pv"
            if bool(self.sw_high_current.get()) != hc:
                self.sw_high_current.configure(state="normal")
                self._sync_switch(self.sw_high_current, hc)
        self.sw_high_current.configure(state="normal" if dc_src == "pv" else "disabled")

        # Alertas en la barra de estado (si el widget no esta visible) y cuenta
        # atras de la proteccion DC en la tarjeta Solar
        self._smart_update(t, is_conn_t)
        self._sky_update(t, is_conn_t)
        self._app_alerts_update(t)

    def _sky_update(self, t, is_conn):  # [sky-patch]
        try:
            self.sky.feed(t, is_conn)
            txt, color, prod = self.sky.row_parts()
            self._cfg(self.lbl_sky_state, text=txt, text_color=color)
            self._cfg(self.lbl_sky_prod, text=prod)
        except Exception:
            pass

    def _open_sky_window(self, mode="Día"):  # [sky-patch]
        w = self._sky_win
        try:
            if w is not None and w.winfo_exists():
                w.set_mode(mode)
                w.lift()
                w.focus()
                return
        except Exception:
            pass
        self._sky_win = SkyWindow(self, self.sky, mode)

    @staticmethod
    def _cfg(w, **kw):
        """configure() solo con lo que CAMBIA. En customtkinter cada configure puede repintar el
        widget (canvas) aunque el valor sea el mismo; la telemetria casi nunca cambia nada, asi que
        repintarlo todo en cada actualizacion era trabajo inutil que bloqueaba la ventana."""
        cache = getattr(w, "_cfg_cache", None)
        if cache is None:
            cache = {}
            try:
                w._cfg_cache = cache
            except Exception:
                w.configure(**kw)
                return
        changed = {k: v for k, v in kw.items() if k not in cache or cache[k] != v}
        if changed:
            w.configure(**changed)
            cache.update(changed)

    def _set_progress(self, v: float):
        if getattr(self, "_progress_last", None) != v:
            self._progress_last = v
            self.progress_soc_big.set(v)

    @staticmethod
    def _sync_switch(sw, state: bool):
        if bool(sw.get()) != state:
            if state:
                sw.select()
            else:
                sw.deselect()


    # =========================================================================
    # ALERTAS PERSONALIZABLES: switch por tarjeta + ventana de rangos  [alertas-patch]
    # =========================================================================
    @staticmethod
    def _alert_fmt(v, kind):
        return str(int(v)) if kind == "int" else ("%g" % float(v))

    def _add_alert_row(self, card, key, color="#38bdf8"):
        meta = alert_config.SCHEMA[key]
        row = ctk.CTkFrame(card, fg_color="transparent")
        row.pack(fill="x", padx=10, pady=(0, 4))
        sw = ctk.CTkSwitch(row, text="", width=40, switch_width=34, switch_height=16,
                           progress_color=color, command=lambda k=key: self._on_alert_switch(k))
        sw.pack(side="left")
        if alert_config.get(key, "enabled"):
            sw.select()
        lbl = ctk.CTkLabel(row, text=meta["card_label"] + " ⚙", font=("Segoe UI", 10, "bold"),
                           text_color="#94a3b8", cursor="hand2")
        lbl.pack(side="left", padx=(2, 0))
        lbl.bind("<Button-1>", lambda e, k=key: self._open_alert_window(k))
        self.__dict__.setdefault("_alert_switches", {})[key] = sw

    def _on_alert_switch(self, key):
        sw = self.__dict__.get("_alert_switches", {}).get(key)
        if sw is None:
            return
        alert_config.set_enabled(key, bool(sw.get()))
        alert_config.save()
        self._alerts_config_changed()

    def _alerts_config_changed(self):
        """Reevalua las alertas con la telemetria actual (app y widget) al cambiar la configuracion."""
        try:
            t = dict(self._last_t or self.ble.state)
            self._app_alerts_update(t)
            fw = self.floating_widget
            if fw is not None and fw.winfo_exists():
                fw.update_from_telemetry(t)
        except Exception:
            pass

    def _open_alert_window(self, key):
        wins = self.__dict__.setdefault("_alert_windows", {})
        old = wins.get(key)
        try:
            if old is not None and old.winfo_exists():
                old.lift()
                old.focus()
                return
        except Exception:
            pass
        meta = alert_config.SCHEMA[key]
        cur = alert_config.section(key)
        win = ctk.CTkToplevel(self)
        win.title(meta["title"])
        win.configure(fg_color=C_CARD)
        win.resizable(False, False)
        win.transient(self)
        self._set_window_icon(win)
        wins[key] = win
        ctk.CTkLabel(win, text="⚙ " + meta["title"].upper(), font=("Segoe UI", 12, "bold"),
                     text_color="#00f0ff").pack(anchor="w", padx=18, pady=(14, 4))
        alert_ids = [a for a, m in alert_config.ALERT_META.items() if m[0] == key]
        on_key = {a: alert_config.ALERT_ON[a] for a in alert_ids if a in alert_config.ALERT_ON}
        hidden = {a + "_dur" for a in alert_ids} | {a + "_tri" for a in alert_ids} | set(on_key.values())
        fmap = {f["key"]: f for f in meta["fields"] if "key" in f}
        main_fields = [f for f in meta["fields"] if f.get("key") not in hidden]
        n_rows = len(main_fields) + (len(alert_ids) + 4 if alert_ids else 0)
        body_h = int(min(560, 31 * n_rows + 20, self.winfo_screenheight() * 0.62))
        body = ctk.CTkScrollableFrame(win, fg_color="transparent", width=470, height=body_h)
        body.pack(fill="x", padx=12, pady=(0, 4))
        body.columnconfigure(0, weight=1)

        fields = []   # (campo, variable)
        r = 0
        for f in main_fields:
            kind = f["kind"]
            if kind == "group":
                ctk.CTkLabel(body, text=f["label"], font=("Segoe UI", 10, "bold"),
                             text_color="#38bdf8").grid(row=r, column=0, columnspan=4, sticky="w", pady=(8, 2))
            elif kind == "note":
                ctk.CTkLabel(body, text=f["label"], font=("Segoe UI", 9), text_color="#64748b", justify="left",
                             anchor="w", wraplength=430).grid(row=r, column=0, columnspan=4, sticky="w", pady=(0, 2))
            elif kind == "bool":
                var = tk.BooleanVar(master=win, value=bool(cur[f["key"]]))
                ctk.CTkCheckBox(body, text=f["label"], variable=var, font=("Segoe UI", 10), text_color="#cbd5e1",
                                **_CK_STYLE).grid(row=r, column=0, columnspan=4, sticky="w", pady=2)
                fields.append((f, var))
            elif kind == "choice":
                labels = [c[1] for c in f["choices"]]
                var = tk.StringVar(master=win, value=labels[[c[0] for c in f["choices"]].index(cur[f["key"]])])
                ctk.CTkLabel(body, text=f["label"], font=("Segoe UI", 10), text_color="#cbd5e1",
                             anchor="w").grid(row=r, column=0, sticky="w", pady=2)
                ctk.CTkOptionMenu(body, values=labels, variable=var, width=200, font=("Segoe UI", 10)
                                  ).grid(row=r, column=1, columnspan=3, sticky="e", padx=(8, 0), pady=2)
                fields.append((f, var))
            else:   # int / float
                var = tk.StringVar(master=win, value=self._alert_fmt(cur[f["key"]], kind))
                ctk.CTkLabel(body, text=f["label"], font=("Segoe UI", 10), text_color="#cbd5e1",
                             anchor="w").grid(row=r, column=0, sticky="w", pady=2)
                ctk.CTkEntry(body, textvariable=var, width=78, height=26, justify="right"
                             ).grid(row=r, column=1, columnspan=2, sticky="e", padx=(8, 4), pady=2)
                ctk.CTkLabel(body, text=f.get("unit", ""), width=40, anchor="w",
                             text_color="#94a3b8").grid(row=r, column=3, sticky="w")
                fields.append((f, var))
            r += 1

        if alert_ids:   # tabla de avisos: activo / tiempo visible / deja el triangulo (una fila por aviso)
            ctk.CTkLabel(body, text="Avisos", font=("Segoe UI", 10, "bold"), text_color="#38bdf8"
                         ).grid(row=r, column=0, columnspan=4, sticky="w", pady=(10, 0))
            r += 1
            ctk.CTkLabel(body, text="Seg. = tiempo en pantalla (0 = hasta cerrarlo). ▲ = deja el triángulo de advertencia: "
                                    "tocarlo lo vuelve a mostrar mientras dure la condición.",
                         font=("Segoe UI", 9), text_color="#64748b", justify="left", anchor="w", wraplength=430
                         ).grid(row=r, column=0, columnspan=4, sticky="w", pady=(0, 2))
            r += 1
            for c_, (txt_, st_) in enumerate((("Aviso", "w"), ("Activo", ""), ("Seg.", ""), ("▲", ""))):
                ctk.CTkLabel(body, text=txt_, font=("Segoe UI", 9, "bold"), text_color="#94a3b8"
                             ).grid(row=r, column=c_, sticky=st_ or None, padx=(0, 0) if c_ == 0 else 4)
            r += 1
            on_vars = {}
            for a in alert_ids:
                nm = alert_config.ALERT_META[a][1]
                ctk.CTkLabel(body, text=nm, font=("Segoe UI", 10), text_color="#cbd5e1", anchor="w"
                             ).grid(row=r, column=0, sticky="w", pady=1)
                ek = on_key.get(a)
                if ek:
                    if ek not in on_vars:   # una misma casilla puede gobernar varios avisos (p. ej. temperatura)
                        on_vars[ek] = tk.BooleanVar(master=win, value=bool(cur[ek]))
                        fields.append((fmap[ek], on_vars[ek]))
                    ctk.CTkCheckBox(body, text="", variable=on_vars[ek], width=30, **_CK_STYLE).grid(row=r, column=1, padx=4, pady=1)
                dvar = tk.StringVar(master=win, value=self._alert_fmt(cur[a + "_dur"], "int"))
                ctk.CTkEntry(body, textvariable=dvar, width=54, height=24, justify="right"
                             ).grid(row=r, column=2, padx=4, pady=1)
                fields.append((dict(fmap[a + "_dur"], label=nm + ": tiempo visible"), dvar))
                if a + "_tri" in fmap:
                    tvar = tk.BooleanVar(master=win, value=bool(cur[a + "_tri"]))
                    ctk.CTkCheckBox(body, text="", variable=tvar, width=30, **_CK_STYLE).grid(row=r, column=3, padx=4, pady=1)
                    fields.append((fmap[a + "_tri"], tvar))
                r += 1

        msg = ctk.CTkLabel(win, text="", font=("Segoe UI", 10), text_color="#fbbf24", wraplength=450, justify="left")
        msg.pack(anchor="w", padx=18, pady=(2, 0))

        def collect():
            vals = {}
            for f, var in fields:
                k, kind = f["key"], f["kind"]
                if kind == "bool":
                    vals[k] = bool(var.get())
                elif kind == "choice":
                    labels = [c[1] for c in f["choices"]]
                    vals[k] = f["choices"][labels.index(var.get())][0]
                else:
                    name = f["label"].strip()
                    try:
                        num = float(str(var.get()).strip().replace(",", "."))
                    except ValueError:
                        raise ValueError("'%s': escribe un número." % name)
                    if num != num or (f["min"] is not None and num < f["min"]) or (f["max"] is not None and num > f["max"]):
                        raise ValueError("'%s' debe estar entre %g y %g %s." % (name, f["min"], f["max"], f.get("unit", "")))
                    vals[k] = int(round(num)) if kind == "int" else num
            return vals

        def on_save():
            try:
                vals = collect()
            except ValueError as exc:
                msg.configure(text="⚠ " + str(exc), text_color="#f87171")
                return
            err = alert_config.validate(key, vals)
            if err:
                msg.configure(text="⚠ " + err, text_color="#f87171")
                return
            for f, _var in fields:
                warn = f.get("confirm_off")
                if warn and cur.get(f["key"]) and not vals[f["key"]]:
                    if not messagebox.askyesno("Desactivar protección",
                                               warn + "\n\n¿Desactivarla de todos modos?", parent=win):
                        return
            alert_config.set_values(key, vals)
            if not alert_config.save():
                msg.configure(text="⚠ No se pudo guardar el archivo de configuración.", text_color="#f87171")
                return
            cur.update(vals)
            msg.configure(text="✔ Guardado", text_color="#10b981")
            self._alerts_config_changed()

        def on_reset():
            d = alert_config.defaults(key)
            for f, var in fields:
                v = d[f["key"]]
                if f["kind"] == "bool":
                    var.set(bool(v))
                elif f["kind"] == "choice":
                    var.set([c[1] for c in f["choices"] if c[0] == v][0])
                else:
                    var.set(self._alert_fmt(v, f["kind"]))
            msg.configure(text="Valores por defecto cargados: pulsa Guardar para aplicarlos.", text_color="#fbbf24")

        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.pack(pady=(8, 14))
        ctk.CTkButton(btns, text="Guardar", width=90, height=28, fg_color="#1d4ed8", hover_color="#2563eb",
                      command=on_save).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Restaurar valores", width=130, height=28, fg_color="#1e293b", hover_color="#334155",
                      command=on_reset).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Cerrar", width=80, height=28, fg_color="#1e293b", hover_color="#334155",
                      command=win.destroy).pack(side="left", padx=4)

        self._center_on_app(win)


def main():
    if not acquire_single_instance_lock():
        notify_already_running()
        return
    app = BluettiDesktopApp(start_minimized="--minimized" in sys.argv)
    app.mainloop()


if __name__ == "__main__":
    main()
