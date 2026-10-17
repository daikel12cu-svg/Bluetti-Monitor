"""
weather_provider.py — Proveedores meteorológicos pluggables para SkyTracker.
Uso:
    from weather_provider import WeatherManager, PROVIDERS
    mgr = WeatherManager(config)
    mgr.fetch()
    wx = mgr.get_hourly(datetime.now())
"""
import json
import os
import threading
import time
import urllib.request
import urllib.error
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List, Callable


# ---------- Config paths ----------
def _wx_config_path() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "BluettiElite100Widget")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "weather_config.json")


def _wx_cache_path() -> str:
    # Cache en %TEMP% para que se limpie solo
    base = os.environ.get("TEMP") or os.environ.get("TMP") or os.path.expanduser("~")
    folder = os.path.join(base, "BluettiElite100Widget")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "wx_cache.json")


# ---------- Base Provider ----------
class BaseProvider(ABC):
    name: str = "base"
    display_name: str = "Base"
    requires_key: bool = True
    free_tier: str = ""

    def __init__(self, api_key: str, lat: float, lon: float):
        self.api_key = api_key
        self.lat = lat
        self.lon = lon
        self.timeout = 8  # seconds
        self.last_error = ""   # motivo del ultimo fallo (para mostrarlo en la ventana de clima)
        self.last_hourly: Dict[str, Dict[str, Any]] = {}   # datos del ultimo fetch OK (para unificar proveedores)

    @abstractmethod
    def _build_url(self) -> str:
        pass

    @abstractmethod
    def _parse_response(self, data: Dict) -> Dict[str, Dict[str, Any]]:
        """Retorna dict { 'YYYY-MM-DD_HH': {cloud_cover, aod_550, rh, temp, precip_prob, temp_drop_1h} }"""
        pass

    def fetch(self) -> bool:
        self.last_error = ""
        self.last_hourly = {}
        if self.requires_key and not self.api_key:
            self.last_error = "sin API key"
            return False
        try:
            url = self._build_url()
            req = urllib.request.Request(url, headers={"User-Agent": "BluettiSkyTracker/1.0"})
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.load(resp)
            hourly = self._parse_response(data)
            self.last_hourly = hourly
            self._save_cache(hourly)
            return True
        except urllib.error.HTTPError as e:   # antes de URLError (es su subclase)
            self.last_error = "clave rechazada (HTTP %s)" % e.code if e.code in (401, 403) else "HTTP %s" % e.code
            return False
        except urllib.error.URLError:
            self.last_error = "sin conexión"
            return False
        except Exception as e:   # JSON invalido, timeout, respuesta inesperada...: que el manager pruebe el siguiente
            self.last_error = type(e).__name__
            return False

    def _save_cache(self, hourly: Dict[str, Dict[str, Any]]):
        try:
            cache = self._load_cache()
            cache.update(hourly)
            # Podar > max_cache_hours (se hace en manager)
            with open(_wx_cache_path(), "w", encoding="utf-8") as f:
                json.dump(cache, f, separators=(",", ":"))
        except Exception:
            pass

    def _load_cache(self) -> Dict[str, Dict[str, Any]]:
        try:
            with open(_wx_cache_path(), "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def get_hourly(self, dt: datetime) -> Optional[Dict[str, Any]]:
        key = dt.strftime("%Y-%m-%d_%H")
        cache = self._load_cache()
        return cache.get(key)


# ---------- OpenWeather (endpoint gratuito 2.5 /forecast) ----------
class OpenWeatherProvider(BaseProvider):
    name = "openweather"
    display_name = "OpenWeather (forecast 2.5)"
    requires_key = True
    free_tier = "1000 calls/día gratis"

    def _build_url(self) -> str:
        # OneCall 3.0 exige una suscripcion aparte (da 401 con una clave normal);
        # /data/2.5/forecast funciona con la clave gratuita (datos cada 3 h).
        return (
            f"https://api.openweathermap.org/data/2.5/forecast?"
            f"lat={self.lat}&lon={self.lon}&units=metric&appid={self.api_key}"
        )

    def _parse_response(self, data: Dict) -> Dict[str, Dict[str, Any]]:
        out = {}
        for h in data.get("list", []):
            cloud = h.get("clouds", {}).get("all", 0)
            main = h.get("main", {})
            rh = main.get("humidity", 0)
            temp = main.get("temp", 0)
            pop = h.get("pop", 0) * 100  # 0-1 -> %
            # OpenWeather no da AOD directo; estimamos de cloud_cover + humidity
            # AOD proxy: si cloud bajo y rh alto -> polvo/humedad
            aod = 0.0
            if cloud < 20 and rh > 70:
                aod = 0.15 + (rh - 70) / 30 * 0.2  # 0.15-0.35
            # cada bloque de 3 h se reparte en 3 horas
            for off in range(3):
                dt = datetime.fromtimestamp(h["dt"] + off * 3600)
                key = dt.strftime("%Y-%m-%d_%H")
                out[key] = {
                    "cloud_cover": cloud,
                    "aod_550": aod,
                    "rh": rh,
                    "temp": temp,
                    "precip_prob": pop,
                    "temp_drop_1h": 0.0,  # se calcula abajo
                }
        # Calcular temp_drop_1h
        keys = sorted(out.keys())
        for i, k in enumerate(keys[:-1]):
            out[k]["temp_drop_1h"] = max(0.0, out[k]["temp"] - out[keys[i + 1]]["temp"])
        return out


# ---------- WeatherAPI ----------
class WeatherAPIProvider(BaseProvider):
    name = "weatherapi"
    display_name = "WeatherAPI.com"
    requires_key = True
    free_tier = "1M calls/mes gratis"

    def _build_url(self) -> str:
        return (
            f"http://api.weatherapi.com/v1/forecast.json?"
            f"key={self.api_key}&q={self.lat},{self.lon}&days=3&aqi=no&alerts=no"
        )

    def _parse_response(self, data: Dict) -> Dict[str, Dict[str, Any]]:
        out = {}
        for day in data.get("forecast", {}).get("forecastday", []):
            for h in day.get("hour", []):
                dt = datetime.fromtimestamp(h["time_epoch"])
                key = dt.strftime("%Y-%m-%d_%H")
                cloud = h.get("cloud", 0)
                rh = h.get("humidity", 0)
                temp = h.get("temp_c", 0)
                pop = h.get("chance_of_rain", 0)
                # WeatherAPI no da AOD
                aod = 0.15 if cloud < 20 and rh > 70 else 0.0
                out[key] = {
                    "cloud_cover": cloud,
                    "aod_550": aod,
                    "rh": rh,
                    "temp": temp,
                    "precip_prob": pop,
                    "temp_drop_1h": 0.0,
                }
        keys = sorted(out.keys())
        for i, k in enumerate(keys):
            if i + 1 < len(keys):
                out[k]["temp_drop_1h"] = max(0.0, out[k]["temp"] - out[keys[i + 1]]["temp"])
        return out


# ---------- MeteoBlue ----------
class MeteoBlueProvider(BaseProvider):
    name = "meteoblue"
    display_name = "MeteoBlue (solar especializado)"
    requires_key = True
    free_tier = "Trial limitado"

    def _build_url(self) -> str:
        # MeteoBlue usa endpoint diferente, simplificado aquí
        return (
            f"https://my.meteoblue.com/packages/basic-1h_basic-day?"
            f"lat={self.lat}&lon={self.lon}&apikey={self.api_key}&format=json"
        )

    def _parse_response(self, data: Dict) -> Dict[str, Dict[str, Any]]:
        out = {}
        # Estructura simplificada; MeteoBlue real tiene más campos
        for h in data.get("data_1h", {}).get("time", []):
            # parseo real sería más complejo
            pass
        return out


# ---------- Visual Crossing ----------
class VisualCrossingProvider(BaseProvider):
    name = "visualcrossing"
    display_name = "Visual Crossing"
    requires_key = True
    free_tier = "1000 calls/día gratis"

    def _build_url(self) -> str:
        end = (datetime.now() + timedelta(days=2)).strftime("%Y-%m-%d")
        return (
            f"https://weather.visualcrossing.com/VisualCrossingWebServices/rest/services/timeline/"
            f"{self.lat},{self.lon}/{datetime.now().strftime('%Y-%m-%d')}/{end}"
            f"?unitGroup=metric&key={self.api_key}&include=hours"
        )

    def _parse_response(self, data: Dict) -> Dict[str, Dict[str, Any]]:
        out = {}
        for day in data.get("days", []):
            for h in day.get("hours", []):
                # la hora llega sin fecha ("13:00:00"): se combina con la fecha del dia
                dt = datetime.strptime(day["datetime"] + " " + h["datetime"], "%Y-%m-%d %H:%M:%S")
                key = dt.strftime("%Y-%m-%d_%H")
                cloud = h.get("cloudcover", 0)
                rh = h.get("humidity", 0)
                temp = h.get("temp", 0)
                pop = h.get("precipprob", 0)
                aod = 0.15 if cloud < 20 and rh > 70 else 0.0
                out[key] = {
                    "cloud_cover": cloud,
                    "aod_550": aod,
                    "rh": rh,
                    "temp": temp,
                    "precip_prob": pop,
                    "temp_drop_1h": 0.0,
                }
        keys = sorted(out.keys())
        for i, k in enumerate(keys):
            if i + 1 < len(keys):
                out[k]["temp_drop_1h"] = max(0.0, out[k]["temp"] - out[keys[i + 1]]["temp"])
        return out


# ---------- Registry ----------
PROVIDERS = {
    "openweather": OpenWeatherProvider,
    "weatherapi": WeatherAPIProvider,
    "meteoblue": MeteoBlueProvider,
    "visualcrossing": VisualCrossingProvider,
}


def provider_choices() -> List[Dict[str, str]]:
    """Para UI: [{value, label, free_tier, requires_key}] — solo los 3 configurados"""
    allowed = {"openweather", "weatherapi", "visualcrossing"}
    return [
        {
            "value": k,
            "label": v.display_name,
            "free_tier": v.free_tier,
            "requires_key": v.requires_key,
        }
        for k, v in PROVIDERS.items()
        if k in allowed
    ]


# ---------- Manager ----------
# Mejores 3 para Cuba (Caribe): OpenWeather, WeatherAPI, VisualCrossing
DEFAULT_CONFIG = {
    "enabled": False,
    "providers_order": ["openweather", "weatherapi", "visualcrossing"],
    "api_keys": {},
    "fetch_interval_min": 15,
    "max_cache_hours": 12,
    "lat": 22.097425,
    "lon": -78.625940,
    "show_subtype_badge": False,  # badge opcional en barra de estado
    "merge_providers": False,     # True = unifica los datos de todos los proveedores que respondan
}

# Peso de cada proveedor al unificar. OpenWeather (forecast 2.5) solo da datos cada 3 h,
# asi que pesa la mitad que los que dan datos horarios reales.
MERGE_WEIGHTS = {"openweather": 0.5, "weatherapi": 1.0, "visualcrossing": 1.0}
MERGE_FIELDS = ("cloud_cover", "aod_550", "rh", "temp", "precip_prob")


class WeatherManager:
    def __init__(self, config: Optional[Dict] = None, on_fetch_success: Optional[Callable[[], None]] = None):
        self.config = {**DEFAULT_CONFIG, **(config or {})}
        self._on_fetch_success = on_fetch_success   # aviso a la app tras cada descarga correcta
        self._load_config()
        self._last_fetch = 0
        self._attempt_ts = 0.0     # ultimo intento (con exito o sin el)
        self.last_used = None      # nombre del proveedor que dio los ultimos datos
        self.last_errors: Dict[str, str] = {}   # fallos del ultimo intento, por proveedor
        self.last_merged: List[str] = []        # proveedores que se unificaron en el ultimo intento (modo unificar)
        self._current_idx = 0
        self._providers: List[BaseProvider] = []
        # Caché en memoria (protegida por lock) para no leer disco en feed/dibujado
        self._mem_cache: Dict[str, Dict[str, Any]] = {}
        self._cache_dirty = False
        # Modo CLIENTE: el pronostico lo manda el servidor (el cliente no descarga nada, no necesita internet ni claves)
        # para que ambas copias usen exactamente los mismos datos y los registros que luego se sincronizan no choquen.
        self._remote = False
        self._remote_ver = 0           # version (hora de descarga) del ultimo pronostico recibido
        self._remote_ts = 0.0          # cuando llego (hora de esta PC)
        self._remote_info: Dict[str, Any] = {}   # texto del servidor: proveedor usado, estado...
        self._loaded_ver = 0           # marca de tiempo del archivo de cache cargado
        self._init_providers()
        self._load_mem_cache()  # Cargar caché a memoria al inicio

        # Hilo de fondo para fetch periódico (no bloquea UI)
        self._fetch_thread: Optional[threading.Thread] = None
        self._stop_fetch = threading.Event()
        self._fetch_lock = threading.Lock()
        self._fetch_sem = threading.Semaphore(1)  # un solo fetch a la vez
        self._backoff = 0  # backoff exponencial tras fallos
        self._start_fetch_thread()

    def _load_config(self):
        try:
            with open(_wx_config_path(), "r", encoding="utf-8") as f:
                saved = json.load(f)
            self.config = {**DEFAULT_CONFIG, **saved}
        except Exception:
            pass

    def save_config(self):
        try:
            with open(_wx_config_path(), "w", encoding="utf-8") as f:
                json.dump(self.config, f, indent=2)
        except Exception:
            pass
        # El hilo leerá la nueva config en su próxima vuelta (bajo lock)

    def _init_providers(self):
        self._providers = []
        for name in self.config["providers_order"]:
            cls = PROVIDERS.get(name)
            if cls:
                key = self.config["api_keys"].get(name, "")
                self._providers.append(cls(key, self.config["lat"], self.config["lon"]))

    # ---- ajustes desde la ventana de clima (sin tomar _fetch_lock: el hilo lo mantiene durante la red y congelaria la UI)
    def set_enabled(self, enabled: bool):
        self.config["enabled"] = bool(enabled)
        self.save_config()
        if enabled:
            threading.Thread(target=self.fetch, kwargs={"force": True}, daemon=True).start()

    def set_provider_order(self, order: List[str]):
        """El primero de la lista pasa a ser el proveedor preferido; los demas quedan de respaldo."""
        valid = [n for n in order if n in PROVIDERS]
        for n in DEFAULT_CONFIG["providers_order"]:
            if n not in valid:
                valid.append(n)
        self.config["providers_order"] = valid
        self._rebuild_providers()
        self.save_config()
        if self.config["enabled"]:
            threading.Thread(target=self.fetch, kwargs={"force": True}, daemon=True).start()

    def set_fetch_interval(self, minutes: int):
        self.config["fetch_interval_min"] = max(1, int(minutes))
        self.save_config()

    def merge_enabled(self) -> bool:
        return bool(self.config.get("merge_providers", False))

    def set_merge_providers(self, enabled: bool):
        """Switch de la ventana de clima: unifica los datos de todos los proveedores (opcional)."""
        self.config["merge_providers"] = bool(enabled)
        self.save_config()
        if self.config["enabled"]:
            threading.Thread(target=self.fetch, kwargs={"force": True}, daemon=True).start()

    @staticmethod
    def _merge_hourly(results: Dict[str, Dict[str, Dict[str, Any]]]) -> Dict[str, Dict[str, Any]]:
        """Promedio ponderado por hora de los datos de varios proveedores.
        Si una hora solo la trae un proveedor, se usa su valor tal cual."""
        acc: Dict[str, Dict[str, List[float]]] = {}
        for name, hourly in results.items():
            w = MERGE_WEIGHTS.get(name, 1.0)
            for key, row in hourly.items():
                slot = acc.setdefault(key, {f: [0.0, 0.0] for f in MERGE_FIELDS})
                for f in MERGE_FIELDS:
                    x = row.get(f)
                    if x is None:
                        continue
                    slot[f][0] += x * w
                    slot[f][1] += w
        out: Dict[str, Dict[str, Any]] = {}
        for key, slot in acc.items():
            row = {f: round(s[0] / s[1], 2) if s[1] else 0.0 for f, s in slot.items()}
            row["temp_drop_1h"] = 0.0
            out[key] = row
        keys = sorted(out.keys())
        for i, k in enumerate(keys[:-1]):
            out[k]["temp_drop_1h"] = max(0.0, out[k]["temp"] - out[keys[i + 1]]["temp"])
        return out

    def set_api_key(self, provider: str, key: str):
        keys = dict(self.config.get("api_keys") or {})
        keys[provider] = (key or "").strip()
        self.config["api_keys"] = keys
        self._rebuild_providers()
        self.save_config()

    def _rebuild_providers(self):
        """Reconstruye la lista de proveedores y la cambia de golpe (el hilo de fondo trabaja con una copia)."""
        new = []
        for name in self.config["providers_order"]:
            cls = PROVIDERS.get(name)
            if cls:
                new.append(cls(self.config["api_keys"].get(name, ""), self.config["lat"], self.config["lon"]))
        self._current_idx = 0
        self._providers = new

    def _cache_file(self) -> str:
        return _wx_cache_path()   # un solo archivo para los dos roles (servidor / cliente nunca corren a la vez)

    def _load_mem_cache(self):
        """Carga el JSON de disco a memoria (al inicio y al cambiar de modo)."""
        path = self._cache_file()
        try:
            with open(path, "r", encoding="utf-8") as f:
                self._mem_cache = json.load(f)
            self._loaded_ver = int(os.path.getmtime(path))
        except Exception:
            self._mem_cache = {}
            self._loaded_ver = 0

    def _save_mem_cache(self):
        """Escribe la caché en memoria a disco (solo si hay cambios)."""
        if not self._cache_dirty:
            return
        try:
            with open(self._cache_file(), "w", encoding="utf-8") as f:
                json.dump(self._mem_cache, f, separators=(",", ":"))
            self._cache_dirty = False
        except Exception:
            pass

    def _start_fetch_thread(self):
        if self._fetch_thread and self._fetch_thread.is_alive():
            return
        self._stop_fetch.clear()
        self._fetch_thread = threading.Thread(target=self._fetch_loop, daemon=True)
        self._fetch_thread.start()

    def _fetch_loop(self):
        """Bucle en hilo aparte: fetch cada intervalo configurado."""
        while not self._stop_fetch.is_set():
            # Leer config bajo lock (copia para no bloquear mucho)
            with self._fetch_lock:
                enabled = self.config["enabled"]
                interval = self.config["fetch_interval_min"]
                providers = list(self._providers)
            if self._remote or not enabled or not providers:
                # Cliente (datos del servidor), deshabilitado o sin proveedores: esperar y reintentar
                self._stop_fetch.wait(30)
                continue
            try:
                with self._fetch_lock:
                    now = time.time()
                    if now - self._last_fetch >= interval * 60:
                        self._fetch_one_locked(providers)
            except Exception:
                pass
            # Esperar en trozos usando Event.wait (responde a stop inmediato)
            for _ in range(interval * 60 // 5):
                if self._stop_fetch.wait(5):
                    break

    def _fetch_one_locked(self, providers: List[BaseProvider]):
        """Fetch único asumiendo que ya se tiene el lock y se cumplen condiciones."""
        if self._remote:
            return  # cliente: nunca se descarga nada
        if not self._fetch_sem.acquire(blocking=False):
            return  # ya hay un fetch en curso
        try:
            self.last_errors = {}
            self.last_used = None
            self.last_merged = []
            if self.merge_enabled():
                # Modo unificar: se consultan todos y se promedian los que respondan
                results: Dict[str, Dict[str, Dict[str, Any]]] = {}
                first_idx = None
                for i, prov in enumerate(providers):
                    ok = prov.fetch()
                    if ok and prov.last_hourly:
                        results[prov.name] = prov.last_hourly
                        if first_idx is None:
                            first_idx = i
                    else:
                        self.last_errors[prov.name] = prov.last_error or "sin datos"
                if results and not self._remote:
                    merged = self._merge_hourly(results)
                    base = dict(self._mem_cache) if self._mem_cache else providers[first_idx]._load_cache()
                    base.update(merged)
                    self._mem_cache = base
                    self._cache_dirty = True
                    self._save_mem_cache()   # el disco queda con lo unificado (cada proveedor escribio lo suyo)
                    self.last_merged = list(results.keys())
                    self.last_used = self.last_merged[0]
                    self._current_idx = first_idx
                    self._last_fetch = time.time()
                    self._backoff = 0
                    self._notify_success()
                else:
                    self._backoff = min(self._backoff * 2 + 60, 600)
            else:
                for i, prov in enumerate(providers):
                    ok = prov.fetch()
                    if not ok:
                        self.last_errors[prov.name] = prov.last_error
                    if ok and self._remote:
                        break   # se paso a modo cliente durante la descarga: se descarta
                    if ok:
                        self.last_used = prov.name
                        self._current_idx = i
                        self._last_fetch = time.time()
                        # Actualizar caché en memoria desde el proveedor
                        self._mem_cache = prov._load_cache()
                        self._cache_dirty = True
                        self._backoff = 0  # éxito: reset backoff
                        self._notify_success()
                        break
                else:
                    # Todos fallaron: backoff exponencial (máx 10 min)
                    self._backoff = min(self._backoff * 2 + 60, 600)
        finally:
            self._attempt_ts = time.time()
            self._fetch_sem.release()

    # ---- mando a distancia: un cliente cambia la configuracion de ESTE gestor (servidor) ----
    def cfg_view(self) -> Dict[str, Any]:
        c = self.config
        return {"enabled": bool(c["enabled"]), "provider": self.preferred_name() or self.current_provider_name(),
                "merge": self.merge_enabled(), "interval": int(c["fetch_interval_min"]),
                "api_keys": dict(c.get("api_keys") or {})}

    def apply_cfg(self, v: Any) -> str:
        """Aplica ajustes que llegan por red (se validan). Devuelve '' si todo bien o el motivo del rechazo."""
        if not isinstance(v, dict):
            return "peticion no valida"
        keys = v.get("api_keys")
        if keys is not None:
            if not isinstance(keys, dict):
                return "claves no validas"
            for name, k in keys.items():
                if name not in PROVIDERS or not isinstance(k, str) or len(k) > 200:
                    return "claves no validas"
        prov = v.get("provider")
        if prov is not None and prov not in PROVIDERS:
            return "proveedor desconocido"
        for f in ("enabled", "merge"):
            if f in v and not isinstance(v[f], bool):
                return "valor no valido: " + f
        iv = v.get("interval")
        if iv is not None and (isinstance(iv, bool) or not isinstance(iv, int) or not 1 <= iv <= 120):
            return "intervalo no valido"
        if keys:
            nk = dict(self.config.get("api_keys") or {})
            nk.update({n: k.strip() for n, k in keys.items()})
            self.config["api_keys"] = nk
            self._rebuild_providers()
            self.save_config()
        if prov is not None:
            self.set_provider_order([prov] + [x for x in self.config["providers_order"] if x != prov])
        if "merge" in v:
            self.set_merge_providers(v["merge"])
        if iv is not None:
            self.set_fetch_interval(iv)
        if "enabled" in v:
            self.set_enabled(v["enabled"])
        return ""

    # ---- modo cliente: el servidor manda el pronostico ----
    def remote_mode(self) -> bool:
        return self._remote

    def set_remote_mode(self, on: bool):
        """True = este equipo es CLIENTE: no descarga nada y usa lo que manda el servidor.
        False = vuelve a descargar por su cuenta (servidor / equipo local)."""
        on = bool(on)
        if on == self._remote:
            return
        self._remote = on
        self._backoff = 0
        self._remote_info = {}
        self._remote_ts = 0.0
        # La cache en memoria NO se toca: al cambiar de rol se sigue con lo ultimo que se tenia (descargado o recibido),
        # asi nada se pierde aunque el servidor nuevo no tenga internet un rato.
        if on:
            self._last_fetch = float(self._last_fetch or self._loaded_ver)
        else:
            self._last_fetch = 0
            self._attempt_ts = 0.0
            if self.config.get("enabled"):
                threading.Thread(target=self.fetch, kwargs={"force": True}, daemon=True).start()
        self._notify_success()

    def cache_version(self) -> int:
        return int(self._remote_ver if self._remote else (self._last_fetch or self._loaded_ver))

    def export_state(self) -> Dict[str, Any]:
        """Lo que el servidor manda a los clientes: pronostico horario y de donde salio."""
        names = {p.name: p.display_name for p in self._providers}
        return {"ver": self.cache_version(), "cache": dict(self._mem_cache),
                "used": names.get(self.last_used or "", ""), "merged": [names.get(n, n) for n in self.last_merged],
                "status": self.status_text() if not self._remote else (self._remote_info.get("status") or "")}

    def apply_remote(self, msg: Dict[str, Any]) -> bool:
        """El cliente recibe el pronostico del servidor. Se valida (llega por red) y reemplaza al actual."""
        if not self._remote or not isinstance(msg, dict):
            return False
        raw = msg.get("cache")
        if not isinstance(raw, dict) or len(raw) > 2000:
            return False
        clean: Dict[str, Dict[str, Any]] = {}
        for k, v in raw.items():
            if not (isinstance(k, str) and len(k) == 13 and isinstance(v, dict)):
                continue
            try:
                datetime.strptime(k, "%Y-%m-%d_%H")
            except Exception:
                continue
            clean[k] = {f: v[f] for f in ("cloud_cover", "aod_550", "rh", "temp", "precip_prob", "temp_drop_1h")
                        if isinstance(v.get(f), (int, float)) and not isinstance(v.get(f), bool)}
        if not clean:
            return False
        ver = msg.get("ver")
        ver = int(ver) if isinstance(ver, (int, float)) and not isinstance(ver, bool) else 0
        self._mem_cache = clean
        self._remote_ver = ver
        self._remote_ts = time.time()
        self._remote_info = {"used": str(msg.get("used") or "")[:60], "status": str(msg.get("status") or "")[:200]}
        self._last_fetch = float(ver) if ver else self._remote_ts
        self._attempt_ts = self._remote_ts
        self._cache_dirty = True
        self._save_mem_cache()
        self._notify_success()
        return True

    def _notify_success(self):
        """Avisa a la app (en un hilo aparte, para no bloquear ni chocar con _fetch_lock)."""
        cb = self._on_fetch_success
        if cb:
            def _run():
                try:
                    cb()
                except Exception:
                    pass
            threading.Thread(target=_run, daemon=True).start()

    def preferred_name(self) -> str:
        order = self.config.get("providers_order") or []
        return order[0] if order else ""

    def status_text(self) -> str:
        """Resultado del ultimo intento: dice si se uso el proveedor elegido o uno de respaldo (y por que)."""
        if self._remote:
            if not self._mem_cache:
                return "Cliente: esperando el pronóstico del servidor"
            when = time.strftime("%H:%M", time.localtime(self._remote_ts)) if self._remote_ts else "guardado"
            extra = (self._remote_info.get("status") or "").strip()
            return "Pronóstico del servidor (recibido %s)%s" % (when, " · " + extra if extra else "")
        names = {p.name: p.display_name for p in self._providers}
        pref, used = self.preferred_name(), self.last_used
        if used is not None and self.last_merged:
            txt = "Datos unificados (%s)" % " + ".join(names.get(n, n) for n in self.last_merged)
            if self.last_errors:
                txt += "; fallaron: " + "; ".join("%s: %s" % (names.get(n, n), e or "sin respuesta")
                                                  for n, e in self.last_errors.items())
            return txt
        if used is None:
            errs = "; ".join("%s: %s" % (names.get(n, n), e or "sin respuesta") for n, e in self.last_errors.items())
            return "Sin datos del clima (%s)" % (errs or "sin proveedores")
        if used != pref:
            return "%s falló (%s); se usó %s" % (names.get(pref, pref), self.last_errors.get(pref) or "sin respuesta",
                                                  names.get(used, used))
        return "Datos actualizados (%s)" % names.get(used, used)

    def stop(self):
        """Para el hilo de fondo (al cerrar la app)."""
        self._stop_fetch.set()
        if self._fetch_thread:
            self._fetch_thread.join(timeout=2)
        self._save_mem_cache()

    def fetch(self, force: bool = False) -> bool:
        """Versión no bloqueante: solo dispara fetch si toca, no espera.
        Devuelve True si se disparó un fetch (se completará en background)."""
        if self._remote:   # cliente: los datos los manda el servidor
            return False
        with self._fetch_lock:
            enabled = self.config["enabled"]
            providers = list(self._providers)
            interval = self.config["fetch_interval_min"]
        if not enabled or not providers:
            return False
        now = time.time()
        wait = self._backoff if self._backoff else interval * 60   # tras fallos, reintento con backoff (60 s ... 10 min)
        if not force and now - max(self._last_fetch, self._attempt_ts) < wait:
            return False
        # Disparar fetch en hilo aparte si no hay uno en curso
        threading.Thread(target=self._fetch_one_locked, args=(providers,), daemon=True).start()
        return True  # se disparó, completará en background

    def _fetch_one(self):
        """Wrapper para fetch manual/force (usa lock y semaphore)."""
        with self._fetch_lock:
            providers = list(self._providers)
            interval = self.config["fetch_interval_min"]
        self._fetch_one_locked(providers)

    def _prune_cache(self):
        try:
            cutoff = time.time() - self.config["max_cache_hours"] * 3600
            to_keep = {}
            for k, v in self._mem_cache.items():
                try:
                    dt = datetime.strptime(k, "%Y-%m-%d_%H")
                    if dt.timestamp() > cutoff:
                        to_keep[k] = v
                except Exception:
                    pass
            self._mem_cache = to_keep
            self._cache_dirty = True
            self._save_mem_cache()
        except Exception:
            pass

    def get_hourly(self, dt: datetime) -> Optional[Dict[str, Any]]:
        if not self._providers:
            return None
        # Leer de memoria, no de disco
        key = dt.strftime("%Y-%m-%d_%H")
        return self._mem_cache.get(key)

    def current_provider_name(self) -> str:
        if self._providers:
            return self._providers[self._current_idx].name
        return ""

    def current_provider_display(self) -> str:
        if self._providers:
            return self._providers[self._current_idx].display_name
        return "Ninguno"
