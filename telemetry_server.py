"""
telemetry_server.py - Servidor y cliente de red (Fases 1 y 2) para Bluetti Elite 100 V2
=========================================================================================
Un solo archivo con todo lo necesario:

  * TelemetryServer : servidor HTTP + WebSocket (aiohttp) que emite el estado de
                      BluettiBleManager y acepta comandos (lista blanca).
  * RemoteBleManager: el CLIENTE. Se comporta como BluettiBleManager pero en vez de
                      hablar por Bluetooth lee los datos de otro servidor por WiFi.
                      La app y el widget lo usan sin saber la diferencia.
  * INDEX_HTML      : la pagina web del telefono (batería, entradas, salidas,
                      switches AC/DC), embebida aqui para no tener carpeta "web/".
  * load/save_server_config : ajustes persistentes (server_config.json).
  * ServerUiMixin   : los controles "Servidor/Cliente [switch] ⚙" que se añaden a
                      la ventana principal (hud_app.py) con un parche minimo.

Reglas de seguridad:
  - Dos contrasenas: "ver" (solo lectura) y "controlar" (lectura + comandos).
    Si la de controlar esta vacia, NADIE puede controlar.
  - Lista blanca de comandos (ver COMMANDS), con validacion de cada argumento.
  - El servidor nunca escribe registros: llama a los metodos que ya existen.
  - Bloqueo temporal por IP tras varios intentos fallidos.
  - Sin enlace con la Bluetti se rechazan los comandos (no se encolan viejos).
"""

import asyncio
import base64
import datetime
import hmac
import json
import logging
import os
import re
import secrets
import socket
import sys
import threading
import time
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

AIOHTTP_ERR = ""          # motivo real si aiohttp no se pudo importar (se muestra en el aviso)
try:
    import aiohttp
    from aiohttp import web, WSMsgType
    AIOHTTP_OK = True
except Exception as _exc:  # aiohttp no instalado o no se pudo importar
    AIOHTTP_ERR = f"{type(_exc).__name__}: {_exc}"
    aiohttp = None
    web = None
    WSMsgType = None
    AIOHTTP_OK = False

logger = logging.getLogger("BluettiServer")

DEFAULT_PORT = 8765
PORT_SPAN = 20                 # si el puerto esta ocupado/reservado, el servidor prueba los PORT_SPAN siguientes
                               # y el cliente los busca solos (asi no hay que tocar nada si cambia)
APP_ID = "bluetti-telemetry"   # lo que responde /api/ping: el cliente comprueba que habla con ESTA app
PUSH_INTERVAL_S = 0.5          # como maximo 2 envios por segundo
AUTH_TIMEOUT_S = 6.0           # tiempo para enviar la contrasena tras abrir el WebSocket
MAX_FAILS = 5                  # intentos fallidos antes de bloquear la IP...
LOCKOUT_S = 60.0               # ...durante este tiempo
CMD_MIN_INTERVAL_S = 0.15      # separacion minima entre comandos de un mismo cliente
SKY_MIN_GAP_S = 0.5            # como mucho 1 peticion de condiciones/estadisticas cada 0,5 s por cliente
SKY_TIMEOUT_S = 4.0
ALERT_MIN_GAP_S = 0.4            # la app (hilo de Tk) tiene este tiempo para preparar los datos
STALE_S = 20.0                 # sin telemetria nueva de la Bluetti durante este tiempo = enlace dudoso (no se muestran datos viejos como si fueran vivos)
IGNORED_KEYS = {"last_update"}                  # claves que cambian siempre y no aportan


def _is_bool(v):
    return isinstance(v, bool)


def _one_of(*options):
    return lambda v: isinstance(v, str) and v in options


def _int_between(lo, hi):
    return lambda v: isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


# LISTA BLANCA de comandos: nombre -> argumentos (nombre, validador) EN EL ORDEN en que se pasan
# al metodo de BluettiBleManager. Cualquier otra cosa se rechaza. El servidor nunca escribe
# registros: solo llama a estos metodos, que ya llevan sus propias protecciones.
COMMANDS = {
    "set_ac_output": (("on", _is_bool),),
    "set_dc_output": (("on", _is_bool),),
    "set_eco_mode": (("on", _is_bool),),
    "set_power_lifting": (("on", _is_bool),),
    "set_grid_enhancement": (("on", _is_bool),),
    "set_dc_high_current": (("on", _is_bool),),
    "set_charge_mode": (("mode", _one_of("silent", "standard", "turbo", "custom")),),
    "set_dc_source": (("source", _one_of("pv", "other")),),
    "set_grid_max_current": (("amps", _int_between(1, 15)),),   # mismo rango que GRID_LIMIT_MIN/MAX_A
    "set_system_power_off": (),  # Apagado total del sistema (registro 2013)
    "ack_ac_reduced_notice": (),
    # --- los resuelve la propia app (ServerUiMixin), no el gestor BLE ---
    "dismiss_alert": (("kind", _one_of("dc", "temp", "other")),),   # la "x" de la barra de estado
    "ble_release": (),         # «Desconectar BLE» de la PC (suelta el Bluetooth)
    "ble_connect": (),         # «Conectar BLE» de la PC
}
APP_COMMANDS = {"dismiss_alert", "ble_release", "ble_connect", "set_system_power_off"}
NO_LINK_NEEDED = {"ack_ac_reduced_notice"} | APP_COMMANDS   # no necesitan enlace con la Bluetti

DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": False,          # el servidor arranca apagado
    "mode": "server",          # "server" | "client"
    "port": DEFAULT_PORT,
    "view_password": "",       # solo ver
    "control_password": "",    # ver + controlar (vacia = control desactivado)
    "client_host": "",         # modo Cliente: direccion (IP) del servidor al que conectarse
    "client_port": DEFAULT_PORT,
    "client_password": "",
    "qr_with_password": False, # el QR del telefono lleva (o no) la contrasena «Solo ver»
    "push_enabled": False,     # avisos al telefono con la app ntfy (apagado por defecto)
    "push_topic": "",          # «tema» de ntfy: hace de contrasena de los avisos (largo y aleatorio)
    "push_server": "https://ntfy.sh",
    "relay_enabled": False,    # acceso desde internet a traves de tu relay de Cloudflare (apagado por defecto)
    "relay_url": "",           # p. ej. https://bluetti-relay.tu-cuenta.workers.dev
    "relay_key": "",           # clave que comparten la app y el relay (la genera desplegar_relay.py)
    "relay_allow_control": False,   # False: desde internet solo se puede VER, aunque se use la clave de «Controlar»
}
MIN_REMOTE_PW = 8              # con acceso desde internet las contrasenas deben tener al menos 8 caracteres


# =============================================================================
# CONFIGURACION PERSISTENTE (misma carpeta que widget_config.json)
# =============================================================================
def _config_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "BluettiElite100Widget")
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception:
        pass
    return os.path.join(folder, "server_config.json")


def normalize_relay_url(raw: Any) -> str:
    """'x.workers.dev', 'https://x.workers.dev/loquesea' -> 'https://x.workers.dev'. '' si no vale.
    (http:// solo se admite para pruebas en esta misma PC.)"""
    if not isinstance(raw, str):
        return ""
    t = raw.strip()
    if not t:
        return ""
    if "://" not in t:
        t = "https://" + t
    scheme, _, rest = t.partition("://")
    host = rest.split("/")[0].strip()
    scheme = scheme.lower()
    if not host or " " in host:
        return ""
    if scheme == "https" or (scheme == "http" and re.match(r"^(localhost|127\.\d+\.\d+\.\d+)(:\d+)?$", host)):
        return f"{scheme}://{host}"
    return ""


