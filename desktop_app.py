"""
====================================================================
BLUETTI ELITE 100 V2 - APLICACIÓN NATIVA DE ESCRITORIO PARA WINDOWS
====================================================================
Ejecuta la MISMA INTERFAZ MODERNA de la vista previa (React, Tailwind CSS,
diagrama de flujo animado, HUD Gamer, medidor circular y pestañas)
en una ventana nativa de Windows 100% OFFLINE, sin necesidad de Internet,
sin cuentas de Google y con conexión BLE ECDH continua y estable.
"""

import http.server
import json
import logging
import mimetypes
import os
import socketserver
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from typing import Optional

from ble_manager import BluettiBleManager

# Asegurar tipos MIME correctos para evitar pantalla negra en Windows (ES modules)
mimetypes.init()
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/javascript", ".mjs")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("text/html", ".html")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("application/json", ".json")

# Logging
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s (Desktop): %(message)s")
logger = logging.getLogger("BluettiDesktop")

PORT = 8765

# Instancia global del Gestor BLE Oficial con Criptografía ECDH
ble = BluettiBleManager(
    poll_interval=1.5,
    default_mac="DC:B4:D9:54:78:1A"
)


def get_web_directory():
    """Localiza el directorio web compilado de React (dist) o el fallback local."""
    candidates = []
    
    # 1. Directorio temporal de PyInstaller si está compilado
    if hasattr(sys, "_MEIPASS"):
        candidates.append(os.path.join(sys._MEIPASS, "dist"))
        candidates.append(sys._MEIPASS)

    base_dir = os.path.dirname(os.path.abspath(__file__))
    candidates.extend([
        os.path.join(base_dir, "dist"),
        os.path.join(base_dir, "..", "dist"),
        os.path.join(os.getcwd(), "dist"),
        os.path.join(base_dir, "windows_hud", "dist"),
        base_dir,
        os.getcwd()
    ])

    for d in candidates:
        if d and os.path.exists(os.path.join(d, "index.html")):
            logger.info(f"Directorio de interfaz React encontrado: {d}")
            return os.path.abspath(d)
            
    for d in candidates:
        if d and os.path.exists(os.path.join(d, "standalone_ui.html")):
            logger.info(f"Directorio de interfaz standalone encontrado: {d}")
            return os.path.abspath(d)

    return base_dir


SERVE_DIR = get_web_directory()
logger.info(f"Sirviendo interfaz desde: {SERVE_DIR}")


