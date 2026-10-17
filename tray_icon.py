"""
tray_icon.py - Icono en la bandeja del sistema (Windows) para la app Bluetti
============================================================================
Requiere:  pip install pystray pillow
Si no estan instalados, TRAY_AVAILABLE = False y la app se comporta como
antes (minimizar la deja en la barra de tareas). Nada se rompe.

pystray corre en su PROPIO hilo; ese hilo NO toca Tk (no es seguro): solo
deja eventos ("show", "widget", "startup", "quit") en self.events y el hilo
de Tk los procesa con after() (ver BluettiDesktopApp._tray_poll).
"""

import queue
import threading

try:
    import pystray
    from PIL import Image, ImageDraw
    TRAY_AVAILABLE = True
except Exception:          # librerias no instaladas
    pystray = None
    TRAY_AVAILABLE = False


def _make_icon_image(size: int = 64):
    """Icono dibujado por codigo (rayo cian sobre fondo oscuro): sin archivos .ico externos."""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, size - 3, size - 3), radius=14, fill=(15, 23, 42, 255),
                        outline=(0, 240, 255, 255), width=3)
    d.polygon([(37, 8), (16, 36), (29, 36), (24, 56), (48, 26), (34, 26)], fill=(0, 240, 255, 255))
    return img


class TrayController:
    def __init__(self, title: str, is_startup_enabled=None):
        self.events: "queue.Queue[str]" = queue.Queue()
        self.widget_open = False                      # lo mantiene la app (para el check del menu)
        self._title = title
        self._is_startup = is_startup_enabled or (lambda: False)
        self._icon = None
        self._last_tip = None
        self._last_state = None

    def start(self) -> bool:
        if not TRAY_AVAILABLE:
            return False
        try:
            put = self.events.put
            menu = pystray.Menu(
                pystray.MenuItem("Abrir panel", lambda icon, item: put("show"), default=True),   # clic izquierdo
                pystray.MenuItem("Widget flotante", lambda icon, item: put("widget"),
                                 checked=lambda item: self.widget_open),
                pystray.MenuItem("Iniciar con Windows", lambda icon, item: put("startup"),
                                 checked=lambda item: bool(self._is_startup())),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Salir", lambda icon, item: put("quit")),
            )
            self._icon = pystray.Icon("BluettiElite100", _make_icon_image(), self._title, menu)
            threading.Thread(target=self._icon.run, name="bluetti-tray", daemon=True).start()
            return True
        except Exception:
            self._icon = None
            return False

    def refresh(self):
        """Reconstruye el menu solo si cambio algun check (pystray no los reevalua por si solo)."""
        if self._icon is None:
            return
        state = (bool(self.widget_open), bool(self._is_startup()))
        if state != self._last_state:
            self._last_state = state
            try:
                self._icon.update_menu()
            except Exception:
                pass

    def set_tooltip(self, text: str):
        if self._icon is None or text == self._last_tip:
            return
        self._last_tip = text
        try:
            self._icon.title = text[:120]
        except Exception:
            pass

    def stop(self):
        if self._icon is not None:
            try:
                self._icon.stop()
            except Exception:
                pass
            self._icon = None