def _sanitize(cfg: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(DEFAULT_CONFIG)
    out["enabled"] = bool(cfg.get("enabled"))
    out["mode"] = "client" if cfg.get("mode") == "client" else "server"
    out["qr_with_password"] = bool(cfg.get("qr_with_password"))
    out["push_enabled"] = bool(cfg.get("push_enabled"))
    topic = cfg.get("push_topic")
    out["push_topic"] = re.sub(r"[^A-Za-z0-9_-]", "", topic)[:64] if isinstance(topic, str) else ""
    out["relay_enabled"] = bool(cfg.get("relay_enabled"))
    out["relay_allow_control"] = bool(cfg.get("relay_allow_control"))
    out["relay_url"] = normalize_relay_url(cfg.get("relay_url"))
    key = cfg.get("relay_key")
    out["relay_key"] = key.strip() if isinstance(key, str) else ""
    srv = cfg.get("push_server")
    srv = srv.strip().rstrip("/") if isinstance(srv, str) else ""
    out["push_server"] = srv if srv.lower().startswith(("http://", "https://")) else DEFAULT_CONFIG["push_server"]
    for key in ("port", "client_port"):
        try:
            p = int(cfg.get(key))
        except (TypeError, ValueError):
            p = DEFAULT_PORT
        out[key] = p if 1024 <= p <= 65535 else DEFAULT_PORT
    for key in ("view_password", "control_password", "client_host", "client_password"):
        v = cfg.get(key)
        out[key] = v if isinstance(v, str) else ""
    return out


def load_server_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(_config_path(), "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for k in DEFAULT_CONFIG:
                if k in raw:
                    cfg[k] = raw[k]
    except Exception:
        pass
    return _sanitize(cfg)


def save_server_config(cfg: Dict[str, Any]) -> bool:
    path = _config_path()
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_sanitize(cfg), f, indent=1, ensure_ascii=False)
        os.replace(tmp, path)
        return True
    except Exception:
        return False


# =============================================================================
# CODIGO QR (minimo, sin dependencias): modo bytes, correccion de errores M, versiones 1-10
# =============================================================================
_QR_M = {   # version: (codewords EC por bloque, [(n bloques, codewords de datos), ...])
    1: (10, [(1, 16)]), 2: (16, [(1, 28)]), 3: (26, [(1, 44)]), 4: (18, [(2, 32)]), 5: (24, [(2, 43)]),
    6: (16, [(4, 27)]), 7: (18, [(4, 31)]), 8: (22, [(2, 38), (2, 39)]), 9: (22, [(3, 36), (2, 37)]),
    10: (26, [(4, 43), (1, 44)]),
}
_QR_ALIGN = {1: [], 2: [6, 18], 3: [6, 22], 4: [6, 26], 5: [6, 30], 6: [6, 34], 7: [6, 22, 38],
             8: [6, 24, 42], 9: [6, 26, 46], 10: [6, 28, 50]}


def _gf_tables():
    exp, log, x = [0] * 512, [0] * 256, 1
    for i in range(255):
        exp[i], log[x] = x, i
        x <<= 1
        if x & 0x100:
            x ^= 0x11D
    for i in range(255, 512):
        exp[i] = exp[i - 255]
    return exp, log


_EXP, _LOG = _gf_tables()


def _rs_ec(data: List[int], n: int) -> List[int]:
    gen = [1]
    for i in range(n):                                   # polinomio generador
        nxt = [0] * (len(gen) + 1)
        for j, c in enumerate(gen):
            nxt[j] ^= c
            if c:
                nxt[j + 1] ^= _EXP[_LOG[c] + i]
        gen = nxt
    rem = [0] * n
    for b in data:
        f = b ^ rem[0]
        rem = rem[1:] + [0]
        if f:
            for i in range(n):
                if gen[i + 1]:
                    rem[i] ^= _EXP[_LOG[gen[i + 1]] + _LOG[f]]
    return rem


def _bch(value: int, poly: int, bits_total: int, bits_data: int) -> int:
    v = value << (bits_total - bits_data)
    for i in range(bits_data - 1, -1, -1):
        if (v >> (i + bits_total - bits_data)) & 1:
            v ^= poly << i
    return (value << (bits_total - bits_data)) | v


def qr_matrix(text: str) -> List[List[bool]]:
    """Matriz del QR (True = modulo oscuro, SIN borde blanco). ValueError si el texto no cabe."""
    raw = text.encode("utf-8")
    ver = next((v for v in range(1, 11)
                if len(raw) <= sum(n * d for n, d in _QR_M[v][1]) - (2 if v < 10 else 3)), None)
    if ver is None:
        raise ValueError("texto demasiado largo para el QR")
    ec_len, groups = _QR_M[ver]
    cap = sum(n * d for n, d in groups)
    bits = "0100" + format(len(raw), "08b" if ver < 10 else "016b") + "".join(format(b, "08b") for b in raw)
    bits += "0" * min(4, cap * 8 - len(bits))
    bits += "0" * (-len(bits) % 8)
    data = [int(bits[i:i + 8], 2) for i in range(0, len(bits), 8)]
    pad = [0xEC, 0x11]
    while len(data) < cap:
        data.append(pad[(len(data) - len(bits) // 8) % 2])
    blocks, pos = [], 0
    for n, d in groups:
        for _ in range(n):
            blocks.append(data[pos:pos + d])
            pos += d
    ecs = [_rs_ec(b, ec_len) for b in blocks]
    final = []
    for i in range(max(len(b) for b in blocks)):
        final += [b[i] for b in blocks if i < len(b)]
    for i in range(ec_len):
        final += [e[i] for e in ecs]
    stream = "".join(format(b, "08b") for b in final)

    size = 17 + 4 * ver
    mod = [[False] * size for _ in range(size)]
    fn = [[False] * size for _ in range(size)]          # modulos de funcion (no llevan datos)

    def put(x, y, dark):
        if 0 <= x < size and 0 <= y < size:
            mod[y][x] = dark
            fn[y][x] = True

    for i in range(size):                                          # lineas de sincronia
        put(6, i, i % 2 == 0)
        put(i, 6, i % 2 == 0)
    for cx, cy in ((3, 3), (size - 4, 3), (3, size - 4)):          # buscadores + separadores
        for dy in range(-4, 5):
            for dx in range(-4, 5):
                d = max(abs(dx), abs(dy))
                put(cx + dx, cy + dy, d not in (2, 4))
    al = _QR_ALIGN[ver]
    for ay in al:
        for ax in al:
            if (ax, ay) in ((6, 6), (6, al[-1]), (al[-1], 6)):
                continue
            for dy in range(-2, 3):
                for dx in range(-2, 3):
                    put(ax + dx, ay + dy, max(abs(dx), abs(dy)) != 1)
    if ver >= 7:                                                   # informacion de version
        vi = _bch(ver, 0x1F25, 18, 6)
        for i in range(18):
            b = (vi >> i) & 1 == 1
            put(size - 11 + i % 3, i // 3, b)
            put(i // 3, size - 11 + i % 3, b)
    for i in range(9):
        fn[8][i] = fn[i][8] = True
    for i in range(8):
        fn[8][size - 1 - i] = fn[size - 1 - i][8] = True
    put(8, size - 8, True)                                         # modulo oscuro fijo

    # colocar datos en zigzag
    k, up = 0, True
    for right in range(size - 1, 0, -2):
        if right == 6:
            right = 5
        for v in range(size):
            y = size - 1 - v if up else v
            for x in (right, right - 1):
                if not fn[y][x]:
                    mod[y][x] = k < len(stream) and stream[k] == "1"
                    k += 1
        up = not up

    masks = [lambda x, y: (x + y) % 2 == 0, lambda x, y: y % 2 == 0, lambda x, y: x % 3 == 0,
             lambda x, y: (x + y) % 3 == 0, lambda x, y: (x // 3 + y // 2) % 2 == 0,
             lambda x, y: x * y % 2 + x * y % 3 == 0, lambda x, y: (x * y % 2 + x * y % 3) % 2 == 0,
             lambda x, y: ((x + y) % 2 + x * y % 3) % 2 == 0]

    def build(m):
        out = [row[:] for row in mod]
        for y in range(size):
            for x in range(size):
                if not fn[y][x] and masks[m](x, y):
                    out[y][x] = not out[y][x]
        f = _bch((0b00 << 3) | m, 0x537, 15, 5) ^ 0x5412        # nivel M = 00
        bit = lambda i: (f >> i) & 1 == 1
        for i in range(6):
            out[i][8] = bit(i)
        out[7][8], out[8][8], out[8][7] = bit(6), bit(7), bit(8)
        for i in range(9, 15):
            out[8][14 - i] = bit(i)
        for i in range(8):
            out[8][size - 1 - i] = bit(i)
        for i in range(8, 15):
            out[size - 15 + i][8] = bit(i)
        out[size - 8][8] = True
        return out

    def penalty(g):
        p = 0
        for lines in (g, [list(c) for c in zip(*g)]):
            for row in lines:
                run = 1
                for i in range(1, size):
                    if row[i] == row[i - 1]:
                        run += 1
                        if run == 5:
                            p += 3
                        elif run > 5:
                            p += 1
                    else:
                        run = 1
                s = "".join("1" if c else "0" for c in row)
                p += 40 * (s.count("10111010000") + s.count("00001011101"))
        for y in range(size - 1):
            for x in range(size - 1):
                if g[y][x] == g[y][x + 1] == g[y + 1][x] == g[y + 1][x + 1]:
                    p += 3
        dark = sum(c for row in g for c in row)
        return p + 10 * (abs(dark * 20 - size * size * 10) // (size * size))

    return min((build(m) for m in range(8)), key=penalty)


# =============================================================================
# FRAGMENTADOR TLS (para redes que cortan *.workers.dev leyendo el nombre del saludo TLS)
# Un mini proxy local (solo 127.0.0.1, solo hacia el relay) parte el primer mensaje TLS (ClientHello)
# en dos registros, justo en medio del nombre del servidor. El filtro no llega a leer el nombre
# completo; el servidor de Cloudflare reensambla el mensaje sin problema. El cifrado sigue siendo
# de extremo a extremo (el proxy solo ve bytes cifrados). Si falla, se conecta directo como siempre.
# =============================================================================
def _frag_client_hello(data: bytes, sni: str) -> bytes:
    """Devuelve `data` con el primer registro TLS partido en dos (si es un ClientHello); si no, igual."""
    if len(data) < 6 or data[0] != 0x16 or data[1] != 0x03:
        return data
    rec_len = int.from_bytes(data[3:5], "big")
    if rec_len < 2 or len(data) < 5 + rec_len:
        return data
    payload, rest, ver = data[5:5 + rec_len], data[5 + rec_len:], data[1:3]
    i = payload.find(sni.encode("ascii", "ignore")) if sni else -1
    cut = (i + max(1, len(sni) // 2)) if i > 0 else len(payload) // 2
    cut = max(1, min(cut, len(payload) - 1))
    r1 = b"\x16" + ver + cut.to_bytes(2, "big") + payload[:cut]
    r2 = b"\x16" + ver + (len(payload) - cut).to_bytes(2, "big") + payload[cut:]
    return r1 + r2 + rest


class _FragProxy:
    """Uso:  async with _FragProxy(url_wss) as px:  ...  proxy=px.url   (px.url es None si no hace falta)."""

    def __init__(self, url: str):
        self.url: Optional[str] = None
        self._target: Optional[Tuple[str, int]] = None
        self._srv = None
        self._writers: set = set()
        if isinstance(url, str) and url.startswith("wss://"):
            host, _, port = url.split("://", 1)[1].split("/")[0].partition(":")
            try:
                p = int(port) if port else 443
            except ValueError:
                p = 0
            if host and p:
                self._target = (host.lower(), p)

    async def __aenter__(self):
        if self._target is not None:
            try:
                self._srv = await asyncio.start_server(self._handle, "127.0.0.1", 0)
                self.url = "http://127.0.0.1:%d" % self._srv.sockets[0].getsockname()[1]
            except Exception as exc:
                logger.info("Fragmentador TLS no disponible: %s", exc)
                self._srv, self.url = None, None
        return self

    async def __aexit__(self, *exc):
        if self._srv is not None:
            self._srv.close()
        for w in list(self._writers):
            try:
                w.close()
            except Exception:
                pass
        self._writers.clear()
        self._srv, self.url = None, None

    async def _handle(self, reader, writer):
        self._writers.add(writer)
        up_w = None
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10.0)
            parts = head.split(b"\r\n", 1)[0].decode("latin1").split()
            ok = False
            if len(parts) >= 2 and parts[0].upper() == "CONNECT":
                host, _, port = parts[1].rpartition(":")
                ok = (host.strip("[]").lower(), int(port)) == self._target
            if not ok:                      # no es un proxy abierto: solo deja pasar hacia el relay
                writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
                await writer.drain()
                return
            up_r, up_w = await asyncio.wait_for(asyncio.open_connection(host, int(port)), 10.0)
            self._writers.add(up_w)
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            tasks = [asyncio.ensure_future(self._pump_up(reader, up_w, host)),
                     asyncio.ensure_future(self._pump(up_r, writer))]
            try:
                await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for t in tasks:
                    t.cancel()
        except Exception as exc:
            logger.debug("Fragmentador TLS: %s", exc)
        finally:
            for w in (writer, up_w):
                if w is not None:
                    self._writers.discard(w)
                    try:
                        w.close()
                    except Exception:
                        pass

    @staticmethod
    async def _pump_up(reader, up_w, sni: str):
        """Del programa hacia el relay: parte el primer registro TLS y copia el resto tal cual."""
        buf = b""
        while len(buf) < 5 or (buf[0] == 0x16 and len(buf) < 5 + int.from_bytes(buf[3:5], "big") <= 20000):
            chunk = await asyncio.wait_for(reader.read(16384), 8.0)
            if not chunk:
                break
            buf += chunk
        if buf:
            up_w.write(_frag_client_hello(buf, sni))
            await up_w.drain()
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                return
            up_w.write(chunk)
            await up_w.drain()

    @staticmethod
    async def _pump(src, dst):
        while True:
            chunk = await src.read(65536)
            if not chunk:
                return
            dst.write(chunk)
            await dst.drain()


async def _ws_connect(sess, px_url: Optional[str], url: str, **kw):
    """ws_connect hacia el relay: primero con el saludo TLS fragmentado; si no se puede abrir, directo."""
    if px_url:
        try:
            ws = await sess.ws_connect(url, proxy=px_url, **kw)
            logger.info("Conexion al relay con TLS fragmentado")
            return ws
        except aiohttp.WSServerHandshakeError:
            raise                          # el relay SI respondio (clave mala, etc.): no es un bloqueo
        except (OSError, asyncio.TimeoutError, aiohttp.ClientError) as exc:
            logger.info("TLS fragmentado no funciono (%s); se prueba directo", exc)
    return await sess.ws_connect(url, **kw)


# =============================================================================
# SERVIDOR
# =============================================================================
class _VMsg:
    """Mensaje recibido de un telefono por el relay (misma forma que los de aiohttp)."""
    __slots__ = ("type", "data")

    def __init__(self, typ, data=None):
        self.type, self.data = typ, data


class _VirtualWs:
    """Un telefono que llega por el relay: se comporta como un WebSocket normal para el resto del servidor."""

    def __init__(self, send, cid: str, ip: str):
        self._send = send                       # corrutina: manda un dict al relay
        self.cid, self.ip = cid, ip
        self.queue: "asyncio.Queue" = asyncio.Queue(maxsize=64)
        self.closed = False

    async def send_str(self, text: str):
        if not self.closed:
            await self._send({"t": "m", "c": self.cid, "d": text})

    async def send_json(self, obj):
        await self.send_str(json.dumps(obj))

    async def close(self, code=None):
        if not self.closed:
            self.closed = True
            await self._send({"t": "close", "c": self.cid})
            self._wake()

    def feed(self, text: str):
        try:
            self.queue.put_nowait(text)
        except asyncio.QueueFull:               # un telefono que inunda: se descarta
            pass

    def remote_closed(self):
        self.closed = True
        self._wake()

    def _wake(self):
        try:
            self.queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    async def receive(self, timeout=None):
        item = await asyncio.wait_for(self.queue.get(), timeout)
        if item is None:
            return _VMsg(WSMsgType.CLOSED)
        return _VMsg(WSMsgType.TEXT, item)

    def __aiter__(self):
        return self

    async def __anext__(self):
        m = await self.receive()
        if m.type != WSMsgType.TEXT:
            raise StopAsyncIteration
        return m


# =============================================================================
# [sync-datos] BLOQUE DE DATOS: sincronizacion de estadisticas entre dos PC (LAN)
# Se inserta en telemetry_server.py: es el unico modulo de esta app y ambas PC
# ejecutan la misma version (una como servidor, otra como cliente via RemoteBleManager).
# =============================================================================
"""
Sincronizacion de DATOS entre dos PC (parche [sync-datos])
==========================================================
Bloque incrustado en telemetry_server.py. Comparte SOLO los datos de estadisticas
entre la PC servidor y la PC cliente, por la LAN del servidor de telemetria
(NUNCA por internet, y sin tocar los archivos de configuracion de cada PC:
server_config, widget_config, alert_settings, app_state, sky_window,
protection_state ni *.log).

Que se comparte (y como se fusiona, sin borrar nada):
  * sky_log.json        (>datos para los graficos de estado/produccion):
                        se unen los dias y sus puntos; los minutos que solo
                        tiene una PC se anaden; franjas aprendidas "slots" y
                        "pend" se unen tomando el mayor; la energia diaria se
                        queda con el valor mayor; el resumen por dia lo vuelve
                        a calcular la propia app al recargar.
  * cell_stats.json     (salud por celda): el formato actual queda tal cual
                        ("cells" = esta PC) y se anade "peers": {pc_id: {...}}.
                        Asi cada PC conserva su cuenta y la de los demas; la
                        suma la puede leer cell_totals().
  * capacity_samples.json (capacidad medida): union de muestras (con su
                        sentido), sin duplicar valores iguales.
  * registro_bateria.csv / cell_events.csv / spike_log.csv: union de filas
                        sin repetir lineas identicas, manteniendo el orden.

Reglas de seguridad:
  * El transporte (no este modulo) solo lo acepta en la LAN y con la
    contrasena de «Controlar».
  * Este modulo solo lee/escribe los 6 archivos de arriba. Nada mas.
  * Escritura de JSON siempre con archivo temporal + os.replace (atomica).
  * Los CSV se fusionan por anadido de las filas que faltan (asi un corte
    de luz no deja el archivo a medias ni se pierde lo recien escrito).
"""
import base64
import json
import os
import socket
import time

# ------------------------------------------------------------------ limites
PAYLOAD_MAX_BYTES = 12_000_000    # payload total que se envia (tras recortar CSV)
CHUNK_RAW_BYTES   = 4_300         # trozo bruto; con base64 queda <= ~5800 (cae en los 8192 del WS)
CSV_MAX_ROWS_SEND = 10_000        # de cada CSV se envian como mucho las ultimas N filas
CSV_MAX_BYTES_SEND = 700_000

NAMES = ("sky_log.json", "cell_stats.json", "capacity_samples.json")
CSV_NAMES = ("registro_bateria.csv", "cell_events.csv", "spike_log.csv")

SAMPLE_LIMIT = 24                 # muestras de capacidad tras la union (12 por PC, aprox)


# ------------------------------------------------------------------ carpetas
def data_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "BluettiElite100Widget")
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception:
        pass
    return folder


def _path(name: str) -> str:
    return os.path.join(data_dir(), name)


# ------------------------------------------------------------------ pc_id
_ID_CACHE = [None]


def get_pc_id() -> str:
    """Identificador persistente de ESTA PC (se crea una vez y se guarda en
    sync_state.json). No es secreto: solo sirve para no mezclar cuentas."""
    if _ID_CACHE[0] is not None:
        return _ID_CACHE[0]
    p = _path("sync_state.json")
    pid = None
    try:
        with open(p, "r", encoding="utf-8") as f:
            pid = (json.load(f) or {}).get("pc_id")
    except Exception:
        pass
    if not isinstance(pid, str) or not pid:
        pid = "{:08x}".format(int.from_bytes(os.urandom(4), "big")) + "-" + \
              "{:04x}".format(int.from_bytes(os.urandom(2), "big"))
        try:
            tmp = p + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"pc_id": pid, "host": socket.gethostname()}, f)
            os.replace(tmp, p)
        except Exception:
            pass
    _ID_CACHE[0] = pid
    return pid


def _janitor(n):
    return "".join(c for c in n if c.isalnum() or c in "_-")[:64]


# ------------------------------------------------------------------ io util
def read_json(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def write_json_atomic(path, obj) -> bool:
    try:
        tmp = path + ".tmp-sync"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
        return True
    except Exception:
        return False


# =====================================================================
# COLECTA:  lo que esta PC ofrece compartir
# =====================================================================
def collect_payload() -> dict:
    out = {"pc_id": get_pc_id(), "ts": time.time(), "app": "bluetti-sync/1"}
    for name in NAMES:
        p = _path(name)
        if os.path.exists(p):
            out[name] = read_json(p)
        else:
            out[name] = None
    csv = {}
    for name in CSV_NAMES:
        p = _path(name)
        if not os.path.exists(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                text = f.read()
            lines = text.splitlines()
            if len(lines) > CSV_MAX_ROWS_SEND:
                lines = lines[:1] + lines[-(CSV_MAX_ROWS_SEND - 1):]   # cabecera + ultimas
                text = "\n".join(lines)
            if len(text) > CSV_MAX_BYTES_SEND:
                text = text[-CSV_MAX_BYTES_SEND:]
            csv[name] = text
        except Exception:
            pass
    out["csv"] = csv
    # si nada que compartir, se dice (el transporte no enviara nada)
    if not (out.get("sky_log.json") or out.get("cell_stats.json")
            or out.get("capacity_samples.json") or csv):
        out["empty"] = True
    return out


# =====================================================================
# APLICAR:  fusionar lo recibido en los archivos de ESTA PC (sin borrar)
# =====================================================================
def _split_csv(text: str):
    if not text:
        return []
    return [ln.rstrip("\r") for ln in text.split("\n") if ln.strip("\r ")]


def merge_csv(own_text, peer_text) -> (str, int):
    """Union de filas sin repetir lineas identicas; devuelve (texto, filas nuevas)."""
    own = _split_csv(own_text)
    peer = _split_csv(peer_text)
    header = own[0] if own else (peer[0] if peer else None)
    if header is None:
        return "", 0
    filas = set(own[1:])
    out = list(own[1:])
    added = 0
    for r in peer[1:]:
        if r and r not in filas:
            out.append(r)
            filas.add(r)
            added += 1
    # orden cronologico estable (por la 1a columna cuando parece una fecha/hora)
    try:
        out.sort(key=lambda r: r.split(",", 1)[0])
    except Exception:
        pass
    text = "\n".join([header] + out)
    if out:
        text += "\n"
    return text, added


def apply_csv_file(path, peer_text) -> int:
    """Anade al CSV solo las filas del peer que no estan. Devuelve cuantas filas nuevas."""
    if not peer_text:
        return 0
    own_text = None
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                own_text = f.read()
        except Exception:
            own_text = None
    if own_text and peer_text.strip() == own_text.strip():
        return 0
    text, added = merge_csv(own_text, peer_text)
    if added <= 0 and own_text is not None:
        return 0
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
        return added
    except Exception:
        return 0


def merge_capacity(own, peer):
    """Union de muestras (valor, sentido): se conserva el orden propio y se anaden al final las
    nuevas de la otra PC (la salud usa las ultimas muestras: no se reordenan por valor)."""
    samples, dirs = [], []
    for k, v in enumerate(own.get("samples") or []):
        try:
            samples.append(round(float(v), 1))
            d = (own.get("dirs") or [])[k] if k < len(own.get("dirs") or []) else 0
            dirs.append(int(d or 0))
        except Exception:
            continue
    seen = set(zip(samples, dirs))
    pdirs = peer.get("dirs") or []
    for k, v in enumerate(peer.get("samples") or []):
        try:
            key = (round(float(v), 1), int(pdirs[k] or 0) if k < len(pdirs) else 0)
        except Exception:
            continue
        if key not in seen:
            seen.add(key)
            samples.append(key[0])
            dirs.append(key[1])
    return {"samples": samples[-SAMPLE_LIMIT * 2:], "dirs": dirs[-SAMPLE_LIMIT * 2:]}


def apply_capacity_file(path, peer) -> bool:
    if not isinstance(peer, dict):
        return False
    own = read_json(path) or {}
    merged = merge_capacity(own, peer)
    if [round(float(v), 1) for v in own.get("samples", [])] == merged["samples"]:
        return False
    return write_json_atomic(path, merged)


def merge_cell_stats(own, peer, pc_id):
    """La cuenta de la otra PC se guarda en 'peers'[pc_id] (se reemplaza por la mas reciente, ya que
    sus contadores son acumulados). 'cells' (esta PC) no se toca."""
    peer_cells = peer.get("cells") if isinstance(peer.get("cells"), dict) else None
    if not peer_cells:
        return False
    peers = own.get("peers") if isinstance(own.get("peers"), dict) else {}
    cur = peers.get(pc_id)
    if isinstance(cur, dict) and cur.get("cells") == peer_cells:
        return False
    peers[pc_id] = {"since": peer.get("since"), "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "cells": dict(peer_cells)}
    own["peers"] = peers
    return True


def apply_cell_stats_file(path, peer, pc_id) -> bool:
    if not isinstance(peer, dict) or not pc_id:
        return False
    own = read_json(path)
    if not isinstance(own, dict):
        own = {}
    if merge_cell_stats(own, peer, pc_id):
        return write_json_atomic(path, own)
    return False


def merge_sky(own, peer):
    """Union de dias/puntos/slots/pend/daily sin borrar lo propio."""
    changed_days = 0
    peer_days = peer.get("days") if isinstance(peer.get("days"), dict) else {}
    for day, rec in peer_days.items():
        if not isinstance(rec, dict):
            continue
        cur = own["days"].get(day)
        if cur is None:
            own["days"][day] = dict(rec)
            own["days"][day]["pts"] = list(rec.get("pts") or [])
            changed_days += 1
        else:
            merged = _merge_day(cur, rec)
            if merged["pts"] != cur.get("pts"):
                changed_days += 1
            own["days"][day] = merged
    slots = peer.get("slots") if isinstance(peer.get("slots"), dict) else {}
    for k, v in slots.items():
        try:
            cur = own["slots"].get(k)
            if cur is None or float(v) > float(cur):
                own["slots"][k] = float(v)
        except Exception:
            continue
    pend = peer.get("pend") if isinstance(peer.get("pend"), dict) else {}
    for k, byday in pend.items():
        if not isinstance(byday, dict):
            continue
        d = own["pend"].setdefault(k, {})
        for day, v in byday.items():
            if day not in d:
                d[day] = v
    daily = peer.get("daily") if isinstance(peer.get("daily"), dict) else {}
    for k, v in daily.items():
        if k not in own["daily"]:
            own["daily"][k] = v
    own.setdefault("slots", {})
    own.setdefault("pend", {})
    return changed_days


def _merge_day(own, peer):
    out = dict(own)
    out["pts"] = list(own.get("pts") or [])
    for key in ("wh_in", "wh_panel", "rmax"):
        try:
            out[key] = max(float(out.get(key, 0) or 0), float(peer.get(key, 0) or 0))
        except Exception:
            pass
    b_own = int(own.get("bin") or 120)
    b_peer = int(peer.get("bin") or 120)
    pts_p = list(peer.get("pts") or [])
    if b_own == b_peer:
        d = {}
        for p in pts_p + list(out["pts"]):
            try:
                d[int(p[0] / b_own)] = p
            except Exception:
                continue
        out["pts"] = [d[k] for k in sorted(d)]
    elif len(pts_p) > len(out["pts"]):
        out["pts"] = pts_p
        out["bin"] = b_peer
    return out


def apply_sky_file(path, peer) -> bool:
    if not isinstance(peer, dict):
        return False
    own = read_json(path)
    if not isinstance(own, dict):
        own = {"k": 1.0, "days": {}, "slots": {}, "pend": {}, "daily": {}, "decay_day": None, "model": 1}
    own.setdefault("days", {})
    own.setdefault("slots", {})
    own.setdefault("pend", {})
    own.setdefault("daily", {})
    before = json.dumps(own, sort_keys=True)
    merge_sky(own, peer)
    if json.dumps(own, sort_keys=True) == before:
        return False
    return write_json_atomic(path, own)


# =====================================================================
# Seccion publica para la app: aplica un payload recibido (de un peer)
# =====================================================================
def apply_payload(payload: dict) -> dict:
    """Fusiona el payload de otra PC en los archivos de esta. Devuelve resumen."""
    if not isinstance(payload, dict):
        return {"ok": False, "error": "payload no valido"}
    who = _janitor(str(payload.get("pc_id") or "desconocida"))
    summ = {"ok": True, "from": who, "sky_days": 0, "cell_peer": False,
            "capacity": 0, "csv": {}, "sky_slots": 0}
    try:
        if payload.get("sky_log.json") is not None:
            p = _path("sky_log.json")
            sl = payload["sky_log.json"]
            own_before = len(read_json(p).get("days", {})) if read_json(p) else 0
            if apply_sky_file(p, sl):
                summ["sky_changed"] = True
                own_after = len(read_json(p).get("days", {})) if read_json(p) else 0
                summ["sky_days"] = max(0, own_after - own_before)
                summ["sky_slots"] = len(sl.get("slots") or {})
        if payload.get("cell_stats.json") is not None:
            if apply_cell_stats_file(_path("cell_stats.json"), payload["cell_stats.json"], who):
                summ["cell_peer"] = True
        if isinstance(payload.get("capacity_samples.json"), dict):
            if apply_capacity_file(_path("capacity_samples.json"), payload["capacity_samples.json"]):
                summ["capacity"] = 1
        for name, text in (payload.get("csv") or {}).items():
            if name in CSV_NAMES and text:
                n = apply_csv_file(_path(name), text)
                if n:
                    summ["csv"][name] = n
    except Exception as exc:
        summ.update(ok=False, error="%s: %s" % (type(exc).__name__, exc))
    return summ


def cell_totals(path=None) -> dict:
    """Suma de contadores por celda de ESTA PC mas todas las peers.
    Devuelve {numero_celda: {min_count, max_count, min_s, max_s, alert_count...}}."""
    if path is None:
        path = _path("cell_stats.json")
    data = read_json(path) or {}
    totals = {}
    blocks = [data.get("cells")]
    blocks += [(p.get("cells") or {}) for p in (data.get("peers") or {}).values()]
    for cells in blocks:
        if not isinstance(cells, dict):
            continue
        for i, c in cells.items():
            if not isinstance(c, dict):
                continue
            t = totals.setdefault(str(i), {})
            for k in ("min_count", "max_count", "warn_count", "alert_count"):
                try:
                    t[k] = t.get(k, 0) + int(c.get(k, 0) or 0)
                except Exception:
                    pass
            for k in ("min_s", "max_s"):
                try:
                    t[k] = round(t.get(k, 0.0) + float(c.get(k, 0) or 0), 1)
                except Exception:
                    pass
            for k in ("lowest_mv", "highest_mv"):
                if c.get(k) is not None and (t.get(k) is None
                                             or (k == "lowest_mv" and c[k] < t[k])
                                             or (k == "highest_mv" and c[k] > t[k])):
                    t[k] = c[k]
    return totals


# =====================================================================
# Troceado por la red (ambos sentidos)
# =====================================================================
def split_payload(payload: dict):
    try:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except Exception:
        return None
    if len(data) > PAYLOAD_MAX_BYTES:
        return None
    parts = []
    cut = CHUNK_RAW_BYTES
    while data:
        if len(data) <= cut:
            parts.append(data)
            break
        end = cut
        while end > cut - 4 and (data[end] & 0xC0) == 0x80:   # no partir un caracter utf-8
            end -= 1
        parts.append(data[:end])
        data = data[end:]
    return parts


def join_payload(parts) -> dict:
    try:
        return json.loads(b"".join(parts).decode("utf-8"))
    except Exception:
        return {}
class TelemetryServer:
    """Servidor en su propio hilo (con su propio bucle asyncio). La app solo llama a
    start()/stop()/publish(); nada de aqui toca Tkinter."""

    def __init__(self, ble, cfg: Dict[str, Any]):
        self.ble = ble
        self.cfg = cfg                      # mismo dict que edita la interfaz
        self.running = False
        self.error = ""
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._stop_evt: Optional["asyncio.Event"] = None
        self._ready = threading.Event()
        self._startup_error: Optional[str] = None
        self._latest: Dict[str, Any] = {}   # ultima telemetria (la deja el hilo BLE)
        self._latest_ts = 0.0               # cuando llego (para detectar que la Bluetti dejo de mandar datos)
        self._last_sent: Dict[str, Any] = {}
        self._clients: Dict[Any, Dict[str, Any]] = {}
        self._fails: Dict[str, Tuple[int, float]] = {}
        self.port = 0                       # puerto REAL en uso (puede diferir del configurado)
        self.relay_state = ""               # texto para la interfaz: "", "Conectando…", "En línea", errores
        self.relay_up = False
        self._relay_ws = None
        self._virt: Dict[str, "_VirtualWs"] = {}
        self._remote_fails: List[float] = []   # fallos de contrasena llegados por internet (freno global)
        self.alert_provider: Any = None        # funcion(peticion) -> dict; lee/guarda alert_config en el hilo de Tk
        self.sky_provider: Any = None          # funcion(peticion) -> dict; la pone la app (lee su SkyTracker en el hilo de Tk)
        self.wx_provider: Any = None           # funcion() -> {ver, cache, used, status}: pronostico del clima que se manda a las copias cliente
        self._wx_ver: Any = None               # ultima version del pronostico enviada
        self.wx_cfg_provider: Any = None       # funcion(peticion) -> dict: un cliente controla el clima de este servidor
        self._wx_chk = 0.0
        self._extra: Dict[str, Any] = {}       # datos que pone la app (alertas ya calculadas, BLE en pausa...)
        self.app_cmds: Dict[str, Any] = {}     # nombre -> funcion(*args) que ejecuta la app (hilo de Tk)
# ===== [sync-datos] =====
        # [sync-datos] intercambio de datos de estadisticas entre las dos PC (LAN)
        self.sync_provider: Any = None   # funcion() -> payload dict (se llama en executor)
        self.sync_apply: Any = None      # funcion(payload) -> resumen dict (en executor)
        self._sync_next = 0.0            # proximo envio automatico de datos
        self._sync_rx: Dict[Any, Any] = {}
        self._sync_notice = None         # (texto, hasta) para la interfaz al recibir confirmacion

    # ---------------------------------------------------------------- API publica
    @property
    def client_count(self) -> int:
        return len(self._clients)

    def publish(self, state: Dict[str, Any]):
        """Llamado desde el hilo BLE con cada telemetria. Solo guarda una copia; el
        bucle del servidor se encarga de enviar (como mucho cada PUSH_INTERVAL_S)."""
        self._latest = dict(state)
        self._latest_ts = time.time()

    def set_extra(self, extra: Dict[str, Any]):
        """La app deja aqui lo que solo ella sabe (alertas con SUS umbrales, si el BLE esta en pausa...).
        Se mezcla en cada snapshot/delta; los clientes lo reciben igual que el resto."""
        self._extra = dict(extra)

    def start(self) -> Tuple[bool, str]:
        if self.running:
            return True, "El servidor ya estaba activo."
        if not AIOHTTP_OK:
            self.error = "Falta la libreria aiohttp"
            return False, "Falta la libreria aiohttp.\nInstalala con:  pip install aiohttp" + f"\n\nDetalle tecnico: {AIOHTTP_ERR}"
        view = self.cfg.get("view_password", "")
        ctrl = self.cfg.get("control_password", "")
        if not view:
            self.error = "Falta la contrasena de 'Solo ver'"
            return False, "Define primero la contrasena de 'Solo ver' (boton ⚙)."
        if ctrl and ctrl == view:
            self.error = "Las contrasenas no pueden ser iguales"
            return False, "La contrasena de 'Controlar' debe ser distinta de la de 'Solo ver' (boton ⚙)."
        self.error = ""
        self._startup_error = None
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="telemetry-server", daemon=True)
        self._thread.start()
        self._ready.wait(6.0)
        if self._startup_error:
            self.error = self._startup_error
            self.running = False
            return False, self._startup_error
        self.running = True
        logger.info("Servidor de telemetria activo en el puerto %s", self.port)
        return True, "Servidor activo."

    def stop(self):
        loop, evt = self._loop, self._stop_evt
        if loop is not None and evt is not None and loop.is_running():
            loop.call_soon_threadsafe(evt.set)
        th = self._thread
        if th is not None:
            th.join(timeout=4.0)
        self._thread = None
        self._loop = None
        self.running = False
        self.port = 0
        self._clients.clear()
        self._last_sent = {}

    def local_urls(self) -> List[str]:
        """Direcciones por las que el telefono puede entrar (todas las IPv4 de esta PC)."""
        ips: List[str] = []

        def add(ip: str):
            if ip and not ip.startswith(("127.", "169.254.")) and ip not in ips:
                ips.append(ip)

        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                add(info[4][0])
        except Exception:
            pass
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.255.255.255", 1))     # no envia nada: solo elige la interfaz
            add(s.getsockname()[0])
            s.close()
        except Exception:
            pass
        port = self.port or self.cfg.get("port", DEFAULT_PORT)
        return [f"http://{ip}:{port}" for ip in ips]

    # ---------------------------------------------------------------- hilo / bucle
    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._main())
        except Exception as exc:
            self._startup_error = self._startup_error or f"Error del servidor: {exc}"
            logger.exception("Fallo en el servidor de telemetria")
        finally:
            self._ready.set()
            try:
                loop.close()
            except Exception:
                pass
            self.running = False

    async def _main(self):
        self._stop_evt = asyncio.Event()
        app = web.Application()
        app.add_routes([
            web.get("/", self._index),
            web.get("/ws", self._ws_handler),
            web.get("/api/state", self._api_state),
            web.get("/api/ping", self._api_ping),
            web.get("/favicon.ico", self._favicon),
            web.get("/sw.js", self._sw),
            web.get("/manifest.webmanifest", self._manifest),
            web.get("/icon-192.png", self._icon192),
            web.get("/icon-512.png", self._icon512),
        ])
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        want = int(self.cfg.get("port", DEFAULT_PORT))
        # Windows reserva rangos de puertos (Hyper-V, hotspot, Docker...) y esos rangos cambian en cada
        # arranque: un puerto que ayer funcionaba puede dar "acceso no permitido" (WinError 10013) hoy.
        # Se prueba el configurado y, si no se puede, los siguientes.
        last_exc: Optional[OSError] = None
        self.port = 0
        for port in range(want, min(want + PORT_SPAN, 65535) + 1):
            try:
                try:
                    await web.TCPSite(runner, "0.0.0.0", port, shutdown_timeout=1.0).start()
                except TypeError:        # version de aiohttp sin ese parametro
                    await web.TCPSite(runner, "0.0.0.0", port).start()
                self.port = port
                break
            except OSError as exc:
                last_exc = exc
        if not self.port:
            await runner.cleanup()
            self._startup_error = (f"No se pudo abrir ningun puerto entre {want} y {want + PORT_SPAN}: {last_exc}\n\n"
                                   "Windows puede tener reservado ese rango. Prueba otro puerto en ⚙ "
                                   "(por ejemplo 5050 o 9100).")
            self._ready.set()
            return
        if self.port != want:
            logger.warning("Puerto %s no disponible; se usa el %s", want, self.port)
        pusher = asyncio.ensure_future(self._push_loop())
        syncer = None
        if self.sync_provider is not None:
            syncer = asyncio.ensure_future(self._sync_loop())
        relay = None
        self.relay_state = ""
        if self.cfg.get("relay_enabled"):
            if len(str(self.cfg.get("view_password", ""))) < MIN_REMOTE_PW or (
                    self.cfg.get("control_password") and len(str(self.cfg.get("control_password"))) < MIN_REMOTE_PW):
                self.relay_state = f"Internet desactivado: las contraseñas necesitan {MIN_REMOTE_PW}+ caracteres"
            elif not self._relay_wanted():
                self.relay_state = "Falta la dirección o la clave del relay"
            else:
                relay = asyncio.ensure_future(self._relay_main())
        self._ready.set()
        await self._stop_evt.wait()
        pusher.cancel()
        if syncer is not None:
            syncer.cancel()
        if relay is not None:
            relay.cancel()
            try:
                await relay
            except BaseException:
                pass
            ws = self._relay_ws
            if ws is not None and not ws.closed:
                try:
                    await asyncio.wait_for(ws.close(), 1.5)
                except Exception:
                    pass
        self.relay_up, self._relay_ws = False, None

        async def _bye(ws):
            try:
                await asyncio.wait_for(ws.send_json({"v": 1, "type": "bye"}), 1.0)
                await asyncio.wait_for(ws.close(code=1001), 1.5)
            except Exception:
                pass
        # todos a la vez (antes uno tras otro: cada cliente lento sumaba espera)
        await asyncio.gather(*[_bye(ws) for ws in list(self._clients)], return_exceptions=True)
        await runner.cleanup()

    # ---------------------------------------------------------------- estado
    def _snapshot(self) -> Dict[str, Any]:
        s = {k: v for k, v in self._latest.items() if k not in IGNORED_KEYS}
        s.update(self._extra)
        stale = bool(self._latest) and time.time() - self._latest_ts > STALE_S
        s["srv_stale"] = stale
        s["link_ok"] = bool(s.get("connected")) and not getattr(self.ble, "is_paused", False) and not stale
        return s

    def _data_age(self) -> Optional[int]:
        return int(time.time() - self._latest_ts) if self._latest_ts else None

    async def _handle_wx_cfg(self, ws, m: Dict[str, Any]):
        """Mando a distancia del clima: un cliente (con permiso de control, en la red local) lee y cambia la
        configuracion del pronostico de ESTE servidor (proveedor, claves, intervalo, unificar) o pide actualizar."""
        info = self._clients.get(ws)
        now = time.time()
        if info is None or now - info.get("last_wxcfg", 0.0) < 0.25:
            return
        info["last_wxcfg"] = now
        req = m.get("req")
        if isinstance(req, bool) or not isinstance(req, int):
            return
        action = m.get("action")
        if action not in ("get", "set", "refresh", "status"):
            return
        if info.get("remote") or info.get("role") != "controller":
            out: Dict[str, Any] = {"ok": False, "error": "Sin permiso de control"}
        elif self.wx_cfg_provider is None:
            out = {"ok": False, "error": "Esta versión del servidor no admite control remoto del clima"}
        else:
            clean: Dict[str, Any] = {"action": action}
            vals = m.get("values")
            if action == "set":
                if not isinstance(vals, dict) or len(vals) > 16:
                    out = {"ok": False, "error": "peticion no valida"}
                    await self._safe_send(ws, json.dumps({"v": 1, "type": "wx_cfg", "req": req, **out}))
                    return
                clean["values"] = vals
            try:
                out = await asyncio.get_running_loop().run_in_executor(None, self.wx_cfg_provider, clean)
            except Exception as e:
                out = {"ok": False, "error": "Error del servidor: %s" % type(e).__name__}
        msg = {"v": 1, "type": "wx_cfg", "req": req}
        msg.update(out if isinstance(out, dict) else {"ok": False})
        await self._safe_send(ws, json.dumps(msg, default=str, separators=(",", ":")))

    def _wx_json(self, only_if_new: bool = False) -> Optional[str]:
        """Mensaje {type:wx} con el pronostico horario del servidor (o None si no hay nada nuevo / nada que mandar)."""
        prov = self.wx_provider
        if prov is None:
            return None
        try:
            st = prov()
        except Exception:
            return None
        if not isinstance(st, dict) or not st.get("cache"):
            return None
        if only_if_new and st.get("ver") == self._wx_ver:
            return None
        self._wx_ver = st.get("ver")
        out: Dict[str, Any] = {"v": 1, "type": "wx"}
        out.update(st)
        return json.dumps(out, default=str, separators=(",", ":"))

    async def _wx_tick(self):
        """Cada pocos segundos: si el servidor descargo un pronostico nuevo, se lo manda a las copias cliente."""
        now = time.time()
        if self.wx_provider is None or now - self._wx_chk < 5.0:
            return
        self._wx_chk = now
        targets = [ws for ws, i in self._clients.items() if i.get("wx")]
        if not targets:
            return
        msg = self._wx_json(only_if_new=True)
        if msg:
            await asyncio.gather(*[self._safe_send(ws, msg) for ws in targets])

    async def _push_loop(self):
        while True:
            await asyncio.sleep(PUSH_INTERVAL_S)
            if not self._clients:
                continue
            await self._wx_tick()
            snap = self._snapshot()
            delta = {k: v for k, v in snap.items() if k not in self._last_sent or self._last_sent[k] != v}
            if not delta:
                continue
            self._last_sent = snap
            msg = json.dumps({"v": 1, "type": "delta", "state": delta}, default=str)
            clients = list(self._clients)
            jobs = [self._safe_send(ws, msg) for ws in clients if not isinstance(ws, _VirtualWs)]
            virt = [ws.cid for ws in clients if isinstance(ws, _VirtualWs)]
            if virt:                      # un solo envio al relay para todos los telefonos de internet
                jobs.append(self._relay_send({"t": "b", "cs": virt, "d": msg}))
            await asyncio.gather(*jobs)

    # ---------------------------------------------------------------- relay de internet
    def _relay_wanted(self) -> bool:
        return bool(self.cfg.get("relay_enabled") and self.cfg.get("relay_url") and self.cfg.get("relay_key"))

    async def _relay_send(self, obj: Dict[str, Any]):
        ws = self._relay_ws
        if ws is None or ws.closed:
            return
        try:
            await asyncio.wait_for(ws.send_str(json.dumps(obj)), timeout=3.0)
        except Exception:
            pass

    async def _relay_main(self):
        """Mantiene la conexion SALIENTE con el relay (la PC llama hacia fuera; nadie entra en ella)."""
        backoff = 2.0
        while True:
            try:
                ok = await self._relay_session()
                backoff = 2.0 if ok else min(backoff * 1.7, 30.0)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                detail = f"{type(exc).__name__}: {exc}".strip().rstrip(":")
                low = detail.lower()
                if "certificate" in low or "ssl" in low:
                    hint = "certificado/SSL (antivirus, proxy o fecha de la PC)"
                elif "getaddrinfo" in low or "name or service" in low or "dns" in low or "11001" in low:
                    hint = "no resuelve el nombre (DNS)"
                elif "timeout" in low or "timed out" in low:
                    hint = "tiempo agotado (firewall o red)"
                elif "refused" in low or "10061" in low:
                    hint = "conexión rechazada"
                elif "proxy" in low:
                    hint = "proxy"
                else:
                    hint = detail[:90]
                self.relay_state = "Sin conexión con el relay: " + hint
                logger.warning("Relay: %s", detail)
                backoff = min(backoff * 1.7, 30.0)
            self.relay_up = False
            self._relay_ws = None
            if self.relay_state == "En línea":
                self.relay_state = "Reconectando…"
            for v in list(self._virt.values()):
                v.remote_closed()
            self._virt.clear()
            await asyncio.sleep(backoff)

    async def _relay_session(self) -> bool:
        base = str(self.cfg.get("relay_url"))
        url = ("wss://" if base.startswith("https://") else "ws://") + base.split("://", 1)[1] + "/host"
        self.relay_state = "Conectando…"
        tasks: List["asyncio.Future"] = []
        px = _FragProxy(url)                       # parte el saludo TLS para esquivar filtros por nombre
        await px.__aenter__()
        try:
            async with aiohttp.ClientSession() as sess:
                try:
                    ws = await _ws_connect(sess, px.url, url,
                                           headers={"Authorization": "Bearer " + str(self.cfg.get("relay_key"))},
                                           heartbeat=20, max_msg_size=1048576)
                except aiohttp.WSServerHandshakeError as exc:
                    self.relay_state = ("Clave del relay incorrecta" if exc.status == 401
                                        else f"El relay respondió {exc.status}")
                    return False
                self._relay_ws = ws
                self.relay_up = True
                self.relay_state = "En línea"
                # el relay guarda la ULTIMA pagina que le manda la app: asi una pagina nueva llega a los
                # telefonos sin tener que volver a ejecutar desplegar_relay.py
                try:
                    await asyncio.wait_for(ws.send_str(json.dumps({"t": "page", "d": INDEX_HTML})), timeout=10.0)
                except Exception:
                    logger.info("No se pudo enviar la pagina al relay")
                logger.info("Conectado al relay %s", base)
                async for m in ws:
                    if m.type != WSMsgType.TEXT:
                        if m.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                            break
                        continue
                    try:
                        d = json.loads(m.data)
                    except Exception:
                        continue
                    if not isinstance(d, dict):
                        continue
                    t, cid = d.get("t"), d.get("c")
                    if not isinstance(cid, str):
                        continue
                    if t == "open":
                        ip = str(d.get("ip") or "?")[:64]
                        if self._locked(ip):
                            await self._relay_send({"t": "m", "c": cid, "d": json.dumps(
                                {"v": 1, "type": "auth", "ok": False, "error": "Demasiados intentos. Espera un minuto."})})
                            await self._relay_send({"t": "close", "c": cid})
                            continue
                        v = _VirtualWs(self._relay_send, cid, ip)
                        self._virt[cid] = v
                        tasks.append(asyncio.ensure_future(self._virt_run(v, ip)))
                    elif t == "m" and cid in self._virt and isinstance(d.get("d"), str):
                        self._virt[cid].feed(d["d"])
                    elif t == "close" and cid in self._virt:
                        self._virt[cid].remote_closed()
                return True
        finally:
            for t in tasks:
                t.cancel()
            await px.__aexit__(None, None, None)

    async def _virt_run(self, v: "_VirtualWs", ip: str):
        try:
            await self._session(v, ip, remote=True)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error en un cliente de internet")
        finally:
            self._virt.pop(v.cid, None)
            try:
                await v.close()
            except Exception:
                pass

    @staticmethod
    async def _safe_send(ws, msg: str):
        try:
            await asyncio.wait_for(ws.send_str(msg), timeout=3.0)
        except Exception:
            pass

    # ---------------------------------------------------------------- autenticacion
    def _role_for_password(self, pw: Any) -> Optional[str]:
        if not isinstance(pw, str) or not pw:
            return None
        data = pw.encode("utf-8")
        view = str(self.cfg.get("view_password", "")).encode("utf-8")
        ctrl = str(self.cfg.get("control_password", "")).encode("utf-8")
        is_ctrl = bool(ctrl) and hmac.compare_digest(data, ctrl)
        is_view = bool(view) and hmac.compare_digest(data, view)
        return "controller" if is_ctrl else ("viewer" if is_view else None)

    def _locked(self, ip: str) -> bool:
        entry = self._fails.get(ip)
        return bool(entry and entry[1] > time.time())

    def _register_fail(self, ip: str):
        count, until = self._fails.get(ip, (0, 0.0))
        if until and until <= time.time():
            count, until = 0, 0.0
        count += 1
        if count >= MAX_FAILS:
            logger.warning("IP %s bloqueada %d s por intentos fallidos", ip, LOCKOUT_S)
            self._fails[ip] = (0, time.time() + LOCKOUT_S)
        else:
            self._fails[ip] = (count, 0.0)

    # ---------------------------------------------------------------- rutas HTTP
    async def _index(self, request):
        return web.Response(text=INDEX_HTML, content_type="text/html", charset="utf-8",
                            headers={"Cache-Control": "no-store"})

    async def _favicon(self, request):
        return web.Response(status=204)

    async def _sw(self, request):
        return web.Response(text=SW_JS, content_type="application/javascript", charset="utf-8",
                            headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})

    async def _manifest(self, request):
        return web.Response(text=MANIFEST_JSON, content_type="application/manifest+json", charset="utf-8")

    async def _icon192(self, request):
        return web.Response(body=base64.b64decode(ICON_192_B64), content_type="image/png",
                            headers={"Cache-Control": "public, max-age=86400"})

    async def _icon512(self, request):
        return web.Response(body=base64.b64decode(ICON_512_B64), content_type="image/png",
                            headers={"Cache-Control": "public, max-age=86400"})

    async def _api_ping(self, request):
        """Sin datos ni contrasena: solo dice 'soy yo'. Sirve para que un cliente encuentre el puerto."""
        return web.json_response({"app": APP_ID, "v": 1})

    async def _api_state(self, request):
        ip = request.remote or "?"
        if self._locked(ip):
            return web.Response(status=429, text="Demasiados intentos. Espera un minuto.")
        auth = request.headers.get("Authorization", "")
        pw = auth[7:] if auth.startswith("Bearer ") else ""
        if self._role_for_password(pw) is None:
            self._register_fail(ip)
            return web.Response(status=401, text="Contrasena incorrecta")
        self._fails.pop(ip, None)
        return web.json_response({"v": 1, "state": self._snapshot()}, dumps=lambda o: json.dumps(o, default=str))

    async def _ws_handler(self, request):
        ip = request.remote or "?"
        if self._locked(ip):
            logger.info("Conexion rechazada por bloqueo temporal: %s", ip)
            return web.Response(status=429, text="Demasiados intentos. Espera un minuto.")
        ws = web.WebSocketResponse(heartbeat=15, max_msg_size=8192)
        await ws.prepare(request)
        await self._session(ws, ip, remote=False)
        return ws

    async def _session(self, ws, ip: str, remote: bool):
        """Vida de un cliente: autenticacion, estado inicial y mensajes. `ws` es un WebSocket de aiohttp
        (red local) o un _VirtualWs (telefono llegado por el relay de internet)."""
        try:
            first = await ws.receive(timeout=AUTH_TIMEOUT_S)
        except asyncio.TimeoutError:
            logger.info("Cliente %s abrio el WebSocket pero no envio la contrasena", ip)
            await ws.close()
            return
        if first.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
            return                                       # se fue sin intentar entrar: no cuenta como fallo
        role = None
        want_wx = False                                  # la copia cliente pide que le mandemos el pronostico del clima
        if first.type == WSMsgType.TEXT:
            try:
                m = json.loads(first.data)
                if isinstance(m, dict) and m.get("type") == "auth":
                    role = self._role_for_password(m.get("password"))
                    caps = m.get("caps")
                    want_wx = isinstance(caps, list) and "wx" in caps
            except Exception:
                role = None
        if role is None:
            logger.warning("Autenticacion fallida desde %s", ip)
            self._register_fail(ip)
            delay = 1.0                                  # frena la fuerza bruta
            if remote:
                now = time.time()
                self._remote_fails = [t for t in self._remote_fails if now - t < 600.0] + [now]
                if len(self._remote_fails) >= 15:        # muchos fallos desde internet (de cualquier IP)
                    delay += 5.0
            await asyncio.sleep(delay)
            try:
                await ws.send_json({"v": 1, "type": "auth", "ok": False, "error": "Contraseña incorrecta"})
            except Exception:
                pass
            await ws.close()
            return

        self._fails.pop(ip, None)
        capped = False
        if remote and role == "controller" and not self.cfg.get("relay_allow_control"):
            role, capped = "viewer", True                # desde internet solo se ve (salvo que se permita)
        # server_time: el cliente calcula la diferencia de reloj entre las dos PC (las cuentas
        # atras de las protecciones usan la hora del servidor)
        ack = {"v": 1, "type": "auth", "ok": True, "role": role, "server_time": time.time()}
        if capped:
            ack["capped"] = True
        # para clientes nuevos (Android): version del protocolo y que funciones ofrece ESTA app
        ack["proto"] = 1
        ack["app"] = APP_ID
        ack["features"] = sorted(f for f, on in (("sky", self.sky_provider is not None),
                                                  ("alert_cfg", self.alert_provider is not None),
                                                  ("app_cmds", bool(self.app_cmds)),
                                                  ("sync", self.sync_provider is not None),
                                                  ("wx", self.wx_provider is not None),
                                                  ("wx_cfg", self.wx_cfg_provider is not None),
                                                  ("pong_age", True), ("stale", True)) if on)
        await ws.send_json(ack)
        snap = self._snapshot()
        if not self._clients:          # primer cliente: el snapshot pasa a ser la base de los deltas
            self._last_sent = snap
        await ws.send_str(json.dumps({"v": 1, "type": "snapshot", "state": snap}, default=str))
        self._clients[ws] = {"role": role, "ip": ip, "last_cmd": 0.0, "remote": remote, "t0": time.time(),
                             "wx": bool(want_wx and not remote)}
        if want_wx and not remote and self.wx_provider is not None:   # pronostico actual nada mas conectar
            _wxm = self._wx_json()
            if _wxm:
                await self._safe_send(ws, _wxm)
        if (role == "controller" and not remote and self.sync_provider is not None
                and self._sync_next < time.time() - 1.0):
            self._sync_next = time.time() + 3.0        # primera sincronizacion pronto
        logger.info("Cliente conectado: %s (%s%s)", ip, role, ", internet" if remote else "")
        try:
            async for m in ws:
                if m.type == WSMsgType.TEXT:
                    await self._on_message(ws, m.data)
                elif m.type == WSMsgType.ERROR:
                    break
        finally:
            _inf = self._clients.pop(ws, None)
            logger.info("Cliente desconectado: %s tras %d s (codigo %s)", ip,
                        int(time.time() - _inf["t0"]) if _inf else 0, getattr(ws, "close_code", None))

    async def _handle_get_sky(self, ws, m: Dict[str, Any]):
        """Condiciones y estadisticas del panel (la misma informacion que la ventana de la app).
        Peticion: {type:get_sky, req:n, mode:'day'|'bars', day:'AAAA-MM-DD', n:7|30, all:bool}.
        La app prepara los datos en su hilo de Tk (donde vive el SkyTracker); aqui solo se valida y se reenvia."""
        info = self._clients.get(ws)
        now = time.time()
        if info is None or now - info.get("last_sky", 0.0) < SKY_MIN_GAP_S:
            return
        info["last_sky"] = now
        req = m.get("req")
        req = req if isinstance(req, int) and not isinstance(req, bool) and 0 <= req < 1_000_000_000 else 0
        mode = m.get("mode")
        day = m.get("day")
        n = m.get("n")
        clean: Dict[str, Any] = {"mode": mode if mode in ("day", "bars") else "day",
                                 "day": day if isinstance(day, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) else None,
                                 "n": n if n in (7, 30) else 7, "all": m.get("all") is True}
        prov = self.sky_provider
        if prov is None:
            out: Dict[str, Any] = {"error": "no disponible en esta version de la app"}
        else:
            try:
                out = await asyncio.get_running_loop().run_in_executor(None, prov, clean)
            except Exception as exc:                     # tiempo agotado, app cerrandose...
                logger.info("get_sky: %s", exc)
                out = {"error": "la app no respondio"}
        msg = {"v": 1, "type": "sky", "req": req}
        msg.update(out)
        try:
            await ws.send_str(json.dumps(msg, default=str, separators=(",", ":")))
        except Exception:
            pass

    async def _handle_alert_cfg(self, ws, m: Dict[str, Any], write: bool):
        """Ajustes de alertas (switches y ventanas ⚙ de la app). Leer: cualquiera; guardar: solo 'controller'.
        Peticion de guardado: {type:set_alert_cfg, req:n, key:'ac_in_voltage', values:{...}}. La app valida y guarda."""
        info = self._clients.get(ws)
        now = time.time()
        if info is None or now - info.get("last_alert", 0.0) < ALERT_MIN_GAP_S:
            return
        info["last_alert"] = now
        req = m.get("req")
        req = req if isinstance(req, int) and not isinstance(req, bool) and 0 <= req < 1_000_000_000 else 0
        clean: Dict[str, Any] = {"action": "set" if write else "get"}
        out: Dict[str, Any]
        if write:
            vals = m.get("values")
            if info.get("role") != "controller":
                out = {"ok": False, "error": "Sin permiso de control"}
            elif not isinstance(m.get("key"), str) or not isinstance(vals, dict) or len(vals) > 64:
                out = {"ok": False, "error": "Peticion no valida"}
            else:
                clean.update(key=m["key"][:40], values=vals)
                out = None  # type: ignore
        else:
            out = None  # type: ignore
        if out is None:
            prov = self.alert_provider
            if prov is None:
                out = {"ok": False, "error": "no disponible en esta version de la app"}
            else:
                try:
                    out = await asyncio.get_running_loop().run_in_executor(None, prov, clean)
                except Exception as exc:
                    logger.info("alert_cfg: %s", exc)
                    out = {"ok": False, "error": "la app no respondio"}
        msg = {"v": 1, "type": "alert_cfg", "req": req}
        msg.update(out)
        try:
            await ws.send_str(json.dumps(msg, default=str, separators=(",", ":")))
        except Exception:
            pass
        if write and out.get("ok"):
            logger.info("Alertas '%s' cambiadas desde %s", m.get("key"), info.get("ip"))

# ===== [sync-datos] =====

    # ---- sincronizacion de datos del servidor (LAN) ----
    async def _sync_loop(self):
        """Cada 2 min (o con request_sync_all) envia a los clientes 'controller' de la LAN
        los datos de estadisticas de esta PC. El otro lado responde con lo suyo."""
        try:
            await asyncio.sleep(4.0)          # deja conectar al cliente
        except Exception:
            return
        while True:
            try:
                await asyncio.sleep(1.0)
                if not self.sync_provider:
                    continue
                if time.time() < self._sync_next:
                    continue
                self._sync_next = time.time() + 120.0
                await self._sync_send_all()
            except Exception:
                try:
                    await asyncio.sleep(5.0)
                except Exception:
                    return

    def request_sync_all(self):
        """Desde la interfaz (otro hilo): envia los datos YA a todos los clientes de la LAN."""
        loop = self._loop
        if loop is not None and loop.is_running():
            try:
                loop.call_soon_threadsafe(self._sync_kick)
            except Exception:
                pass

    def _sync_kick(self):
        self._sync_next = time.time()

    async def _sync_send_all(self):
        for ws, info in list(self._clients.items()):
            if info.get("remote") or info.get("role") != "controller" or not info.get("sync"):
                continue
            try:
                await self._sync_send(ws)
            except Exception:
                pass

    async def _sync_send(self, ws):
        """Trocea y envia el payload (cada trozo cabe en el limite de 8192 del WebSocket)."""
        import base64
        import secrets
        try:
            if self.sync_provider is None:
                return
            raw = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(None, self.sync_provider), 15.0)
            if not isinstance(raw, dict) or raw.get("empty"):
                return
            parts = split_payload(raw)
            if not parts:
                return
            uid = secrets.token_hex(8)
            who = raw.get("pc_id") or ""
            for i, chunk in enumerate(parts):
                if ws.closed:
                    return
                try:
                    await ws.send_str(json.dumps({
                        "v": 1, "type": "sync_data", "uid": uid, "n": i + 1, "N": len(parts),
                        "who": who, "d": base64.b64encode(chunk).decode("ascii")}))
                except Exception:
                    return
        except Exception:
            pass

    def _sync_rx_add(self, m: Dict[str, Any]):
        """Acumula los trozos de un sync_data; cuando llegan todos, devuelve el payload."""
        import base64
        try:
            uid = str(m.get("uid") or "")[:40]
            N, n, d = m.get("N"), m.get("n"), m.get("d")
            if not uid or not isinstance(N, int) or not isinstance(n, int):
                return None
            if N < 1 or N > 3000 or n < 1 or n > N or not isinstance(d, str) or len(d) > 8000:
                return None
            st = self._sync_rx.setdefault(uid, {"parts": {}, "N": N, "t": time.time()})
            st["parts"][n] = base64.b64decode(d)
            st["t"] = time.time()
            if len(st["parts"]) != N:
                now = time.time()
                for k in [k for k, v in list(self._sync_rx.items()) if now - v["t"] > 600]:
                    self._sync_rx.pop(k, None)
                return None
            self._sync_rx.pop(uid, None)
            payload = join_payload([st["parts"][i] for i in sorted(st["parts"])])
            if payload:
                payload["__uid"] = uid
            return payload if payload else None
        except Exception:
            return None

    async def _sync_apply_async(self, ws, payload):
        """Aplica lo recibido (la app lo fusiona) y confirma al emisor con sync_ack."""
        summary = {"ok": False, "error": "sin receptor"}
        if self.sync_apply is not None:
            try:
                summary = await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(None, self.sync_apply, payload), 20.0)
            except Exception as exc:
                summary = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        try:
            await ws.send_json({"v": 1, "type": "sync_ack", "uid": payload.get("__uid"),
                                "ok": bool(summary.get("ok", True)),
                                "summary": {"from": summary.get("from"),
                                             "sky_days": summary.get("sky_days"),
                                             "sky_slots": summary.get("sky_slots"),
                                             "cell_peer": summary.get("cell_peer"),
                                             "capacity": summary.get("capacity"),
                                             "csv": summary.get("csv")}})
        except Exception:
            pass

    def _sync_ack_received(self, m):
        """El cliente aplico lo nuestro: deja una noticia para la interfaz."""
        s = m.get("summary")
        if not isinstance(s, dict):
            return
        partes = []
        if s.get("sky_days"):
            partes.append("cielo %d dia(s)" % s["sky_days"])
        if s.get("capacity"):
            partes.append("capacidad")
        if s.get("cell_peer"):
            partes.append("celdas")
        if s.get("csv"):
            try:
                partes.append("registro %d fila(s)" % sum(s["csv"].values()))
            except Exception:
                pass
        if s.get("sky_slots"):
            partes.append("franjas %d" % s["sky_slots"])
        texto = ("Sincronizado OK: " + " · ".join(partes)) if partes else "Sincronizado OK (sin cambios)"
        self._sync_notice = (texto[:60], time.time() + 8.0)
    async def _on_message(self, ws, raw: str):
        try:
            m = json.loads(raw)
        except Exception:
            return
        if not isinstance(m, dict):
            return
        kind = m.get("type")
        if kind == "ping":
            await ws.send_json({"v": 1, "type": "pong", "age": self._data_age()})
            return
        if kind == "get_snapshot":      # el cliente pide el estado completo (p. ej. tras un comando rechazado)
            await ws.send_str(json.dumps({"v": 1, "type": "snapshot", "state": self._snapshot()}, default=str))
            return
        if kind == "get_sky":
            await self._handle_get_sky(ws, m)
            return
        if kind in ("get_alert_cfg", "set_alert_cfg"):
            await self._handle_alert_cfg(ws, m, kind == "set_alert_cfg")
            return
        if kind == "wx_cfg":
            await self._handle_wx_cfg(ws, m)
            return
# ===== [sync-datos] =====
        if kind in ("sync_data", "sync_ack"):
            info = self._clients.get(ws, {})
            if info.get("remote") or info.get("role") != "controller":
                return                      # solo LAN y con contrasena de «Controlar»
            if kind == "sync_data":
                if not info.get("sync"):
                    info["sync"] = True                 # esta copia habla sync: tambien recibira lo nuestro
                    self._sync_next = min(self._sync_next, time.time() + 2.0)
                payload = self._sync_rx_add(m)
                if payload:
                    asyncio.ensure_future(self._sync_apply_async(ws, payload))
            elif kind == "sync_ack":
                self._sync_ack_received(m)
            return
        if kind != "cmd":
            return
        info = self._clients.get(ws, {})
        cid, name = m.get("id"), m.get("name")
        args = m.get("args") if isinstance(m.get("args"), dict) else {}
        err = None
        now = time.time()
        spec = COMMANDS.get(name) if isinstance(name, str) else None
        vals: List[Any] = []
        if info.get("role") != "controller":
            err = "Sin permiso de control"
        elif spec is None:
            err = "Comando no permitido"
        elif set(args) - {key for key, _ in spec}:
            err = "Argumentos no permitidos"
        else:
            for key, check in spec:         # cada argumento se valida y se pasa EN ORDEN
                if not check(args.get(key)):
                    err = f"Argumento '{key}' invalido"
                    break
                vals.append(args[key])
        if err is None:
            if now - info.get("last_cmd", 0.0) < CMD_MIN_INTERVAL_S:
                err = "Demasiado rapido"
            elif name not in NO_LINK_NEEDED and not self._snapshot().get("link_ok"):
                err = "El servidor no esta conectado a la Bluetti"
        if err is None:
            info["last_cmd"] = now
            try:
                if name in APP_COMMANDS:
                    fn = self.app_cmds.get(name)
                    if fn is None:
                        raise RuntimeError("no disponible en esta version")
                    fn(*vals)
                else:
                    getattr(self.ble, name)(*vals)
                logger.info("Comando %s%s desde %s", name, tuple(vals), info.get("ip"))
            except Exception as exc:
                err = f"Error: {exc}"
        reply = {"v": 1, "type": "ack", "id": cid, "ok": err is None}
        if err:
            reply["error"] = err
        await ws.send_json(reply)


# =============================================================================
# CLIENTE:  RemoteBleManager  (Fase 2)
# Hace de BluettiBleManager pero los datos vienen de otro servidor por WiFi.
# Tiene la misma "cara" que el gestor BLE (.state, .is_paused, set_*, pause_and_release,
# resume_connection, start, stop...), asi que hud_app.py y floating_widget.py funcionan
# igual sin cambios. Nunca toca el Bluetooth.
# =============================================================================
_TUPLE_KEYS = ("cell_balance_limits", "cap_range_wh")      # JSON las convierte en listas: se devuelven a tupla
_LIVE_ZERO = ("dc_input_watts", "dc_input_volts", "dc_input_current", "ac_input_watts", "ac_input_volts",
              "ac_input_current", "ac_output_watts", "dc_output_watts", "ac_input_hz", "ac_output_hz")


def _blank_state() -> Dict[str, Any]:
    """Estado inicial (mismas claves y tipos que BluettiBleManager.state para lo que usa la interfaz)."""
    return {
        "soc": None, "runtime_minutes": None,
        "dc_input_watts": 0, "dc_input_volts": 0.0, "dc_input_current": 0.0,
        "ac_input_watts": 0, "ac_input_volts": 0.0, "ac_input_current": 0.0,
        "ac_output_watts": 0, "ac_output_volts": 0.0, "dc_output_watts": 0,
        "ac_input_hz": 0.0, "ac_output_hz": 0.0,
        "ac_output_on": False, "dc_output_on": False, "eco_mode_on": False,
        "eco_dc_mode_on": False, "eco_ac_mode_on": False,
        "power_lifting_on": False, "grid_enhancement_on": False,
        "grid_max_current": None, "dc_source": None, "dc_high_current_on": False,
        "charge_mode": "standard",
        "batt_temp": None, "batt_temps": [], "cell_mv": None,
        "cell_alert": "ok", "cell_alert_cells": [], "cell_warn": "ok", "cell_warn_cells": [],
        "prot_dc_active": False, "prot_dc_end_ts": None, "prot_ac_reduced_pending": False,
        "prot_lim_active": False, "prot_lim_end_ts": None, "prot_lim_fire_ts": 0.0, "prot_lim_restored_ts": 0.0,
        "prot_bat_crit_active": False, "prot_bat_crit_level": 0, "prot_bat_crit_end_ts": None, "prot_bat_crit_fire_ts": 0.0,
        "connected": False, "encryption_active": False,
        "device_name": "Servidor remoto", "last_update": "--",
    }


class _HostOffline(Exception):
    """El servidor (o el relay) cerro la conexion antes de dejarnos entrar."""


class RemoteBleManager:
    """Gestor 'falso' que lee de un TelemetryServer. Corre en su propio hilo con su bucle asyncio.

    is_paused  = el usuario desconecto el cliente (boton "Desconectar"): no reintenta hasta "Conectar".
    state["connected"] = hay WebSocket Y el servidor tiene enlace con la Bluetti. Si el WiFi se cae o
    el servidor pierde la Bluetti queda en False, y la app reutiliza su estimacion "offline" de siempre."""

    def __init__(self, host: str, port: int, password: str, on_telemetry_callback=None,
                 on_status_callback=None, on_notice=None, on_resync=None):
        self.host = (host or "").strip()
        self.port = int(port)
        self.password = password or ""
        self.on_telemetry = on_telemetry_callback
        self.on_status = on_status_callback
        self.on_notice = on_notice          # texto corto para la etiqueta de estado (p. ej. "Solo lectura")
        self.on_resync = on_resync          # primer snapshot de cada sesion: la app reinicia sus detectores de eventos
        self.on_devices = None
        self.on_wx = None                   # recibe el pronostico del servidor ({type:wx,...}) y {type:wx_caps, supported}
        self._wx_req_n = 0                  # mando a distancia del clima: ids de peticion y respuestas pendientes
        self._wx_res: Dict[int, Dict[str, Any]] = {}
        self.device_address = f"{self.host}:{self.port}"
        self.active_port = 0                # puerto donde se encontro el servidor la ultima vez
        self.state: Dict[str, Any] = _blank_state()
        self.is_running = False
        self.is_paused = False
        self.simulation_mode = False
        self.role: Optional[str] = None     # "viewer" | "controller" (None = sin sesion)
        self.ws_up = False
        self.link_ok = False                # el servidor tiene enlace con la Bluetti
        self.auth_failed = False
        self.error = ""                     # motivo corto de la ultima falla de conexion
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._interrupt: Optional["asyncio.Event"] = None
        self._ws = None
        self._cmd_id = 0
        self._time_offset = 0.0             # hora del servidor - hora local
        self._synced = False
        self._lock = threading.Lock()
# ===== [sync-datos] =====
        # [sync-datos] lado cliente: comparte/recibe datos con el servidor
        self.sync_provider = None      # funcion() -> payload dict
        self.sync_apply = None         # funcion(payload) -> resumen dict
        self.on_sync_done = None       # funcion(resumen) (la llama esta clase desde su hilo)
        self.server_features: Tuple[str, ...] = ()
        self._sync_rx: Dict[Any, Any] = {}
        self._sync_next_ts = 0.0

    # ------------------------------------------------------------ misma interfaz que BluettiBleManager
    def start(self, device_address: Optional[str] = None):
        if self.is_running:
            return
        if not AIOHTTP_OK:
            self.error = "Falta aiohttp"
            self._status("Desconectado (falta la libreria aiohttp: " + AIOHTTP_ERR[:60] + ")")
            return
        self.is_running = True
        self._thread = threading.Thread(target=self._run, name="remote-client", daemon=True)
        self._thread.start()

    def stop(self):
        self.is_running = False
        self._kick(close_ws=True)
        th = self._thread
        if th is not None and th is not threading.current_thread():
            th.join(timeout=3.0)
        self._thread = None

    def pause_and_release(self):
        """En modo cliente: desconectarse del servidor (no reintenta hasta resume_connection)."""
        self.is_paused = True
        self._status("Desconectado (cliente pausado)")
        self._kick(close_ws=True)

    def resume_connection(self):
        self.is_paused = False
        self.auth_failed = False
        self._status("Reconectando...")
        self._kick()

    def set_device_address(self, address: str):      # no aplica: la Bluetti la elige el servidor
        pass

    def toggle_simulation_mode(self, enabled: Optional[bool] = None):
        pass

    def request_register_snapshot(self, ranges):
        import concurrent.futures
        fut: concurrent.futures.Future = concurrent.futures.Future()
        fut.set_exception(RuntimeError("No disponible en modo Cliente."))
        return fut

    # comandos: se mandan al servidor (que decide); el estado local se adelanta como hace el gestor BLE
    def set_ac_output(self, turn_on: bool):
        self._do("set_ac_output", {"ac_output_on": bool(turn_on)}, on=bool(turn_on))

    def set_dc_output(self, turn_on: bool):
        self._do("set_dc_output", {"dc_output_on": bool(turn_on)}, on=bool(turn_on))

    def set_eco_mode(self, turn_on: bool):
        v = bool(turn_on)
        self._do("set_eco_mode", {"eco_mode_on": v, "eco_dc_mode_on": v, "eco_ac_mode_on": v}, on=v)

    def set_power_lifting(self, turn_on: bool):
        self._do("set_power_lifting", {"power_lifting_on": bool(turn_on)}, on=bool(turn_on))

    def set_grid_enhancement(self, turn_on: bool):
        self._do("set_grid_enhancement", {"grid_enhancement_on": bool(turn_on)}, on=bool(turn_on))

    def set_dc_high_current(self, turn_on: bool):
        self._do("set_dc_high_current", {"dc_high_current_on": bool(turn_on)}, on=bool(turn_on))

    def set_charge_mode(self, mode: str):
        self._do("set_charge_mode", {"charge_mode": mode}, mode=mode)

    def set_dc_source(self, source: str, _internal: bool = False):
        opt = {"dc_source": source}
        if source != "pv":
            opt["dc_high_current_on"] = False
        self._do("set_dc_source", opt, source=source)

    def set_grid_max_current(self, amps: int):
        a = int(amps)
        self._do("set_grid_max_current", {"grid_max_current": a}, amps=a)

    def ack_ac_reduced_notice(self):
        self._do("ack_ac_reduced_notice", {"prot_ac_reduced_pending": False})

    def set_system_power_off(self):
        """Apagado total del sistema (registro 2013) - desde cliente remoto."""
        self._do("set_system_power_off", {})

    # ------------------------------------------------------------ envio de comandos
    def _do(self, name: str, optimistic: Dict[str, Any], **args):
        if self._send_cmd(name, args):
            with self._lock:
                st = dict(self.state)
                st.update(optimistic)
                self.state = st

    def _send_cmd(self, name: str, args: Dict[str, Any]) -> bool:
        loop = self._loop
        if self.role != "controller":
            if name != "ack_ac_reduced_notice":      # cerrar un aviso en modo solo-lectura no merece queja
                self._notice("Solo lectura" if self.ws_up else "Sin conexión")
            return False
        if not (self.ws_up and loop is not None and loop.is_running()):
            self._notice("Sin conexión")
            return False
        self._cmd_id += 1
        msg = {"v": 1, "type": "cmd", "id": self._cmd_id, "name": name, "args": args}
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._ws_send(msg)))
        return True

    def wx_cfg_send(self, action: str, values: Optional[Dict[str, Any]] = None) -> Optional[int]:
        """Pide al servidor leer ('get'), cambiar ('set'), actualizar ('refresh') o consultar ('status') su clima.
        Se llama desde el hilo de la interfaz; devuelve el id de la peticion (la respuesta se recoge con wx_cfg_result)
        o None si no hay conexion."""
        loop = self._loop
        if not (self.ws_up and loop is not None and loop.is_running()):
            return None
        self._wx_req_n += 1
        req = self._wx_req_n
        msg: Dict[str, Any] = {"v": 1, "type": "wx_cfg", "req": req, "action": action}
        if values:
            msg["values"] = values
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._ws_send(msg)))
        return req

    def wx_cfg_result(self, req: Optional[int]) -> Optional[Dict[str, Any]]:
        """Respuesta del servidor a la peticion `req` (una sola vez) o None si aun no llego."""
        if req is None:
            return None
        return self._wx_res.pop(req, None)

    async def _ws_send(self, obj: Dict[str, Any]):
        ws = self._ws
        if ws is None or ws.closed:
            return
        try:
            await ws.send_json(obj)
        except Exception:
            pass

    # ------------------------------------------------------------ hilo / bucle
    def _run(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._main())
        except Exception:
            logger.exception("Fallo en el cliente de telemetria")
        finally:
            try:
                loop.close()
            except Exception:
                pass
            self._loop = None

    def _kick(self, close_ws: bool = False):
        """Despierta al bucle (desde cualquier hilo): reintento inmediato / salir / cerrar el WebSocket."""
        loop, ev = self._loop, self._interrupt
        if loop is None or ev is None or not loop.is_running():
            return
        loop.call_soon_threadsafe(ev.set)
        if close_ws:
            asyncio.run_coroutine_threadsafe(self._close_ws(), loop)

    async def _close_ws(self):
        ws = self._ws
        if ws is not None and not ws.closed:
            try:
                await ws.close()
            except Exception:
                pass

    async def _wait(self, seconds: float):
        try:
            await asyncio.wait_for(self._interrupt.wait(), timeout=seconds)
        except asyncio.TimeoutError:
            return
        self._interrupt.clear()

    async def _main(self):
        self._interrupt = asyncio.Event()
        backoff, first = 1.0, True
        while self.is_running:
            if self.is_paused or self.auth_failed:
                await self._wait(1.0)
                continue
            self._status(f"Conectando al servidor {self.host}:{self.port}..." if first
                         else "Reintentando conexión con el servidor...")
            first = False
            extra = 0.0
            authed = False
            try:
                authed = await self._session()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error, offline_msg, extra = self._describe(exc)
                logger.info("Cliente: %s (%s)", self.error, exc)
                self._link_lost(offline_msg)
            else:
                self._link_lost()
            if not self.is_running:
                break
            if self.is_paused or self.auth_failed:
                continue
            backoff = 1.0 if authed else min(backoff * 1.7, 8.0)
            await self._wait(max(backoff, extra))

    @staticmethod
    def _describe(exc: Exception):
        """(motivo corto para la etiqueta, texto de estado, espera extra en segundos)."""
        if isinstance(exc, _HostOffline):
            return "Servidor apagado o sin internet", "Desconectado (el servidor no está disponible)", 3.0
        if isinstance(exc, aiohttp.WSServerHandshakeError):
            if exc.status == 429:
                return "Espera 1 min", "Desconectado (el servidor te bloqueo un minuto)", 30.0
            return f"Error HTTP {exc.status}", "Desconectado (el servidor no responde bien)", 0.0
        if isinstance(exc, (OSError, asyncio.TimeoutError, aiohttp.ClientError)):
            return "Servidor no responde", "Desconectado (no se encuentra el servidor)", 0.0
        return "Conexión perdida", "Desconectado. Reconectando automáticamente...", 0.0

    @staticmethod
    async def _ping(host: str, port: int, timeout: float) -> bool:
        """True si en host:port responde ESTA app (nunca se manda la contrasena a un desconocido)."""
        try:
            tmo = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(timeout=tmo) as sess:
                async with sess.get(f"http://{host}:{port}/api/ping") as r:
                    data = await r.json(content_type=None)
                    return isinstance(data, dict) and data.get("app") == APP_ID
        except Exception:
            return False

    async def _resolve_port(self, host: str) -> int:
        """El servidor puede haber tenido que cambiar de puerto (Windows reserva rangos distintos en cada
        arranque). Se prueba primero el ultimo que funciono; si no responde, se buscan los PORT_SPAN
        siguientes al configurado, todos a la vez. Si no aparece ninguno se devuelve el configurado y la
        conexion normal da el error de siempre."""
        first = self.active_port or self.port
        if await self._ping(host, first, 2.5):
            self.active_port = first
            return first
        cands = [p for p in range(self.port, min(self.port + PORT_SPAN, 65535) + 1) if p != first]
        res = await asyncio.gather(*[self._ping(host, p, 1.5) for p in cands])
        for p, ok in zip(cands, res):
            if ok:
                logger.info("Servidor encontrado en el puerto %s (configurado: %s)", p, self.port)
                self.active_port = p
                return p
        self.active_port = 0
        return self.port

    async def _session(self) -> bool:
        """Una conexion completa. True si llego a autenticarse."""
        if not self.host or not self.password:
            self.error = "Faltan datos"
            raise ConnectionError("faltan direccion o contraseña")
        host = f"[{self.host}]" if ":" in self.host and not self.host.startswith("[") else self.host
        if "://" in self.host:           # direccion del relay de internet (https://...): sin puerto ni busqueda
            origin = normalize_relay_url(self.host)
            if not origin:
                raise ConnectionError("direccion del relay no valida")
            url = ("wss://" if origin.startswith("https://") else "ws://") + origin.split("://", 1)[1] + "/ws"
        else:
            port = await self._resolve_port(host)
            url = f"http://{host}:{port}/ws"
        timeout = aiohttp.ClientTimeout(total=None, sock_connect=5)
        self._synced = False
        async with _FragProxy(url) as px, aiohttp.ClientSession(timeout=timeout) as sess:
            ws = await _ws_connect(sess, px.url, url, heartbeat=8)
            async with ws:
                self._ws = ws
                await ws.send_json({"v": 1, "type": "auth", "password": self.password, "caps": ["wx"]})
                first = await ws.receive(timeout=8.0)
                if first.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.CLOSED):
                    raise _HostOffline()
                data = None
                if first.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(first.data)
                    except Exception:
                        data = None
                if not (isinstance(data, dict) and data.get("type") == "auth"):
                    raise ConnectionError("respuesta inesperada")
                if not data.get("ok"):
                    # contraseña mala: NO se reintenta (el servidor bloquea la IP tras 5 fallos)
                    self.auth_failed = True
                    self.is_paused = True
                    self.error = "Contraseña incorrecta"
                    self._status("Desconectado (contraseña incorrecta)")
                    self._notice("Contraseña incorrecta")
                    return False
                self.role = data.get("role")
                st = data.get("server_time")
                if isinstance(st, (int, float)) and not isinstance(st, bool):
                    self._time_offset = float(st) - time.time()
                self.ws_up = True
                self.error = ""
                self.server_features = tuple(data.get("features") or []) if isinstance(
                    data.get("features"), (list, tuple)) else ()
                _cbw = self.on_wx
                if _cbw:   # el servidor sabe mandar pronostico? (si es una version vieja, esta copia descarga el suyo)
                    try:
                        _cbw({"type": "wx_caps", "supported": "wx" in self.server_features})
                    except Exception:
                        pass
                if (self.role == "controller" and "sync" in self.server_features
                        and self.sync_provider is not None):
                    asyncio.get_running_loop().create_task(self._sync_client_loop())
                async for m in ws:
                    if m.type == aiohttp.WSMsgType.TEXT:
                        self._on_server_msg(m.data)
                    elif m.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSING,
                                    aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                        break
                    if not self.is_running or self.is_paused:
                        break
                return True

    # ------------------------------------------------------------ mensajes del servidor