class BluettiHttpHandler(http.server.SimpleHTTPRequestHandler):
    """Manejador HTTP local con soporte para API REST, MIME types estrictos y archivos estáticos."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=SERVE_DIR, **kwargs)

    def guess_type(self, path):
        """Fuerza los tipos MIME correctos para evitar bloqueos de ES Modules en Windows."""
        p_str = str(path).lower()
        if p_str.endswith(".js") or p_str.endswith(".mjs"):
            return "application/javascript; charset=utf-8"
        if p_str.endswith(".css"):
            return "text/css; charset=utf-8"
        if p_str.endswith(".html"):
            return "text/html; charset=utf-8"
        if p_str.endswith(".json"):
            return "application/json; charset=utf-8"
        if p_str.endswith(".svg"):
            return "image/svg+xml"
        return super().guess_type(path)

    def log_message(self, format, *args):
        # Desactivar logs ruidosos de polling
        pass

    def do_GET(self):
        # Endpoints API REST
        if self.path == "/api/telemetry" or self.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            data = json.dumps(ble.state).encode("utf-8")
            self.wfile.write(data)
            return

        # Redirección raíz a index.html o standalone_ui.html
        if self.path in ("/", ""):
            index_path = os.path.join(SERVE_DIR, "index.html")
            if not os.path.exists(index_path) and os.path.exists(os.path.join(SERVE_DIR, "standalone_ui.html")):
                self.path = "/standalone_ui.html"
            else:
                self.path = "/index.html"

        super().do_GET()

    def do_POST(self):
        if self.path.startswith("/api/command/"):
            action = self.path.split("/api/command/")[-1]
            content_len = int(self.headers.get("Content-Length", 0))
            post_body = self.rfile.read(content_len) if content_len > 0 else b"{}"

            try:
                params = json.loads(post_body.decode("utf-8")) if post_body else {}
            except Exception:
                params = {}

            logger.info(f"Comando de Hardware recibido: {action} {params}")

            if action == "set_ac_output":
                ble.set_ac_output(params.get("turn_on", True))
            elif action == "set_dc_output":
                ble.set_dc_output(params.get("turn_on", True))
            elif action == "set_eco_mode":
                ble.set_eco_mode(params.get("turn_on", True))
            elif action == "set_power_lifting":
                ble.set_power_lifting(params.get("turn_on", True))
            elif action == "set_grid_enhancement":
                ble.set_grid_enhancement(params.get("turn_on", True))
            elif action == "set_charge_mode":
                ble.set_charge_mode(params.get("mode", "standard"))
            elif action == "toggle_simulation":
                ble.toggle_simulation_mode(params.get("enabled", None))
            elif action == "release_ble":
                ble.pause_and_release()
            elif action == "resume_ble":
                ble.resume_connection()

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "state": ble.state}).encode("utf-8"))
            return

        super().do_POST()

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()


class ReusableTCPServer(socketserver.TCPServer):
    allow_reuse_address = True


def start_server_on_free_port() -> int:
    """Busca un puerto libre a partir de 8765 e inicia el servidor HTTP."""
    global SERVE_DIR
    SERVE_DIR = get_web_directory()
    logger.info(f"Directorio de recursos web seleccionado: {SERVE_DIR}")

    for p in range(PORT, PORT + 20):
        try:
            httpd = ReusableTCPServer(("127.0.0.1", p), BluettiHttpHandler)
            logger.info(f"Servidor HTTP local escuchando en http://127.0.0.1:{p}")
            t = threading.Thread(target=httpd.serve_forever, daemon=True)
            t.start()
            return p
        except OSError:
            continue
    # Si todos fallan, dejar que el sistema asigne uno
    httpd = ReusableTCPServer(("127.0.0.1", 0), BluettiHttpHandler)
    allocated_port = httpd.server_address[1]
    logger.info(f"Servidor HTTP local asignado en http://127.0.0.1:{allocated_port}")
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    return allocated_port


def wait_for_server(port: int, timeout: float = 4.0) -> bool:
    """Verifica activamente que el servidor local esté listo antes de abrir la ventana."""
    start_t = time.time()
    url = f"http://127.0.0.1:{port}/api/status"
    while time.time() - start_t < timeout:
        try:
            with urllib.request.urlopen(url, timeout=0.5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.1)
    return False


def launch_native_window(target_url: str):
    """
    Abre la interfaz en una ventana de escritorio independiente, sin barras de navegación,
    utilizando Edge/Chrome en modo aplicación dedicada o WebView2.
    """
    # 1. Modo Aplicación Dedicada con Microsoft Edge / Google Chrome nativo de Windows
    browsers = [
        os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
        os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
        os.path.expandvars(r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe")
    ]

    for browser_exe in browsers:
        if os.path.exists(browser_exe):
            logger.info(f"Lanzando ventana de escritorio nativa: {browser_exe}")
            subprocess.Popen([browser_exe, f"--app={target_url}"])
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                ble.stop()
            return

    # 2. Intentar PyWebView
    try:
        import webview
        window = webview.create_window(
            title="Bluetti Elite 100 V2 • Centro de Control Oficial",
            url=target_url,
            width=1180,
            height=820,
            min_size=(850, 600),
            background_color="#020617"
        )
        webview.start(gui="edgechromium", debug=False)
        return
    except Exception:
        pass

    # 3. Fallback a navegador del sistema
    logger.info("Abriendo en el navegador predeterminado...")
    webbrowser.open(target_url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        ble.stop()


def main():
    print("=" * 70)
    print("   BLUETTI ELITE 100 V2 - APLICACION OFICIAL DE ESCRITORIO")
    print("   100% Offline • Diseno Oficial React • Cifrado ECDH + AES")
    print("=" * 70)
    print()

    # 1. Iniciar motor BLE en segundo plano
    ble.start()

    # 2. Iniciar servidor web local en puerto libre verificado
    active_port = start_server_on_free_port()
    wait_for_server(active_port, timeout=3.0)

    target_url = f"http://127.0.0.1:{active_port}/index.html"
    print(f"[OK] Panel web listo en: {target_url}")

    # 3. Abrir ventana de escritorio nativa
    launch_native_window(target_url)


if __name__ == "__main__":
    main()