# ===== [sync-datos] =====

    # ---- sincronizacion de datos del cliente (LAN) ----
    def request_sync(self):
        """Desde la interfaz (otro hilo): envia YA los datos de esta PC al servidor."""
        self._sync_next_ts = 0.0

    async def _sync_client_loop(self):
        try:
            await asyncio.sleep(3.0)          # tras autenticar y recibir el snapshot
            self._sync_next_ts = 0.0
            while self.is_running and not self.is_paused and self.ws_up:
                try:
                    if time.time() >= self._sync_next_ts:
                        self._sync_next_ts = time.time() + 120.0
                        await self._sync_send()
                except Exception:
                    pass
                await asyncio.sleep(2.0)
        except Exception:
            pass

    async def _sync_send(self):
        import base64
        import secrets
        try:
            if self.sync_provider is None or self._ws is None or self._ws.closed:
                return
            raw = await asyncio.wait_for(
                asyncio.get_running_loop().run_in_executor(None, self.sync_provider), 15.0)
            if not isinstance(raw, dict) or raw.get("empty"):
                return
            parts = split_payload(raw)
            if not parts:
                return
            uid = secrets.token_hex(8)
            who = raw.get("pc_id") or ""
            for i, chunk in enumerate(parts):
                ws = self._ws
                if ws is None or ws.closed:
                    return
                try:
                    await self._ws_send({"v": 1, "type": "sync_data", "uid": uid, "n": i + 1,
                                        "N": len(parts), "who": who,
                                        "d": base64.b64encode(chunk).decode("ascii")})
                except Exception:
                    return
        except Exception:
            pass

    def _sync_rx_add(self, m):
        import base64
        try:
            uid = str(m.get("uid") or "")[:40]
            N, n, d = m.get("N"), m.get("n"), m.get("d")
            if not uid or not isinstance(N, int) or not isinstance(n, int):
                return None
            if N < 1 or N > 3000 or n < 1 or n > N or not isinstance(d, str) or len(d) > 8000:
                return None
            st = self._sync_rx.setdefault(uid, {"parts": {}, "N": N, "t": time.time()})
            st["parts"][n] = base64.b64decode(d)
            st["t"] = time.time()
            if len(st["parts"]) != N:
                now = time.time()
                for k in [k for k, v in list(self._sync_rx.items()) if now - v["t"] > 600]:
                    self._sync_rx.pop(k, None)
                return None
            self._sync_rx.pop(uid, None)
            payload = join_payload([st["parts"][i] for i in sorted(st["parts"])])
            if payload:
                payload["__uid"] = uid
            return payload if payload else None
        except Exception:
            return None

    async def _apply_sync(self, payload):
        """Aplica un payload recibido y confirma al servidor."""
        summary = {"ok": True}
        if self.sync_apply is not None:
            try:
                res = await asyncio.get_running_loop().run_in_executor(None, self.sync_apply, payload)
                summary = res if isinstance(res, dict) else {"ok": True}
            except Exception as exc:
                summary = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        cb = self.on_sync_done
        if cb:
            try:
                cb(summary)
            except Exception:
                pass
        try:
            await self._ws_send({"v": 1, "type": "sync_ack", "uid": payload.get("__uid"),
                                "ok": bool(summary.get("ok", True)),
                                "summary": {"from": summary.get("from"),
                                             "sky_days": summary.get("sky_days"),
                                             "sky_slots": summary.get("sky_slots"),
                                             "cell_peer": summary.get("cell_peer"),
                                             "capacity": summary.get("capacity"),
                                             "csv": summary.get("csv")}})
        except Exception:
            pass
    def _on_server_msg(self, raw: str):
        try:
            m = json.loads(raw)
        except Exception:
            return
        if not isinstance(m, dict):
            return
        kind = m.get("type")
        if kind == "snapshot":
            self._apply(m.get("state"), full=True)
        elif kind == "delta":
            self._apply(m.get("state"), full=False)
        elif kind == "wx_cfg":
            r = m.get("req")
            if isinstance(r, int) and not isinstance(r, bool):
                if len(self._wx_res) > 24:                 # nadie las recoge: no acumular
                    self._wx_res.clear()
                self._wx_res[r] = m
        elif kind == "wx":
            cb = self.on_wx
            if cb:
                try:
                    cb(m)
                except Exception:
                    pass
        elif kind == "bye":
            self.error = "Servidor apagado"
# ===== [sync-datos] =====
        elif kind == "sync_data":
            payload = self._sync_rx_add(m)
            if payload:
                asyncio.ensure_future(self._apply_sync(payload))
        elif kind == "sync_ack":
            cb = self.on_sync_done
            if cb:
                try:
                    cb(m.get("summary") or {})
                except Exception:
                    pass
        elif kind == "ack" and not m.get("ok"):
            self._notice(self._short_error(str(m.get("error") or "")))
            # el estado que adelantamos era falso: se pide el real
            asyncio.ensure_future(self._ws_send({"v": 1, "type": "get_snapshot"}))

    @staticmethod
    def _short_error(err: str) -> str:
        low = err.lower()
        if "permiso" in low:
            return "Solo lectura"
        if "bluetti" in low:
            return "Servidor sin Bluetti"
        if "rapido" in low:
            return "Demasiado rápido"
        return "Comando rechazado"

    def _apply(self, st, full: bool):
        if not isinstance(st, dict):
            return
        if full and not self._synced:
            self._synced = True
            if self.on_resync:
                try:
                    self.on_resync()
                except Exception:
                    pass
        off = self._time_offset
        with self._lock:
            new = dict(self.state)
            link = self.link_ok
            for k, v in st.items():
                if k == "link_ok":
                    link = bool(v)
                    continue
                if k == "connected":                 # lo calculamos nosotros (WebSocket + enlace del servidor)
                    continue
                if k in _TUPLE_KEYS and isinstance(v, list):
                    v = tuple(v)
                elif (k.endswith("_ts") and isinstance(v, (int, float)) and not isinstance(v, bool) and v):
                    v = v - off                        # hora del servidor -> hora de esta PC
                new[k] = v
            self.link_ok = link
            new["last_update"] = time.strftime("%H:%M:%S")
            prev = bool(new.get("connected"))
            now = bool(self.ws_up and link)
            new["connected"] = now
            new["encryption_active"] = now
            self.state = new
        if now != prev:
            if now:
                self._status(f"Conectado al servidor {self.host}")
            else:
                self._status("Desconectado (el servidor no tiene enlace con la Bluetti)")
        self._emit()

    def _link_lost(self, status: Optional[str] = None):
        """El WebSocket se cerro (WiFi caido, servidor apagado, pausa...)."""
        was_up = self.ws_up
        self.ws_up = False
        self.link_ok = False
        self.role = None
        self._ws = None
        with self._lock:
            new = dict(self.state)
            for k in _LIVE_ZERO:
                if k in new:
                    new[k] = 0.0 if isinstance(new[k], float) else 0
            new["connected"] = False
            new["encryption_active"] = False
            self.state = new
        if self.is_running and not self.is_paused and not self.auth_failed:
            self._status(status or "Desconectado. Reconectando automáticamente...")
        if was_up or self.is_paused:
            self._emit()

    def _emit(self):
        cb = self.on_telemetry
        if cb is not None:
            try:
                cb(dict(self.state))
            except Exception:
                logger.exception("Error en el callback de telemetria")

    def _status(self, msg: str):
        cb = self.on_status
        if cb is not None:
            try:
                cb(msg)
            except Exception:
                pass

    def _notice(self, text: str):
        cb = self.on_notice
        if cb is not None:
            try:
                cb(text)
            except Exception:
                pass


# =============================================================================
# AVISOS AL TELEFONO (ntfy.sh): la PC con la Bluetti manda un mensaje cuando salta una alerta.
# Funciona por internet sin abrir puertos ni instalar nada en la PC; el telefono solo necesita
# la app gratuita «ntfy» suscrita al mismo tema. Apagado por defecto.
# =============================================================================
class PushNotifier:
    COOLDOWN_S = 600.0      # la misma alerta no se repite antes de 10 min (aunque parpadee)
    LINK_LOST_S = 60.0      # sin enlace con la Bluetti durante tanto tiempo -> aviso
    MAX_PER_MIN = 6         # tope de seguridad: nunca mas de 6 mensajes por minuto

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self.last_error = ""
        self._active: set = set()
        self._last: Dict[str, float] = {}
        self._lost_since: Optional[float] = None
        self._lost_notified = False
        self._sent: List[float] = []
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("push_enabled") and self.cfg.get("push_topic"))

    @staticmethod
    def _key(text: str) -> str:
        """Identidad de una alerta sin los numeros que cambian (cuenta atras, voltaje...)."""
        return re.sub(r"\d+(?:[.:,]\d+)*", "#", text).strip()

    @staticmethod
    def _level(color: str) -> Tuple[int, List[str]]:
        c = (color or "").lower()
        if c in ("#ef4444", "#f87171", "#ff3366"):
            return 5, ["rotating_light"]
        if c in ("#f59e0b", "#fb923c", "#fbbf24"):
            return 4, ["warning"]
        return 3, ["information_source"]

    def update(self, alerts, connected: bool, paused: bool, now: float, extra: str = "") -> List[Dict[str, Any]]:
        """Compara con lo que ya estaba activo y devuelve los mensajes NUEVOS a enviar."""
        out: List[Dict[str, Any]] = []
        cur: Dict[str, Tuple[str, str]] = {}
        for a in alerts or []:
            try:
                cur[self._key(str(a[0]))] = (str(a[0]), str(a[1]))
            except Exception:
                continue
        for k, (text, color) in cur.items():
            if k not in self._active and now - self._last.get(k, -1e9) >= self.COOLDOWN_S:
                prio, tags = self._level(color)
                text = text.lstrip("⚠ ").strip() or text
                out.append({"title": "Bluetti: " + text, "message": (extra or text), "priority": prio, "tags": tags})
                self._last[k] = now
        self._active = set(cur)
        if connected or paused:                       # recuperado (o el usuario la desconecto a proposito)
            if self._lost_notified and connected:
                out.append({"title": "Bluetti reconectada", "message": extra or "Vuelve la conexion con la Bluetti.",
                            "priority": 2, "tags": ["white_check_mark"]})
            self._lost_since, self._lost_notified = None, False
        else:
            if self._lost_since is None:
                self._lost_since = now
            elif not self._lost_notified and now - self._lost_since >= self.LINK_LOST_S:
                self._lost_notified = True
                msg = "La PC perdio el enlace Bluetooth con la Bluetti."
                if extra:
                    msg += f" Último dato: {extra}"
                out.append({"title": "Bluetti sin conexion", "message": msg,
                            "priority": 4, "tags": ["warning"]})
        return out

    def _post(self, msg: Dict[str, Any]) -> Tuple[bool, str]:
        body = dict(msg)
        body["topic"] = self.cfg.get("push_topic", "")
        try:
            req = urllib.request.Request(
                str(self.cfg.get("push_server") or DEFAULT_CONFIG["push_server"]),
                data=json.dumps(body).encode("utf-8"), method="POST",
                headers={"Content-Type": "application/json", "User-Agent": "bluetti-hud"})
            with urllib.request.urlopen(req, timeout=10) as r:
                r.read()
            self.last_error = ""
            return True, ""
        except Exception as exc:
            self.last_error = f"{type(exc).__name__}: {exc}"[:120]
            logger.info("Aviso al telefono fallido: %s", self.last_error)
            return False, self.last_error

    def send(self, msg: Dict[str, Any]):
        """Envia en un hilo aparte (nunca bloquea la ventana)."""
        now = time.time()
        with self._lock:
            self._sent = [t for t in self._sent if now - t < 60.0]
            if len(self._sent) >= self.MAX_PER_MIN:
                return
            self._sent.append(now)
        threading.Thread(target=self._post, args=(msg,), name="push", daemon=True).start()

    def send_test(self, done) -> None:
        """Mensaje de prueba; done(ok, error) se llama desde el hilo de envio."""
        msg = {"title": "Bluetti: prueba", "message": "Si lees esto, los avisos al telefono funcionan.",
               "priority": 3, "tags": ["white_check_mark"]}
        threading.Thread(target=lambda: done(*self._post(msg)), name="push-test", daemon=True).start()


# =============================================================================
# CONTROLES DE LA VENTANA PRINCIPAL:  🌐 [Servidor | Cliente] [switch] ⚙
# (se mezcla en BluettiDesktopApp con un parche pequeno; ver apply_patch.py)
# =============================================================================
class ServerUiMixin:
    _C_CARD = "#0e141f"

    def _build_server_controls(self, parent):
        import customtkinter as ctk
        # Se empaquetan de derecha a izquierda: ⚙, switch, selector, 🌐, estado
        self.lbl_srv_gear = ctk.CTkLabel(parent, text="⚙", font=("Segoe UI", 16, "bold"),
                                         text_color="#94a3b8", cursor="hand2")
        self.lbl_srv_gear.pack(side="right", padx=(6, 0))
        self.lbl_srv_gear.bind("<Button-1>", lambda e: self._open_server_window())
        self.sw_server = ctk.CTkSwitch(parent, text="", width=40, switch_width=34, switch_height=16,
                                       progress_color="#38bdf8", command=self._on_toggle_server)
        self.sw_server.pack(side="right", padx=(6, 0))
        self.seg_server_mode = ctk.CTkSegmentedButton(parent, values=["Servidor", "Cliente"], height=22,
                                                      font=("Segoe UI", 10, "bold"), command=self._on_server_mode)
        self.seg_server_mode.pack(side="right", padx=(6, 0))
        self.seg_server_mode.set("Servidor")
        ctk.CTkLabel(parent, text="🌐", font=("Segoe UI Emoji", 12)).pack(side="right")
        # ancho fijo: que el texto cambiante no altere el tamano de la ventana
        self.lbl_srv_status = ctk.CTkLabel(parent, text="", width=150, anchor="e",
                                           font=("Segoe UI", 10, "bold"), text_color="#94a3b8")
        self.lbl_srv_status.pack(side="right", padx=(0, 6))

    # ---------------------------------------------------------------- arranque / cierre
    def _server_init(self):
        """Se llama una vez al final del arranque: restaura modo y estado guardados."""
        self._srv_cfg = load_server_config()
        self._local_ble = self.ble             # el gestor Bluetooth de esta PC (siempre existe)
        self._remote: Optional[RemoteBleManager] = None
        self._local_was_paused = False
        self._srv_notice = ("", 0.0)
        self._auth_warned = False
        self.srv = TelemetryServer(self._local_ble, self._srv_cfg)
        self._push = PushNotifier(self._srv_cfg)
        # Ordenes de la web que resuelve la propia app (siempre en el hilo de Tk, como los botones)
        self.srv.sky_provider = self._sky_provider_thread
        self.srv.wx_provider = self._wx_provider_thread
        self.srv.wx_cfg_provider = self._wx_cfg_thread
        self.srv.alert_provider = self._alert_provider_thread
        self.srv.app_cmds = {
            "dismiss_alert": lambda kind: self.after(0, self._web_dismiss_alert, kind),
            "ble_release": lambda: self.after(0, self._web_ble, True),
            "ble_connect": lambda: self.after(0, self._web_ble, False),
            "set_system_power_off": lambda: self.after(0, self._system_power_off),
        }
        # [sync-datos] la app puede enviar/recibir sus datos de estadisticas
        self.srv.sync_provider = self._sync_provider_thread
        self.srv.sync_apply = self._sync_apply_thread
        # Lo que diga el gestor Bluetooth local solo llega a la pantalla mientras sea el activo
        self._local_ble.on_telemetry = self._tele_from_local
        self._local_ble.on_status = self._status_from_local
        self.seg_server_mode.set("Cliente" if self._srv_cfg["mode"] == "client" else "Servidor")
        if self._srv_cfg["enabled"]:
            if self._srv_cfg["mode"] == "server":
                self._server_start_ui()
            else:
                ok, msg = self._client_start_ui()
                if not ok:                      # sin ventanas emergentes al arrancar: queda dicho en la etiqueta
                    self.sw_server.deselect()
                    self._srv_notice = (msg.splitlines()[0][:30], time.time() + 10.0)
        self.after(1000, self._server_status_tick)

    def _server_shutdown(self):
        """Al cerrar la app: se detiene lo que haya (servidor, o cliente + Bluetooth local en pausa)."""
        try:
            self.srv.stop()
        except Exception:
            pass
        remote = getattr(self, "_remote", None)
        if remote is not None:
            try:
                remote.stop()
            except Exception:
                pass
            try:
                self._local_ble.stop()
            except Exception:
                pass

    # ---------------------------------------------------------------- quien manda en la pantalla
    def _tele_from_local(self, t):
        if self.ble is self._local_ble:
            self._handle_telemetry(t)

    def _status_from_local(self, msg):
        if self.ble is self._local_ble:
            self._handle_status(msg)

    def _tele_from_remote(self, remote, t):
        if self.ble is remote:
            self._handle_telemetry(t)

    def _status_from_remote(self, remote, msg):
        if self.ble is remote:
            self._handle_status(msg)

    def _notice_from_remote(self, remote, text):
        if self.ble is remote:
            self._srv_notice = (text, time.time() + 6.0)

    def _resync_from_remote(self, remote):
        if self.ble is remote:
            self.after(0, self._reset_alert_trackers)

    def _reset_alert_trackers(self):
        """Primer dato real del servidor: los detectores de eventos (pico de arranque, avisos...) parten
        de cero para no anunciar como 'nuevo' algo que ya paso antes de conectar."""
        self._alert_tracker = None
        fw = getattr(self, "floating_widget", None)
        try:
            if fw is not None and fw.winfo_exists():
                fw._prot_seen_init = False
        except Exception:
            pass

    def _swap_ble(self, new):
        """La app y el widget pasan a leer de 'new' (gestor Bluetooth local o RemoteBleManager)."""
        self.ble = new
        self._alert_tracker = None
        self._dc_override = None
        fw = getattr(self, "floating_widget", None)
        try:
            if fw is not None and fw.winfo_exists():
                fw.ble = new
                fw._prot_seen_init = False
        except Exception:
            pass
        self._on_ble_toggled()      # boton Conectar/Desconectar + repintado

    def _ble_btn_text(self, which: int) -> str:
        """Textos del boton de la barra superior: en modo Cliente conectan/desconectan del servidor."""
        if getattr(self, "_remote", None) is not None:
            return ("▶ Conectar", "⏏ Desconectar")[which]
        return ("▶ Conectar BLE", "⏏ Desconectar BLE")[which]

    # ---------------------------------------------------------------- modo Servidor
    def _server_start_ui(self) -> Tuple[bool, str]:
        ok, msg = self.srv.start()
        if ok:
            self.sw_server.select()
            self.seg_server_mode.configure(state="disabled")
            self._srv_cfg["enabled"] = True
            save_server_config(self._srv_cfg)
        else:
            self.sw_server.deselect()
            self.seg_server_mode.configure(state="normal")
        return ok, msg

    # ---------------------------------------------------------------- modo Cliente
    def _client_start_ui(self) -> Tuple[bool, str]:
        cfg = self._srv_cfg
        if not AIOHTTP_OK:
            return False, "Falta la libreria aiohttp.\nInstalala con:  pip install aiohttp" + f"\n\nDetalle tecnico: {AIOHTTP_ERR}"
        if not cfg["client_host"].strip():
            return False, "Escribe la dirección (IP) del servidor (botón ⚙)."
        if not cfg["client_password"]:
            return False, "Escribe la contraseña del servidor (botón ⚙)."
        if self._remote is not None:
            return True, "El cliente ya estaba activo."
        local = self._local_ble
        self._local_was_paused = bool(local.is_paused)
        remote = RemoteBleManager(cfg["client_host"], cfg["client_port"], cfg["client_password"])
        # (los callbacks comprueban que este siga siendo el gestor activo)
        remote.on_telemetry = lambda t, r=remote: self._tele_from_remote(r, t)
        remote.on_status = lambda m, r=remote: self._status_from_remote(r, m)
        remote.on_notice = lambda m, r=remote: self._notice_from_remote(r, m)
        remote.on_resync = lambda r=remote: self._resync_from_remote(r)
        remote.on_wx = lambda m, r=remote: self._wx_from_remote(r, m)
        self._remote = remote
        self._wx_set_remote(True)               # el clima lo manda el servidor: este equipo no descarga ni necesita internet
        # [sync-datos] mismo intercambio de datos en modo Cliente
        remote.sync_provider = self._sync_provider_thread
        remote.sync_apply = self._sync_apply_thread
        remote.on_sync_done = self._sync_done_ui
        self._auth_warned = False
        self._swap_ble(remote)                  # app y widget pasan a leer del servidor
        if not local.is_paused:
            local.pause_and_release()           # solo una copia puede usar el Bluetooth de la Bluetti
        remote.start()
        self.sw_server.select()
        self.seg_server_mode.configure(state="disabled")
        cfg["enabled"] = True
        save_server_config(cfg)
        return True, "Cliente activo."

    def _client_stop_ui(self):
        """Deja el modo Cliente y devuelve el Bluetooth a esta PC (si ya lo tenia en uso antes)."""
        remote = self._remote
        if remote is None:
            return
        self._remote = None
        remote.stop()
        self._wx_set_remote(False)              # vuelve a descargar por su cuenta (servidor / equipo local)
        local = self._local_ble
        self._swap_ble(local)
        if not self._local_was_paused:
            local.resume_connection()
            fw = getattr(self, "floating_widget", None)
            try:
                if fw is not None and fw.winfo_exists():
                    fw.set_connecting(True)
            except Exception:
                pass

    def _add_qr_block(self, win, body, row: int, vars_, url_override: Optional[str] = None,
                      title: str = "Código QR para el teléfono (red local)") -> int:
        """Codigo QR con la direccion del servidor: se escanea con la camara del telefono. Si algo falla
        aqui, el resto de la ventana sigue funcionando."""
        try:
            import tkinter as tk
            import customtkinter as ctk
            from urllib.parse import quote
            urls = [url_override] if url_override else self.srv.local_urls()
            if not urls:
                return row
            url = next((u for u in urls if "//192.168.137." in u), urls[0])    # hotspot de Windows primero
            draws = vars_.setdefault("_qr_draws", [])                         # todos los QR de la ventana
            inc = vars_.get("_qr_inc")
            first_block = inc is None
            if first_block:
                inc = vars_["_qr_inc"] = tk.BooleanVar(master=win, value=bool(self._srv_cfg.get("qr_with_password", False)))
            canvas = tk.Canvas(body, bg="#ffffff", highlightthickness=0)
            cap = ctk.CTkLabel(body, text="", font=("Consolas", 10), text_color="#94a3b8")

            def draw():
                pw = (self._srv_cfg.get("view_password") or "") if inc.get() else ""
                target = url + "/" + (("#p=" + quote(pw, safe="")) if pw else "")
                try:
                    m = qr_matrix(target)
                except ValueError:
                    m = qr_matrix(url + "/")
                    target = url + "/"
                n, sc, q = len(m), 4, 4
                px = (n + 2 * q) * sc
                canvas.delete("all")
                canvas.configure(width=px, height=px)
                for y, line in enumerate(m):
                    for x, dark in enumerate(line):
                        if dark:
                            canvas.create_rectangle((x + q) * sc, (y + q) * sc, (x + q + 1) * sc, (y + q + 1) * sc,
                                                    fill="#000000", outline="#000000")
                cap.configure(text=url + ("   (con contraseña)" if pw else ""))

            def on_toggle():
                self._srv_cfg["qr_with_password"] = bool(inc.get())
                save_server_config(self._srv_cfg)
                for d in draws:
                    d()

            draws.append(draw)
            ctk.CTkLabel(body, text=title, font=("Segoe UI", 10, "bold"),
                         text_color="#38bdf8").grid(row=row, column=0, columnspan=3, sticky="w", pady=(8, 2))
            row += 1
            canvas.grid(row=row, column=0, columnspan=3, pady=(2, 2))
            row += 1
            cap.grid(row=row, column=0, columnspan=3)
            row += 1
            if not first_block:
                draw()
                return row
            ctk.CTkCheckBox(body, text="Incluir la contraseña «Solo ver» en el QR (entra sin escribirla)",
                            variable=inc, font=("Segoe UI", 10), text_color="#cbd5e1", checkbox_width=18,
                            checkbox_height=18, command=on_toggle).grid(row=row, column=0, columnspan=3,
                                                                       sticky="w", pady=2)
            row += 1
            ctk.CTkLabel(body, text="Con la contraseña dentro, quien vea el QR podrá ver los datos: no lo "
                                    "compartas. Los cambios de contraseña se reflejan al guardar y reabrir esta ventana."
                                    + ("" if self.srv.running else " (Enciende el servidor para que la dirección responda.)"),
                         font=("Segoe UI", 9), text_color="#64748b", justify="left", anchor="w",
                         wraplength=440).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 4))
            row += 1
            draw()
        except Exception:
            pass
        return row

    def _add_relay_block(self, win, body, row: int, vars_) -> int:
        """Acceso desde internet por tu relay de Cloudflare: interruptor, direccion, clave y permiso de control."""
        import tkinter as tk
        import customtkinter as ctk
        cfg = self._srv_cfg
        ctk.CTkLabel(body, text="Acceso desde internet (relay de Cloudflare)", font=("Segoe UI", 10, "bold"),
                     text_color="#38bdf8").grid(row=row, column=0, columnspan=3, sticky="w", pady=(10, 2))
        row += 1
        en = tk.BooleanVar(master=win, value=bool(cfg.get("relay_enabled")))
        vars_["relay_enabled"] = en
        ctk.CTkCheckBox(body, text="Permitir ver los datos desde internet", variable=en, font=("Segoe UI", 10),
                        text_color="#cbd5e1", checkbox_width=18, checkbox_height=18
                        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=2)
        row += 1
        url = tk.StringVar(master=win, value=cfg.get("relay_url", ""))
        vars_["relay_url"] = url
        ctk.CTkLabel(body, text="Dirección del relay", font=("Segoe UI", 10), text_color="#cbd5e1",
                     anchor="w").grid(row=row, column=0, sticky="w", pady=2)
        ctk.CTkEntry(body, textvariable=url, width=250, height=26).grid(row=row, column=1, columnspan=2, sticky="e",
                                                                       padx=(8, 0), pady=2)
        row += 1
        key = tk.StringVar(master=win, value=cfg.get("relay_key", ""))
        vars_["relay_key"] = key
        ctk.CTkLabel(body, text="Clave del relay", font=("Segoe UI", 10), text_color="#cbd5e1",
                     anchor="w").grid(row=row, column=0, sticky="w", pady=2)
        ctk.CTkEntry(body, textvariable=key, width=250, height=26, show="•").grid(row=row, column=1, columnspan=2,
                                                                                 sticky="e", padx=(8, 0), pady=2)
        row += 1
        ctl = tk.BooleanVar(master=win, value=bool(cfg.get("relay_allow_control")))
        vars_["relay_allow_control"] = ctl
        ctk.CTkCheckBox(body, text="Permitir también CONTROLAR (encender/apagar salidas) desde internet", variable=ctl,
                        font=("Segoe UI", 10), text_color="#fbbf24", checkbox_width=18, checkbox_height=18
                        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=2)
        row += 1
        state = self.srv.relay_state if self.srv.running else ""
        if not state:
            state = "En línea" if self.srv.relay_up else ("Apagado" if not cfg.get("relay_enabled") else "Se conecta al encender el servidor")
        ctk.CTkLabel(body, text="Estado del relay: " + state, font=("Segoe UI", 10, "bold"),
                     text_color="#10b981" if state == "En línea" else "#94a3b8", anchor="w"
                     ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(2, 0))
        row += 1
        ctk.CTkLabel(body, text="La PC llama al relay (nadie entra en ella) y tu teléfono entra por la dirección del relay "
                                f"con las mismas contraseñas, que deben tener al menos {MIN_REMOTE_PW} caracteres. Desde "
                                "internet, por defecto, solo se puede VER aunque uses la contraseña de «Controlar». "
                                "Para crear el relay ejecuta desplegar_relay.py (cierra antes esta app).",
                     font=("Segoe UI", 9), text_color="#64748b", justify="left", anchor="w",
                     wraplength=440).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 4))
        row += 1
        return row

    def _add_push_block(self, win, body, row: int, vars_) -> int:
        """Avisos al telefono (ntfy): interruptor, tema, servidor, generar tema y mandar prueba."""
        import tkinter as tk
        import customtkinter as ctk
        cfg = self._srv_cfg
        ctk.CTkLabel(body, text="Avisos al teléfono (app «ntfy»)", font=("Segoe UI", 10, "bold"),
                     text_color="#38bdf8").grid(row=row, column=0, columnspan=3, sticky="w", pady=(10, 2))
        row += 1
        en = tk.BooleanVar(master=win, value=bool(cfg.get("push_enabled")))
        vars_["push_enabled"] = en
        ctk.CTkCheckBox(body, text="Enviar al teléfono las alertas de la app (también fuera de casa)", variable=en,
                        font=("Segoe UI", 10), text_color="#cbd5e1", checkbox_width=18,
                        checkbox_height=18).grid(row=row, column=0, columnspan=3, sticky="w", pady=2)
        row += 1
        topic = tk.StringVar(master=win, value=cfg.get("push_topic", ""))
        vars_["push_topic"] = topic
        ctk.CTkLabel(body, text="Tema", font=("Segoe UI", 10), text_color="#cbd5e1",
                     anchor="w").grid(row=row, column=0, sticky="w", pady=2)
        ctk.CTkEntry(body, textvariable=topic, width=250, height=26).grid(row=row, column=1, columnspan=2,
                                                                         sticky="e", padx=(8, 0), pady=2)
        row += 1
        server = tk.StringVar(master=win, value=cfg.get("push_server", DEFAULT_CONFIG["push_server"]))
        vars_["push_server"] = server
        ctk.CTkLabel(body, text="Servidor ntfy", font=("Segoe UI", 10), text_color="#cbd5e1",
                     anchor="w").grid(row=row, column=0, sticky="w", pady=2)
        ctk.CTkEntry(body, textvariable=server, width=250, height=26).grid(row=row, column=1, columnspan=2,
                                                                          sticky="e", padx=(8, 0), pady=2)
        row += 1
        result = ctk.CTkLabel(body, text="", font=("Segoe UI", 9), text_color="#94a3b8", anchor="w",
                              justify="left", wraplength=440)

        def gen():
            topic.set("bluetti-" + secrets.token_urlsafe(12).replace("=", ""))
            result.configure(text="Tema nuevo generado: pulsa Guardar y suscríbete a él en el teléfono.",
                             text_color="#fbbf24")

        def test():
            t = re.sub(r"[^A-Za-z0-9_-]", "", topic.get().strip())
            if not t:
                result.configure(text="Escribe o genera un tema primero.", text_color="#f87171")
                return
            tmp = PushNotifier({"push_enabled": True, "push_topic": t,
                                "push_server": server.get().strip().rstrip("/") or DEFAULT_CONFIG["push_server"]})
            result.configure(text="Enviando prueba…", text_color="#94a3b8")

            def done(ok, err):
                def paint():
                    try:
                        result.configure(text="✔ Enviada: debería sonar en el teléfono." if ok else "⚠ No se pudo enviar: " + err,
                                         text_color="#10b981" if ok else "#f87171")
                    except Exception:
                        pass
                try:
                    win.after(0, paint)
                except Exception:
                    pass
            tmp.send_test(done)

        bt = ctk.CTkFrame(body, fg_color="transparent")
        bt.grid(row=row, column=0, columnspan=3, sticky="w", pady=(2, 2))
        ctk.CTkButton(bt, text="Generar tema", width=110, height=26, fg_color="#1e293b", hover_color="#334155",
                      command=gen).pack(side="left", padx=(0, 6))
        ctk.CTkButton(bt, text="Enviar prueba", width=110, height=26, fg_color="#1e293b", hover_color="#334155",
                      command=test).pack(side="left")
        row += 1
        result.grid(row=row, column=0, columnspan=3, sticky="w")
        row += 1
        ctk.CTkLabel(body, text="1) Instala la app gratuita «ntfy» en el teléfono.  2) Suscríbete al mismo tema.  "
                                "El tema hace de contraseña de los avisos: usa uno largo y aleatorio (botón «Generar tema») "
                                "y no lo compartas. Hace falta internet en esta PC; los mensajes llevan solo el texto de la "
                                "alerta y el % de batería. Se avisa también si la PC pierde la Bluetti durante más de 1 minuto.",
                     font=("Segoe UI", 9), text_color="#64748b", justify="left", anchor="w",
                     wraplength=440).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 4))
        row += 1
        return row

    def _on_server_mode(self, value: str):
        self._srv_cfg["mode"] = "client" if value == "Cliente" else "server"
        save_server_config(self._srv_cfg)
        win = getattr(self, "_srv_win", None)
        try:                                   # la ventana abierta ya no corresponde al modo
            if win is not None and win.winfo_exists():
                win.destroy()
        except Exception:
            pass

    def _on_toggle_server(self):
        from tkinter import messagebox
        if self.sw_server.get():
            if self._srv_cfg["mode"] == "client":
                ok, msg = self._client_start_ui()
                if not ok:
                    self.sw_server.deselect()
                    self.seg_server_mode.configure(state="normal")
                    messagebox.showwarning("Cliente", msg, parent=self)
            else:
                ok, msg = self._server_start_ui()
                if not ok:
                    messagebox.showwarning("Servidor", msg, parent=self)
        else:
            if self._remote is not None:
                self._client_stop_ui()
            else:
                self.srv.stop()
            self._srv_cfg["enabled"] = False
            save_server_config(self._srv_cfg)
            self.seg_server_mode.configure(state="normal")

    def _web_dismiss_alert(self, kind: str):
        """La «x» de la barra de estado, pulsada desde la web: mismo efecto que en la app."""
        try:
            tr = self._get_alert_tracker()
            if kind == "dc":
                tr.dismiss_dc()
            elif kind == "temp":
                tr.dismiss_temp()
            else:
                tr.dismiss_all()
            self._refresh_alert_ui()
        except Exception:
            logger.exception("dismiss_alert desde la web")

    def _web_ble(self, release: bool):
        """«Desconectar/Conectar BLE» pulsado desde la web (solo si cambia algo)."""
        try:
            if self._remote is None and bool(self._local_ble.is_paused) != release:
                self._toggle_ble_release()
        except Exception:
            logger.exception("ble desde la web")

    def _srv_extra_tick(self):
        """Cada segundo (modo Servidor): deja en el servidor lo que solo la app sabe, para que la web
        muestre EXACTAMENTE lo mismo: las alertas con tus umbrales y si el BLE esta en pausa."""
        if self._remote is not None or not self.srv.running:
            return
        tr = getattr(self, "_alert_tracker", None)
        alerts, sev, allv = [], "ok", []
        if tr is not None:
            try:
                alerts = [[str(a[0]), str(a[1]), a[2] if isinstance(a[2], str) else None] for a in tr.alerts(time.time())]
                sev = tr.severity()
                allv = [[str(a[0]), str(a[1]), a[2] if isinstance(a[2], str) else None]
                        for a in tr._vis.visible(tr._all_alerts(time.time()), True)]
            except Exception:
                alerts, sev, allv = [], "ok", []
        extra = {"ui_alerts": alerts, "ui_all": allv, "ui_sev": sev, "srv_ble_paused": bool(self._local_ble.is_paused)}
        sky = getattr(self, "sky", None)
        if sky is not None:      # la linea «☀ Soleado con rendimiento máximo · Producción: X Wh (hoy)» de la app
            try:
                txt, col, prod = sky.row_parts()
                extra.update({"sky_txt": str(txt), "sky_col": str(col), "sky_prod": str(prod), "sky_state": str(sky.state)})
            except Exception:
                pass
        ac = self._alert_cfg_module()
        if ac is not None:
            try:
                extra["alert_en"] = {k: bool(ac.get(k, "enabled")) for k in ac.SCHEMA}
            except Exception:
                pass
        # estimacion «sin enlace» de la app (% y tiempo aproximados mientras la Bluetti no responde)
        extra.update({"off_soc": None, "off_mode": None, "off_rem": None})
        try:
            if not (bool(self._local_ble.state.get("connected", False)) and not self._local_ble.is_paused):
                src = self._offline_src()
                if src is not None:
                    o_soc, o_rem = src._offline_state()
                    if o_soc is not None:
                        extra["off_soc"] = int(round(o_soc))
                        extra["off_mode"] = str(getattr(src, "_off_mode", "neutral"))
                        extra["off_rem"] = int(o_rem) if o_rem is not None else None
        except Exception:
            pass
        self.srv.set_extra(extra)

    # ---------------------------------------------------------------- ajustes de alertas (switches y ⚙ de la app)
    def _alert_provider_thread(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """Lo llama el servidor (otro hilo); la configuracion se toca SIEMPRE en el hilo de Tk."""
        import concurrent.futures
        fut: "concurrent.futures.Future" = concurrent.futures.Future()

        def run():
            try:
                fut.set_result(self._alert_cfg_handle(req))
            except Exception as exc:
                fut.set_exception(exc)
        self.after(0, run)
        return fut.result(timeout=SKY_TIMEOUT_S)

    @staticmethod
    def _alert_cfg_module():
        try:
            from ble_manager import alert_config
            return alert_config
        except Exception:
            return None

    def _alert_cfg_dump(self, ac) -> Dict[str, Any]:
        schema = {k: {"title": v["title"], "card_label": v["card_label"], "fields": v["fields"]} for k, v in ac.SCHEMA.items()}
        return {"ok": True, "schema": schema, "values": {k: ac.section(k) for k in ac.SCHEMA}}

    def _alert_cfg_handle(self, req: Dict[str, Any]) -> Dict[str, Any]:
        ac = self._alert_cfg_module()
        if ac is None:
            return {"ok": False, "error": "esta app no tiene alertas configurables"}
        if req.get("action") != "set":
            return self._alert_cfg_dump(ac)
        key, vals = req.get("key"), req.get("values") or {}
        if key not in ac.SCHEMA:
            return {"ok": False, "error": "Alerta desconocida"}
        fields = {f["key"]: f for f in ac.SCHEMA[key]["fields"] if "key" in f}
        cur = ac.section(key)
        new: Dict[str, Any] = {}
        enabled = vals.get("enabled", cur.get("enabled"))
        if not isinstance(enabled, bool):
            return {"ok": False, "error": "'enabled' debe ser verdadero o falso"}
        for k, v in vals.items():
            if k == "enabled":
                continue
            f = fields.get(k)
            if f is None:
                return {"ok": False, "error": "Campo desconocido: %s" % str(k)[:30]}
            name = str(f.get("label", k)).strip()
            if f["kind"] == "bool":
                if not isinstance(v, bool):
                    return {"ok": False, "error": "'%s': valor no valido." % name}
            elif f["kind"] == "choice":
                if v not in [c[0] for c in f["choices"]]:
                    return {"ok": False, "error": "'%s': opcion no valida." % name}
            else:
                if isinstance(v, bool) or not isinstance(v, (int, float)) or v != v:
                    return {"ok": False, "error": "'%s': escribe un numero." % name}
                if (f.get("min") is not None and v < f["min"]) or (f.get("max") is not None and v > f["max"]):
                    return {"ok": False, "error": "'%s' debe estar entre %g y %g %s." % (name, f["min"], f["max"], f.get("unit", ""))}
                v = int(round(v)) if f["kind"] == "int" else float(v)
            new[k] = v
        merged = dict(cur)
        merged.update(new)
        err = ac.validate(key, merged)
        if err:
            return {"ok": False, "error": err}
        ac.set_enabled(key, enabled)
        ac.set_values(key, new)
        if not ac.save():
            return {"ok": False, "error": "No se pudo guardar el archivo de configuracion"}
        try:
            sw = self.__dict__.get("_alert_switches", {}).get(key)
            if sw is not None:
                sw.select() if enabled else sw.deselect()
            self._alerts_config_changed()
        except Exception:
            pass
        out = self._alert_cfg_dump(ac)
        out["saved"] = key
        return out

    # ---------------------------------------------------------------- condiciones y estadisticas (ventana ☀ de la app)
    # ---- pronostico del clima: el servidor lo descarga y lo reparte; los clientes no descargan nada ----
    def _wx_manager(self):
        return getattr(getattr(self, "sky", None), "_weather_provider", None)

    def _wx_provider_thread(self) -> Dict[str, Any]:
        """Lo llama el servidor (otro hilo): pronostico horario actual. Solo lee memoria, no toca Tk."""
        wp = self._wx_manager()
        return wp.export_state() if wp is not None else {}

    def _wx_cfg_thread(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """Mando a distancia: un cliente con permiso de control gestiona el clima de este servidor (hilo del servidor)."""
        wp = self._wx_manager()
        if wp is None:
            return {"ok": False, "error": "Este equipo no tiene gestor de clima"}
        if wp.remote_mode():
            return {"ok": False, "error": "Este equipo es cliente: no gestiona el clima"}
        act = req.get("action")
        out: Dict[str, Any] = {"ok": True}
        if act == "set":
            err = wp.apply_cfg(req.get("values"))
            if err:
                return {"ok": False, "error": err}
            try:
                self.sky.invalidate_curves()
            except Exception:
                pass
        elif act == "refresh":
            out["started"] = bool(wp.fetch(force=True))
        out.update({"cfg": wp.cfg_view(), "status": wp.status_text(), "attempt": wp._attempt_ts, "ver": wp.cache_version()})
        return out

    def _wx_set_remote(self, on: bool):
        wp = self._wx_manager()
        if wp is not None:
            wp.set_remote_mode(on)
            try:
                self.sky.invalidate_curves()
            except Exception:
                pass

    def _wx_from_remote(self, remote, m):
        """Mensajes del servidor sobre el clima (hilo de red). Solo se atienden si este sigue siendo el gestor activo."""
        if self._remote is not remote:
            return
        if m.get("type") == "wx_caps":
            self._wx_set_remote(bool(m.get("supported")))   # servidor viejo: este equipo descarga por su cuenta
            return
        wp = self._wx_manager()
        if wp is not None and wp.apply_remote(m):
            try:
                self.sky.invalidate_curves()
            except Exception:
                pass

    def _sky_provider_thread(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """Lo llama el servidor (otro hilo). El SkyTracker se lee SIEMPRE en el hilo de Tk (donde se alimenta):
        se pide con after(0) y se espera el resultado unos segundos."""
        import concurrent.futures
        fut: "concurrent.futures.Future" = concurrent.futures.Future()

        def run():
            try:
                fut.set_result(self._sky_build(req))
            except Exception as exc:
                fut.set_exception(exc)
        self.after(0, run)
        return fut.result(timeout=SKY_TIMEOUT_S)

    def _sky_build(self, req: Dict[str, Any]) -> Dict[str, Any]:
        """Mismos datos que dibuja SkyWindow (hud_app.py): puntos del dia, curva esperada, sol, resumenes por dia."""
        mod = sys.modules.get(type(self).__module__)
        tr = getattr(self, "sky", None)
        if tr is None or mod is None or not hasattr(mod, "STATES") or not hasattr(mod, "SkyTracker"):
            return {"error": "esta version de la app no tiene condiciones del panel"}
        g = lambda n, d=None: getattr(mod, n, d)                 # constantes del modulo de la app
        meta = {"states": {k: [v[0], v[1], v[2], v[3]] for k, v in g("STATES").items()}, "keys": list(g("STATE_KEYS")),
                "alias": dict(g("ALIAS", {})), "bat_col": dict(g("BAT_COL", {})), "bat_name": dict(g("BAT_NAME", {})),
                "high_a": g("HIGH_A", 15.0), "ac_w": g("AC_CHG_W", 20.0), "full": g("FULL_SOC", 99), "neutral_w": g("BAT_NEUTRAL_W", 10.0),
                "sun_err": g("SUN_ERR_S", 180.0), "peak": [g("PEAK_H0", 9), g("PEAK_H1", 17)], "rib_px": g("RIB_PX", 50.0),
                "rib_a": g("RIB_A", 0.5), "lat": g("LAT_DEG", 0.0), "lon": g("LON_DEG", 0.0), "cable_m": g("CABLE_RUN_M", 20.0),
                "quiet": list(g("QUIET", ("na", "night", "lowsun"))),
                "bat_line": g("BAT_LINE", "#c084fc"), "bat_ac": g("BAT_AC", "#22c55e"), "gap_rib_a": g("GAP_RIB_ALPHA", 0.6),
                "bat_tag_hours": list(g("BAT_TAG_HOURS", (10, 16))), "peak_min_w": g("PEAK_MIN_W", 10)}
        today = tr.day or datetime.date.today().isoformat()
        now_label = ""
        try:
            now_label = g("state_label")(tr.state, datetime.date.today().month)
        except Exception:
            pass
        out: Dict[str, Any] = {"meta": meta, "today": today, "now": now_label, "mode": req["mode"], "slots": len(tr.data.get("slots", {}))}
        if req["mode"] == "bars":
            summ = tr.summaries()
            if req.get("all"):
                out["rows"] = sorted(([d, r] for d, r in summ.items()), key=lambda q: q[0])
            else:
                try:
                    end = datetime.date.fromisoformat(today)
                except Exception:
                    end = datetime.date.today()
                days = [(end - datetime.timedelta(days=i)).isoformat() for i in range(req["n"] - 1, -1, -1)]
                out["rows"] = [[d, summ.get(d)] for d in days]
            months: Dict[str, List[float]] = {}
            for d, r in summ.items():
                if r and r.get("wh", 0) > 0:
                    a = months.setdefault(d[:7], [0.0, 0])
                    a[0] += r["wh"]
                    a[1] += 1
            out["months"] = [[k, round(a[0], 1), int(a[1])] for k, a in sorted(months.items())[-6:]]
            return out
        dates = tr.dates()
        if today not in dates:
            dates = [today] + dates
        day = req.get("day") or today
        if day not in dates:
            day = today
        v = tr.view(day)
        sun = tr.sun_times(day)
        fx0, fx1 = ((max(0.0, sun[0] - 1800.0), min(86400.0, sun[1] + 1800.0)) if sun else (5 * 3600.0, 20 * 3600.0))
        curve = tr.expected_curve(day, fx0, fx1, 60)
        out.update({"day": day, "days": dates, "bin": v["bin"], "wh_in": v["wh_in"], "wh_panel": v["wh_panel"],
                    "pts": v["pts"], "sun": list(sun) if sun else None, "fx": [fx0, fx1],
                    "curve": [[int(c[0]), round(c[1], 1)] for c in curve]})
        # Curva "segun pronostico" y estimados de huecos / extremos: MISMA logica que la ventana de la app
        # (funciones compartidas de hud_app.py), para que la web muestre lo mismo.
        try:
            cwx = tr.expected_curve_wx(day, fx0, fx1, 60)
        except Exception:
            cwx = []
        out["curve_wx"] = [[int(c[0]), round(c[1], 1)] for c in cwx]
        gaps: List[Any] = []
        if g("gap_estimates") is not None:
            try:
                wxc, sk, lin = g("_wx_cache_of")(tr), g("_sk"), g("lin_soc")
                for e in g("gap_estimates")(tr, day, v["pts"], v["bin"], fx0, fx1, sun, fx0, fx1, curve, cwx, 60):
                    if not e["vis"]:
                        continue
                    pa, pb = e["pa"], e["pb"]
                    ln = g("gap_soc_line")(tr, day, e["s0"], e["s1"], pa, pb, v["bin"])
                    soc, bk = [], None
                    if ln:
                        d0_, d1_ = ln[4], ln[5]
                        bk = g("gap_bat_key")(lin(ln, d0_), lin(ln, d1_))
                        if d1_ > d0_:
                            n_ = max(2, min(600, int((d1_ - d0_) // 60)))
                            soc = [[int(d0_ + (d1_ - d0_) * i / n_), round(lin(ln, d0_ + (d1_ - d0_) * i / n_), 1)]
                                   for i in range(n_ + 1)]
                    ref = pa if pa is not None else pb
                    runs = g("gap_sky_runs")(wxc, day, e["s0"], e["s1"], sk(ref[4]))
                    gaps.append({"s": [int(e["s0"]), int(e["s1"])],
                                 "pw": [[int(s_), round(w_, 1)] for s_, w_ in e["pw"]],
                                 "sky": [[int(a_), int(b_), k_] for a_, b_, k_ in runs], "bat": bk, "soc": soc})
            except Exception:
                logger.exception("get_sky: estimados de huecos")
        out["gaps"] = gaps
        return out

    def _push_tick(self):
        """Cada segundo, en modo Servidor: manda al telefono las alertas nuevas (las que ya ve la app)."""
        push = getattr(self, "_push", None)
        if push is None or not push.enabled or self._remote is not None:
            return
        now = time.time()
        tr = getattr(self, "_alert_tracker", None)
        alerts = tr.alerts(now) if tr is not None else []
        ble = self._local_ble
        connected = bool(ble.state.get("connected")) and not ble.is_paused

        # Extra robusto: lee de ble.state (fuente directa) con fallback a _last_t,
        # y cachea la última temperatura válida para no perderla en gaps.
        last = getattr(self, "_last_t", None) or {}
        st = ble.state if isinstance(getattr(ble, "state", None), dict) else {}

        def pick(key):
            for src in (st, last):
                v = src.get(key)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    return v
            return None

        soc, temp = pick("soc"), pick("batt_temp")
        if temp is not None:
            self._push_last_temp = temp
        else:
            temp = getattr(self, "_push_last_temp", None)

        # Calcular estado de carga/descarga: (entradas) - (salidas)
        ac_in = pick("ac_input_watts") or 0
        dc_in = pick("dc_input_watts") or 0
        ac_out = pick("ac_output_watts") or 0
        dc_out = pick("dc_output_watts") or 0
        net_w = ac_in + dc_in - ac_out - dc_out

        # Detectar Modo UPS: batería al 100% Y red AC presente (voltaje >= 50V o watts >= 20W)
        ac_volts = pick("ac_input_volts") or 0
        is_ups = (soc is not None and soc >= 100) and (ac_volts >= 50.0 or ac_in >= 20)

        if is_ups:
            charge_state = "Modo UPS"
        elif net_w > 5:
            charge_state = f"Cargando +{int(net_w)}W"
        elif net_w < -5:
            charge_state = f"Descargando {int(net_w)}W"
        else:
            charge_state = "Consumo equilibrado"

        parts = [f"Batería {soc}%"] if soc is not None else []
        if temp is not None:
            parts.append(f"{temp:.1f} °C")
        parts.append(charge_state)
        extra = " · ".join(parts)

        for m in push.update(alerts, connected, bool(ble.is_paused), now, extra):
            push.send(m)

# ===== [sync-datos] =====

    # ---- sincronizacion de datos (interfaz) ----
    def _sync_provider_thread(self):
        """Payload con los datos de estadisticas de ESTA PC (lo llama la red, otro hilo)."""
        try:
            return collect_payload()
        except Exception as exc:
            return {"error": "%s: %s" % (type(exc).__name__, exc)}

    def _sync_apply_thread(self, payload):
        """Aplica lo de la otra PC: se hace ternando en SIEMPRE en el hilo de Tk (el SkyTracker
        vive ahi). Primero se guarda lo pendiente, luego se fusiona el archivo y se recarga."""
        import concurrent.futures
        fut: "concurrent.futures.Future" = concurrent.futures.Future()

        def run():
            try:
                sky = getattr(self, "sky", None)
                if sky is not None:                       # persistir lo que aun no estaba en disco
                    try:
                        sky.save()
                    except Exception:
                        pass
                summ = apply_payload(payload)
                if not isinstance(summ, dict):
                    summ = {"ok": False}
                if summ.get("sky_days") or summ.get("sky_slots") or summ.get("sky_changed"):
                    if sky is not None:                    # recargar lo fusionado (no pisarlo)
                        try:
                            sky._load()
                            if sky.day:                    # re-enlazar el dia en curso con lo recien cargado
                                sky.rec = sky.data["days"].setdefault(
                                    sky.day, {"pts": [], "wh_in": 0.0, "wh_panel": 0.0, "rmax": 0.0,
                                              "bin": getattr(sky, "_bs", 60)})
                                sky.rec.setdefault("rmax", 0.0)
                                sky.rec.setdefault("bin", getattr(sky, "_bs", 60))
                                sky._bs = int(sky.rec["bin"])
                        except Exception:
                            pass
                bl = getattr(self, "_local_ble", None) or getattr(self, "ble", None)
                if summ.get("capacity") and bl is not None and hasattr(bl, "refresh_cap_samples"):
                    try:
                        bl.refresh_cap_samples()
                    except Exception:
                        pass
                fut.set_result(summ)
            except Exception as exc:
                fut.set_exception(exc)
        self.after(0, run)
        return fut.result(timeout=15.0)

    def _sync_done_ui(self, summary):
        """Lo llama el hilo del cliente cuando el servidor confirma que aplico lo nuestro."""
        try:
            self.after(0, lambda s=summary: self._srv_notice_set(self._sync_summary_text(s)))
        except Exception:
            pass

    def _sync_summary_text(self, s):
        if not isinstance(s, dict):
            return "Sincronizacion hecha"
        partes = []
        if s.get("sky_days"):
            partes.append("cielo %d dia(s)" % s["sky_days"])
        if s.get("capacity"):
            partes.append("capacidad")
        if s.get("cell_peer"):
            partes.append("celdas")
        if s.get("csv"):
            try:
                partes.append("registro %d fila(s)" % sum(s["csv"].values()))
            except Exception:
                pass
        if s.get("sky_slots"):
            partes.append("franjas %d" % s["sky_slots"])
        return ("Sincronizado OK: " + " · ".join(partes)) if partes else "Sincronizado OK (sin cambios)"

    def _srv_notice_set(self, texto):
        try:
            self._srv_notice = (texto[:60], time.time() + 8.0)
            t = getattr(self, "lbl_srv_status", None)
            if t is not None:
                self._cfg(t, text=texto[:60], text_color="#34d399")
        except Exception:
            pass

    def _sync_now_ui(self):
        """Boton del ⚙: envia YA lo nuestro (la otra copia contesta con lo suyo)."""
        try:
            if self._remote is not None:
                self._remote.request_sync()
            elif getattr(self, "srv", None) is not None:
                self.srv.request_sync_all()
            self._srv_notice = ("Sincronizando datos…", time.time() + 10.0)
        except Exception:
            pass

    def _sync_server_check(self):
        """Cada segundo (modo Servidor): pinta la confirmacion de que el cliente aplico lo nuestro."""
        srv = getattr(self, "srv", None)
        if srv is None:
            return
        n = getattr(srv, "_sync_notice", None)
        if n is None or n[1] < time.time() or n[1] == getattr(self, "_sync_notice_shown", None):
            return
        self._sync_notice_shown = n[1]
        self._srv_notice = n
    def _server_status_tick(self):
        try:
            self._push_tick()
        except Exception:
            pass
        try:
            self._srv_extra_tick()
        except Exception:
            pass
        try:
            self._sync_server_check()
        except Exception:
            pass
        try:
            text, color = "", "#94a3b8"
            note, until = self._srv_notice
            noting = bool(note) and time.time() < until
            r = self._remote
            if r is not None:
                if noting:
                    text, color = "⚠ " + note, "#fbbf24"
                elif r.auth_failed:
                    text, color = "⚠ Contraseña mala", "#f87171"
                elif r.is_paused:
                    text, color = "○ Desconectado", "#94a3b8"
                elif not r.ws_up:
                    text, color = "◌ " + (r.error or "Conectando…"), "#fbbf24"
                elif not r.link_ok:
                    text, color = "⚠ Sin Bluetooth", "#fbbf24"
                else:
                    text, color = ("● Controla", "#10b981") if r.role == "controller" else ("● Solo ver", "#38bdf8")
                    if r.active_port and r.active_port != r.port:
                        text += f" · puerto {r.active_port}"
                if r.auth_failed and not self._auth_warned:
                    self._auth_warned = True
                    from tkinter import messagebox
                    messagebox.showwarning("Cliente", "El servidor rechazó la contraseña.\n\n"
                                           "Corrígela en ⚙ y pulsa «Conectar».", parent=self)
            elif self.srv.running:
                n = self.srv.client_count
                text, color = f"● {n} cliente{'' if n == 1 else 's'}", "#10b981"
                if self._srv_cfg.get("relay_enabled"):
                    text += " · ☁" if self.srv.relay_up else " · ☁✕"      # ☁ = en línea por internet
                if self.srv.port and self.srv.port != self._srv_cfg.get("port"):
                    text += f" · puerto {self.srv.port}"      # el configurado estaba ocupado/reservado
            elif self.srv.error:
                text, color = "⚠ No pudo iniciar", "#f87171"
            elif noting:
                text, color = "⚠ " + note, "#fbbf24"
            self._cfg(self.lbl_srv_status, text=text, text_color=color)
        except Exception:
            pass
        try:
            self.after(1000, self._server_status_tick)
        except Exception:
            pass

    def _open_server_window(self):
        import math
        import tkinter as tk
        import customtkinter as ctk

        old = getattr(self, "_srv_win", None)
        try:
            if old is not None and old.winfo_exists():
                old.lift()
                old.focus()
                return
        except Exception:
            pass

        cfg = self._srv_cfg
        is_server = cfg["mode"] == "server"
        win = ctk.CTkToplevel(self)
        win.title("Servidor de red" if is_server else "Cliente de red")
        win.configure(fg_color=self._C_CARD)
        win.resizable(False, False)
        win.transient(self)
        # Icono de la app
        try:
            import sys, os
            base_dir = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
            ico_path = os.path.join(base_dir, "bluetti.ico")
            win.iconbitmap(ico_path)
        except Exception:
            pass
        self._srv_win = win
        ctk.CTkLabel(win, text="🌐 " + ("SERVIDOR DE RED" if is_server else "CLIENTE DE RED"),
                     font=("Segoe UI", 12, "bold"), text_color="#00f0ff").pack(anchor="w", padx=18, pady=(14, 4))
        if is_server:     # con el QR y los avisos la ventana es alta: se desplaza
            body = ctk.CTkScrollableFrame(win, fg_color="transparent", width=470,
                                          height=int(min(640, self.winfo_screenheight() * 0.66)))
        else:
            body = ctk.CTkFrame(win, fg_color="transparent", width=470)
        body.pack(fill="x", padx=12, pady=(0, 4))
        body.columnconfigure(0, weight=1)

        def note(text, row, color="#64748b"):
            ctk.CTkLabel(body, text=text, font=("Segoe UI", 9), text_color=color, justify="left", anchor="w",
                         wraplength=440).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 4))

        entries = []
        vars_ = {}

        def add_entry(row, label, key, value, width=160, secret=False):
            var = tk.StringVar(master=win, value=str(value))
            ctk.CTkLabel(body, text=label, font=("Segoe UI", 10), text_color="#cbd5e1",
                         anchor="w").grid(row=row, column=0, sticky="w", pady=2)
            ent = ctk.CTkEntry(body, textvariable=var, width=width, height=26, justify="right",
                               show="•" if secret else "")
            ent.grid(row=row, column=1, columnspan=2, sticky="e", padx=(8, 0), pady=2)
            vars_[key] = var
            if secret:
                entries.append(ent)

        row = 0
        if is_server:
            note("Esta PC es la que habla con la Bluetti por Bluetooth y comparte los datos por WiFi/hotspot.", row)
            row += 1
            add_entry(row, "Puerto", "port", cfg["port"], width=80)
            row += 1
            add_entry(row, "Contraseña «Solo ver»", "view_password", cfg["view_password"], secret=True)
            row += 1
            add_entry(row, "Contraseña «Controlar»", "control_password", cfg["control_password"], secret=True)
            row += 1
            note("«Controlar» puede encender/apagar salidas (hasta 1800 W). Si la dejas vacía, nadie podrá controlar.", row)
            row += 1
            show_var = tk.BooleanVar(master=win, value=False)
            ctk.CTkCheckBox(body, text="Mostrar contraseñas", variable=show_var, font=("Segoe UI", 10),
                            text_color="#cbd5e1", checkbox_width=18, checkbox_height=18,
                            command=lambda: [e.configure(show="" if show_var.get() else "•") for e in entries]
                            ).grid(row=row, column=0, columnspan=3, sticky="w", pady=2)
            row += 1
            ctk.CTkLabel(body, text="Direcciones para el teléfono (navegador)", font=("Segoe UI", 10, "bold"),
                         text_color="#38bdf8").grid(row=row, column=0, columnspan=3, sticky="w", pady=(8, 2))
            row += 1
            urls = self.srv.local_urls()
            ctk.CTkLabel(body, text="\n".join(urls) if urls else "No se detectó ninguna red activa.",
                         font=("Consolas", 11, "bold"), text_color="#fcd34d", justify="left", anchor="w"
                         ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(0, 2))
            row += 1
            note("Con el hotspot de Windows suele ser 192.168.137.1. La primera vez, Windows pedirá permitir "
                 "la app en el firewall (redes privadas).", row)
            row += 1
            row = self._add_qr_block(win, body, row, vars_)
            if self._srv_cfg.get("relay_url"):
                row = self._add_qr_block(win, body, row, vars_, url_override=self._srv_cfg["relay_url"],
                                         title="Código QR para internet (relay)")
            row = self._add_relay_block(win, body, row, vars_)
            row = self._add_push_block(win, body, row, vars_)
        else:
            note("Esta copia NO usará Bluetooth: mostrará (y podrá controlar) lo que le llegue del servidor. "
                 "Con el hotspot de Windows del servidor, la dirección suele ser 192.168.137.1. Desde internet, "
                 "escribe la dirección del relay (https://...workers.dev).", row)
            row += 1
            add_entry(row, "Dirección del servidor (IP)", "client_host", cfg["client_host"], width=160)
            row += 1
            add_entry(row, "Puerto", "client_port", cfg["client_port"], width=80)
            row += 1
            add_entry(row, "Contraseña", "client_password", cfg["client_password"], secret=True)
            row += 1
            show_var = tk.BooleanVar(master=win, value=False)
            ctk.CTkCheckBox(body, text="Mostrar contraseña", variable=show_var, font=("Segoe UI", 10),
                            text_color="#cbd5e1", checkbox_width=18, checkbox_height=18,
                            command=lambda: [e.configure(show="" if show_var.get() else "•") for e in entries]
                            ).grid(row=row, column=0, columnspan=3, sticky="w", pady=2)
            row += 1

        msg = ctk.CTkLabel(win, text="", font=("Segoe UI", 10), text_color="#fbbf24", wraplength=450, justify="left")
        msg.pack(anchor="w", padx=18, pady=(2, 0))

        def on_save(_e=None):
            def fail(text):
                msg.configure(text="⚠ " + text, text_color="#f87171")

            new = {}
            try:
                port_key = "port" if is_server else "client_port"
                port = int(str(vars_[port_key].get()).strip())
                if not 1024 <= port <= 65535:
                    raise ValueError
                new[port_key] = port
            except ValueError:
                return fail("'Puerto' debe ser un número entre 1024 y 65535.")
            if is_server:
                view = vars_["view_password"].get()
                ctrl = vars_["control_password"].get()
                if len(view) < 4:
                    return fail("La contraseña «Solo ver» debe tener al menos 4 caracteres.")
                if ctrl and len(ctrl) < 4:
                    return fail("La contraseña «Controlar» debe tener al menos 4 caracteres (o déjala vacía).")
                if ctrl and ctrl == view:
                    return fail("Las dos contraseñas deben ser distintas.")
                new["view_password"], new["control_password"] = view, ctrl
                if "relay_enabled" in vars_:
                    rraw = vars_["relay_url"].get().strip()
                    rurl, rkey = normalize_relay_url(rraw), vars_["relay_key"].get().strip()
                    if rraw and not rurl:
                        return fail("La dirección del relay no es válida (debe empezar por https://).")
                    if vars_["relay_enabled"].get():
                        if not rurl:
                            return fail("Escribe la dirección del relay (la da desplegar_relay.py).")
                        if not rkey:
                            return fail("Escribe la clave del relay (la da desplegar_relay.py).")
                        if len(view) < MIN_REMOTE_PW or (ctrl and len(ctrl) < MIN_REMOTE_PW):
                            return fail(f"Para usar internet, las contraseñas deben tener al menos {MIN_REMOTE_PW} caracteres.")
                    new.update(relay_enabled=bool(vars_["relay_enabled"].get()), relay_url=rurl, relay_key=rkey,
                               relay_allow_control=bool(vars_["relay_allow_control"].get()))
                if "push_topic" in vars_:
                    ptopic = re.sub(r"[^A-Za-z0-9_-]", "", vars_["push_topic"].get().strip())
                    pserver = vars_["push_server"].get().strip().rstrip("/") or DEFAULT_CONFIG["push_server"]
                    if vars_["push_enabled"].get() and not ptopic:
                        return fail("Escribe un tema para los avisos al teléfono (o desmarca la casilla).")
                    if not pserver.lower().startswith(("http://", "https://")):
                        return fail("El servidor ntfy debe empezar por https://")
                    new.update(push_enabled=bool(vars_["push_enabled"].get()), push_topic=ptopic, push_server=pserver)
            else:
                host = vars_["client_host"].get().strip()
                raw_host = host.split("/")[0] if "://" not in host else host
                relay_addr = ""
                if host.lower().startswith("https://") or raw_host.lower().endswith(".workers.dev"):
                    relay_addr = normalize_relay_url(host)      # relay de internet: https://xxx.workers.dev
                    if not relay_addr:
                        return fail("La dirección del relay no es válida.")
                for prefix in ("http://", "https://", "ws://", "wss://"):
                    if host.lower().startswith(prefix):
                        host = host[len(prefix):]
                host = host.split("/")[0].strip()
                if relay_addr:
                    host = relay_addr
                elif host.count(":") == 1:             # se pego "192.168.137.1:8765"
                    host, _, port_txt = host.partition(":")
                    if not (port_txt.isdigit() and 1024 <= int(port_txt) <= 65535):
                        return fail("El puerto de la dirección debe estar entre 1024 y 65535.")
                    new["client_port"] = int(port_txt)
                if not host:
                    return fail("Escribe la dirección (IP) del servidor.")
                if not vars_["client_password"].get():
                    return fail("Escribe la contraseña del servidor.")
                new["client_host"] = host
                new["client_password"] = vars_["client_password"].get()
            restart = any(cfg.get(k) != new.get(k) for k in ("port", "view_password", "control_password",
                                                                  "relay_enabled", "relay_url", "relay_key"))
            cfg.update(new)
            if not save_server_config(cfg):
                return fail("No se pudo guardar el archivo de configuración.")
            if is_server and self.srv.running and restart:      # aplicar los cambios reiniciando el servidor
                self.srv.stop()
                ok, text = self._server_start_ui()
                if not ok:
                    return fail(text)
            elif not is_server and self._remote is not None:   # idem con el cliente
                self._client_stop_ui()
                ok, text = self._client_start_ui()
                if not ok:
                    self.sw_server.deselect()
                    self.seg_server_mode.configure(state="normal")
                    cfg["enabled"] = False
                    save_server_config(cfg)
                    return fail(text)
            if not is_server:
                vars_["client_host"].set(cfg["client_host"])
                vars_["client_port"].set(str(cfg["client_port"]))
            msg.configure(text="✔ Guardado", text_color="#10b981")

        btns = ctk.CTkFrame(win, fg_color="transparent")
        btns.pack(pady=(8, 14))
        ctk.CTkButton(btns, text="↻ Sincronizar datos", width=140, height=28,
                      fg_color="#0e7490", hover_color="#155e75",
                      command=lambda: self._sync_now_ui()).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Guardar", width=90, height=28, fg_color="#1d4ed8", hover_color="#2563eb",
                      command=on_save).pack(side="left", padx=4)
        ctk.CTkButton(btns, text="Cerrar", width=80, height=28, fg_color="#1e293b", hover_color="#334155",
                      command=win.destroy).pack(side="left", padx=4)
        win.bind("<Escape>", lambda e: win.destroy())

        win.update_idletasks()
        try:
            sc = float(win._get_window_scaling())
        except Exception:
            sc = 1.0
        w, h = math.ceil(win.winfo_reqwidth() / sc), math.ceil(win.winfo_reqheight() / sc)
        x = self.winfo_rootx() + self.winfo_width() + 8
        if x + w > self.winfo_screenwidth():
            x = max(0, self.winfo_rootx() - w - 8)
        y = max(0, self.winfo_rooty())
        win.geometry("%dx%d+%d+%d" % (w, h, x, y))
        win.lift()


# =============================================================================
# PAGINA WEB DEL TELEFONO (Fase 1)
# =============================================================================
# ---------------------------------------------------------------- PWA (instalable + abre sin internet)
# El service worker guarda la pagina en el telefono: si al recargar no hay red, se abre igual y la propia
# pagina reintenta conectar (antes Chrome mostraba "sin conexion / copia guardada" y no cargaba nada).
# Solo funciona en https (el relay de Cloudflare); en http://192.168... el navegador no lo permite.
SW_JS = r"""const C='bluetti-shell-v2';
const OFFLINE='<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1"><title>Bluetti</title><body style="background:#080b11;color:#e2e8f0;font:16px system-ui;text-align:center;padding:40px 20px"><h2 style="color:#00f0ff">⚡ Bluetti</h2><p>Sin conexión con el servidor.</p><p style="color:#94a3b8">Reintentando solo… <a href="/" style="color:#7dd3fc">Reintentar ahora</a></p><script>setTimeout(()=>location.reload(),6000);addEventListener("online",()=>location.reload())</script>';
self.addEventListener('install',e=>{self.skipWaiting()});
self.addEventListener('notificationclick',e=>{e.notification.close();e.waitUntil((async()=>{const cs=await clients.matchAll({type:'window',includeUncontrolled:true});for(const c of cs){if('focus' in c)return c.focus()}return clients.openWindow('/')})())});
self.addEventListener('activate',e=>{e.waitUntil((async()=>{for(const k of await caches.keys())if(k!==C)await caches.delete(k);await self.clients.claim()})())});
function tmo(p,ms){return new Promise((ok,no)=>{const t=setTimeout(()=>no(new Error('t')),ms);p.then(v=>{clearTimeout(t);ok(v)},e=>{clearTimeout(t);no(e)})})}
async function nav(){
  const c=await caches.open(C);
  try{
    const r=await tmo(fetch('/',{cache:'no-store'}),5000);
    if(r&&r.ok&&(r.headers.get('content-type')||'').indexOf('text/html')>=0){c.put('/',r.clone());return r}
  }catch(_){}
  const m=await c.match('/');
  return m||new Response(OFFLINE,{status:200,headers:{'content-type':'text/html; charset=utf-8'}});
}
self.addEventListener('fetch',e=>{
  const r=e.request,u=new URL(r.url);
  if(r.method!=='GET'||u.origin!==location.origin)return;
  if(r.mode==='navigate'||u.pathname==='/'){e.respondWith(nav());return}
  if(u.pathname.indexOf('/icon-')===0||u.pathname==='/manifest.webmanifest'){
    e.respondWith((async()=>{const c=await caches.open(C);const m=await c.match(r);
      const f=fetch(r).then(x=>{if(x.ok)c.put(r,x.clone());return x}).catch(()=>m);return m||f})());
  }
});
"""
MANIFEST_JSON = json.dumps({
    "name": "Bluetti Elite 100 V2", "short_name": "Bluetti", "start_url": "/", "scope": "/",
    "display": "standalone", "orientation": "any", "background_color": "#080b11", "theme_color": "#080b11",
    "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
              {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"}]})
ICON_192_B64 = "iVBORw0KGgoAAAANSUhEUgAAAMAAAADACAIAAADdvvtQAAAC1klEQVR42u3dsU4CQRCAYdhQXILN0fKgPoMPamtlYWlhQiyEIMfdzsx+f2uDy8fsXjhgPx3nnfRozRIIIAEkgASQBJAAEkACSAJIAAkgASQBJIAEkACSABJAAkgACSAJIAEkgASQBJAAEkACSAJIAAkgASQBJIAEkAASQBJAAkgACSAJIAEkgASQBJAAEkACSAJIAAkgASQd4jyUr/cPz8e1pvMp5gPbT8cZHYyyAkInO6NGT7pCrVuzCgzlA0RPmTVs9DBU5DI+8sUqKFEm0I21oOeBlelu6xB/jWINg8/Xvx//y9va6xNzDm06ga4tQXY9fV9jfWH1fy+sgJ61x0/ktfJmqgCqvnkBVFnPZvsXQGYPQPQIoGdeFo29fwFk/ABED0BJ9di/ABJANi+A6AFoOD0OQACZPQAJoIzjx/516WAJim1eG9/22egZQc/Pn9a4+bXR89/C7l/3+Hi6IWcgAWTzAiiLHtdfrsIWzZ6Fc6uev0bPZpWcXg7R9AAUfvwUPjk1egSQow9Ajj4AGT+j6RkCED0A0QMQPQBJAwEyfgCiJ0E1341f8kS608MEMnsASrX3jXyXGUBmD0ACyPgBaLgDED0AmT0A0XN5VHd8+v3pn5AHyOwBaMMDUHA9NwbMdD6t8QUdvt6l2uzZ+DfFTCABZPwARA9AZU/Q9ABk9gBED0D0ADTcAUgAGT8A0QMQPQDVPwDRA5DZAxA9AAmggQ5Axk9iQGv8BlHVzav7WnUGdO1muY7rUkDPxrcgBt3Cuhgye/IBuvFyCbtGkfX0HT+7aDfVM+QQvWgIKd1KtmH/c3pyH6IZqrF6zSrQs6T9dJydndHJDQijvNM6ECC5jBdAEkACSAAJIAkgASSABJAEkAASQAJIAkgACSABJIAkgASQABJAEkACSAAJIAkgASSABJAEkAASQAJIAEkACSABJIAkgASQABJAEkACSAAJIOl334WP5bOQQfEmAAAAAElFTkSuQmCC"
ICON_512_B64 = "iVBORw0KGgoAAAANSUhEUgAAAgAAAAIACAIAAAB7GkOtAAAI9UlEQVR42u3dsW4qyRqFUbA6QMIJpDwoz+AHJSUiIHRKYFkN3VBV/14rn9EV07W/opk5d7vbHzYA5PnyEQAIAAACAIAAACAAAAgAAAIAgAAAIAAACAAAAgCAAAAgAAAIAAACAIAAACAAAAgAAAIAgAAAIAAACAAAAgCAAAAIAAACAIAAACAAAAgAAAIAgAAAIAAACAAAAgCAAAAgAAAIAAACAIAAACAAAAgAAAIAgAAAIAAACAAAAgCAAAAIAAACAIAAACAAAAgAAAIAgAAAIAAACAAAAgCAAAAgAAAIAAACAIAAACAAAAgAAAIAgAAAIAAACAAAAgCAAAAgAAACAIAAACAAAAgAAAIAgAAAIAAACAAAAgCAAAAgAAAIAAACAIAAACAAAAgAAAIAgAAAIAAACAAAAgCAAAAgAAACAIAAACAAAAgAAAIAgAAAIAAACAAAAgCAAAAgAAAIAAACAIAAACAAAAgAAAIAgAAAIAAACAAAAgCAAAAgAAAIAIAAACAAAAgAAAIAgAAAIAAACAAAAgCAAAAgAAAIAAACAIAAACAAAAgAAAIAgAAAIAAACAAAAgCAAAAgAAAIAIAAACAAAAgAAAIAgAAAIAAACAAAAgDAGCYfQUP3y9WHQD2709GHMITtbn/wKVh80AMBwO6DEggAph9kQAAw/SADAoDpBxkowr8Gav3BeREAPM3g1CTxCshDDF3wOsg3AOsPzhEC4KkFpwkB8LyCM4UAeFLByUIAPKPgfCEAAAiA6wk4ZQiA5xKcNQTAEwlOHAIAgAC4jIBzF8z/J3Bj/vwT7DWt+MPgmj3Wph/nxXnxDcCtH2o+5L4N+AbgOmP6cXwcnx75EdjFHzz5obwCcgaYd429nRc9AN8/4c+/10G+AYR+gbX+4evPKqdAQgQAxlv/8Os/AuDiA86CsyAAkHT9BwFw5SF0/b3/cSIEYNgJ8LuT9cd5FABcdqw/zoUAAPPGzvsfBABc/0EAwPqDAEDp9ff+BwEAd38QAAAEAGpf/73/QQAgcf1BAMD6gwBA0vp7/4MAACAAEHP9BwGA0PX3/gcBAHd/EACw/iAAUJv3PwgAuP6DAID1BwGA2uvv/Q8CAO7+IAAACADUvv57/4MAQOL6gwCA9QcBgKT19/4HAQBAAMD1HwQACq8/CABYfxAASFp/738QAAAEAGKu/yAAELr+3v8gAODuDwIA1h8EAGrz/gcBANd/EACw/iAAUHv9vf9BAMDdHwQAAAGA2td/73+oYfIRYP2p+ahcri8G/nQUALD+ZE3/41+ekAGvgLD+T14Pvf8pvf6r/30EAGCk9Q9pgADg+o/1D22AAGD9n+D9T9T6l2+AAGD9IZQAYP1x/Q/9EiAAMJf3PwgAuP6DAID1BwGA2uvv/Q8CAO7+IAAACADUvv57/0NJ/jRQrP/A/8O0Ct8AsP5YfwQA62/9QQDA+oMA4PoPCADW3/UfBADrb/0RALD+1h8BAKw/AgCu/9YfAQDrb/0RALD+1h8BAOsPAgC4/iMA4Ppv/REAsP7WHwHA+mP9EQCsP9YfAQCsPwKA6z/WHwHA+gMCgPV3/QcBwPpbfxAAsP4gALj+W38QAKy/9QcBwPpbfxAArD8gAOD6DwKA67/1RwB8BFh/648AgPW3/ggAWH/rjwAA1h8BANd/648AYP2tPwxu8hFQ+A7bZ6Vc//ENAIQTBAAyrv/WHwEAd38QALD+IABg/UEAYKl+fgCw/ggAAAIAMVz/EQCw/iAA8H49/ABg/REAcPcHAQDrDwIA1h8EAFbmz6kGAQDXfxAAsP4gAGD9QQAoqMkPANYfAQB3fxAAsP4gAGD9QQDgvfwXACAA4Pof9o/jdBz67y8AYP1BAMD6080lver1XwCo4zM/AFj/tAYUXn8BAOuvAaHrLwBg/TUgdP03m83kcQHrX7IB98vV9AsA9fkvAEje8Zd5BQSu/wgAYP0RAMD6IwDQuzf9AGD9EQBw9wcBAOsPAgCAAEBLq/8A4PqPAEAi648AgPUHAQDrDwIAvVnrBwDrjwCAuz8IAFh/EAAABAA6svwHANd/BAASWX8QAKw/CABYfxAA6NzLPwBYfxAA3P1BAMD6gwAAIADQrxd+AHD9BwEgkfUHAcD6AwKA9QcB8BEwlvk/AFh/EADc/QEBAEAAcP0HAYBhzPkBwPqDAODuDwgA1h8QAKw/IAAM5p8fAKw/CADu/oAAACAAuP4DAsCo/vwBwPqDAODuDwgA1h8QgObul6sPwfrjXAjAsFtzOvoQ2k/Gww8A1t95RABcdtz9cSIQAAAEwJXH9R9nAQGgymrcztYfBKDRxXPx704uPu7+LD8FfgEWAF9+rT+efwTAGQBPPgtsd/uDT+HDD7Fvsjg1Tk0PJh9Bq1Phgcb04xtA9DMtAzgmjolvAE4LwEf5Edg1BJw7AQBAAHAZASdOAPBEgrMmAHguwSkTAAAEwPUEcL4EwDMKOFkC4EkFnCkB8LwCTpMAeGoB56gb/jC4NfmDfcD0+wbgOQacGgHwNAPOS5e8AnoXr4PA9AuADACmXwBkAEw/AqAEYPcRAD0Ai48AAPAp/jVQAAEAQAAAEAAABAAAAQBAAAAQAAAEAAABAEAAABAAAAQAAAEAQAAAEAAABAAAAQBAAAAQAAAEAAABAEAAABAAAAEAQAAAEAAABAAAAQBAAAAQAAAEAAABAEAAABAAAAQAAAEAQAAAEAAABAAAAQBAAAAQAAAEAAABAEAAABAAAAHwEQAIAAACAIAAACAAAAgAAAIAgAAAIAAACAAAAgCAAAAgAAAIAAACAIAAACAAAAgAAAIAgAAAIAAACAAAAgCAAAAIAAACAIAAACAAAAgAAAIAgAAAIAAACAAAAgCAAAAgAAAIAAACAIAAACAAAAgAAAIAgAAAIAAACAAAAgCAAAAIAAACAIAAACAAAAgAAAIAgAAAIAAACAAAAgCAAAAgAAAIAAACAIAAACAAAAgAAAIAgAAAIAAACAAAAgCAAAAgAAACAIAAACAAAAgAAAIAgAAAIAAACAAAAgCAAAAgAAAIAABt/AKCxTaTkWTeHwAAAABJRU5ErkJggg=="


INDEX_HTML = r"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="theme-color" content="#080b11">
<link rel="manifest" href="/manifest.webmanifest">
<link rel="icon" href="/icon-192.png">
<link rel="apple-touch-icon" href="/icon-192.png">
<meta name="mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>Bluetti Elite 100 V2</title>
<style>
:root{--bg:#080b11;--card:#0e141f;--bd:#1e293b;--tx:#e2e8f0;--mut:#94a3b8;--cy:#00f0ff;--ok:#00ff88;--warn:#fbbf24;--bad:#ff3366;--blue:#1d4ed8}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.35 system-ui,"Segoe UI",sans-serif;
  padding:max(12px,env(safe-area-inset-top)) max(14px,env(safe-area-inset-right)) max(24px,env(safe-area-inset-bottom)) max(14px,env(safe-area-inset-left));
  max-width:560px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--bd);border-radius:16px;padding:14px;margin-bottom:12px}
.hide{display:none!important}
h1{font-size:15px;margin:0 0 10px;color:var(--cy);letter-spacing:.5px}
h2{font-size:12px;margin:0 0 8px;color:var(--mut);letter-spacing:.6px;font-weight:700;display:flex;justify-content:space-between;gap:8px}
h2 em{font-style:normal;color:var(--tx);font-weight:600;letter-spacing:0}
input{width:100%;padding:12px;border-radius:10px;border:1px solid var(--bd);background:var(--bg);color:var(--tx);font-size:16px}
button{font:inherit;cursor:pointer;-webkit-tap-highlight-color:transparent}
.btn{width:100%;padding:12px;border-radius:10px;border:0;background:var(--blue);color:#fff;font-weight:700;margin-top:10px}
.err{color:var(--bad);margin-top:8px;min-height:1.2em;font-size:13px}
.top{display:flex;align-items:center;justify-content:space-between;gap:8px;font-size:13px;color:var(--mut);margin-bottom:10px}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px;background:#64748b}
.dot.ok{background:var(--ok)}.dot.bad{background:var(--bad)}
.soc{font-size:64px;font-weight:800;text-align:center;line-height:1}
.bar{height:10px;border-radius:6px;background:var(--bd);margin:10px 8px 8px;overflow:hidden}
.bar>i{display:block;height:100%;width:0;background:#334155;transition:width .4s,background .4s}
.c{text-align:center}.state{font-weight:700;margin-top:4px}.time{color:#38bdf8;margin-top:2px}
.sec{display:flex;justify-content:space-between;align-items:baseline;margin-bottom:6px}
.sec b{font-size:12px;color:var(--mut);letter-spacing:.5px}.sec span{font-size:24px;font-weight:800}
.row{display:flex;justify-content:space-between;align-items:center;padding:7px 0;border-top:1px solid var(--bd)}
.row small{color:var(--mut);display:block;font-size:12px;font-family:Consolas,monospace}
.row strong{font-size:18px}
.kv{display:grid;grid-template-columns:1fr auto;gap:4px 12px;font-size:13px}
.kv span:nth-child(odd){color:var(--mut)}.kv span:nth-child(even){text-align:right;font-weight:600}
.btns{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.sw{padding:13px 8px;border-radius:12px;border:1px solid var(--bd);background:var(--bg);color:var(--mut);font-weight:800;font-size:15px}
.sw.on{background:#14301f;border-color:var(--ok);color:var(--ok)}
.sw.sel{background:#10264f;border-color:var(--blue);color:#93c5fd}
.sw:disabled{opacity:.45}
.sub{font-size:11px;color:var(--mut);letter-spacing:.5px;margin:12px 0 6px;font-weight:700}
.opts{display:grid;grid-template-columns:repeat(auto-fit,minmax(110px,1fr));gap:8px}
.opts .sw{padding:10px 6px;font-size:13px}
.step{display:flex;align-items:center;justify-content:center;gap:14px;margin-top:10px}
.step button{width:44px;height:40px;border-radius:10px;border:1px solid var(--bd);background:var(--bg);color:var(--tx);font-size:22px;font-weight:800}
.step button:disabled{opacity:.4}.step strong{font-size:20px;min-width:90px;text-align:center}
.alerts{display:flex;flex-direction:column;gap:8px}
.al{display:flex;align-items:center;justify-content:space-between;gap:10px;padding:10px 12px;border-radius:12px;border:1px solid;font-weight:700;font-size:14px}
.al.warn{color:var(--warn);border-color:#5b4510;background:#241c07}
.al.bad{color:#ff6b88;border-color:#6b1a2c;background:#2a0c14}
.al.info{color:#7dd3fc;border-color:#12415c;background:#08202e}
.al button{border:0;border-radius:8px;padding:6px 10px;background:#ffffff1a;color:inherit;font-weight:800}
.cells{display:grid;grid-template-columns:repeat(4,1fr);gap:5px;margin:4px 0 8px}
.cell{border:1px solid #26324a;border-radius:7px;background:var(--bg);padding:5px 2px;text-align:center;font:600 12px Consolas,monospace;color:#cbd5e1}
.cell.lo{border-color:#38bdf8;color:#38bdf8}.cell.hi{border-color:var(--warn);color:var(--warn)}
.cell.wn{border-color:#fb923c;color:#fb923c}.cell.cr{border-color:var(--bad);color:var(--bad)}
.pill{display:inline-block;padding:3px 10px;border-radius:999px;border:1px solid;font-size:12px;font-weight:700}
.sum{display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap;font:600 11px Consolas,monospace;color:var(--mut)}
.al.u{background:#ffffff0d}
details{font-size:13px}summary{cursor:pointer;font-size:12px;font-weight:700;letter-spacing:.6px;color:var(--mut);list-style:none}
summary::-webkit-details-marker{display:none}summary:after{content:" ▾"}details[open] summary:after{content:" ▴"}
.det{margin-top:10px}.det h3{font-size:11px;letter-spacing:.5px;color:#38bdf8;margin:12px 0 4px}
.det .kv{font-size:12.5px}.det p{margin:4px 0;color:var(--mut);font-size:12px}
.bleb{margin-top:12px;width:100%}
#skc{width:100%;display:block;border-radius:10px;touch-action:pan-y;background:#080b11;cursor:crosshair}
.rng{display:flex;gap:6px}.rng button,.mini{border:1px solid var(--bd);background:var(--bg);color:var(--mut);border-radius:8px;padding:5px 11px;font-weight:700;font-size:12px;cursor:pointer}
.rng button.sel{background:#10264f;border-color:var(--blue);color:#93c5fd}
.skbar{display:flex;gap:8px;align-items:center;justify-content:space-between;flex-wrap:wrap;margin:8px 0}
#skDay{background:var(--bg);color:var(--tx);border:1px solid var(--bd);border-radius:8px;padding:5px 8px;font-size:13px}
.skline{font-size:13px;margin:2px 0 4px}.skline b{font-weight:800}
.skhover{font:600 11px/1.35 Consolas,monospace;color:#cbd5e1;min-height:30px;margin:6px 0 4px}
.sklegs{margin-top:8px}.skrow{display:flex;flex-wrap:wrap;gap:4px 14px;font:10.5px Consolas,monospace;color:var(--mut);margin-bottom:4px}
.skrow i{display:inline-block;width:18px;vertical-align:middle;margin-right:6px}
.skrow span{white-space:nowrap}.skrow input{width:16px;height:16px;margin:0 6px 0 0;vertical-align:middle;accent-color:#0ea5e9;cursor:pointer}
.sksum{font:11px/1.45 Consolas,monospace;color:#cbd5e1;white-space:pre-wrap;margin-top:8px}
.info{border:1px solid var(--bd);background:var(--bg);color:#38bdf8;border-radius:50%;width:26px;height:26px;font-size:15px;font-weight:800;line-height:1;margin-left:8px;cursor:pointer;padding:0}
#cPill{cursor:pointer}
.mdl{position:fixed;inset:0;z-index:30;background:rgba(2,6,12,.82);display:flex;align-items:flex-start;justify-content:center;overflow:auto;padding:max(12px,env(safe-area-inset-top)) 12px 24px}
.mbox{background:var(--card);border:1px solid var(--bd);border-radius:16px;padding:14px;width:100%;max-width:560px}
.mh{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;font-size:12px;font-weight:700;letter-spacing:.6px;color:var(--mut)}
.mh button{border:1px solid var(--bd);background:var(--bg);color:var(--tx);border-radius:10px;padding:6px 12px;font-size:14px;font-weight:700}
.alrow{display:none;align-items:center;gap:8px;padding:6px 0 2px;border-top:1px solid var(--bd);font-size:12px;font-weight:700;color:var(--mut)}
.alrow.on{display:flex}.alrow a{cursor:pointer;flex:1;padding:4px 0}
.tg{position:relative;width:38px;height:20px;flex:none;display:inline-block}
.tg input{opacity:0;width:0;height:0;position:absolute}
.tg i{position:absolute;inset:0;background:var(--bd);border-radius:20px;transition:.2s;cursor:pointer}
.tg i:before{content:"";position:absolute;width:14px;height:14px;left:3px;top:3px;background:#cbd5e1;border-radius:50%;transition:.2s}
.tg input:checked+i{background:#1d4ed8}.tg input:checked+i:before{transform:translateX(18px);background:#fff}
.tg input:disabled+i{opacity:.5;cursor:default}
.afld{display:grid;grid-template-columns:1fr auto;gap:6px 10px;align-items:center;font-size:13px;margin:4px 0}
.afld label{color:#cbd5e1}.afld input[type=number],.afld select{width:96px;padding:7px 8px;font-size:14px;text-align:right}
.afld select{width:100%;max-width:210px;text-align:left}
.agrp{font-size:11px;font-weight:800;letter-spacing:.5px;color:#38bdf8;margin:12px 0 2px}
.anote{font-size:11.5px;color:#64748b;margin:2px 0}
.amsg{min-height:1.3em;font-size:12.5px;margin:8px 0 0}
.abtns{display:flex;gap:8px;margin-top:10px;flex-wrap:wrap}.abtns button{flex:1;min-width:90px;padding:10px;border-radius:10px;border:1px solid var(--bd);background:var(--bg);color:var(--tx);font-weight:700}
.abtns .pri{background:var(--blue);border-color:var(--blue);color:#fff}
.wlb{width:100%;margin:0 0 10px;padding:11px;border-radius:12px;border:1px solid var(--bd);background:var(--card);color:var(--mut);font-weight:800;font-size:14px}
.wlb.on{background:#10264f;border-color:var(--blue);color:#93c5fd}
.stale{background:#3b1d0a;border:1px solid #92400e;color:#fdba74;border-radius:12px;padding:9px 12px;margin:0 0 10px;font-size:13px}
.stale a{color:#fed7aa;text-decoration:underline;cursor:pointer;margin-left:6px}
#cSky.skfull{position:fixed;inset:0;z-index:20;margin:0;border-radius:0;overflow:auto;padding:12px max(12px,env(safe-area-inset-left))}
body.off .dim{opacity:.45}
.foot{text-align:center;color:var(--mut);font-size:12px;margin-top:4px}
.foot a{color:var(--mut);text-decoration:underline;cursor:pointer}
#toast{position:fixed;left:50%;bottom:22px;transform:translateX(-50%);background:#7f1d1d;color:#fff;padding:10px 16px;border-radius:10px;font-size:14px;opacity:0;transition:opacity .25s;pointer-events:none;z-index:9}
#toast.show{opacity:1}
#tri.tdim{opacity:.12}
/* ---- horizontal: 3 columnas ---- */
@media (orientation:landscape) and (min-width:640px){
  body{max-width:1200px}
  #grid{display:grid;grid-template-columns:1.05fr 1fr 1.1fr;gap:0 12px;align-items:start;
    grid-template-areas:"al al al" "bat inp ctl" "bat out ctl" "cel cel ctl" "det det det" "his his his"}
  #grid>.card{margin-bottom:12px}
  #cAl{grid-area:al}#cBat{grid-area:bat}#cIn{grid-area:inp}#cOut{grid-area:out}#cCtl{grid-area:ctl}#cCel{grid-area:cel}#cDet{grid-area:det}#cSky{grid-area:his}
  #grid>.card{min-width:0}
  .cells{grid-template-columns:repeat(8,minmax(0,1fr))}
  .cell{font-size:9.5px;padding:5px 0;overflow:hidden;white-space:nowrap;letter-spacing:-.2px}
  .alerts{flex-direction:row;flex-wrap:wrap}.al{flex:1 1 300px}
}
@media (orientation:landscape) and (max-height:520px){
  body{font-size:14px;padding-top:max(6px,env(safe-area-inset-top))}
  .card{padding:10px;margin-bottom:8px;border-radius:12px}
  .soc{font-size:46px}.bar{margin:6px 4px}.sec span{font-size:20px}.row strong{font-size:16px}
  .sw{padding:9px 6px;font-size:14px}.top{margin-bottom:6px}
}
</style></head><body>

<div id="login" class="card">
  <h1>⚡ BLUETTI ELITE 100 V2</h1>
  <input id="pw" type="password" placeholder="Contraseña" autocomplete="current-password">
  <button class="btn" id="go">Entrar</button>
  <div class="err" id="lerr"></div>
</div>

<div id="app" class="hide">
  <div class="top"><span><svg id="tri" class="hide" title="Avisos activos (toca para verlos)" viewBox="0 0 20 20" width="20" height="20" style="cursor:pointer;margin-right:6px;vertical-align:-4px;color:#f59e0b"><polygon points="10,1.5 1,18 19,18" fill="currentColor"/><rect x="8.8" y="7" width="2.4" height="5.5" fill="#0e141f"/><circle cx="10" cy="14.8" r="1.3" fill="#0e141f"/></svg><i class="dot" id="dot"></i><span id="conn">Conectando…</span></span><span id="temp"></span></div>
  <div class="stale hide" id="stale"><span id="staleTx"></span><a id="reBtn">Reconectar ahora</a></div>
  <button class="wlb" id="wlBtn">🔆 Mantener pantalla encendida: NO</button>
  <div class="stale hide" id="wlNote">Con http el navegador no puede bloquear la pantalla del todo. Usa la dirección https (relay).</div>
  <button class="wlb" id="ntBtn">🔔 Avisos del sistema: NO</button>

  <div id="grid">
    <div class="card hide" id="cAl"><div class="alerts" id="alerts"></div></div>

    <div class="card dim" id="cBat">
      <div class="soc" id="soc">--%</div>
      <div class="bar"><i id="bar"></i></div>
      <div class="c state" id="state">--</div>
      <div class="c time" id="time">⏱ --</div>
      <div class="sub">SALUD Y ENERGÍA</div>
      <div class="kv">
        <span>Capacidad estimada</span><span id="hCap">--</span>
        <span>Salud</span><span id="hHealth">--</span>
        <span>Ciclos equivalentes</span><span id="hCyc">--</span>
        <span>Energía cargada</span><span id="hChg">--</span>
        <span>Energía descargada</span><span id="hDis">--</span>
        <span>Calibración del %</span><span id="hCal">--</span>
      </div>
    </div>

    <div class="card dim" id="cIn">
      <div class="sec"><b>ENTRADA TOTAL</b><span id="inT" style="color:var(--cy)">-- W</span></div>
      <div class="row"><div>☀ Solar MPPT (Máx 1000W)<small id="solSub">--V • --A</small><small id="solMode">Fuente DC: --</small></div><strong id="solW">-- W</strong></div>
      <div class="alrow" data-k="dc_in_current"><label class="tg"><input type="checkbox"><i></i></label><a>Alertas de Corriente ⚙</a></div>
      <div class="row"><div>🔌 Red AC (Máx 1200W)<small id="acSub">--V • --A • --Hz</small></div><strong id="acW">-- W</strong></div>
      <div class="alrow" data-k="ac_in_voltage"><label class="tg"><input type="checkbox"><i></i></label><a>Alertas de Voltaje ⚙</a></div>
    </div>

    <div class="card dim" id="cOut">
      <div class="sec"><b>SALIDA TOTAL</b><span id="outT" style="color:var(--bad)">-- W</span></div>
      <div class="row"><div>⚡ Inversor AC (1800W)<small id="aoSub">--V • --Hz</small></div><strong id="aoW">-- W</strong></div>
      <div class="alrow" data-k="ac_out_current"><label class="tg"><input type="checkbox"><i></i></label><a>Alertas de Corriente ⚙</a></div>
      <div class="row"><div>🔋 Salidas DC<small>USB / 12 V</small></div><strong id="doW">-- W</strong></div>
      <div class="alrow" data-k="dc_out_battery"><label class="tg"><input type="checkbox"><i></i></label><a>Alertas de Batería ⚙</a></div>
    </div>

    <div class="card" id="cCtl">
      <h2>CONTROL <em id="ctlNote"></em></h2>
      <div class="btns">
        <button class="sw" data-t="ac">AC</button>
        <button class="sw" data-t="dc">DC</button>
        <button class="sw" data-t="eco">ECO</button>
        <button class="sw" data-t="grid">Refuerzo red</button>
        <button class="sw" data-t="pl" style="grid-column:1/-1">Power Lifting (2700 W)</button>
      </div>
      <div class="sub">ENTRADA DC (FUENTE)</div>
      <div class="opts">
        <button class="sw" data-src="other">Baterías (Máx 3A)</button>
        <button class="sw" data-src="pv">Panel solar</button>
        <button class="sw" data-t="hc">Alta corriente</button>
      </div>
      <div class="sub">MODO DE CARGA (RED AC)</div>
      <div class="opts">
        <button class="sw" data-cm="silent">Silencioso</button>
        <button class="sw" data-cm="standard">Estándar</button>
        <button class="sw" data-cm="turbo">Turbo</button>
        <button class="sw" data-cm="custom">Personalizado</button>
      </div>
      <div class="step hide" id="stepBox">
        <button id="aMinus">−</button><strong id="aVal">-- A</strong><button id="aPlus">+</button>
      </div>
      <button class="sw bleb" id="bleBtn">⏏ Desconectar BLE de la PC</button>
    </div>

    <div class="card dim" id="cCel">
      <h2>BANCO DE BATERÍAS <em id="bankV">--V</em></h2>
      <div class="cells" id="cells"></div>
      <div class="sum"><span id="cSum">Min -- • Max -- • Dif --</span><span><span class="pill" id="cPill">Sin datos</span><button class="info" id="infoBtn" title="Detalle del banco">ⓘ</button></span></div>
      <div class="sum" style="margin-top:6px"><span id="tSum">Temp. celdas --</span></div>
    </div>
    <div class="card dim" id="cDet">
      <details><summary>DETALLE DEL BANCO DE BATERÍAS</summary><div class="det" id="det"></div></details>
    </div>
    <div class="card dim" id="cSky">
      <h2>☀ CONDICIONES Y ESTADÍSTICAS DEL PANEL</h2>
      <div class="skline hide" id="skLine"></div>
      <div class="skbar"><span class="rng" id="skMode"><button data-m="day">Día</button><button data-m="week">Semana</button><button data-m="month">Mes</button></span><select id="skDay"></select></div>
      <div class="skhover" id="skHover"></div>
      <canvas id="skc"></canvas>
      <div class="skbar"><span class="rng" id="skZ"><button id="skZp">＋</button><button id="skZm">－</button><button id="skZr">Todo el día</button></span>
        <span class="rng"><button id="skPng">📷 PNG</button><button id="skCsv">CSV</button><button id="skFull">⤢</button></span></div>
      <div class="sklegs" id="skLeg"></div>
      <div class="sksum" id="skSum"></div>
    </div>
  </div>
  <div class="foot" id="foot"></div>
  <div class="foot"><a id="reBtn2">Reconectar ahora</a></div>
  <div class="foot"><a id="logout">Cerrar sesión</a></div>
</div>
<div class="mdl hide" id="mdl"><div class="mbox"><div class="mh"><span>ⓘ DETALLE DEL BANCO DE BATERÍAS</span><button id="mx">Cerrar</button></div><div class="det" id="detM"></div></div></div>
<div class="mdl hide" id="amdl"><div class="mbox"><div class="mh"><span id="amT">⚙ ALERTAS</span><button id="amx">Cerrar</button></div><div id="amB"></div><div class="amsg" id="amMsg"></div><div class="abtns" id="amBt"></div></div></div>
<div id="toast"></div>

<script>
const $=id=>document.getElementById(id);
let ws=null,PW='',role=null,S={},wsUp=false,authFailed=false,retry=0,cmdId=0,pend={},clkOff=0,bye=false,stepT=null,capped=false,authed=false;
const STALE_S_JS=20;
try{if('serviceWorker' in navigator&&window.isSecureContext)navigator.serviceWorker.register('/sw.js').catch(()=>{})}catch(_){}
const num=(v,d=0)=>(typeof v==='number'&&isFinite(v))?v.toFixed(d):'--';
const hm=m=>Math.floor(m/60)+'h '+String(m%60).padStart(2,'0')+'m';
const GRADE={excellent:['Equilibrada','#00ff88'],good:['Buen equilibrio','#4ade80'],acceptable:['Aceptable','#bef264'],fair:['Regular','#fbbf24'],bad:['Desequilibrio','#ff3366']};

function toast(t){const e=$('toast');e.textContent=t;e.classList.add('show');setTimeout(()=>e.classList.remove('show'),2500)}

/* ---- conexion: una sola a la vez, con tiempo maximo y reconexion al volver a la pagina ---- */
let gen=0,retryT=null,lastMsg=0,lastAge=null,pingN=0,lastSnap=0,lastSave=0;
/* ultimos datos guardados en el telefono: al abrir sin red se ven (atenuados) en vez de una pantalla vacia */
function saveLast(){const n=Date.now();if(n-lastSave<5000)return;lastSave=n;try{localStorage.setItem('bluetti_last',JSON.stringify({t:n,s:S}))}catch(_){}}
function ago(ms){const m=Math.round(ms/60000);return m<1?'menos de 1 min':(m<90?m+' min':Math.round(m/60)+' h')}
function detach(){if(ws){ws.onopen=ws.onmessage=ws.onclose=ws.onerror=null;try{ws.close()}catch(_){}}}
function reconnect(delay){
  clearTimeout(retryT);detach();gen++;wsUp=false;render();
  if(authFailed||!PW)return;
  retryT=setTimeout(connect,delay||0);
}
function connect(){
  clearTimeout(retryT);authFailed=false;bye=false;detach();
  const my=++gen,url=(location.protocol==='https:'?'wss://':'ws://')+location.host+'/ws';
  let w;try{w=new WebSocket(url)}catch(_){retryT=setTimeout(connect,3000);return}
  ws=w;lastMsg=Date.now();
  const ct=setTimeout(()=>{if(my===gen&&w.readyState!==1)fail()},9000);      /* un intento que se queda "conectando" para siempre */
  const fail=()=>{clearTimeout(ct);if(my!==gen)return;
    if(!authed)$('lerr').textContent='No se pudo conectar con el servidor (¿está apagado o sin internet?). Reintentando…';
    reconnect(Math.min(1000*Math.pow(2,retry++),8000))};
  w.onopen=()=>{clearTimeout(ct);w.send(JSON.stringify({type:'auth',password:PW}))};
  w.onmessage=e=>{if(my!==gen)return;lastMsg=Date.now();let m;try{m=JSON.parse(e.data)}catch(_){return}handle(m)};
  w.onclose=fail;w.onerror=()=>{};
}
/* al volver a la pagina (desbloquear, cambiar de app, recuperar internet) el WebSocket suele estar muerto aunque diga "abierto": se comprueba ya */
function probe(){
  if(!PW||authFailed)return;
  if(!ws||ws.readyState!==1){retry=0;reconnect(0);return}
  const t0=Date.now(),g0=gen;
  try{ws.send('{"type":"ping"}')}catch(_){reconnect(0);return}
  setTimeout(()=>{if(g0===gen&&lastMsg<t0)reconnect(0)},4000);
}
document.addEventListener('visibilitychange',()=>{if(document.visibilityState==='visible'){probe();if(typeof skyAsk==='function'&&authed)skyAsk()}});
window.addEventListener('pageshow',e=>{if(e.persisted)probe()});
window.addEventListener('online',()=>{retry=0;probe()});
window.addEventListener('focus',probe);
const reNow=()=>{retry=0;toast('Reconectando…');reconnect(0)};
$('reBtn').onclick=reNow;$('reBtn2').onclick=reNow;

function handle(m){
  if(m.type==='auth'){
    if(m.ok){role=m.role;wsUp=true;retry=0;bye=false;authed=true;capped=!!m.capped;$('lerr').textContent='';
      if(typeof m.server_time==='number')clkOff=m.server_time*1000-Date.now();
      try{localStorage.setItem('bluetti_pw',PW)}catch(_){}
      skyAuth();alertAsk();
      $('login').classList.add('hide');$('app').classList.remove('hide');}
    else{authFailed=true;try{localStorage.removeItem('bluetti_pw')}catch(_){}
      $('app').classList.add('hide');$('login').classList.remove('hide');$('lerr').textContent=m.error||'Contraseña incorrecta';}
  }else if(m.type==='snapshot'){S=m.state||{};lastSnap=Date.now();saveLast();render();}
  else if(m.type==='delta'){Object.assign(S,m.state||{});lastSnap=Date.now();saveLast();render();}
  else if(m.type==='bye'){bye=true;render();}
  else if(m.type==='pong'){lastAge=(typeof m.age==='number')?m.age:null;}
  else if(m.type==='sky'){skyMsg(m);}
  else if(m.type==='alert_cfg'){alertMsg(m);}
  else if(m.type==='ack'&&!m.ok){pend={};toast(m.error||'Comando rechazado');
    if(ws&&ws.readyState===1)ws.send(JSON.stringify({type:'get_snapshot'}));render();}
}

/* valor mostrado: lo que acabo de pedir (3 s) o lo que dice la Bluetti */
const KEYS={ac:['ac_output_on','set_ac_output'],dc:['dc_output_on','set_dc_output'],eco:['eco_mode_on','set_eco_mode'],
  grid:['grid_enhancement_on','set_grid_enhancement'],pl:['power_lifting_on','set_power_lifting'],hc:['dc_high_current_on','set_dc_high_current']};
function val(k,real){const p=pend[k];return(p&&Date.now()-p.ts<3000)?p.v:real}
const isOn=k=>!!val(k,S[KEYS[k][0]]);
const srcNow=()=>val('src',S.dc_source);
const modeNow=()=>val('cm',S.charge_mode);
const ampsNow=()=>val('amps',S.grid_max_current);

function canCtl(){return role==='controller'&&wsUp&&!!S.link_ok}
function send(name,args){ws.send(JSON.stringify({type:'cmd',id:++cmdId,name:name,args:args||{}}))}

function toggle(k){
  if(!canCtl())return;
  const want=!isOn(k);
  if(want&&k==='pl'&&!confirm('¿Activar Power Lifting (hasta 2700 W)?'))return;
  if(want&&k==='hc'&&!confirm('¿Activar la alta corriente de entrada del panel solar?\n\nConfirma que tu panel y cableado la soportan.'))return;
  pend[k]={v:want,ts:Date.now()};
  send(KEYS[k][1],{on:want});render();
}
function setSrc(v){if(!canCtl()||srcNow()===v)return;pend.src={v:v,ts:Date.now()};send('set_dc_source',{source:v});render()}
function setMode(v){if(!canCtl()||modeNow()===v)return;pend.cm={v:v,ts:Date.now()};send('set_charge_mode',{mode:v});render()}
function stepAmps(d){
  if(!canCtl())return;
  const n=Math.max(1,Math.min(15,(ampsNow()||1)+d));
  pend.amps={v:n,ts:Date.now()};render();
  clearTimeout(stepT);stepT=setTimeout(()=>{if(canCtl())send('set_grid_max_current',{amps:n})},600);
}

function alertsList(s,link){
  const out=[];
  if(!link)return out;
  const now=Date.now()+clkOff;
  if(s.prot_dc_active){
    const end=s.prot_dc_end_ts?s.prot_dc_end_ts*1000:0;
    const rem=end?Math.max(0,Math.ceil((end-now)/1000)):60;
    out.push(['info','Protección de alta corriente DC '+Math.floor(rem/60)+':'+String(rem%60).padStart(2,'0')]);
  }
  if(s.prot_ac_reduced_pending)out.push(['warn','Alta corriente AC reducida',role==='controller'?'Entendido':null]);
  const gv=s.ac_input_volts||0;
  if(gv>0&&(gv<101||(s.grid_low_voltage_alarm&&gv<106)))out.push(['bad','⚠ Alarma bajo voltaje de red '+gv.toFixed(1)+' V']);
  else if(gv>0&&gv<106)out.push(['warn','⚠ Voltaje de red inestable '+gv.toFixed(1)+' V']);
  const t=s.batt_temp;
  if(typeof t==='number'){if(t>=46)out.push(['bad','🌡 Temperatura crítica '+t.toFixed(1)+'°C']);else if(t>=43)out.push(['warn','🌡 Temperatura alta '+t.toFixed(1)+'°C']);}
  const cl=(a)=>(a||[]).join(', ');
  if(s.cell_alert==='low'||s.cell_alert==='high')out.push(['bad','Celda crítica '+(s.cell_alert==='low'?'baja':'alta')+': '+cl(s.cell_alert_cells)]);
  else if(s.cell_warn==='low'||s.cell_warn==='high')out.push(['warn','Celda '+(s.cell_warn==='low'?'baja':'alta')+': '+cl(s.cell_warn_cells)]);
  if(s.soc_calibration==='suspect')out.push(['warn','Calibración del % sospechosa']);
  return out;
}

function renderDetail(s,link){
  const det=$('det'),dm=$('detM'),mopen=!$('mdl').classList.contains('hide');if(!det.parentNode.open&&!mopen&&det._built)return;
  const f=(v,d,u)=>typeof v==='number'&&isFinite(v)?v.toFixed(d)+(u||''):'--';
  const R=(a,b)=>'<span>'+a+'</span><span>'+b+'</span>';
  const ZN={plateau:'meseta',top:'codo superior',bottom:'codo inferior'};
  let h='';
  h+='<h3>EQUILIBRIO DE CELDAS</h3>';
  if(link&&Array.isArray(s.cell_mv)){
    const lim=s.cell_balance_limits,pk=s.cell_peak_mv,tr=s.cell_trough_mv,z=s.cell_zone||'plateau';
    h+='<div class="kv">'+R('Diferencia máx-mín',(s.cell_delta_mv==null?'--':s.cell_delta_mv)+' mV')
      +R('Voltaje medio celda',typeof s.cell_avg_mv==='number'?(s.cell_avg_mv/1000).toFixed(3)+' V':'--')
      +R('Equilibrio',GRADE[s.cell_grade]?GRADE[s.cell_grade][0]:'--')
      +R('Zona de la curva',ZN[z]||z)
      +(lim?R('Límites (mV)','óptimo ≤'+lim[0]+' · bueno ≤'+lim[1]+' · aceptable ≤'+lim[2]+' · regular ≤'+lim[3]+' · malo >'+lim[3]):'')
      +(pk&&tr?R('Sesión','más alta '+(pk/1000).toFixed(3)+' V · más baja '+(tr/1000).toFixed(3)+' V'):'')+'</div>';
    h+='<p>'+(z==='plateau'?'En la meseta un Δ alto indica dispersión (resistencia, conexión o temperatura), no falta de equilibrio de carga.':'En el codo de la curva el Δ refleja desequilibrio real.')+'</p>';
    if(!s.cell_balance_reliable)h+='<p>Fiabilidad BAJA: potencia muy alta distorsiona el Δ.</p>';
  }else h+='<p>Sin lectura de celdas todavía.</p>';
  h+='<h3>ALERTAS DE CELDAS</h3>';
  const bad=(s.cell_alert_cells||[]).join(', '),wb=(s.cell_warn_cells||[]).join(', ');
  let at='Sin alertas (aviso ≤2.90 / ≥3.60 V · crítica ≤2.60 / ≥3.65 V)';
  if(s.cell_alert==='low')at='Celda CRÍTICA baja (≤2.60 V): '+bad;else if(s.cell_alert==='high')at='Celda CRÍTICA alta (≥3.65 V): '+bad;
  else if(s.cell_warn==='low')at='Aviso: celda baja (≤2.90 V): '+wb;else if(s.cell_warn==='high')at='Aviso: celda alta (≥3.60 V): '+wb;
  h+='<p>'+at+'</p><div class="kv">'+R('Calibración del %',s.soc_calibration==='ok'?'Coherente':(s.soc_calibration==='suspect'?'SOSPECHOSA':'Sin datos'))+'</div>';
  h+='<h3>TEMPERATURA</h3>';
  h+=(typeof s.batt_temp==='number')?'<div class="kv">'+R('Promedio',s.batt_temp.toFixed(1)+' °C')+R('Mín / máx',f(s.batt_temp_min,0)+' / '+f(s.batt_temp_max,0)+' °C')+'</div>':'<p>Sin lectura todavía.</p>';
  h+='<h3>CAPACIDAD Y SALUD (medida por energía)</h3>';
  const n=s.cap_health_samples||0;
  if(n){
    const rg=s.cap_range_wh;
    h+='<div class="kv">'+R('Capacidad',f(s.cap_est_wh,0)+' de 1024 Wh · salud '+f(s.cap_health_pct,0)+'%')
      +R('Muestras',n+(rg?' (rango '+rg[0]+'–'+rg[1]+' Wh)':''))+'</div>';
  }else h+='<p>Aún sin medir. Se mide al cargar o descargar 15 puntos de % seguidos (entre 8% y 95%) con más de 60 W de media.</p>';
  h+='<div class="kv">';
  if(typeof s.cap_window_progress==='number')h+=R('Ventana en curso',s.cap_window_progress+'/15 puntos de %');
  if(s.cap_counter_dis_wh||s.cap_counter_chg_wh)h+=R('Contraste contador','descarga '+f(s.cap_counter_dis_wh,0)+' Wh (n='+(s.cap_counter_dis_n||0)+') · carga '+f(s.cap_counter_chg_wh,0)+' Wh (n='+(s.cap_counter_chg_n||0)+')');
  h+=R('Nominal',f(s.cap_rated_ah,1,' Ah')+' × 51.2 V = 1024 Wh')+'</div><p>Precisión esperada ≈ ±10 %; mejora con cada muestra.</p>';
  h+='<h3>CICLOS DE CARGA (contadores del BMS)</h3><div class="kv">'+R('Ciclos equivalentes',f(s.cycles_eq,0))
    +R('Energía cargada',f(s.energy_chg_kwh,2,' kWh'))+R('Energía descargada',f(s.energy_dis_kwh,2,' kWh'))+'</div>';
  if(det._h!==h){det._h=h;det.innerHTML=h;dm.innerHTML=h;}
  det._built=true;
}

/* ---- vigilante: cada 5 s. Si en 25 s no llega NADA (ni la respuesta al ping) la conexion esta muerta: se rehace ---- */
setInterval(()=>{
  if(!authed||!PW||authFailed)return;
  if(!ws||ws.readyState!==1)return;                       /* los intentos colgados los resuelve el tiempo maximo de connect() */
  if(Date.now()-lastMsg>25000){reconnect(0);return}
  if(++pingN%2===0)try{ws.send('{"type":"ping"}')}catch(_){reconnect(0)}
},5000);

/* ---- condiciones y estadisticas del panel (la misma informacion que la ventana ☀ de la app) ---- */
const SK={mode:'day',day:null,d:null,xr:null,hx:null,geo:null,req:0,last:0,csvN:0,csvReq:-1,err:'',full:false};
try{const m=localStorage.getItem('bluetti_skymode');if(m==='day'||m==='week'||m==='month')SK.mode=m}catch(_){}
SK.vis={};try{SK.vis=JSON.parse(localStorage.getItem('bluetti_skvis')||'{}')||{}}catch(_){}
const SKY_BG='#080b11',SKY_GRID='#1e293b',MONO='ui-monospace,Consolas,"Courier New",monospace',DOW=['lun','mar','mié','jue','vie','sáb','dom'];
const skKey=i=>{const k=SK.meta.keys[Math.round(i)]||'na';return SK.meta.alias[k]||k};
const stCol=i=>(SK.meta.states[skKey(i)]||['','','#94a3b8'])[2];
const stName=i=>(SK.meta.states[skKey(i)]||['?'])[0];
const fmtE=wh=>wh>=1000?(wh/1000).toFixed(2)+' kWh':Math.round(wh)+' Wh';
const fmtHM=s=>String(Math.floor(s/3600)).padStart(2,'0')+':'+String(Math.floor(s%3600/60)).padStart(2,'0');
const dmy=iso=>iso.slice(8,10)+'/'+iso.slice(5,7)+'/'+iso.slice(0,4);
const hm2=m=>Math.floor(m/60)+'h'+String(m%60).padStart(2,'0')+'m';
const hexA=(c,a)=>{const n=parseInt(c.slice(1),16);return 'rgba('+(n>>16&255)+','+(n>>8&255)+','+(n&255)+','+a+')'};
function batSt(p){const M=SK.meta,n=p.length>10?p[10]:null;
  if(p.length>7&&p[7]>=M.full&&(n===null||n>-M.neutral_w))return 'full';
  if(n===null)return null;return n>M.neutral_w?'chg':(n<-M.neutral_w?'dis':'bal')}
function skMerge(st){const o={};for(const k in st){const a=SK.meta.alias[k]||k;o[a]=(o[a]||0)+st[k]}return o}
function skDominant(st){const m=skMerge(st);let best=null;for(const k in m){if(SK.meta.quiet.indexOf(k)<0&&(best===null||m[k]>m[best]))best=k}return best}
function skStatesText(mins){const m=skMerge(mins);return Object.keys(m).filter(k=>k!=='na'&&k!=='night'&&m[k]>0).sort((a,b)=>m[b]-m[a])
  .map(k=>SK.meta.states[k][0]+' '+hm2(m[k])).join(' · ')}

/* ---------- peticiones ---------- */
let skyT=null;
function skySend(o){if(!ws||ws.readyState!==1)return;SK.last=Date.now();try{ws.send(JSON.stringify(Object.assign({type:'get_sky'},o)))}catch(_){}}
function skyAsk(extra){clearTimeout(skyT);const wait=Math.max(0,650-(Date.now()-SK.last));
  skyT=setTimeout(()=>{const o=Object.assign({req:++SK.req,mode:SK.mode==='day'?'day':'bars',day:SK.day,n:SK.mode==='month'?30:7},extra||{});skySend(o)},wait)}
function skyMsg(m){
  if(m.req===SK.csvReq){SK.csvReq=-1;skyCsv(m);return}
  if(m.req!==SK.req)return;
  if(m.error){SK.err=m.error;SK.d=null;skyDraw();return}
  SK.err='';SK.meta=m.meta;SK.d=m;if(m.mode==='day')SK.day=m.day;
  skyFillDays();skyDraw();
}
function skyFillDays(){const sel=$('skDay'),d=SK.d;if(!d||d.mode!=='day')return;
  const sig=d.days.join(',')+'|'+d.day;if(sel._sig===sig)return;sel._sig=sig;sel.textContent='';
  for(const x of d.days){const o=document.createElement('option');o.value=x;o.textContent=dmy(x);sel.appendChild(o)}sel.value=d.day}
function skyLine(){const el=$('skLine');if(typeof S.sky_txt!=='string'){el.classList.add('hide');return}
  el.classList.remove('hide');const c=/^#[0-9a-fA-F]{6}$/.test(S.sky_col)?S.sky_col:'#94a3b8';
  el.textContent='';const a=document.createElement('b');a.style.color=c;a.textContent=S.sky_txt;const b=document.createElement('span');b.textContent=' · '+(S.sky_prod||'');el.appendChild(a);el.appendChild(b)}
function skyAuth(){SK.d=null;skyAsk()}
document.querySelectorAll('#skMode button').forEach(b=>b.onclick=()=>{SK.mode=b.dataset.m;SK.xr=null;SK.hx=null;SK.d=null;
  try{localStorage.setItem('bluetti_skymode',SK.mode)}catch(_){}skyDraw();skyAsk()});
$('skDay').onchange=e=>{SK.day=e.target.value;SK.xr=null;SK.hx=null;SK.d=null;skyDraw();skyAsk()};
setInterval(()=>{if(document.hidden||!wsUp)return;if(SK.mode==='day'&&SK.d&&SK.d.day!==SK.d.today)return;skyAsk()},20000);

/* ---------- dibujo ---------- */
function skyOk(){const d=SK.d;if(!d||!SK.meta)return false;
  if(SK.mode==='day')return d.mode==='day';
  return d.mode==='bars'&&d.rows&&d.rows.length===(SK.mode==='month'?30:7)}
function skyDraw(){
  document.querySelectorAll('#skMode button').forEach(b=>b.classList.toggle('sel',b.dataset.m===SK.mode));
  $('skDay').classList.toggle('hide',SK.mode!=='day');$('skZ').classList.toggle('hide',SK.mode!=='day');
  const cv=$('skc');if(!cv||cv.offsetParent===null)return;
  const dpr=window.devicePixelRatio||1,W=cv.clientWidth;
  const Hh=SK.full?Math.max(220,window.innerHeight-(window.innerWidth>window.innerHeight?150:330)):(window.innerWidth>700?380:(W<400?290:320));
  cv.style.height=Hh+'px';
  cv.width=Math.round(W*dpr);cv.height=Math.round(Hh*dpr);
  const g=cv.getContext('2d');g.setTransform(dpr,0,0,dpr,0,0);
  if(!skyOk()){g.fillStyle=SKY_BG;g.fillRect(0,0,W,Hh);g.fillStyle='#94a3b8';g.font='13px system-ui,sans-serif';g.textAlign='center';g.textBaseline='middle';
    g.fillText(SK.err||'Cargando…',W/2,Hh/2);SK.geo=null;$('skSum').textContent='';$('skLeg').textContent='';$('skHover').textContent='';return}
  const r=SK.mode==='day'?paintDay(g,W,Hh,SK.xr,SK.hx):paintBars(g,W,Hh,SK.hx);
  SK.geo=r.geo;$('skHover').textContent=r.hover;
  skyLegend();$('skSum').textContent=skySummary().join('\n');
}
function skyLegendItems(){const M=SK.meta;
  if(SK.mode==='day')return [
    ['sun','sahara','cloud','dense','neblina','niebla','lowsun'].filter(k=>M.states[k]).map(k=>[M.states[k][2],'',M.states[k][0]]),
    ['chg','dis','bal','full'].map(k=>[M.bat_col[k],'','batería '+M.bat_name[k]]),
    [['#fbbf24','d','esperado despejado',1],['#22c55e','d','según pronóstico',1],['#00f0ff','','medido',1],
     ['#2dd4bf','d','estimado (protección DC / batería llena)',1],['#555555','d','huecos (sin datos BLE)',1],
     [M.bat_line||'#c084fc','','batería % (línea y cinta)',1],[M.bat_ac||'#22c55e','','carga por AC',1],['#ef4444','','> '+M.high_a.toFixed(0)+' A',1]]];
  return [[['#fbbf24','','potencial despejado'],['#a3e635','d','no aprovechado (supuesto, batería llena)'],['#64748b','','color de la barra = estado predominante']]]}
function skyLegend(){const box=$('skLeg');box.textContent='';
  for(const row of skyLegendItems()){const r=document.createElement('div');r.className='skrow';
    for(const [c,dsh,t,cb] of row){const s=document.createElement('span');
      if(cb){const k=document.createElement('input');k.type='checkbox';k.checked=SK.vis[t]!==false;
        k.onchange=()=>{SK.vis[t]=k.checked;try{localStorage.setItem('bluetti_skvis',JSON.stringify(SK.vis))}catch(_){}skyDraw()};s.appendChild(k)}
      const i=document.createElement('i');i.style.borderTop='2px '+(dsh?'dashed ':'solid ')+c;
      s.appendChild(i);s.appendChild(document.createTextNode(t));r.appendChild(s)}box.appendChild(r)}}

/* ---------- estimados: malla punteada + contorno discontinuo CERRADO (como en la app) ---------- */
const patC={};
function meshPat(g,col,dens){const dpr=window.devicePixelRatio||1,sc=Math.max(1,Math.round(dpr)),k=col+dens+'_'+sc;
  if(!patC[k]){const c=document.createElement('canvas');c.width=c.height=4*sc;const x=c.getContext('2d');x.fillStyle=col;
    const P=dens===50?[[0,0],[2,0],[1,1],[3,1],[0,2],[2,2],[1,3],[3,3]]:[[0,0],[2,1],[0,2],[2,3]];
    P.forEach(q=>x.fillRect(q[0]*sc,q[1]*sc,sc,sc));patC[k]=c}
  const p=g.createPattern(patC[k],'repeat');try{p.setTransform(new DOMMatrix([1/sc,0,0,1/sc,0,0]))}catch(_){}return p}
function meshPoly(g,up,dn,col){if(up.length<2||dn.length<2)return;
  g.beginPath();up.forEach((q,i)=>i?g.lineTo(q[0],q[1]):g.moveTo(q[0],q[1]));for(let i=dn.length-1;i>=0;i--)g.lineTo(dn[i][0],dn[i][1]);g.closePath();
  g.fillStyle=meshPat(g,col,25);g.fill();g.strokeStyle=col;g.lineWidth=2;g.setLineDash([3,2]);g.stroke();g.setLineDash([]);g.lineWidth=1}
function meshRect(g,x,y,w,h,col){g.fillStyle=meshPat(g,col,50);g.fillRect(x,y,w,h);
  g.strokeStyle=col;g.lineWidth=1;g.setLineDash([3,2]);g.strokeRect(x+.5,y+.5,Math.max(0,w-1),Math.max(0,h-1));g.setLineDash([])}
/* cinta de la bateria: se desvanece hacia abajo. bp = [x, ySoc, color, yPotencia] */
function ribbon(g,bp,base,amul){const M=SK.meta;if(bp.length<2)return;let i0=0;const key=b=>b[2]+(b[1]>b[3]?'1':'0');
  for(let i=1;i<=bp.length;i++){if(i===bp.length||key(bp[i])!==key(bp[i0])){
    const sg_=bp.slice(i0,Math.min(i,bp.length-1)+1);
    if(sg_.length>=2){const col=bp[i0][2];
      const ytop=Math.min(...sg_.map(b=>Math.min(base,b[1]))),ybot=Math.min(base,Math.max(...sg_.map(b=>b[1]))+M.rib_px);
      const gr=g.createLinearGradient(0,ytop,0,Math.max(ytop+1,ybot));gr.addColorStop(0,hexA(col,M.rib_a*amul));gr.addColorStop(1,hexA(col,0));
      g.fillStyle=gr;g.beginPath();sg_.forEach((b,k)=>k?g.lineTo(b[0],Math.min(base,b[1])):g.moveTo(b[0],Math.min(base,b[1])));
      for(let k=sg_.length-1;k>=0;k--)g.lineTo(sg_[k][0],Math.min(base,sg_[k][1]+M.rib_px));g.closePath();g.fill()}
    i0=i}}}

function paintDay(g,W,H,xr,hx){
  const M=SK.meta,d=SK.d,pts=d.pts,bs=d.bin,sun=d.sun,fx0=d.fx[0],fx1=d.fx[1];
  const V=t=>SK.vis[t]!==false,vEsp=V('esperado despejado'),vWx=V('según pronóstico'),vMed=V('medido'),
    vEst=V('estimado (protección DC / batería llena)'),vGap=V('huecos (sin datos BLE)'),vBat=V('batería % (línea y cinta)'),
    vAc=V('carga por AC'),vHot=V('> '+M.high_a.toFixed(0)+' A'),BL=M.bat_line||'#c084fc',BA=M.bat_ac||'#22c55e';
  const x0=xr?xr[0]:fx0,x1=xr?xr[1]:fx1,span=x1-x0;
  const narrow=W<520,L=narrow?40:50,R=narrow?36:44,T=44,B=40;
  g.fillStyle=SKY_BG;g.fillRect(0,0,W,H);g.lineWidth=1;g.setLineDash([]);
  const X=sx=>L+(Math.min(Math.max(sx,x0),x1)-x0)/span*(W-L-R);
  const vis=pts.filter(p=>p[0]+bs>=x0&&p[0]<=x1);
  const curve=(d.curve||[]).filter(c=>c[0]>=x0-60&&c[0]<=x1+60),cwx=(d.curve_wx||[]).filter(c=>c[0]>=x0-60&&c[0]<=x1+60),gaps=d.gaps||[];
  let ymax=100;for(const p of vis){ymax=Math.max(ymax,p[1],p.length>6?p[6]:p[1],p[2])}
  for(const c of curve.concat(cwx))if(c[0]>=x0&&c[0]<=x1)ymax=Math.max(ymax,c[1]);
  for(const e of gaps)for(const q of e.pw||[])if(q[0]>=x0&&q[0]<=x1)ymax=Math.max(ymax,q[1]);
  ymax=Math.ceil(ymax*1.08/100)*100;
  const Y=w=>T+(1-w/ymax)*(H-T-B),Ys=pc=>T+(1-pc/100)*(H-T-B),base=Y(0);
  const ln=(a,b,c,e)=>{g.beginPath();g.moveTo(a,b);g.lineTo(c,e);g.stroke()};
  g.font='9px '+MONO;g.textBaseline='middle';
  const ystep=ymax<=300?50:(ymax<=600?100:200);
  for(let v=0;v<=ymax;v+=ystep){g.strokeStyle=SKY_GRID;ln(L,Y(v),W-R,Y(v));g.fillStyle='#64748b';g.textAlign='right';g.fillText(v,L-5,Y(v))}
  g.fillStyle='#a78bfa';g.textAlign='left';for(const pc of [0,25,50,75,100])g.fillText(pc+'%',W-R+5,Ys(pc));
  g.font='bold 9px '+MONO;g.fillStyle='#64748b';g.textAlign='right';g.fillText('W',L-5,T-12);g.fillStyle='#a78bfa';g.textAlign='left';g.fillText('Bat.',W-R+5,T-12);
  g.font='7px '+MONO;g.fillStyle='#64748b';g.textAlign='right';g.fillText('Cielo',L-5,9);g.fillStyle='#a78bfa';g.fillText('Batería',L-5,19);
  g.font='9px '+MONO;
  const maxTk=narrow?7:14,tstep=[60,120,300,600,900,1800,3600,7200].find(s=>span/s<=maxTk)||7200;
  g.textAlign='center';
  for(let t=Math.ceil(x0/tstep)*tstep;t<=x1;t+=tstep){g.strokeStyle='#131b29';ln(X(t),T,X(t),H-B);g.fillStyle='#64748b';g.fillText(tstep<3600?fmtHM(t):Math.floor(t/3600)+'h',X(t),H-B+10)}
  /* columnas de salida / puesta del sol: al FONDO (las curvas y la bateria van por encima); su linea se dibuja mas abajo */
  const suns=[];
  if(sun){[[sun[0],'sale'],[sun[1],'se pone']].forEach(([tt,lab])=>{if(tt>=x0&&tt<=x1){suns.push(tt);
    g.fillStyle='#2a2208';g.fillRect(X(tt-M.sun_err),T,X(tt+M.sun_err)-X(tt-M.sun_err),H-B-T);
    g.fillStyle='#fbbf24';g.textAlign='center';g.fillText(lab+' '+fmtHM(tt)+(narrow?'':' ±'+Math.floor(M.sun_err/60)+'m'),Math.min(Math.max(X(tt),40),W-40),H-B+25)}})}
  for(const p of vis){const xa=X(p[0]),wd=Math.max(X(p[0]+bs),xa+1)-xa;g.fillStyle=stCol(p[4]);g.fillRect(xa,4,wd,9);
    const bk=batSt(p);if(bk){g.fillStyle=M.bat_col[bk]||'#94a3b8';g.fillRect(xa,15,wd,8)}}
  /* huecos sin datos: cielo (pronostico) y bateria (tendencia), mismo color pero punteado */
  if(vGap)for(const e of gaps){
    for(const r of e.sky||[]){if(r[1]>=x0&&r[0]<=x1&&M.states[r[2]]){const xa=X(r[0]);meshRect(g,xa,4,Math.max(X(r[1]),xa+1)-xa,9,M.states[r[2]][2])}}
    if(e.bat&&M.bat_col[e.bat]){const xa=X(e.s[0]);meshRect(g,xa,15,Math.max(X(e.s[1]),xa+1)-xa,8,M.bat_col[e.bat])}}
  const segs=[],bmap={};let cur=[];
  for(const p of vis){const mid=p[0]+bs/2;bmap[mid]=batSt(p);if(cur.length&&mid-cur[cur.length-1][0]>3*bs){segs.push(cur);cur=[]}
    cur.push([mid,p[1],p.length>6?p[6]:p[1],p.length>7?p[7]:-1,p.length>8?p[8]:0,p.length>9?p[9]:0,p.length>11?p[11]:0])}
  if(cur.length)segs.push(cur);
  /* huecos: potencia estimada (esperado escalado por lo medido antes y despues) */
  if(vGap)for(const e of gaps){const pw=e.pw||[];if(pw.length>=2)meshPoly(g,pw.map(q=>[X(q[0]),Y(q[1])]),[[X(pw[0][0]),base],[X(pw[pw.length-1][0]),base]],'#555555')}
  for(const sg of segs){
    if(vMed&&sg.length>=2){g.fillStyle='#0b3b46';g.beginPath();sg.forEach((q,i)=>i?g.lineTo(X(q[0]),Y(q[1])):g.moveTo(X(q[0]),Y(q[1])));g.lineTo(X(sg[sg.length-1][0]),base);g.lineTo(X(sg[0][0]),base);g.closePath();g.fill()}
    if(vEst){let run=[];
      for(const q of sg.concat([null])){
        if(q&&(q[6]||q[2]>q[1]+10)){run.push(q);continue}
        if(run.length){const h=bs/2,r0=run[0],rl=run[run.length-1];   /* incluso 1 sola barra: media barra a cada lado */
          meshPoly(g,[[X(r0[0]-h),Y(r0[2])]].concat(run.map(r=>[X(r[0]),Y(r[2])]),[[X(rl[0]+h),Y(rl[2])]]),
                     [[X(r0[0]-h),Y(r0[1])]].concat(run.map(r=>[X(r[0]),Y(r[1])]),[[X(rl[0]+h),Y(rl[1])]]),'#2dd4bf')}
        run=[]}}
    if(vMed){if(sg.length>=2){g.lineWidth=2;for(let i=1;i<sg.length;i++){const hot=sg[i][5]>M.high_a||sg[i-1][5]>M.high_a;if(hot&&!vHot)continue;
        g.strokeStyle=hot?'#ef4444':'#00f0ff';ln(X(sg[i-1][0]),Y(sg[i-1][1]),X(sg[i][0]),Y(sg[i][1]))}g.lineWidth=1}
      else{g.fillStyle='#00f0ff';g.beginPath();g.arc(X(sg[0][0]),Y(sg[0][1]),2,0,7);g.fill()}}
  }
  /* curvas de referencia y lineas del sol: por encima de lo medido */
  if(vEsp&&curve.length>=2){g.strokeStyle='#fbbf24';g.setLineDash([4,3]);g.beginPath();curve.forEach((c,i)=>i?g.lineTo(X(c[0]),Y(c[1])):g.moveTo(X(c[0]),Y(c[1])));g.stroke();g.setLineDash([])}
  if(vWx&&cwx.length>=2){g.strokeStyle='#22c55e';g.setLineDash([4,3]);g.beginPath();cwx.forEach((c,i)=>i?g.lineTo(X(c[0]),Y(c[1])):g.moveTo(X(c[0]),Y(c[1])));g.stroke();g.setLineDash([])}
  g.strokeStyle='#fbbf24';for(const tt of suns){g.setLineDash([2,4]);ln(X(tt),T,X(tt),H-B);g.setLineDash([])}
  /* LINEA Y CINTA DE BATERIA: por encima de todo (verde solo mientras carga por AC) */
  const bmarks={};
  if(vBat){
    for(const sg of segs){
      const bp=sg.filter(q=>q[3]>=0).map(q=>[X(q[0]),Ys(q[3]),q[4]>M.ac_w?BA:BL,Y(q[1])]);ribbon(g,bp,base,1);
      g.lineWidth=1.5;for(let i=1;i<sg.length;i++){if(sg[i][3]>=0&&sg[i-1][3]>=0){const ac=sg[i][4]>M.ac_w;if(ac&&!vAc)continue;g.strokeStyle=ac?BA:BL;ln(X(sg[i-1][0]),Ys(sg[i-1][3]),X(sg[i][0]),Ys(sg[i][3]))}}g.lineWidth=1;
      for(let hh=Math.ceil(x0/3600);hh<=Math.floor(x1/3600);hh++){const tt=hh*3600;if(sg[0][0]-600<=tt&&tt<=sg[sg.length-1][0]+600){
        const cd=sg.filter(q=>q[3]>=0&&Math.abs(q[0]-tt)<=600);if(cd.length){const q=cd.reduce((a,b)=>Math.abs(b[0]-tt)<Math.abs(a[0]-tt)?b:a);bmarks[hh]=[q[0],q[3],q[4]>M.ac_w?BA:BL]}}}}
    /* bateria estimada en huecos y extremos: recta discontinua + cinta tenue */
    if(vGap)for(const e of gaps){const sc=e.soc||[];if(sc.length>=2){
      ribbon(g,sc.map(q=>[X(q[0]),Ys(q[1]),BL,base]),base,M.gap_rib_a||0.6);
      g.strokeStyle=BL;g.lineWidth=1.5;g.setLineDash([3,2]);g.beginPath();sc.forEach((q,i)=>i?g.lineTo(X(q[0]),Ys(q[1])):g.moveTo(X(q[0]),Ys(q[1])));g.stroke();g.setLineDash([]);g.lineWidth=1}}
    for(const k in bmarks){const mk=bmarks[k];g.fillStyle=SKY_BG;g.strokeStyle=mk[2];g.lineWidth=1.5;g.beginPath();g.arc(X(mk[0]),Ys(mk[1]),3,0,7);g.fill();g.stroke();g.lineWidth=1}}
  const tag=(px,py,text,col,up)=>{g.font='bold 9px '+MONO;const yy=Math.min(Math.max(py+(up?-14:14),T+8),H-B-8),w=g.measureText(text).width;
    const xx=Math.min(Math.max(px,L+w/2+4),W-R-w/2-4);g.fillStyle=SKY_BG;g.strokeStyle=col;g.fillRect(xx-w/2-3,yy-7,w+6,14);g.strokeRect(xx-w/2-3,yy-7,w+6,14);
    g.fillStyle=col;g.textAlign='center';g.fillText(text,xx,yy)};
  /* valores de bateria a las horas fijas de la app (de toda la jornada: no cambian con el zoom) */
  if(vBat){const allmk=[];
    for(const hh of (M.bat_tag_hours||[10,16])){const c_=pts.filter(q=>q.length>7&&q[7]>=0&&Math.abs(q[0]+bs/2-hh*3600)<=600);
      if(c_.length){const q=c_.reduce((a,b)=>Math.abs(b[0]+bs/2-hh*3600)<Math.abs(a[0]+bs/2-hh*3600)?b:a);allmk.push([q[0]+bs/2,q[7],q.length>8&&q[8]>M.ac_w?BA:BL])}}
    if(allmk.length){const mn=Math.min(...allmk.map(q=>q[1]));for(const q of allmk)if(q[0]>=x0&&q[0]<=x1)tag(X(q[0]),Ys(q[1]),Math.round(q[1])+'%',q[2],q[1]>=mn)}}
  const win=pts.filter(q=>M.peak[0]*3600<=q[0]+bs/2&&q[0]+bs/2<=M.peak[1]*3600);
  if(win.length>=2){const pmx=win.reduce((a,b)=>b[1]>a[1]?b:a),mnl=win.filter(q=>q[1]>(M.peak_min_w||0)&&!(q.length>7&&q[7]>=M.full)),
      pmn=mnl.length?mnl.reduce((a,b)=>b[1]<a[1]?b:a):null;
    if(pmn&&pmx[1]-pmn[1]>=20)[[pmx,true,'▲'],[pmn,false,'▼']].forEach(([pk,up,sym])=>{const t=pk[0]+bs/2;if(t>=x0&&t<=x1){const qx=X(t),qy=Y(pk[1]);
      g.fillStyle='#00f0ff';g.strokeStyle='#e0f2fe';g.beginPath();g.moveTo(qx,qy-5);g.lineTo(qx+5,qy);g.lineTo(qx,qy+5);g.lineTo(qx-5,qy);g.closePath();g.fill();g.stroke();
      tag(qx,qy,sym+' '+Math.round(pk[1])+' W · '+fmtHM(pk[0]),'#00f0ff',up)}})}
  if(span<=7200){g.fillStyle='#00f0ff';g.strokeStyle='#080b11';for(const p of vis){g.beginPath();g.arc(X(p[0]+bs/2),Y(p[1]),2,0,7);g.fill();g.stroke()}}
  let hover=span<(fx1-fx0)*0.995?'Dos dedos / rueda: zoom · arrastra: mover · doble toque: restablecer':'Toca o arrastra sobre la gráfica para ver el detalle · pellizca para acercar.';
  if(hx!==null&&vis.length){const p=vis.reduce((a,b)=>Math.abs(X(b[0]+bs/2)-hx)<Math.abs(X(a[0]+bs/2)-hx)?b:a),px=X(p[0]+bs/2);
    g.strokeStyle='#e2e8f0';ln(px,T,px,H-B);g.fillStyle='#00f0ff';g.beginPath();g.arc(px,Y(p[1]),4,0,7);g.fill();g.stroke();
    const b=batSt(p),pc=p[2]>0?' ('+Math.round(p[1]/p[2]*100)+'%)':'',
      est=p.length>6&&(p[6]>p[1]+10||(p.length>11&&p[11]))?' · estimado ≈ '+Math.round(p[6])+' W':'',
      bat=(p.length>7&&p[7]>=0?' · batería '+Math.round(p[7])+'%':'')+(b?' ('+M.bat_name[b]+(p.length>10?', '+(p[10]>=0?'+':'')+Math.round(p[10])+' W)':')'):''),
      ac=p.length>8&&p[8]>M.ac_w?' · carga AC '+Math.round(p[8])+' W':'',
      amp=p.length>9&&p[9]>0?' · '+p[9].toFixed(1)+' A'+(p[9]>M.high_a?' ⚠':''):'';
    hover=fmtHM(p[0])+' · '+stName(p[4])+' · '+Math.round(p[1])+' W'+pc+' de '+Math.round(p[2])+' W esperados'+est+bat+ac+amp+' · cable ≈ '+Math.round(p[3])+' W · acumulado '+fmtE(p[5])}
  return {hover,geo:{mode:'day',xr:[x0,x1],fx0,fx1,L,R,W}};
}

function paintBars(g,W,H,hx){
  const M=SK.meta,d=SK.d,rows=d.rows,n=rows.length,narrow=W<520,L=narrow?36:50,R=14,T=40,B=34;
  g.fillStyle=SKY_BG;g.fillRect(0,0,W,H);g.lineWidth=1;g.setLineDash([]);
  const vals=rows.map(([,r])=>r?r.wh/1000:0),pots=rows.map(([,r])=>r?(r.pot||0)/1000:0),ners=rows.map(([,r])=>r?(r.ner||0)/1000:0);
  const ymax=Math.ceil(Math.max(1,...vals.map((v,i)=>v+ners[i]),...pots)*1.1*2)/2,slot=(W-L-R)/n,bw=slot*0.7;
  const Y=v=>T+(1-v/ymax)*(H-T-B),ln=(a,b,c,e)=>{g.beginPath();g.moveTo(a,b);g.lineTo(c,e);g.stroke()};
  g.font='9px '+MONO;g.textBaseline='middle';
  const step=ymax<=3?0.5:(ymax<=6?1:2);
  for(let v=0;v<=ymax+1e-9;v+=step){g.strokeStyle=SKY_GRID;ln(L,Y(v),W-R,Y(v));g.fillStyle='#64748b';g.textAlign='right';g.fillText(String(+v.toFixed(2)),L-5,Y(v))}
  g.font='bold 9px '+MONO;g.textAlign='right';g.fillStyle='#64748b';g.fillText('kWh',L-5,T-12);g.font='9px '+MONO;
  const bars=[];
  rows.forEach(([day,r],i)=>{const xc=L+(i+0.5)*slot,dom=r?skDominant(r.st):null,col=dom?SK.meta.states[dom][2]:'#334155',v=vals[i];
    if(v>0){g.fillStyle=col;g.fillRect(xc-bw/2,Y(v),bw,Y(0)-Y(v));
      if(ners[i]>0.005){g.fillStyle='#2a3b12';g.strokeStyle='#a3e635';g.fillRect(xc-bw/2,Y(v+ners[i]),bw,Y(v)-Y(v+ners[i]));g.strokeRect(xc-bw/2,Y(v+ners[i]),bw,Y(v)-Y(v+ners[i]))}
      if(n<=7){g.fillStyle='#e2e8f0';g.textAlign='center';g.font='bold 9px '+MONO;g.fillText(v.toFixed(2),xc,Y(v+ners[i])-8);g.font='9px '+MONO}}
    if(pots[i]>0){g.strokeStyle='#fbbf24';g.lineWidth=2;ln(xc-bw/2,Y(pots[i]),xc+bw/2,Y(pots[i]));g.lineWidth=1}
    g.fillStyle='#94a3b8';g.textAlign='center';
    const wd=(new Date(day+'T12:00:00').getDay()+6)%7;
    if(n<=7)g.fillText(DOW[wd]+' '+day.slice(8)+'/'+day.slice(5,7),xc,H-B+12);else if(i%5===0||i===n-1)g.fillText(day.slice(8)+'/'+day.slice(5,7),xc,H-B+12);
    bars.push([xc,day,r])});
  let hover='Toca una barra para ver el detalle del día.';
  if(hx!==null){const b=bars.reduce((a,c)=>Math.abs(c[0]-hx)<Math.abs(a[0]-hx)?c:a);
    g.strokeStyle='#e2e8f0';g.strokeRect(b[0]-slot/2,T,slot,H-B-T);const r=b[2];
    if(!r)hover=dmy(b[1])+' · sin datos';
    else{const dom=skDominant(r.st);hover=dmy(b[1])+' · '+(r.wh/1000).toFixed(2)+' kWh · pico '+Math.round(r.pk)+' W'+
      (r.pot>0?' · '+Math.round(r.act/r.pot*100)+'% del potencial despejado':'')+(r.ner>1?' · no aprovechado (supuesto, batería llena) ≈ '+fmtE(r.ner):'')+
      (r.full>0?' · batería ≥'+M.full+'% '+hm2(r.full):'')+' · predomina: '+(dom?SK.meta.states[dom][0]:'—')}}
  return {hover,geo:{mode:'bars'}};
}

/* ---------- resumenes (mismos textos que la ventana de la app) ---------- */
function skySummary(){return SK.mode==='day'?sumDay():sumBars()}
function sumDay(){const d=SK.d,M=SK.meta,pts=d.pts,bs=d.bin,k=bs/3600,L=[];
  L.push('Día '+dmy(d.day)+(d.day===d.today?' · ahora: '+d.now:''));
  if(d.sun){const dl=Math.floor((d.sun[1]-d.sun[0])/60);L.push('Sol: sale '+fmtHM(d.sun[0])+' · se pone '+fmtHM(d.sun[1])+' (±'+Math.floor(M.sun_err/60)+' min) · '+Math.floor(dl/60)+' h '+String(dl%60).padStart(2,'0')+' min de día ('+M.lat.toFixed(3)+'°N, '+Math.abs(M.lon).toFixed(3)+'°O)')}
  L.push('Energía entregada al equipo: '+fmtE(d.wh_in)+' · estimada en el panel: '+fmtE(d.wh_panel));
  if(pts.length){const sum=f=>pts.reduce((a,p)=>a+f(p),0),e_w=sum(p=>p[1])*k,e_x=sum(p=>p[2])*k,e_l=sum(p=>p[3])*k,e_n=sum(p=>Math.max((p.length>6?p[6]:p[1])-p[1],0))*k;
    const pk=pts.reduce((a,b)=>b[1]>a[1]?b:a);
    L.push('Pico: '+Math.round(pk[1])+' W a las '+fmtHM(pk[0])+' · pérdida en cable (14 AWG, '+Math.round(M.cable_m)+' m): '+fmtE(e_l)+' (máx '+Math.round(Math.max(...pts.map(p=>p[3])))+' W)');
    if(e_x>0)L.push('Frente al potencial despejado del tiempo monitoreado ('+fmtE(e_x)+'): '+Math.round(e_w/e_x*100)+' %');
    const socs=pts.filter(p=>p.length>7&&p[7]>=0).map(p=>p[7]);
    if(socs.length){const fm=Math.floor(pts.filter(p=>p.length>7&&p[7]>=M.full).length*bs/60);
      L.push('Batería: mín '+Math.round(Math.min(...socs))+'% · máx '+Math.round(Math.max(...socs))+'% · con ≥'+M.full+'% (limita la carga) '+hm2(fm))}
    const am=Math.floor(pts.filter(p=>p.length>8&&p[8]>M.ac_w).length*bs/60),hm=Math.floor(pts.filter(p=>p.length>9&&p[9]>M.high_a).length*bs/60);
    if(am||hm)L.push('Carga por AC: '+hm2(am)+' · corriente solar > '+Math.round(M.high_a)+' A: '+hm2(hm));
    const bm={};for(const p of pts){const b=batSt(p);if(b)bm[b]=(bm[b]||0)+Math.max(1,Math.floor(bs/60))}
    if(Object.keys(bm).length)L.push('Batería: '+['chg','bal','dis','full'].filter(x=>x in bm).map(x=>M.bat_name[x]+' '+hm2(bm[x])).join(' · '));
    if(e_n>0.5)L.push('Con batería llena (supuesto: último rendimiento válido, no es una medición): no aprovechada ≈ '+fmtE(e_n)+' · producción potencial ≈ '+fmtE(d.wh_in+e_n));
    const mins={};for(const p of pts){const kx=skKey(p[4]);mins[kx]=(mins[kx]||0)+Math.max(1,Math.floor(bs/60))}
    const tx=skStatesText(mins);if(tx)L.push('Tiempo por estado: '+tx);
  }else L.push('Sin puntos registrados este día (se guardan solo con el equipo conectado y sol).');
  L.push('Referencia despejada: '+d.slots+' franjas de 15 min aprendidas (mejora sola con los días despejados; el polvo no la baja)');
  return L}
function sumBars(){const d=SK.d,rows=d.rows,n=rows.length,L=[],have=rows.filter(([,r])=>r&&r.wh>0);
  if(have.length){const tot=have.reduce((a,[,r])=>a+r.wh,0),best=have.reduce((a,b)=>b[1].wh>a[1].wh?b:a),worst=have.reduce((a,b)=>b[1].wh<a[1].wh?b:a);
    L.push('Últimos '+n+' días: total '+fmtE(tot)+' · promedio '+fmtE(tot/have.length)+'/día ('+have.length+' con datos)');
    L.push('Mejor día: '+dmy(best[0])+' ('+fmtE(best[1].wh)+') · peor: '+dmy(worst[0])+' ('+fmtE(worst[1].wh)+')');
    const s=f=>have.reduce((a,[,r])=>a+(r[f]||0),0),act=s('act'),pot=s('pot'),ner=s('ner');
    L.push('Estimada en el panel: '+fmtE(s('whp'))+' · pérdida en cable: '+fmtE(s('lw'))+(pot>0?' · '+Math.round(act/pot*100)+' % del potencial despejado':'')+(ner>1?' · no aprovechado (supuesto) ≈ '+fmtE(ner):''));
    const mins={};for(const [,r] of have)for(const k in r.st)mins[k]=(mins[k]||0)+r.st[k];
    const tx=skStatesText(mins);if(tx)L.push('Tiempo por estado: '+tx);
  }else L.push('Sin datos en los últimos '+n+' días.');
  if(SK.mode==='month'&&have.length){const wk={};
    for(const [day,r] of have){const dt=new Date(day+'T12:00:00Z'),dn=(dt.getUTCDay()+6)%7;dt.setUTCDate(dt.getUTCDate()-dn+3);
      const w1=new Date(Date.UTC(dt.getUTCFullYear(),0,4)),wn=1+Math.round(((dt-w1)/864e5-3+((w1.getUTCDay()+6)%7))/7),key='S'+String(wn).padStart(2,'0')+'/'+dt.getUTCFullYear();wk[key]=(wk[key]||0)+r.wh}
    L.push('Semanas: '+Object.keys(wk).map(k=>k+' '+fmtE(wk[k])).join(' · '))}
  if(d.months&&d.months.length)L.push('Meses: '+d.months.map(([k,wh,c])=>k.slice(5)+'/'+k.slice(0,4)+' '+fmtE(wh)+' ('+c+' d)').join(' · '));
  return L}

/* ---------- zoom / arrastre / toque ---------- */
function skyZoom(f,cx){const g=SK.geo;if(!g||g.mode!=='day')return;
  const [x0,x1]=g.xr,full=g.fx1-g.fx0,span=x1-x0,nw=Math.min(full,Math.max(600,span*f));
  if(nw>=full*0.995)SK.xr=null;
  else{const fr=cx===undefined?0.5:Math.min(1,Math.max(0,(cx-g.L)/Math.max(1,g.W-g.L-g.R))),tm=x0+fr*span;let n0=tm-fr*nw;n0=Math.max(g.fx0,Math.min(n0,g.fx1-nw));SK.xr=[n0,n0+nw]}
  skyDraw()}
(function(){const cv=$('skc'),ptrs=new Map();let pan=null,pinch=null;
  cv.addEventListener('pointerdown',e=>{try{cv.setPointerCapture(e.pointerId)}catch(_){}ptrs.set(e.pointerId,e.offsetX);
    if(ptrs.size===2&&SK.geo&&SK.geo.mode==='day'){const a=[...ptrs.values()];pinch={d:Math.abs(a[0]-a[1])||1,xr:SK.geo.xr.slice(),mid:(a[0]+a[1])/2};pan=null;SK.hx=null}
    else if(ptrs.size===1){pan={x:e.offsetX,xr:SK.xr?SK.xr.slice():null,moved:false};SK.hx=e.offsetX;skyDraw()}});
  cv.addEventListener('pointermove',e=>{const g=SK.geo;
    if(!ptrs.has(e.pointerId)){if(e.pointerType==='mouse'){SK.hx=e.offsetX;skyDraw()}return}
    ptrs.set(e.pointerId,e.offsetX);
    if(ptrs.size===2&&pinch&&g&&g.mode==='day'){const a=[...ptrs.values()],dist=Math.abs(a[0]-a[1])||1,[b0,b1]=pinch.xr,sp0=b1-b0,full=g.fx1-g.fx0,
        nw=Math.min(full,Math.max(600,sp0*pinch.d/dist)),fr=Math.min(1,Math.max(0,(pinch.mid-g.L)/Math.max(1,g.W-g.L-g.R))),tm=b0+fr*sp0;
      if(nw>=full*0.995)SK.xr=null;else{let n0=tm-fr*nw;n0=Math.max(g.fx0,Math.min(n0,g.fx1-nw));SK.xr=[n0,n0+nw]}skyDraw();return}
    if(!pan)return;
    if(Math.abs(e.offsetX-pan.x)>3)pan.moved=true;
    if(g&&g.mode==='day'&&pan.xr&&pan.moved){const sp=pan.xr[1]-pan.xr[0],sh=-(e.offsetX-pan.x)/Math.max(1,g.W-g.L-g.R)*sp,n0=Math.max(g.fx0,Math.min(pan.xr[0]+sh,g.fx1-sp));SK.xr=[n0,n0+sp];SK.hx=null}
    else SK.hx=e.offsetX;
    skyDraw()});
  const up=e=>{ptrs.delete(e.pointerId);if(ptrs.size<2)pinch=null;if(!ptrs.size){pan=null;if(e.pointerType==='mouse'&&e.type==='pointercancel'){SK.hx=null;skyDraw()}}};
  cv.addEventListener('pointerup',up);cv.addEventListener('pointercancel',up);
  cv.addEventListener('pointerleave',e=>{if(e.pointerType==='mouse'&&!ptrs.size){SK.hx=null;skyDraw()}});
  cv.addEventListener('wheel',e=>{if(SK.mode!=='day')return;e.preventDefault();skyZoom(e.deltaY<0?0.8:1.25,e.offsetX)},{passive:false});
  cv.addEventListener('dblclick',()=>{SK.xr=null;skyDraw()});
  let lastTap=0;cv.addEventListener('pointerup',e=>{if(e.pointerType!=='touch')return;const t=Date.now();if(t-lastTap<320){SK.xr=null;SK.hx=null;skyDraw()}lastTap=t});
})();
$('skZm').onclick=()=>skyZoom(1.25);$('skZp').onclick=()=>skyZoom(0.8);$('skZr').onclick=()=>{SK.xr=null;skyDraw()};
$('skFull').onclick=()=>{SK.full=!SK.full;$('cSky').classList.toggle('skfull',SK.full);document.body.style.overflow=SK.full?'hidden':'';$('skFull').textContent=SK.full?'✕':'⤢';skyDraw()};
window.addEventListener('resize',skyDraw);
window.addEventListener('orientationchange',()=>setTimeout(skyDraw,300));

/* ---------- exportar: PNG de la gráfica + resumen, o CSV con los datos (como el botón 📷 de la app) ---------- */
function wrapTxt(g,text,maxW){const out=[];let line='';for(const w of text.split(' ')){const t=line?line+' '+w:w;if(line&&g.measureText(t).width>maxW){out.push(line);line=w}else line=t}if(line)out.push(line);return out}
function skyPngLayout(g,LW,CH,draw){
  let y=14;const left=20;
  if(draw){g.fillStyle='#0e141f';g.fillRect(0,0,LW,draw)}
  if(draw){g.fillStyle='#00f0ff';g.font='bold 14px system-ui,sans-serif';g.textAlign='left';g.textBaseline='alphabetic';g.fillText('☀ CONDICIONES Y ESTADÍSTICAS DEL PANEL',left,y+14)}
  y+=22;
  const sub=SK.mode==='day'?'Día '+dmy(SK.d.day):(SK.mode==='week'?'Últimos 7 días':'Últimos 30 días');
  if(draw){g.fillStyle='#94a3b8';g.font='12px system-ui,sans-serif';g.fillText(sub+' · Bluetti Elite 100 V2',left,y+10)}
  y+=22;
  if(draw){g.save();g.translate(left-8,y);const r=SK.mode==='day'?paintDay(g,LW-24,CH,SK.xr,null):paintBars(g,LW-24,CH,null);g.restore()}
  y+=CH+10;
  g.font='11px '+MONO;
  for(const row of skyLegendItems()){let x=left+30;
    for(const [c,dsh,t] of row){const w=g.measureText(t).width;if(x+26+w>LW-10){x=left+30;y+=17}
      if(draw){g.strokeStyle=c;g.lineWidth=2;g.setLineDash(dsh?[4,3]:[]);g.beginPath();g.moveTo(x,y+7);g.lineTo(x+18,y+7);g.stroke();g.setLineDash([]);g.fillStyle='#94a3b8';g.textAlign='left';g.textBaseline='alphabetic';g.fillText(t,x+24,y+11)}
      x+=24+w+18}
    y+=19}
  y+=6;g.font='11px '+MONO;
  for(const l of skySummary())for(const t of wrapTxt(g,l,LW-2*left)){if(draw){g.fillStyle='#cbd5e1';g.textAlign='left';g.textBaseline='alphabetic';g.fillText(t,left,y+11)}y+=16}
  return y+12}
function skySaveBlob(blob,name,share){
  (async()=>{try{if(share&&navigator.canShare){const f=new File([blob],name,{type:blob.type});if(navigator.canShare({files:[f]})){await navigator.share({files:[f],title:'Condiciones del panel'});return}}}catch(e){if(e&&e.name==='AbortError')return}
    const u=URL.createObjectURL(blob),a=document.createElement('a');a.href=u;a.download=name;document.body.appendChild(a);a.click();a.remove();setTimeout(()=>URL.revokeObjectURL(u),4000);toast('Archivo guardado')})()}
function skyName(ext){return 'sky_'+(SK.mode==='day'?SK.d.day:SK.mode==='week'?'semana_'+SK.d.today:'mes_'+SK.d.today)+'.'+ext}
function skyPng(){if(!skyOk()){toast('Aún no hay datos que exportar');return}
  const LW=1000,CH=SK.mode==='day'?420:330,S2=2,tmp=document.createElement('canvas').getContext('2d'),H=Math.ceil(skyPngLayout(tmp,LW,CH,0));
  const c=document.createElement('canvas');c.width=LW*S2;c.height=H*S2;const g=c.getContext('2d');g.scale(S2,S2);skyPngLayout(g,LW,CH,H);
  c.toBlob(b=>{if(!b){toast('No se pudo crear la imagen');return}skySaveBlob(b,skyName('png'),true)},'image/png')}
const csvq=v=>{v=String(v==null?'':v);return /[;"\n]/.test(v)?'"'+v.replace(/"/g,'""')+'"':v};
function skyCsvDownload(rows){const txt='﻿'+rows.map(r=>r.map(csvq).join(';')).join('\r\n')+'\r\n';skySaveBlob(new Blob([txt],{type:'text/csv;charset=utf-8'}),skyName('csv'),false)}
function skyCsv(m){const M=SK.meta||m.meta,rows=[];
  if(m.mode==='day'){rows.push(['hora','estado','potencia_w','esperado_w','cable_w','energia_acum_wh','supuesto_bateria_llena_w','bateria_pct','entrada_ac_w','corriente_solar_max_a','bateria_neta_w','bateria_estado']);
    const gx=(p,i)=>p.length>i?p[i]:'';for(const p of m.pts){const b=batSt(p);rows.push([fmtHM(p[0]),stName(p[4]),p[1],p[2],p[3],p[5],gx(p,6),gx(p,7),gx(p,8),gx(p,9),gx(p,10),b?M.bat_name[b]:''])}}
  else{rows.push(['fecha','energia_entregada_wh','energia_panel_wh','pico_w','potencial_wh','cable_wh','no_aprovechado_supuesto_wh','min_bateria_llena','min_carga_ac','min_corriente_alta','estado_predominante']);
    for(const [d,r] of m.rows){if(!r)continue;const dom=skDominant(r.st);rows.push([dmy(d),r.wh,r.whp,r.pk,r.pot==null?'':r.pot,r.lw==null?'':r.lw,r.ner==null?'':r.ner,r.full==null?'':r.full,r.ac==null?'':r.ac,r.hi==null?'':r.hi,dom?M.states[dom][0]:''])}}
  skyCsvDownload(rows)}
function skyCsvExport(){if(!skyOk()){toast('Aún no hay datos que exportar');return}
  if(SK.mode==='day'){skyCsv(SK.d);return}
  SK.csvReq=900000000+(++SK.csvN);clearTimeout(skyT);const w=Math.max(0,650-(Date.now()-SK.last));setTimeout(()=>skySend({req:SK.csvReq,mode:'bars',all:true}),w);toast('Preparando CSV…')}
$('skPng').onclick=skyPng;$('skCsv').onclick=skyCsvExport;

/* ---- mantener la pantalla encendida (y el telefono sin bloquearse) ----
   1) API Wake Lock (Chrome/Edge/Safari reciente; exige https, es decir, el acceso por internet).
   2) Si no existe (p. ej. http en la red local): un video negro, mudo y en bucle, que el telefono cuenta como "viendo video". */
const NOSLEEP={mp4:'data:video/mp4;base64,AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAAM1bW9vdgAAAGxtdmhkAAAAAAAAAAAAAAAAAAAD6AAAB9AAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAAl90cmFrAAAAXHRraGQAAAADAAAAAAAAAAAAAAABAAAAAAAAB9AAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAEAAAABAAAAAAAAkZWR0cwAAABxlbHN0AAAAAAAAAAEAAAfQAAAAAAABAAAAAAHXbWRpYQAAACBtZGhkAAAAAAAAAAAAAAAAAABAAAAAgABVxAAAAAAALWhkbHIAAAAAAAAAAHZpZGUAAAAAAAAAAAAAAABWaWRlb0hhbmRsZXIAAAABgm1pbmYAAAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAAHGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAAAUJzdGJsAAAAwnN0c2QAAAAAAAAAAQAAALJhdmMxAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAEAAQABIAAAASAAAAAAAAAABFUxhdmM2MC4zMS4xMDIgbGlieDI2NAAAAAAAAAAAAAAAGP//AAAAOGF2Y0MBZAAK/+EAGmdkAAqscgREJsBEAAADAAQAAAMACDxIlhGAAQAHaOhDg5LIsP34+AAAAAAQcGFzcAAAAAEAAAABAAAAFGJ0cnQAAAAAAAALjAAAC4wAAAAYc3R0cwAAAAAAAAABAAAAAgAAQAAAAAAUc3RzcwAAAAAAAAABAAAAAQAAABxzdHNjAAAAAAAAAAEAAAABAAAAAgAAAAEAAAAcc3RzegAAAAAAAAAAAAAAAgAAAtUAAAAOAAAAFHN0Y28AAAAAAAAAAQAAA2UAAABidWR0YQAAAFptZXRhAAAAAAAAACFoZGxyAAAAAAAAAABtZGlyYXBwbAAAAAAAAAAAAAAAAC1pbHN0AAAAJal0b28AAAAdZGF0YQAAAAEAAAAATGF2ZjYwLjE2LjEwMAAAAAhmcmVlAAAC621kYXQAAAKvBgX//6vcRem95tlIt5Ys2CDZI+7veDI2NCAtIGNvcmUgMTY0IHIzMTA4IDMxZTE5ZjkgLSBILjI2NC9NUEVHLTQgQVZDIGNvZGVjIC0gQ29weWxlZnQgMjAwMy0yMDIzIC0gaHR0cDovL3d3dy52aWRlb2xhbi5vcmcveDI2NC5odG1sIC0gb3B0aW9uczogY2FiYWM9MSByZWY9MTYgZGVibG9jaz0xOjA6MCBhbmFseXNlPTB4MzoweDEzMyBtZT11bWggc3VibWU9MTAgcHN5PTEgcHN5X3JkPTEuMDA6MC4wMCBtaXhlZF9yZWY9MSBtZV9yYW5nZT0yNCBjaHJvbWFfbWU9MSB0cmVsbGlzPTIgOHg4ZGN0PTEgY3FtPTAgZGVhZHpvbmU9MjEsMTEgZmFzdF9wc2tpcD0xIGNocm9tYV9xcF9vZmZzZXQ9LTIgdGhyZWFkcz0yIGxvb2thaGVhZF90aHJlYWRzPTEgc2xpY2VkX3RocmVhZHM9MCBucj0wIGRlY2ltYXRlPTEgaW50ZXJsYWNlZD0wIGJsdXJheV9jb21wYXQ9MCBjb25zdHJhaW5lZF9pbnRyYT0wIGJmcmFtZXM9OCBiX3B5cmFtaWQ9MiBiX2FkYXB0PTIgYl9iaWFzPTAgZGlyZWN0PTMgd2VpZ2h0Yj0xIG9wZW5fZ29wPTAgd2VpZ2h0cD0yIGtleWludD0yNTAga2V5aW50X21pbj0xIHNjZW5lY3V0PTQwIGludHJhX3JlZnJlc2g9MCByY19sb29rYWhlYWQ9NjAgcmM9Y3JmIG1idHJlZT0xIGNyZj00MC4wIHFjb21wPTAuNjAgcXBtaW49MCBxcG1heD02OSBxcHN0ZXA9NCBpcF9yYXRpbz0xLjQwIGFxPTE6MS4wMACAAAAAHmWIgQAC3/7yK/ApnYG9wUaoeHwt0dzkbiP5DrOpgQAAAApBmggtiCv/8ycU',webm:'data:video/webm;base64,GkXfo59ChoEBQveBAULygQRC84EIQoKEd2VibUKHgQJChYECGFOAZwEAAAAAAAIIEU2bdLpNu4tTq4QVSalmU6yBoU27i1OrhBZUrmtTrIHYTbuMU6uEElTDZ1OsggEeTbuMU6uEHFO7a1OsggHy7AEAAAAAAABZAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVSalmsirXsYMPQkBNgI1MYXZmNjAuMTYuMTAwV0GNTGF2ZjYwLjE2LjEwMESJiECfQAAAAAAAFlSua8GuAQAAAAAAADjXgQFzxYhghbFTyAadxJyBACK1nIN1bmSIgQCGhVZfVlA4g4EBI+ODhDuaygDgibCBQLqBQJqBAhJUw2f8c3OgY8CAZ8iaRaOHRU5DT0RFUkSHjUxhdmY2MC4xNi4xMDBzc9ZjwItjxYhghbFTyAadxGfIoUWjh0VOQ09ERVJEh5RMYXZjNjAuMzEuMTAyIGxpYnZweGfIoUWjiERVUkFUSU9ORIeTMDA6MDA6MDIuMDAwMDAwMDAwAB9DtnXO54EAo6qBAACA8AIAnQEqQABAAABHCIWFiJmEiAICAAZwPEJgCrIg9zAA/v+rUICjnYED6AAxAgABEBAAGAAZ1/QMAAEsBgCmAP7/q1CAHFO7a5G7j7OBALeK94EB8YIBn/CBAw=='};
let wl=null,wlOn=false,wlVid=null;
const wlApi=!!(navigator.wakeLock&&navigator.wakeLock.request);
function wlLabel(){const b=$('wlBtn');
  const how=!wlOn?'':(wl?' (sistema)':(wlVid?' (video)':' (sin permiso)'));
  b.textContent='🔆 Mantener pantalla encendida: '+(wlOn?'SÍ':'NO')+how;b.classList.toggle('on',wlOn);
  const n=$('wlNote');if(n){const show=wlOn&&!wlApi;   /* el aviso se oculta solo a los 10 s */
    if(show&&!n.dataset.shown){n.dataset.shown='1';n.classList.remove('hide');setTimeout(()=>n.classList.add('hide'),10000)}
    else if(!show){n.classList.add('hide');delete n.dataset.shown}}}
function wlVideo(on){
  if(!on){if(wlVid){try{wlVid.pause();wlVid.remove()}catch(_){}wlVid=null}return true}
  if(wlVid&&!wlVid.paused)return true;
  const iOS=/iPhone|iPad|iPod/.test(navigator.userAgent);
  const order=iOS?['mp4','webm']:['webm','mp4'];       /* webm primero (Chrome/Android/Firefox); en iPhone, mp4 */
  return (async()=>{for(const k of order){
    const v=document.createElement('video');v.setAttribute('playsinline','');v.setAttribute('muted','');v.muted=true;v.loop=true;
    v.style.cssText='position:fixed;right:0;bottom:0;width:240px;height:180px;opacity:.02;pointer-events:none;z-index:-1';v.src=NOSLEEP[k];document.body.appendChild(v);
    try{await v.play();wlVid=v;return true}catch(_){v.remove()}}
    return false})()}
async function wlAcquire(){
  let ok=false;
  if(navigator.wakeLock&&navigator.wakeLock.request){
    try{if(!wl){wl=await navigator.wakeLock.request('screen');wl.addEventListener('release',()=>{wl=null;wlLabel();if(wlOn&&document.visibilityState==='visible')setTimeout(wlAcquire,500)})}ok=true}catch(_){}}
  ok=(await wlVideo(true))||ok;     /* el video se usa tambien con Wake Lock: ayuda si el sistema lo suelta */
  wlLabel();return ok}
async function wlSet(on){
  wlOn=on;try{localStorage.setItem('bluetti_wl',on?'1':'0')}catch(_){}
  if(on){const ok=await wlAcquire();if(!ok){wlOn=false;toast('Este navegador no permite mantener la pantalla encendida')}else toast('La pantalla no se apagará mientras esta página esté abierta y visible')}
  else{if(wl){try{await wl.release()}catch(_){}wl=null}wlVideo(false)}
  wlLabel();
}
$('wlBtn').onclick=()=>wlSet(!wlOn);wlLabel();
{let w0=null;try{w0=localStorage.getItem('bluetti_wl')}catch(_){}
 if(w0==='1'){wlOn=true;wlLabel();document.addEventListener('pointerdown',function f(){document.removeEventListener('pointerdown',f);if(wlOn)wlAcquire()},{once:true})}}
document.addEventListener('visibilitychange',()=>{if(wlOn&&document.visibilityState==='visible')wlAcquire()});
/* cada 20 s: si el sistema solto el bloqueo (Chromebook, ahorro de energia...) o el video se paro, se vuelve a pedir */
setInterval(()=>{if(!wlOn||document.visibilityState!=='visible')return;
  if((wlApi&&!wl)||!wlVid||wlVid.paused)wlAcquire()},20000);

/* ---- triangulo de avisos + notificaciones del sistema ---- */
let forceUntil=0,triSev='ok',ntOn=false,ntInit=false,ntActive={};const ntSeen={};
const ntOk=()=>('Notification' in window)&&window.isSecureContext;
function triSet(sev){triSev=sev;const t=$('tri');t.classList.toggle('hide',sev==='ok');if(sev==='ok')t.classList.remove('tdim');t.style.color=sev==='critical'?'#ef4444':'#f59e0b'}
setInterval(()=>{if(triSev!=='ok')$('tri').classList.toggle('tdim')},500);
$('tri').onclick=()=>{forceUntil=Date.now()<forceUntil?0:Date.now()+10000;render()};
function ntLabel(){const b=$('ntBtn');let t=ntOn?'SÍ':'NO';
  if(!ntOk())t='no disponible (requiere https)';else if(Notification.permission==='denied')t='bloqueado en el navegador';
  b.textContent='🔔 Avisos del sistema: '+t;b.classList.toggle('on',ntOn)}
async function sysNotify(title,body,key,crit){
  const opt={body:body,tag:key,icon:'/icon-192.png',badge:'/icon-192.png',requireInteraction:!!crit,vibrate:[200,100,200]};
  try{if('serviceWorker' in navigator){const reg=await navigator.serviceWorker.getRegistration();if(reg&&reg.showNotification){await reg.showNotification(title,opt);return}}}catch(_){}
  try{new Notification(title,opt)}catch(_){}}
async function ntSet(on){
  if(on){
    if(!ntOk()){toast('Los avisos del sistema requieren https (acceso por internet / relay)');return}
    let p=Notification.permission;
    if(p==='default'){try{p=await Notification.requestPermission()}catch(_){}}
    if(p!=='granted'){ntOn=false;ntLabel();toast('Permiso de notificaciones denegado');return}
    ntOn=true;sysNotify('Bluetti','Avisos del sistema activados','bluetti-test',false);
  }else ntOn=false;
  try{localStorage.setItem('bluetti_nt',ntOn?'1':'0')}catch(_){}
  ntLabel();}
$('ntBtn').onclick=()=>ntSet(!ntOn);
try{ntOn=localStorage.getItem('bluetti_nt')==='1'&&ntOk()&&Notification.permission==='granted'}catch(_){}
ntLabel();
function notifyAlerts(s,link,fromApp){
  if(!link||!fromApp)return;
  const now=Date.now(),cur={};
  for(const a of s.ui_alerts)cur[String(a[0]).replace(/\d+(?:[.:,]\d+)*/g,'#').trim()]=a;
  if(!ntInit){ntInit=true;for(const k in cur)ntSeen[k]=now;ntActive=cur;return}   /* lo que ya estaba activo al abrir no se notifica */
  if(ntOn&&ntOk()&&Notification.permission==='granted'&&(document.visibilityState!=='visible'||!document.hasFocus())){
    for(const k in cur){
      if(ntActive[k]||now-(ntSeen[k]||0)<600000)continue;      /* misma alerta: no antes de 10 min */
      ntSeen[k]=now;const a=cur[k],red=['#ef4444','#f87171','#ff3366'].indexOf(String(a[1]).toLowerCase())>=0;
      sysNotify('Bluetti: '+String(a[0]).replace(/^\s*⚠\s*/,''),typeof s.soc==='number'?'Batería '+s.soc+'%':'',k,red);}
  }
  ntActive=cur;}

function render(){
  const s=S,link=!!s.link_ok&&wsUp;
  document.body.classList.toggle('off',!link);
  $('dot').className='dot '+(link?'ok':'bad');
  $('conn').textContent=!wsUp?(bye?'Servidor apagado':'Sin conexión con el servidor'):(s.link_ok?'Conectado a la Bluetti':(s.srv_ble_paused?'Bluetooth desconectado en la PC':(s.srv_stale?'La PC dejó de recibir datos de la Bluetti':'Servidor sin enlace con la Bluetti')));
  const st=$('stale'),quiet=wsUp&&authed&&Date.now()-lastMsg>12000,lost=!wsUp&&!!PW&&!authFailed&&!bye;
  st.classList.toggle('hide',!(quiet||lost||(wsUp&&s.srv_stale)));
  $('staleTx').textContent=lost?('Sin conexión con el servidor. Reintentando solo (intento '+retry+')… '+(lastSnap?'Últimos datos de hace '+ago(Date.now()-lastSnap)+'. ':'')):quiet?'Sin respuesta del servidor desde hace '+Math.round((Date.now()-lastMsg)/1000)+' s. ':('Hace '+(lastAge==null?'más de '+Math.round(STALE_S_JS):lastAge)+' s que la PC no recibe datos de la Bluetti (revisa el Bluetooth de la PC). ');

  const t=s.batt_temp;
  if(typeof t==='number'){$('temp').textContent='🌡 '+t.toFixed(1)+'°C';$('temp').style.color=t>=46?'var(--bad)':(t>=43?'var(--warn)':'var(--mut)');}
  else $('temp').textContent='';

  const est=!link&&typeof s.off_soc==='number';
  const soc=est?s.off_soc:s.soc,inW=(s.dc_input_watts||0)+(s.ac_input_watts||0),outW=(s.ac_output_watts||0)+(s.dc_output_watts||0),net=inW-outW;
  if(typeof soc==='number'){
    const col=soc>50?'var(--ok)':(soc>20?'var(--warn)':'var(--bad)');
    $('soc').textContent=(est&&s.off_mode==='discharging'?'▼ ':'')+soc+'%'+(est&&s.off_mode==='charging'?' ▲':'');$('soc').style.color=col;$('bar').style.width=soc+'%';$('bar').style.background=col;
  }else{$('soc').textContent='--%';$('soc').style.color='var(--mut)';$('bar').style.width='0';}

  if(!link){$('state').textContent=wsUp?'Desconectado':'Sin datos en vivo';$('state').style.color='var(--mut)';
    const r=s.off_rem;
    $('time').textContent=est&&typeof r==='number'?'⏱ '+(s.off_mode==='charging'?'Carga completa en':'Autonomía restante')+' (est.): '+hm(r):'⏱ --h --m';}
  else{
    $('state').textContent=net>5?'Cargando (+'+net+'W)':(net<-5?'Descargando ('+net+'W)':'Flujo neto: 0 W');
    $('state').style.color=net>5?'var(--cy)':(net<-5?'var(--bad)':'var(--mut)');
    const rt=s.runtime_minutes;
    if(typeof rt==='number'&&rt>0&&(net>5||net<-5))$('time').textContent='⏱ '+(net>5?'Carga completa en ':'Autonomía restante: ')+hm(rt);
    else $('time').textContent=(net>5||net<-5)?'⏱ Calculando autonomía…':'⏱ En reposo';
  }

  /* salud y energia */
  $('hCap').textContent=typeof s.cap_est_wh==='number'?s.cap_est_wh+' / 1024 Wh':'midiendo…';
  $('hHealth').textContent=typeof s.cap_health_pct==='number'?s.cap_health_pct+' %':'midiendo…';
  $('hCyc').textContent=num(s.cycles_eq,0);
  $('hChg').textContent=typeof s.energy_chg_kwh==='number'?s.energy_chg_kwh.toFixed(2)+' kWh':'--';
  $('hDis').textContent=typeof s.energy_dis_kwh==='number'?s.energy_dis_kwh.toFixed(2)+' kWh':'--';
  $('hCal').textContent=s.soc_calibration==='ok'?'Coherente':(s.soc_calibration==='suspect'?'Sospechosa':'Sin datos');

  const sv=s.dc_input_volts||0,sa=s.dc_input_current||0;
  $('solSub').textContent=sv>0?num(sv,1)+'V • '+num(sa,1)+'A':'--V • --A';
  const gv=s.ac_input_volts||0,hzI=s.ac_input_hz||0;
  $('acSub').textContent=gv>0?num(gv)+'V • '+num(s.ac_input_current||0,1)+'A'+(hzI>0?' • '+num(hzI)+'Hz':''):'Sin red';
  const ov=s.ac_output_volts||0,hzO=s.ac_output_hz||0;
  $('aoSub').textContent=ov>0?num(ov)+'V'+(hzO>0?' • '+num(hzO)+'Hz':''):'120V • --Hz';
  let sm='Fuente DC: --';
  if(link){if(s.dc_source==='pv')sm='PV • Alta I: '+(s.dc_high_current_on?'ON':'OFF');else if(s.dc_source==='other')sm='Fuente: Otros (batería)';}
  $('solMode').textContent=sm;
  skyLine();alertRows();
  const w=k=>link?(s[k]||0)+' W':'-- W';
  $('solW').textContent=w('dc_input_watts');$('acW').textContent=w('ac_input_watts');
  $('aoW').textContent=w('ac_output_watts');$('doW').textContent=w('dc_output_watts');
  $('inT').textContent=link?inW+' W':'-- W';$('outT').textContent=link?outW+' W':'-- W';

  /* alertas */
  /* alertas: las que calcula la app (con TUS umbrales); si el servidor es antiguo, las calculo aqui */
  const fromApp=Array.isArray(s.ui_alerts),box=$('alerts');
  const forced=Date.now()<forceUntil&&fromApp&&Array.isArray(s.ui_all);   /* triángulo tocado: también los avisos vencidos que dejan triángulo */
  const al=!link?[]:(fromApp?(forced?s.ui_all:s.ui_alerts):alertsList(s,link));
  triSet(!link?'ok':(typeof s.ui_sev==='string'?s.ui_sev:(al.some(a=>a[0]==='bad')?'critical':(al.length?'warning':'ok'))));
  notifyAlerts(s,link,fromApp);
  const sig=JSON.stringify([al,role,wsUp]);
  $('cAl').classList.toggle('hide',!al.length);
  if(sig!==box._sig){
    box._sig=sig;box.textContent='';
    for(const a of al){
      const d=document.createElement('div');
      const sp=document.createElement('span');
      let kind=null,txt;
      if(fromApp){
        txt=a[0];kind=a[2]||null;const c=/^#[0-9a-fA-F]{6}$/.test(a[1])?a[1]:'#fbbf24';
        d.className='al u';d.style.color=c;d.style.borderColor=c+'66';
      }else{d.className='al '+a[0];txt=a[1];kind=a[2]?'other':null;}
      sp.textContent=txt;d.appendChild(sp);
      if(kind&&role==='controller'){
        const b=document.createElement('button');b.textContent='Cerrar';
        b.onclick=()=>{if(wsUp){send('dismiss_alert',{kind:(kind==='dc'||kind==='temp')?kind:'other'});
          if(!fromApp){send('ack_ac_reduced_notice');S.prot_ac_reduced_pending=false;}toast('Aviso cerrado');}};
        d.appendChild(b);
      }
      box.appendChild(d);
    }
  }

  /* banco de baterias */
  const cells=s.cell_mv;
  $('bankV').textContent=typeof s.pack_volts==='number'&&s.pack_volts>0?s.pack_volts.toFixed(1)+' V':'--V';
  const cg=$('cells');
  if(cg.children.length!==16){cg.textContent='';for(let i=0;i<16;i++){const d=document.createElement('div');d.className='cell';cg.appendChild(d);}}
  const crit=s.cell_alert_cells||[],warn=s.cell_warn_cells||[],delta=s.cell_delta_mv||0;
  for(let i=0;i<16;i++){
    const el=cg.children[i],mv=(Array.isArray(cells)&&link)?cells[i]:null;
    el.textContent='('+(i+1)+')'+(typeof mv==='number'?(mv/1000).toFixed(3):'-.---');
    let cls='cell';
    if(typeof mv==='number'){
      if(crit.indexOf(i+1)>=0)cls+=' cr';else if(warn.indexOf(i+1)>=0)cls+=' wn';
      else if(delta>=3&&i+1===s.cell_min_idx)cls+=' lo';else if(delta>=3&&i+1===s.cell_max_idx)cls+=' hi';
    }
    el.className=cls;
  }
  if(Array.isArray(cells)&&link&&typeof s.cell_min_mv==='number'){
    $('cSum').textContent='Min('+s.cell_min_idx+') '+(s.cell_min_mv/1000).toFixed(3)+'V • Max('+s.cell_max_idx+') '+(s.cell_max_mv/1000).toFixed(3)+'V • Dif '+delta+'mV';
  }else $('cSum').textContent='Min -- • Max -- • Dif --';
  let pt='Sin datos',pc='#94a3b8';
  if(link&&Array.isArray(cells)){
    if(s.cell_alert==='low'||s.cell_alert==='high'){pt='Crítica '+(s.cell_alert==='low'?'baja':'alta');pc='#ff3366';}
    else if(s.cell_warn==='low'||s.cell_warn==='high'){pt='Celda '+(s.cell_warn==='low'?'baja':'alta');pc='#fb923c';}
    else if(GRADE[s.cell_grade]){
      pt=GRADE[s.cell_grade][0];pc=GRADE[s.cell_grade][1];
      if((s.cell_grade==='fair'||s.cell_grade==='bad')&&!s.cell_balance_reliable){pt='Δ incierto';pc='#fbbf24';}
      else if(s.cell_zone==='plateau'&&s.cell_grade==='fair')pt='Dispersión leve';
      else if(s.cell_zone==='plateau'&&s.cell_grade==='bad')pt='Dispersión alta';
    }
  }
  const pill=$('cPill');pill.textContent=pt;pill.style.color=pc;pill.style.borderColor=pc;
  const tm=s.batt_temp;
  $('tSum').textContent=(link&&typeof tm==='number')?'Temp. celdas '+tm.toFixed(1)+'°C'+((typeof s.batt_temp_min==='number'&&typeof s.batt_temp_max==='number')?' (mín '+s.batt_temp_min+' • máx '+s.batt_temp_max+')':''):'Temp. celdas --';

  /* controles */
  const ctl=role==='controller',can=canCtl();
  $('ctlNote').textContent=ctl?(link?'':'sin enlace con la Bluetti'):(capped?'solo lectura (internet)':'solo lectura');
  const names={ac:'AC',dc:'DC',eco:'ECO',grid:'Refuerzo red',pl:'Power Lifting (2700 W)',hc:'Alta corriente'};
  document.querySelectorAll('[data-t]').forEach(b=>{
    const k=b.dataset.t,on=isOn(k);
    b.textContent=names[k]+(k==='ac'||k==='dc'?(on?' ● ON':' ○ OFF'):(on?' ●':''));
    b.classList.toggle('on',on);
    b.disabled=!can||(k==='hc'&&srcNow()!=='pv');
  });
  document.querySelectorAll('[data-src]').forEach(b=>{b.classList.toggle('sel',srcNow()===b.dataset.src);b.disabled=!can});
  document.querySelectorAll('[data-cm]').forEach(b=>{b.classList.toggle('sel',modeNow()===b.dataset.cm);b.disabled=!can});
  const showStep=modeNow()==='custom';
  $('stepBox').classList.toggle('hide',!showStep);
  const am=ampsNow();$('aVal').textContent=typeof am==='number'?am+' A máx':'-- A';
  $('aMinus').disabled=!can||!(am>1);$('aPlus').disabled=!can||!(am<15);
  const bb=$('bleBtn'),paused=!!s.srv_ble_paused;
  bb.textContent=paused?'▶ Conectar BLE de la PC':'⏏ Desconectar BLE de la PC';
  bb.classList.toggle('sel',paused);bb.disabled=!(ctl&&wsUp);
  bb.classList.toggle('hide',!ctl);
  renderDetail(s,link);
  $('foot').textContent=ctl?'':(capped?'Solo lectura: el control desde internet está desactivado en el servidor':'Modo solo lectura');
}

document.querySelectorAll('[data-t]').forEach(b=>b.onclick=()=>toggle(b.dataset.t));
document.querySelectorAll('[data-src]').forEach(b=>b.onclick=()=>setSrc(b.dataset.src));
document.querySelectorAll('[data-cm]').forEach(b=>b.onclick=()=>setMode(b.dataset.cm));
$('bleBtn').onclick=()=>{
  if(role!=='controller'||!wsUp)return;
  if(!S.srv_ble_paused){if(!confirm('¿Desconectar el Bluetooth de la PC?\n\nDejarás de ver datos hasta que lo vuelvas a conectar (desde aquí o en la app).'))return;send('ble_release');}
  else send('ble_connect');
  toast(S.srv_ble_paused?'Conectando…':'Desconectando…');};
/* ---- ajustes de alertas: los mismos switches y ventanas ⚙ de la app ---- */
let ALC=null,alKey=null,alReq=0,alBusy=false;
function alertAsk(){if(ws&&ws.readyState===1&&wsUp)try{ws.send(JSON.stringify({type:'get_alert_cfg',req:++alReq}))}catch(_){}}
function alertMsg(m){
  if(!m.ok){const e=$('amMsg');if(!$('amdl').classList.contains('hide')){e.style.color='#f87171';e.textContent='⚠ '+(m.error||'Error');}else if(alBusy)toast(m.error||'Error');alBusy=false;return}
  ALC={schema:m.schema,values:m.values};alBusy=false;
  if(m.saved){const e=$('amMsg');e.style.color='#10b981';e.textContent='✔ Guardado';toast('Alertas guardadas')}
  if(alKey&&!$('amdl').classList.contains('hide')&&!m.saved)alertForm();
  render();
}
function alertRows(){
  const en=S.alert_en,rows=document.querySelectorAll('.alrow');
  rows.forEach(r=>{
    const k=r.dataset.k,ok=!!en&&typeof en[k]==='boolean'&&wsUp;
    r.classList.toggle('on',ok);if(!ok)return;
    const cb=r.querySelector('input');cb.checked=en[k];cb.disabled=role!=='controller';
    if(!r._b){r._b=1;
      cb.onchange=()=>{if(role!=='controller'||!wsUp){cb.checked=!cb.checked;return}
        cb.disabled=true;alBusy=true;ws.send(JSON.stringify({type:'set_alert_cfg',req:++alReq,key:k,values:{enabled:cb.checked}}));};
      r.querySelector('a').onclick=()=>openAlert(k);}
  });
}
function openAlert(k){alKey=k;$('amMsg').textContent='';$('amdl').classList.remove('hide');document.body.style.overflow='hidden';
  if(ALC)alertForm();else{$('amB').textContent='Cargando…';alertAsk()}}
function closeAlert(){$('amdl').classList.add('hide');document.body.style.overflow=SK.full?'hidden':'';alKey=null}
function alertForm(){
  const sc=ALC.schema[alKey],cur=ALC.values[alKey],ro=role!=='controller',B=$('amB');
  $('amT').textContent='⚙ '+sc.title.toUpperCase();B.textContent='';B._f=[];
  const el=(t,c,x)=>{const e=document.createElement(t);if(c)e.className=c;if(x!=null)e.textContent=x;return e};
  if(ro)B.appendChild(el('div','anote','Solo lectura: con la contraseña de control puedes cambiar estos valores.'));
  for(const f of sc.fields){
    if(f.kind==='group'){B.appendChild(el('div','agrp',f.label));continue}
    if(f.kind==='note'){B.appendChild(el('div','anote',f.label));continue}
    const row=el('div','afld'),lb=el('label',null,f.label.replace(/^ +/,'')),inp=ro?null:null;
    let c;
    if(f.kind==='bool'){c=document.createElement('input');c.type='checkbox';c.checked=!!cur[f.key];c.style.width='22px';c.style.height='22px'}
    else if(f.kind==='choice'){c=document.createElement('select');for(const ch of f.choices){const o=el('option',null,ch[1]);o.value=ch[0];c.appendChild(o)}c.value=cur[f.key]}
    else{c=document.createElement('input');c.type='number';c.inputMode='decimal';c.step=f.kind==='int'?'1':'any';c.value=cur[f.key];if(f.unit)lb.textContent+=' ('+f.unit+')'}
    c.disabled=ro;row.appendChild(lb);row.appendChild(c);B.appendChild(row);B._f.push([f,c]);
    if(/^ +/.test(f.label))row.style.paddingLeft='14px';
  }
  const bt=$('amBt');bt.textContent='';
  if(!ro){const sv=el('button','pri','Guardar'),rs=el('button',null,'Restaurar valores');
    sv.onclick=()=>alertSave();rs.onclick=()=>{
      for(const [f,c] of B._f){const d=f.default;if(f.kind==='bool')c.checked=!!d;else c.value=d}
      $('amMsg').style.color='#fbbf24';$('amMsg').textContent='Valores por defecto cargados: pulsa Guardar para aplicarlos.'};
    bt.appendChild(sv);bt.appendChild(rs);}
}
function alertSave(){
  const B=$('amB'),sc=ALC.schema[alKey],cur=ALC.values[alKey],vals={},msg=$('amMsg');
  msg.style.color='#f87171';
  for(const [f,c] of B._f){
    if(f.kind==='bool')vals[f.key]=c.checked;
    else if(f.kind==='choice')vals[f.key]=c.value;
    else{const n=parseFloat(String(c.value).replace(',','.'));
      if(!isFinite(n)){msg.textContent="⚠ '"+f.label.trim()+"': escribe un número.";return}
      if((f.min!=null&&n<f.min)||(f.max!=null&&n>f.max)){msg.textContent="⚠ '"+f.label.trim()+"' debe estar entre "+f.min+' y '+f.max+' '+(f.unit||'')+'.';return}
      vals[f.key]=f.kind==='int'?Math.round(n):n;}
  }
  for(const f of sc.fields){if(f.confirm_off&&cur[f.key]&&!vals[f.key]&&!confirm(f.confirm_off+'\n\n¿Desactivarla de todos modos?'))return}
  if(!wsUp)return;
  msg.style.color='#94a3b8';msg.textContent='Guardando…';alBusy=true;
  ws.send(JSON.stringify({type:'set_alert_cfg',req:++alReq,key:alKey,values:vals}));
}
$('amx').onclick=closeAlert;
$('amdl').addEventListener('click',e=>{if(e.target===$('amdl'))closeAlert()});
const openDet=()=>{$('mdl').classList.remove('hide');document.body.style.overflow='hidden';$('det')._built=false;renderDetail(S,!!S.link_ok&&wsUp)};
const closeDet=()=>{$('mdl').classList.add('hide');document.body.style.overflow=SK.full?'hidden':''};
document.querySelector('#cDet details').addEventListener('toggle',()=>{$('det')._built=false;renderDetail(S,!!S.link_ok&&wsUp)});
$('infoBtn').onclick=openDet;$('cPill').onclick=openDet;$('mx').onclick=closeDet;
$('mdl').addEventListener('click',e=>{if(e.target===$('mdl'))closeDet()});
document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeDet();closeAlert()}});
$('aMinus').onclick=()=>stepAmps(-1);$('aPlus').onclick=()=>stepAmps(1);
function login(){PW=$('pw').value;if(!PW)return;$('lerr').textContent='';connect();}
$('go').onclick=login;
$('pw').addEventListener('keydown',e=>{if(e.key==='Enter')login()});
$('logout').onclick=()=>{try{localStorage.removeItem('bluetti_pw');localStorage.removeItem('bluetti_last')}catch(_){}lastSnap=0;PW='';role=null;S={};authed=false;
  if(ws){try{ws.close()}catch(_){}}$('app').classList.add('hide');$('login').classList.remove('hide');$('pw').value='';};
setInterval(render,1000);
let saved=null;
try{saved=localStorage.getItem('bluetti_pw')}catch(_){}
/* QR con contraseña: http://ip:puerto/#p=... (se borra de la barra de direcciones al leerla) */
if(location.hash.indexOf('#p=')===0){
  try{saved=decodeURIComponent(location.hash.slice(3))}catch(_){}
  try{history.replaceState(null,'',location.pathname)}catch(_){}
}
if(saved){PW=saved;
  try{const l=JSON.parse(localStorage.getItem('bluetti_last')||'null');if(l&&l.s&&typeof l.t==='number'){S=l.s;lastSnap=l.t}}catch(_){}
  $('login').classList.add('hide');$('app').classList.remove('hide');render();      /* se ve la app al instante, aunque no haya red */
  connect();}
</script></body></html>
"""
