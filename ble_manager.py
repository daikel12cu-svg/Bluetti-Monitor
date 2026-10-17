"""
ble_manager.py - Gestor Oficial BLE con Cifrado ECDH & Modbus para Bluetti Elite 100 V2 / 200 V2
=================================================================================================
Implementa el protocolo de autenticación criptográfica oficial por curva elíptica (SECP256R1),
intercambio de claves ECDH, cifrado de sesión AES-128/256-CBC con reensamblado automático de fragmentos BLE
y comandos Modbus RTU para sincronización en tiempo real y conmutación de salidas.
"""

import asyncio
import collections
import concurrent.futures
import hashlib
import json
import logging
import os
import queue
import random
import sys
import threading
import time
import types
from enum import Enum
from typing import Callable, Optional, Dict, Any, List

from bleak import BleakClient, BleakScanner
from bleak.backends.characteristic import BleakGATTCharacteristic

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from pyasn1.type import univ
import pyasn1.codec.der.decoder as der_decoder
import pyasn1.codec.der.encoder as der_encoder

# Configuración de Logging
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s (BLE): %(message)s")
logger = logging.getLogger("BluettiBLE")


# =============================================================================
# CONFIGURACION PERSONALIZABLE DE ALERTAS (antes alert_config.py)  [alertas-patch]
# Fuente UNICA de umbrales, tiempos e interruptores de alertas. ble_manager la usa aqui;
# hud_app.py y floating_widget.py la importan con "from ble_manager import alert_config".
# Los valores se guardan en %LOCALAPPDATA%\BluettiElite100Widget\alert_settings.json y se
# vuelven a leer solos si el archivo cambia (cada ~2 s). Con todo por defecto, comportamiento de siempre.
# =============================================================================
_ac_BASE_NAME = "alert_settings.json"
_ac_RECHECK_S = 2.0
_ac_NEVER_S = 1.0e12      # "nunca" para tiempos de proteccion desactivada


def _ac_F(key, label, kind, default, mn=None, mx=None, unit="", **extra):
    d = {"key": key, "label": label, "kind": kind, "default": default,
         "min": mn, "max": mx, "unit": unit}
    d.update(extra)
    return d


def _ac_G(label):
    return {"kind": "group", "label": label}


def _ac_N(label):
    return {"kind": "note", "label": label}


_ac_SCHEMA = {
    "ac_in_voltage": {
        "title": "Alertas de Voltaje (Red AC)",
        "card_label": "Alertas de Voltaje",
        "fields": [
            _ac_N("Estas alertas solo avisan: no cambian nada en el equipo."),
            _ac_F("warn_v", "Voltaje inestable (precaución) por debajo de", "float", 106.0, 80.0, 130.0, "V"),
            _ac_F("crit_v", "Voltaje bajo (alerta) por debajo de", "float", 101.0, 60.0, 125.0, "V"),
            _ac_F("device_alarm", "Avisar también de la alarma real de bajo voltaje del equipo", "bool", True),
        ],
    },
    "dc_in_current": {
        "title": "Alertas de Corriente (Solar DC)",
        "card_label": "Alertas de Corriente",
        "fields": [
            _ac_G("Alerta de alta corriente"),
            _ac_F("alert_a", "Avisar a partir de", "float", 15.0, 5.0, 30.0, "A"),
            _ac_G("Protección automática (independiente del switch de alertas)"),
            _ac_N("Al activarse, la fuente DC pasa a 'Otros' un tiempo y después vuelve a Panel Solar."),
            _ac_F("prot_a_on", "Regla A: corriente alta sostenida", "bool", True,
               confirm_off="Desactivar la regla A deja sin protección la entrada solar frente a corriente alta sostenida."),
            _ac_F("prot_a_s", "     Tiempo sostenido", "int", 180, 10, 3600, "s"),
            _ac_F("prot_band_low_a", "     Banda que NO reinicia el temporizador (desde)", "float", 14.0, 5.0, 30.0, "A"),
            _ac_F("prot_b_on", "Regla B: la corriente no baja de un mínimo", "bool", True,
               confirm_off="Desactivar la regla B deja sin protección la entrada solar frente a corriente alta prolongada."),
            _ac_F("prot_b_low_a", "     Corriente mínima", "float", 11.0, 1.0, 30.0, "A"),
            _ac_F("prot_b_s", "     Tiempo (desde que llega al umbral)", "int", 600, 60, 7200, "s"),
            _ac_F("prot_b_dip_s", "     Bajadas toleradas de hasta", "int", 5, 0, 60, "s"),
            _ac_F("refresh_s", "Tiempo en 'Otros' antes de volver a Panel", "int", 60, 10, 600, "s"),
            _ac_G("Switch de Alta corriente"),
            _ac_F("prot_with_hc", "Aplicar la protección tambien con Alta corriente activada", "bool", False),
            _ac_F("prot_hc_a", "     Umbral con Alta corriente activada", "float", 18.0, 10.0, 30.0, "A"),
            _ac_F("restore_hc", "Al restaurar Panel, el switch de Alta corriente:", "choice", "off",
               choices=[("off", "Queda siempre apagado"), ("previous", "Vuelve a su estado anterior")]),
            _ac_N("'Estado anterior' solo tiene efecto si la protección se aplica con Alta corriente activada."),
        ],
    },
    "ac_out_current": {
        "title": "Alertas de Corriente (Inversor AC / Red AC)",
        "card_label": "Alertas de Corriente",
        "fields": [
            _ac_G("1) Alta corriente al cargar (red AC)"),
            _ac_F("charge_on", "Avisar de alta corriente al cargar", "bool", True),
            _ac_F("charge_a", "     Umbral", "float", 12.0, 3.0, 30.0, "A"),
            _ac_F("charge_prot_on", "Protección: pasar a modo Silencioso (permanente)", "bool", True,
               confirm_off="Sin esta protección el equipo seguirá cargando con corriente alta por la entrada AC (cable/enchufe)."),
            _ac_F("charge_s", "     Tiempo sostenido para activar la protección", "int", 300, 30, 3600, "s"),
            _ac_G("2) Alta corriente por consumo (Silencioso activo + bypass)"),
            _ac_N("Con el modo Silencioso ya activo, un consumo alto pasa por la entrada AC y puede castigar el cable/enchufe."),
            _ac_F("cons_on", "Avisar de alta corriente por consumo", "bool", True),
            _ac_F("cons_a", "     Umbral", "float", 12.0, 3.0, 30.0, "A"),
            _ac_F("cons_s", "     Tiempo sostenido para avisar", "int", 300, 30, 3600, "s"),
            _ac_G("3) Picos de potencia (arranque de equipos)"),
            _ac_F("spike_on", "Avisar de picos de arranque", "bool", True),
            _ac_F("spike_min_w", "     Pico mínimo para avisar", "int", 150, 50, 3000, "W"),
            _ac_F("spike_factor", "     El pico debe ser al menos X veces el consumo que se queda", "float", 2.0, 1.0, 10.0, "x"),
            _ac_G("4) Consumo al límite (salida AC + DC; escala desde Alto Consumo)"),
            _ac_F("lim_on", "Avisar de «Consumo al Límite» (rojo, con triángulo)", "bool", True),
            _ac_F("lim_w", "     Umbral", "int", 1600, 500, 3000, "W"),
            _ac_F("lim_hyst_w", "     Se rearma al bajar esto bajo el umbral", "int", 100, 0, 1000, "W"),
            _ac_F("lim_prot_on", "Protección: apagar la salida AC", "bool", True,
               confirm_off="Sin esta protección la salida AC seguirá entregando un consumo por encima del límite."),
            _ac_F("lim_s", "     Tiempo sostenido para activar la protección", "int", 120, 10, 3600, "s"),
            _ac_F("lim_off_s", "     Tiempo con la salida AC apagada", "int", 60, 10, 600, "s"),
            _ac_N("Al restaurar, si el consumo sigue por encima del umbral durante 30 s, la salida AC se apaga definitivamente."),
        ],
    },
    "dc_out_battery": {
        "title": "Alertas de Batería (Salidas DC)",
        "card_label": "Alertas de Batería",
        "fields": [
            _ac_G("Celdas"),
            _ac_F("cell_warn_on", "Avisos de celda alta/baja (ámbar)", "bool", True),
            _ac_F("cell_warn_low_mv", "     Aviso celda baja a", "int", 2900, 2700, 3200, "mV"),
            _ac_F("cell_warn_high_mv", "     Aviso celda alta a", "int", 3600, 3450, 3640, "mV"),
            _ac_F("cell_crit_low_mv", "Critica: celda baja a", "int", 2600, 2400, 2800, "mV"),
            _ac_F("cell_crit_high_mv", "Critica: celda alta a", "int", 3650, 3600, 3700, "mV"),
            _ac_N("Las alertas de celda crítica siempre permanecen activas (solo se ajusta su umbral)."),
            _ac_F("balance_on", "Dispersión / desbalance entre celdas", "bool", True),
            _ac_F("calib_on", "% de carga posiblemente descalibrado", "bool", True),
            _ac_F("temp_on", "Temperatura de batería (avisos existentes)", "bool", True),
            _ac_G("Parpadeo del widget"),
            _ac_F("blink_soc_on", "Parpadeo por batería baja", "bool", True),
            _ac_F("blink_soc_pct", "     Por debajo de", "int", 15, 1, 50, "%"),
            _ac_F("blink_time_on", "Parpadeo por autonomía corta (descargando)", "bool", True),
            _ac_F("blink_time_min", "     Quedan menos de", "int", 20, 1, 120, "min"),
            _ac_G("Avisos que hay que cerrar (no activan el triángulo)"),
            _ac_F("low_soc_on", "Batería baja", "bool", True),
            _ac_F("low_soc_pct", "     Avisar al llegar a", "int", 20, 5, 60, "%"),
            _ac_F("low_soc_rearm", "     Se rearma al subir esto sobre el umbral", "int", 5, 1, 20, "puntos"),
            _ac_F("low_soc_10_on", "Batería muy baja (10 %)", "bool", True),
            _ac_F("low_soc_10_pct", "     Avisar al llegar a", "int", 10, 1, 20, "%"),
            _ac_F("low_soc_10_rearm", "     Se rearma al subir esto sobre el umbral", "int", 3, 1, 10, "puntos"),
            _ac_G("Protección automática: Batería Crítica (apaga salidas y avisa con triángulo)"),
            _ac_F("bat_crit_on", "Activar protección por batería crítica", "bool", True,
               confirm_off="Sin esta protección el equipo seguirá descargándose por debajo del 5 % sin apagar las salidas."),
            _ac_F("bat_crit_5_on", "Crítica al 5 %", "bool", True),
            _ac_F("bat_crit_5_w", "     Apaga AC+DC si consumo >", "int", 300, 50, 2000, "W"),
            _ac_N("Si el consumo es ≤ 300 W, el aviso se puede cerrar para postergar al 4 %."),
            _ac_F("bat_crit_4_on", "Crítica al 4 %", "bool", True),
            _ac_F("bat_crit_4_w", "     Apaga AC+DC si consumo >", "int", 200, 50, 2000, "W"),
            _ac_N("Si el consumo es ≤ 200 W, el aviso se puede cerrar para postergar al 3 %."),
            _ac_F("bat_crit_3_on", "Apagado inmediato al 3 % (5 s)", "bool", True),
            _ac_N("Al llegar al 3 % se apaga el sistema (registro 2013) sea cual sea el consumo."),
            _ac_F("load_on", "Alto Consumo (salida AC + DC)", "bool", True),
            _ac_F("load_w", "     Avisar por encima de", "int", 1200, 100, 3000, "W"),
            _ac_F("load_hyst_w", "     Se rearma al bajar esto bajo el umbral", "int", 100, 0, 1000, "W"),
            _ac_F("load_only_no_ac", "     Solo cuando NO se está cargando por AC", "bool", True),
            _ac_N("El parpadeo del widget por potencia sigue en >= 1000 W de flujo neto."),
            _ac_G("Carga por red AC (avisos puntuales: iniciada / completada / detenida / desconectada)"),
            _ac_F("ac_start_on", "Avisar cuando la carga AC se inicia", "bool", True),
            _ac_F("ac_completed_on", "Avisar «Carga AC completada» (llega al 100 % con red)", "bool", True),
            _ac_F("ac_stop_on", "Avisar «Carga AC detenida» (red se pierde antes del 100 %)", "bool", True),
            _ac_F("ac_restored_on", "Avisar «Electricidad Restablecida» (vuelve la red con la batería al 100 %)", "bool", True),
            _ac_F("ac_disconnected_on", "Avisar «Red AC Desconectada» (se va la red al 100 % en UPS)", "bool", True),
            _ac_F("ac_chg_w", "     Se considera carga AC a partir de", "int", 20, 5, 500, "W"),
            _ac_F("ac_chg_confirm_s", "     Debe mantenerse (para evitar falsos avisos)", "int", 3, 0, 60, "s"),
            _ac_N("Con la bateria al 100 % la red solo alimenta la salida (modo UPS) y no cuenta como carga: si vuelve la red en ese momento se avisa «Electricidad Restablecida»."),
            _ac_G("Temperatura de la batería"),
            _ac_F("temp_warn_c", "Aviso «algo caliente» a partir de", "float", 43.0, 30.0, 60.0, "°C"),
            _ac_F("temp_crit_c", "Alerta «muy caliente» a partir de", "float", 46.0, 31.0, 70.0, "°C"),
            _ac_F("temp_hyst_c", "     Histéresis (para salir del aviso)", "float", 0.5, 0.0, 5.0, "°C"),
            _ac_G("Carga AC limitada por protección y por temperatura"),
            _ac_N("Cerca del 100 % (o con la batería caliente) se limita la corriente de entrada AC con el modo Personalizado. Al terminar la carga, o tras estar sin red AC el tiempo indicado, se vuelve al modo de carga anterior."),
            _ac_F("chgp_on", "Activar la protección de carga", "bool", True),
            _ac_F("chgt_on", "     Tener en cuenta la temperatura (aviso / alerta)", "bool", True),
            _ac_F("chgp_soc_warn", "     Empezar a limitar desde (con aviso de temperatura)", "int", 70, 30, 99, "%"),
            _ac_F("chgp_soc_start", "     Empezar a limitar desde (sin aviso)", "int", 80, 30, 99, "%"),
            _ac_F("chgp_soc2", "     Segundo escalón desde (baja si hay aviso)", "int", 85, 30, 99, "%"),
            _ac_F("chgp_soc3", "     Tercer escalón desde", "int", 90, 30, 99, "%"),
            _ac_F("chgp_soc4", "     Último escalón desde", "int", 95, 30, 100, "%"),
            _ac_F("chgp_a1", "     Corriente máxima inicial", "int", 4, 1, 15, "A"),
            _ac_F("chgp_a2", "     Corriente en el segundo escalón", "int", 3, 1, 15, "A"),
            _ac_F("chgp_a3", "     Corriente en el último escalón", "int", 2, 1, 15, "A"),
            _ac_F("chgp_a_alert", "     Corriente con alerta de temperatura (desde el tercer escalón)", "int", 1, 1, 15, "A"),
            _ac_F("chgp_end_s", "     La carga se da por terminada tras estar al 100 % durante", "int", 120, 10, 900, "s"),
            _ac_F("chgp_idle_min", "     Sin red AC durante (se vuelve al modo anterior)", "int", 30, 1, 240, "min"),
            _ac_F("chg_lim_soc_on", "Avisar «Carga limitada por Protección»", "bool", True),
            _ac_F("chg_lim_temp_on", "Avisar «Carga limitada por Temperatura»", "bool", True),
        ],
    },
}


# ----------------------------------------------------------------------------
# Visibilidad de cada aviso: tiempo en pantalla (0 = hasta cerrarlo a mano) y si deja el
# triangulo de advertencia (tocarlo lo vuelve a mostrar mientras dure la condicion).
# id: (seccion, nombre, segundos por defecto, triangulo por defecto | None = no aplica, severidad 1 aviso / 2 critica)
# ----------------------------------------------------------------------------
_ac_ALERT_META = {
    "v_warn":      ("ac_in_voltage",  "Voltaje inestable", 5, True, 1),
    "v_crit":      ("ac_in_voltage",  "Voltaje bajo / alarma de red", 5, True, 2),
    "dc_high":     ("dc_in_current",  "Alta corriente DC", 5, True, 2),
    "dc_prot":     ("dc_in_current",  "Protección de alta corriente DC (cuenta atrás)", 0, True, 1),
    "dc_restored": ("dc_in_current",  "Alta corriente restaurada", 5, None, 1),
    "ac_high":     ("ac_out_current", "Alta corriente AC", 5, True, 2),
    "ac_reduced":  ("ac_out_current", "Alta corriente AC reducida", 0, True, 1),
    "ac_cons":     ("ac_out_current", "Alta corriente por consumo", 5, True, 2),
    "spike":       ("ac_out_current", "Pico de arranque", 5, None, 1),
    "lim_load":    ("ac_out_current", "Consumo al Límite", 0, True, 2),
    "lim_prot":    ("ac_out_current", "Protección por Consumo al Límite (salida AC apagada)", 0, None, 1),
    "lim_restored": ("ac_out_current", "Restaurada Salida AC", 5, None, 1),
    "cell_crit":   ("dc_out_battery", "Celda crítica", 5, True, 2),
    "cell_warn":   ("dc_out_battery", "Celda alta/baja fuera de rango", 5, True, 1),
    "balance":     ("dc_out_battery", "Dispersión / desbalance entre celdas", 5, True, 1),
    "calib":       ("dc_out_battery", "% de carga posiblemente descalibrado", 5, True, 1),
    "temp_warn":   ("dc_out_battery", "Batería algo caliente", 0, True, 1),
    "temp_crit":   ("dc_out_battery", "Batería muy caliente", 0, True, 2),
    "low_soc":     ("dc_out_battery", "Batería baja", 0, False, 1),
    "load_high":   ("dc_out_battery", "Alto Consumo", 0, False, 1),
    "ac_start":      ("dc_out_battery", "Carga AC iniciada", 5, None, 1),
    "ac_completed":  ("dc_out_battery", "Carga AC completada", 5, None, 1),
    "ac_stop":       ("dc_out_battery", "Carga AC detenida", 5, None, 1),
    "ac_restored":   ("dc_out_battery", "Electricidad Restablecida", 5, None, 1),
    "ac_disconnected": ("dc_out_battery", "Red AC Desconectada", 5, None, 1),
    "chg_lim_soc": ("dc_out_battery", "Carga limitada por Protección", 5, None, 1),
    "chg_lim_temp": ("dc_out_battery", "Carga limitada por Temperatura", 5, None, 1),
    "low_soc_10":   ("dc_out_battery", "Batería muy baja (10 %)", 0, False, 1),
    "bat_crit_5":   ("dc_out_battery", "Batería Crítica (5 %)", 0, True, 2),
    "bat_crit_4":   ("dc_out_battery", "Batería Crítica (4 %)", 0, True, 2),
    "bat_crit_3":   ("dc_out_battery", "Apagado Inmediato por Batería Crítica (3 %)", 0, True, 2),
}


# aviso -> casilla "Avisar..." que ya existe en su seccion (la ventana de ajustes las agrupa en la tabla de avisos)
_ac_ALERT_ON = {
    "ac_high": "charge_on", "ac_cons": "cons_on", "spike": "spike_on", "cell_warn": "cell_warn_on",
    "balance": "balance_on", "calib": "calib_on", "temp_warn": "temp_on", "temp_crit": "temp_on",
    "low_soc": "low_soc_on", "low_soc_10": "low_soc_10_on", "load_high": "load_on",
    "ac_start": "ac_start_on", "ac_completed": "ac_completed_on", "ac_stop": "ac_stop_on",
    "ac_restored": "ac_restored_on", "ac_disconnected": "ac_disconnected_on",
    "chg_lim_soc": "chg_lim_soc_on", "chg_lim_temp": "chg_lim_temp_on",
    "lim_load": "lim_on", "lim_prot": "lim_prot_on", "lim_restored": "lim_prot_on",
    "bat_crit_5": "bat_crit_on", "bat_crit_4": "bat_crit_on", "bat_crit_3": "bat_crit_on",
}


def _ac_add_vis_fields():
    for aid, (sec, name, dur, tri, _sev) in _ac_ALERT_META.items():
        fl = _ac_SCHEMA[sec]["fields"]
        fl.append(_ac_F(aid + "_dur", "     Tiempo visible (0 = hasta cerrarlo)", "int", dur, 0, 3600, "s"))
        if tri is not None:
            fl.append(_ac_F(aid + "_tri", "     Deja el triángulo (tocarlo lo vuelve a mostrar)", "bool", tri))


_ac_add_vis_fields()


def _ac_build_defaults():
    out = {}
    for sec, meta in _ac_SCHEMA.items():
        d = {"enabled": True}
        for f in meta["fields"]:
            if "key" in f:
                d[f["key"]] = f["default"]
        out[sec] = d
    return out


_ac_DEFAULTS = _ac_build_defaults()

_ac_lock = threading.RLock()
_ac_data = None
_ac_mtime = None
_ac_last_check = 0.0


def _ac_path():
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "BluettiElite100Widget")
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception:
        pass
    return os.path.join(folder, _ac_BASE_NAME)


def _ac_field_map(sec):
    return {f["key"]: f for f in _ac_SCHEMA[sec]["fields"] if "key" in f}


def _ac_coerce(f, value):
    """Convierte y recorta un valor al tipo/rango del campo; si no sirve, el defecto."""
    kind = f["kind"]
    try:
        if kind == "bool":
            if isinstance(value, bool):
                return value
            raise ValueError
        if kind == "choice":
            allowed = [c[0] for c in f["choices"]]
            return value if value in allowed else f["default"]
        num = float(value)
        if num != num or num in (float("inf"), float("-inf")):
            raise ValueError
        if f.get("min") is not None:
            num = max(f["min"], num)
        if f.get("max") is not None:
            num = min(f["max"], num)
        return int(round(num)) if kind == "int" else float(num)
    except Exception:
        return f["default"]


def _ac_normalize(raw):
    out = _ac_build_defaults()
    if not isinstance(raw, dict):
        return out
    for sec in _ac_SCHEMA:
        src = raw.get(sec)
        if not isinstance(src, dict):
            continue
        if isinstance(src.get("enabled"), bool):
            out[sec]["enabled"] = src["enabled"]
        for key, f in _ac_field_map(sec).items():
            if key in src:
                out[sec][key] = _ac_coerce(f, src[key])
    return out


def _ac_load_locked():
    global _ac_data, _ac_mtime
    p = _ac_path()
    try:
        _ac_mtime = os.path.getmtime(p)
        with open(p, "r", encoding="utf-8") as fh:
            _ac_data = _ac_normalize(json.load(fh))
    except Exception:
        _ac_data = _ac_build_defaults()
        try:
            _ac_mtime = os.path.getmtime(p)
        except Exception:
            _ac_mtime = None


def _ac_ensure():
    """Carga la primera vez y vuelve a cargar si el archivo cambio en disco."""
    global _ac_last_check
    with _ac_lock:
        if _ac_data is None:
            _ac_load_locked()
            _ac_last_check = time.monotonic()
            return
        now = time.monotonic()
        if now - _ac_last_check < _ac_RECHECK_S:
            return
        _ac_last_check = now
        try:
            m = os.path.getmtime(_ac_path())
        except Exception:
            m = None
        if m != _ac_mtime:
            _ac_load_locked()


def _ac_section(name):
    """Copia de los valores de una seccion (incluye 'enabled')."""
    _ac_ensure()
    with _ac_lock:
        return dict(_ac_data[name])


def _ac_get(name, key):
    _ac_ensure()
    with _ac_lock:
        return _ac_data[name][key]


def _ac_defaults(name):
    return dict(_ac_DEFAULTS[name])


def _ac_set_enabled(name, enabled):
    _ac_ensure()
    with _ac_lock:
        _ac_data[name]["enabled"] = bool(enabled)


def _ac_set_values(name, values):
    _ac_ensure()
    fm = _ac_field_map(name)
    with _ac_lock:
        for k, v in values.items():
            if k in fm:
                _ac_data[name][k] = _ac_coerce(fm[k], v)


def _ac_save():
    global _ac_mtime
    with _ac_lock:
        if _ac_data is None:
            return False
        p = _ac_path()
        tmp = p + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(_ac_data, fh, indent=1, ensure_ascii=False)
            os.replace(tmp, p)
            _ac_mtime = os.path.getmtime(p)
            return True
        except Exception:
            return False


def _ac_validate(name, v):
    """None si los valores son coherentes entre si; si no, el texto del problema."""
    try:
        if name == "ac_in_voltage":
            if v["crit_v"] >= v["warn_v"]:
                return "El voltaje 'bajo' debe ser menor que el 'inestable'."
        elif name == "dc_in_current":
            if v["prot_band_low_a"] > v["alert_a"]:
                return "La banda de la regla A no puede empezar por encima del umbral de alerta."
            if v["prot_b_low_a"] >= v["alert_a"]:
                return "La corriente minima de la regla B debe ser menor que el umbral de alerta."
            if v["prot_hc_a"] <= v["prot_b_low_a"]:
                return "El umbral con Alta corriente debe ser mayor que la corriente minima de la regla B."
        elif name == "dc_out_battery":
            if not v["cell_crit_low_mv"] < v["cell_warn_low_mv"]:
                return "Celda baja: la critica debe ser menor que el aviso."
            if not v["cell_warn_high_mv"] < v["cell_crit_high_mv"]:
                return "Celda alta: el aviso debe ser menor que la critica."
            if v["low_soc_pct"] + v["low_soc_rearm"] > 100:
                return "El rearme de 'Bateria baja' no puede superar el 100 %."
            if not v["temp_warn_c"] < v["temp_crit_c"]:
                return "Temperatura: el aviso debe ser menor que la alerta."
            if not (v["chgp_soc_warn"] <= v["chgp_soc_start"] <= v["chgp_soc2"] <= v["chgp_soc3"] <= v["chgp_soc4"]):
                return "Carga limitada: los porcentajes deben ir de menor a mayor (desde con aviso <= desde sin aviso <= 2.º <= 3.º <= último)."
            if not (v["chgp_a1"] >= v["chgp_a2"] >= v["chgp_a3"] >= v["chgp_a_alert"]):
                return "Carga limitada: las corrientes deben ir de mayor a menor (inicial >= 2.º >= último >= con alerta)."
    except KeyError:
        return None
    return None


# ----------------------------------------------------------------------------
# Enlaces con ble_manager.py (umbrales y tiempos de las protecciones)
# ----------------------------------------------------------------------------
def _ac_apply_to_ble(g, state):
    """Escribe en el diccionario de globales de ble_manager los valores
    configurados. Se llama antes de cada lectura, asi las constantes PROT_*,
    CELL_* y SPIKE_* del modulo siguen siendo las que usa el resto del codigo."""
    try:
        d = _ac_section("dc_in_current")
        a = _ac_section("ac_out_current")
        b = _ac_section("dc_out_battery")

        hc_on = bool(state.get("dc_high_current_on"))
        width = max(0.0, d["alert_a"] - d["prot_band_low_a"])
        thr = d["prot_hc_a"] if (hc_on and d["prot_with_hc"]) else d["alert_a"]
        g["PROT_DC_ALERT_A"] = float(thr)
        g["PROT_DC_BAND_LOW_A"] = max(0.0, float(thr) - width)
        g["PROT_DC_A_SECONDS"] = float(d["prot_a_s"]) if d["prot_a_on"] else _ac_NEVER_S
        g["PROT_DC_B_LOW_A"] = float(d["prot_b_low_a"])
        g["PROT_DC_B_SECONDS"] = float(d["prot_b_s"]) if d["prot_b_on"] else _ac_NEVER_S
        g["PROT_DC_B_DIP_TOL_S"] = float(d["prot_b_dip_s"])
        g["PROT_DC_REFRESH_S"] = float(d["refresh_s"])

        if state.get("charge_mode") == "silent":
            thr_ac, secs = a["cons_a"], float(a["cons_s"])
        else:
            thr_ac = a["charge_a"]
            secs = float(a["charge_s"]) if a["charge_prot_on"] else _ac_NEVER_S
        g["PROT_AC_ALERT_A"] = float(thr_ac)
        g["PROT_AC_RELEASE_A"] = max(0.1, float(thr_ac) - 0.5)
        g["PROT_AC_SECONDS"] = secs

        g["SPIKE_MIN_PEAK_W"] = float(a["spike_min_w"])
        g["SPIKE_FACTOR"] = float(a["spike_factor"])

        g["CELL_WARN_LOW_MV"] = int(b["cell_warn_low_mv"])
        g["CELL_WARN_HIGH_MV"] = int(b["cell_warn_high_mv"])
        g["CELL_CRIT_LOW_MV"] = int(b["cell_crit_low_mv"])
        g["CELL_CRIT_HIGH_MV"] = int(b["cell_crit_high_mv"])
    except Exception:
        pass


# ----------------------------------------------------------------------------
# Enlaces con el widget flotante y con la barra de estado de la app
# ----------------------------------------------------------------------------
def _ac_mask_telemetry(t):
    """Copia de la telemetria sin la alarma real de bajo voltaje si el usuario
    la desactivo (o desactivo todas las alertas de voltaje)."""
    try:
        if t.get("grid_low_voltage_alarm"):
            v = _ac_section("ac_in_voltage")
            if not (v["enabled"] and v["device_alarm"]):
                t = dict(t)
                t["grid_low_voltage_alarm"] = False
    except Exception:
        pass
    return t


def _ac_current_limits(charge_mode):
    """(limite_dc, limite_ac, dc_activo, ac_activo) vigentes. Con el modo de
    carga Silencioso, la corriente AC se evalua con el umbral de 'consumo'."""
    d = _ac_section("dc_in_current")
    a = _ac_section("ac_out_current")
    silent = (charge_mode == "silent")
    lim_ac = a["cons_a"] if silent else a["charge_a"]
    ac_on = a["enabled"] and (a["cons_on"] if silent else a["charge_on"])
    return float(d["alert_a"]), float(lim_ac), bool(d["enabled"]), bool(ac_on)


def _ac_gate_currents(charge_mode, dc_a, ac_a):
    """Corrientes a evaluar para alertas: 0 A si esa alerta esta desactivada."""
    _, _, dc_on, ac_on = _ac_current_limits(charge_mode)
    return (dc_a if dc_on else 0.0), (ac_a if ac_on else 0.0)


def _ac_widget_gate(g, charge_mode, volts, dc_a, ac_a):
    """Prepara los datos que el widget usa para sus alertas de entrada.
    - Fija CURRENT_LIMIT_DC / CURRENT_LIMIT_AC (globales de floating_widget).
    - Devuelve (voltaje, corriente_dc, corriente_ac) ya filtrados.
    El voltaje se 'traduce' a un valor equivalente respecto a los umbrales
    internos del widget (101 V critico / 106 V inestable), para que los umbrales
    configurados se apliquen sin tocar su logica."""
    try:
        lim_dc, lim_ac, _, _ = _ac_current_limits(charge_mode)
        g["CURRENT_LIMIT_DC"] = lim_dc
        g["CURRENT_LIMIT_AC"] = lim_ac
        dc_a, ac_a = _ac_gate_currents(charge_mode, dc_a, ac_a)
        v = _ac_section("ac_in_voltage")
        if not v["enabled"] or not volts or volts <= 0:
            volts = 0.0
        elif volts < v["crit_v"]:
            volts = 100.0
        elif volts < v["warn_v"]:
            volts = 105.0
        else:
            volts = max(float(volts), 106.0)
    except Exception:
        pass
    return volts, dc_a, ac_a


def _ac_tracker_view(t):
    """Copia de la telemetria para AppAlertTracker con lo desactivado a cero."""
    try:
        t = dict(t)
        v = _ac_section("ac_in_voltage")
        if not v["enabled"]:
            t["ac_input_volts"] = 0.0
        dc_a, ac_a = _ac_gate_currents(t.get("charge_mode"), t.get("dc_input_current") or 0.0,
                                   t.get("ac_input_current") or 0.0)
        t["dc_input_current"], t["ac_input_current"] = dc_a, ac_a
        a = _ac_section("ac_out_current")
        if not (a["enabled"] and a["spike_on"]):
            t["spike_ts"] = 0.0
    except Exception:
        pass
    return t


def _ac_blink_soc_low(soc):
    b = _ac_section("dc_out_battery")
    return bool(b["enabled"] and b["blink_soc_on"] and soc < b["blink_soc_pct"])


def _ac_blink_time_low(runtime_min):
    b = _ac_section("dc_out_battery")
    return bool(b["enabled"] and b["blink_time_on"] and runtime_min < b["blink_time_min"])


def _ac_cell_category(text):
    t = str(text).lower()
    if "celda crítica" in t or "celda critica" in t:
        return "crit"
    if "fuera de rango" in t:
        return "cell_warn"
    if "dispersi" in t or "desbalance" in t:
        return "balance"
    if "descalibr" in t:
        return "calib"
    if "caliente" in t or "temperatura" in t:
        return "temp"
    return None


def _ac_filter_cell_alerts(items):
    """Filtra las alertas de bateria [(texto, color, severidad)] segun los
    interruptores. Se reconocen por su texto, asi tambien se filtran avisos que
    se hayan anadido despues (p. ej. temperatura). La celda critica nunca se filtra."""
    try:
        b = _ac_section("dc_out_battery")
        keep = {"cell_warn": "cell_warn_on", "balance": "balance_on",
                "calib": "calib_on", "temp": "temp_on"}
        out = []
        for it in items:
            cat = _ac_cell_category(it[0])
            if cat == "crit":
                out.append(it)
            elif not b["enabled"]:
                continue
            elif cat is None or b.get(keep[cat], True):
                out.append(it)
        return out
    except Exception:
        return items


def _ac_filter_alert_lines(items):
    """Quita del banner/barra de estado lo desactivado (picos de arranque, temperatura)."""
    try:
        a = _ac_section("ac_out_current")
        b = _ac_section("dc_out_battery")
        out = list(items)
        if not (a["enabled"] and a["spike_on"]):
            out = [it for it in out if not str(it[0]).startswith("Pico de Arranque")]
        if not (b["enabled"] and b["temp_on"]):
            out = [it for it in out if "caliente" not in str(it[0]).lower()]
        return out
    except Exception:
        return items


_ac_EV_ID = {"start": "ac_start", "completed": "ac_completed", "stop": "ac_stop",
             "restored": "ac_restored", "disconnected": "ac_disconnected",
             "limsoc": "chg_lim_soc", "limtemp": "chg_lim_temp",
             "limrest": "lim_restored"}   # aviso puntual -> id de visibilidad


def _ac_out_w(t):
    dc_w = (t.get("dc_output_watts") or 0) if t.get("dc_output_on", False) else 0
    return int((t.get("ac_output_watts") or 0) + dc_w)


class _ac_NoticeState:
    """Avisos que hay que cerrar a mano y que NO activan el triangulo:
    'Bateria baja' y 'Consumo alto' (salida AC + DC). Cada uno salta una vez al
    cruzar su umbral y no vuelve a saltar hasta rearmarse (subir/bajar la
    histeresis configurada). Cerrar el aviso no lo rearma."""

    BATT_COLOR = "#f59e0b"
    LOAD_COLOR = "#fb923c"
    AC_START_COLOR = "#10b981"
    AC_COMPLETED_COLOR = "#3b82f6"   # azul (éxito)
    AC_STOP_COLOR = "#fb923c"         # ámbar/naranja (interrumpida)
    AC_RESTORED_COLOR = "#22d3ee"
    AC_DISCONNECTED_COLOR = "#f59e0b" # ámbar (informativo UPS)
    LIM_SOC_COLOR = "#60a5fa"
    LIM_TEMP_COLOR = "#fb923c"
    LIM_COLOR = "#ef4444"
    LIM_RESTORED_COLOR = "#3b82f6"
    LIM_PROT_COLOR = "#38bdf8"
    LOW_SOC_10_COLOR = "#f59e0b"
    BAT_CRIT_COLOR = "#ef4444"      # rojo con triángulo (como Consumo al Límite)

    def __init__(self):
        self._acs = None            # carga AC actual (None = aun sin saber: el primer dato no avisa)
        self._acs_cand = None       # (nuevo estado, desde cuando) a la espera de confirmarse
        self._ev = {}               # avisos puntuales activos: "start"/"stop"/... -> instante en que terminan
        self._ev_a = {}             # amperios de los avisos de carga limitada
        self._acp = None            # hay red AC (None = aun sin saber)
        self._acp_cand = None
        self._lim_seen = None       # ultimo aviso de carga limitada ya visto (el primer dato no avisa)
        self.batt_armed = True
        self.batt_active = False
        self.batt_soc = None
        self.batt_10_armed = True
        self.batt_10_active = False
        self.batt_10_soc = None
        self.load_armed = True
        self.load_active = False
        self.load_w = 0
        self.lim_armed = True
        self.lim_active = False     # consumo por encima del limite de 'Consumo al Limite' (rojo, hasta cerrarlo)
        self.lim_w_now = 0
        self.lim_prot_active = False
        self.lim_prot_end = None
        self._lim_prot_seen = None
        self._lim_rest_seen = None
        # Batería Crítica: 5% -> 4% -> 3%
        self.bat_crit_armed = True  # rearma al subir sobre 5% + histeresis
        self.bat_crit_active = False
        self.bat_crit_level = 0     # 5, 4, o 3
        self.bat_crit_end_ts = None
        self.bat_crit_fire_ts = 0.0
        self.bat_crit_dismissed = False  # usuario cerró el aviso para postergar
        self.fire_seq = 0   # sube cada vez que aparece un aviso nuevo (cada consumidor compara con el suyo)

    def update(self, t, is_conn):
        """True si acaba de aparecer un aviso nuevo."""
        fired = False
        b = _ac_section("dc_out_battery")
        a = _ac_section("ac_out_current")
        if not a["enabled"] or not a["lim_on"]:
            self.lim_active = False
        if not b["enabled"] or not b["low_soc_on"]:
            self.batt_active = False
        if not b["enabled"] or not b["load_on"]:
            self.load_active = False
        if not is_conn or not b["enabled"]:
            self._acs = None
            self._acs_cand = None
            self._acp = None
            self._acp_cand = None
            self._ev.clear()
            self.lim_active = False
            self.lim_prot_active = False
            return False

        if b["ac_start_on"] or b["ac_stop_on"] or b["ac_restored_on"]:
            if self._ac_charge_event(t, b):
                fired = True
        else:
            self._acs = None
            self._acs_cand = None
            self._acp = None
            self._acp_cand = None

        ts = float(t.get("chg_lim_ts") or 0.0)   # avisos de carga limitada (los genera la proteccion de ble_manager)
        if self._lim_seen is None:
            self._lim_seen = ts
        elif ts > self._lim_seen:
            self._lim_seen = ts
            kind = "limtemp" if t.get("chg_lim_ev_kind") == "temp" else "limsoc"
            aid = _ac_EV_ID[kind]
            if b[aid + "_on"]:
                self._ev[kind] = time.time() + _ac_event_secs(aid)
                self._ev_a[kind] = t.get("chg_lim_ev_a")
                fired = True

        soc = t.get("soc")
        if b["low_soc_on"] and isinstance(soc, (int, float)):
            if soc >= b["low_soc_pct"] + b["low_soc_rearm"]:
                self.batt_armed = True
            if self.batt_armed and soc <= b["low_soc_pct"]:
                self.batt_armed = False
                self.batt_active = True
                fired = True
            if soc > b["low_soc_pct"]:
                self.batt_active = False   # ya no esta baja: el aviso desaparece
            self.batt_soc = int(soc)

        if b["load_on"]:
            dc_w = (t.get("dc_output_watts") or 0) if t.get("dc_output_on", False) else 0
            out_w = int((t.get("ac_output_watts") or 0) + dc_w)
            ac_in = t.get("ac_input_watts") or 0
            if out_w <= b["load_w"] - b["load_hyst_w"]:
                self.load_armed = True
            if (self.load_armed and out_w > b["load_w"]
                    and (not b["load_only_no_ac"] or ac_in <= 5)):
                self.load_armed = False
                self.load_active = True
                fired = True
            if self.load_active and (out_w <= b["load_w"] - b["load_hyst_w"]
                                     or (b["load_only_no_ac"] and ac_in > 5)):
                self.load_active = False   # ya no hay consumo alto: el aviso desaparece solo
            self.load_w = out_w
        if a["lim_on"]:
            out_w = _ac_out_w(t)
            applies = not b["load_only_no_ac"] or (t.get("ac_input_watts") or 0) <= 5
            if out_w <= a["lim_w"] - a["lim_hyst_w"]:
                self.lim_armed = True
            if self.lim_armed and out_w > a["lim_w"] and applies:
                self.lim_armed = False
                self.lim_active = True
                fired = True
            if self.lim_active and (out_w <= a["lim_w"] - a["lim_hyst_w"] or not applies):
                self.lim_active = False
            self.lim_w_now = out_w
        self.lim_prot_active = bool(t.get("prot_lim_active"))
        self.lim_prot_end = t.get("prot_lim_end_ts")
        pts = float(t.get("prot_lim_fire_ts") or 0.0)
        if self._lim_prot_seen is None:
            self._lim_prot_seen = pts
        elif pts > self._lim_prot_seen:
            self._lim_prot_seen = pts
            fired = True
        rts = float(t.get("prot_lim_restored_ts") or 0.0)
        if self._lim_rest_seen is None:
            self._lim_rest_seen = rts
        elif rts > self._lim_rest_seen:
            self._lim_rest_seen = rts
            if a["lim_prot_on"]:
                self._ev["limrest"] = time.time() + _ac_event_secs("lim_restored")
                fired = True

        # Batería muy baja (10%) - comportamiento igual que low_soc (20%)
        if b.get("low_soc_10_on") and isinstance(soc, (int, float)):
            if soc >= b["low_soc_10_pct"] + b["low_soc_10_rearm"]:
                self.batt_10_armed = True
            if self.batt_10_armed and soc <= b["low_soc_10_pct"]:
                self.batt_10_armed = False
                self.batt_10_active = True
                fired = True
            if soc > b["low_soc_10_pct"]:
                self.batt_10_active = False
            self.batt_10_soc = int(soc)

        # Batería Crítica: 5% -> 4% -> 3%
        if b.get("bat_crit_on") and isinstance(soc, (int, float)):
            out_w = _ac_out_w(t)
            # Rearma al subir por encima del nivel actual + histeresis (3 puntos por defecto)
            rearm_pct = {5: 5 + 3, 4: 4 + 3, 3: 3 + 3}
            thresholds = {5: 5, 4: 4, 3: 3}
            w_thresholds = {5: b.get("bat_crit_5_w", 300), 4: b.get("bat_crit_4_w", 200)}
            bat_crit_5_on = b.get("bat_crit_5_on", True)
            bat_crit_4_on = b.get("bat_crit_4_on", True)
            bat_crit_3_on = b.get("bat_crit_3_on", True)

            # Detectar nivel actual (5, 4, o 3)
            current_level = 0
            if bat_crit_5_on and soc <= 5:
                current_level = 5
            elif bat_crit_4_on and soc <= 4:
                current_level = 4
            elif bat_crit_3_on and soc <= 3:
                current_level = 3

            if current_level:
                # Rearma si sube por encima del umbral + histeresis
                if soc >= rearm_pct.get(current_level, current_level + 3):
                    self.bat_crit_armed = True
                    self.bat_crit_dismissed = False

                # Si estaba armado y entramos en nivel crítico
                if self.bat_crit_armed and not self.bat_crit_active:
                    self.bat_crit_armed = False
                    self.bat_crit_active = True
                    self.bat_crit_level = current_level
                    self.bat_crit_fire_ts = time.time()
                    self.bat_crit_end_ts = time.time() + 60.0  # 1 min cuenta atrás
                    self.bat_crit_dismissed = False
                    fired = True
                elif self.bat_crit_active and current_level != self.bat_crit_level:
                    # Bajó de nivel (5->4 o 4->3)
                    self.bat_crit_level = current_level
                    self.bat_crit_fire_ts = time.time()
                    self.bat_crit_end_ts = time.time() + 60.0
                    self.bat_crit_dismissed = False
                    fired = True

                # Actualizar cuenta atrás si está activo
                if self.bat_crit_active:
                    self.bat_crit_end_ts = max(self.bat_crit_end_ts, time.time() + 60.0)
            else:
                # SOC subió por encima de 5% -> se rearma todo
                if soc > 5:
                    self.bat_crit_armed = True
                    self.bat_crit_active = False
                    self.bat_crit_level = 0
                    self.bat_crit_dismissed = False

        if fired:
            self.fire_seq += 1
        return fired

    def _ac_charge_event(self, t, b):
        fired = self._ac_disconnected_event(t, b)
        if self._ac_restored_event(t, b):
            fired = True
        if self._ac_charge_event_core(t, b):
            fired = True
        return fired

    def _ac_restored_event(self, t, b):
        """«Electricidad Restablecida»: vuelve la red con la bateria al 100 % (ahi no hay 'carga AC iniciada')."""
        now = time.time()
        ac_in = t.get("ac_input_watts") or 0
        volts = t.get("ac_input_volts") or 0
        soc = t.get("soc")
        full = isinstance(soc, (int, float)) and soc >= 100
        present = bool(volts >= AC_VOLT_MIN_PLAUSIBLE_V or ac_in >= b["ac_chg_w"])
        if self._acp is None:
            self._acp, self._acp_cand = present, None
            return False
        if present == self._acp:
            self._acp_cand = None
            return False
        if self._acp_cand is None or self._acp_cand[0] != present:
            self._acp_cand = (present, now)
        if now - self._acp_cand[1] < b["ac_chg_confirm_s"]:
            return False
        self._acp, self._acp_cand = present, None
        if not (present and full) or not b["ac_restored_on"]:
            return False
        self._ev.pop("start", None)
        self._ev.pop("stop", None)
        self._ev["restored"] = now + _ac_event_secs("ac_restored")
        return True

    def _ac_disconnected_event(self, t, b):
        """«Red AC Desconectada»: se va la red con la batería al 100 % (modo UPS)."""
        now = time.time()
        ac_in = t.get("ac_input_watts") or 0
        volts = t.get("ac_input_volts") or 0
        soc = t.get("soc")
        full = isinstance(soc, (int, float)) and soc >= 100
        present = bool(volts >= AC_VOLT_MIN_PLAUSIBLE_V or ac_in >= b["ac_chg_w"])
        if self._acp is None:
            return False
        if present == self._acp:
            return False
        # present=False, _acp=True -> AC se desconectó
        if not present and self._acp and full:
            if not b.get("ac_disconnected_on", True):
                return False
            self._ev["disconnected"] = now + _ac_event_secs("ac_disconnected")
            return True
        return False

    def _ac_charge_event_core(self, t, b):
        """Detecta inicio / fin de la carga por red AC. True si hay que mostrar un aviso nuevo."""
        now = time.time()
        ac_in = t.get("ac_input_watts") or 0
        soc = t.get("soc")
        full = isinstance(soc, (int, float)) and soc >= 100
        on = bool(ac_in >= b["ac_chg_w"] and not full)
        if self._acs is None:
            self._acs, self._acs_cand = on, None
            return False
        if on == self._acs:
            self._acs_cand = None
            return False
        if self._acs_cand is None or self._acs_cand[0] != on:
            self._acs_cand = (on, now)
        if now - self._acs_cand[1] < b["ac_chg_confirm_s"]:
            return False
        self._acs, self._acs_cand = on, None
        if on:
            # False -> True: INICIO DE CARGA
            key, other, aid = "start", "stop", "ac_start"
        else:
            # True -> False: FIN DE CARGA - ¿POR QUÉ?
            ac_present = bool(ac_in >= b["ac_chg_w"] or t.get("ac_input_volts", 0) >= AC_VOLT_MIN_PLAUSIBLE_V)
            if full and ac_present:
                # SOC llegó a 100% con AC presente -> COMPLETADA
                key, other, aid = "completed", "start", "ac_completed"
            else:
                # AC se fue antes del 100% -> DETENIDA
                key, other, aid = "stop", "start", "ac_stop"
        self._ev.pop(other, None)   # alternan: al llegar uno desaparece el otro
        if not b.get(aid + "_on", True):
            return False
        self._ev[key] = now + _ac_event_secs(aid)
        return True

    def _ac_live(self):
        now = time.time()
        for k in [k for k, e in self._ev.items() if now >= e]:
            del self._ev[k]
        return self._ev

    def any_active(self):
        # los avisos puntuales hacen el aviso del widget "persistente" solo si deben durar mas de 5 s
        # (el aviso normal del widget ya se oculta solo a los 5 s)
        ev = any(_ac_vis_cfg(_ac_EV_ID[k])[0] not in range(1, 6) for k in self._ac_live())
        return bool(self.batt_active or self.batt_10_active or self.load_active or self.lim_active
                    or self.lim_prot_active or self.bat_crit_active or ev)

    def lines(self):
        out = []
        if self.batt_active:
            soc = "--" if self.batt_soc is None else self.batt_soc
            out.append((f"Batería baja ({soc}%)", self.BATT_COLOR, "notice"))
        if self.batt_10_active:
            soc = "--" if self.batt_10_soc is None else self.batt_10_soc
            out.append((f"Batería muy baja ({soc}%)", self.LOW_SOC_10_COLOR, "notice"))
        if self.lim_active:
            out.append((f"Consumo al Límite: {self.lim_w_now} W", self.LIM_COLOR, "notice"))
        elif self.load_active:
            out.append((f"Alto Consumo: {self.load_w} W", self.LOAD_COLOR, "notice"))
        if self.lim_prot_active and self.lim_prot_end is None:
            out.append(("Salida AC apagada por consumo al límite", self.LIM_PROT_COLOR, "notice"))
        elif self.lim_prot_active:
            left = max(0, int(round(float(self.lim_prot_end) - time.time())))
            out.append((f"Salida AC apagada por consumo al límite: {left} s", self.LIM_PROT_COLOR, "notice"))
        # Batería Crítica: 5% / 4% / 3% con triángulo y cuenta atrás
        if self.bat_crit_active:
            left = max(0, int(round(float(self.bat_crit_end_ts) - time.time())))
            if self.bat_crit_level == 3:
                out.append((f"Apagado Inmediato por Batería Crítica: {left:02d} s", self.BAT_CRIT_COLOR, "notice"))
            else:
                out.append((f"Batería Crítica ({self.bat_crit_level}%), apagando en {left:02d} s", self.BAT_CRIT_COLOR, "notice"))
        ev = self._ac_live()
        if "start" in ev:
            out.append(("Carga AC iniciada", self.AC_START_COLOR, "notice"))
        if "completed" in ev:
            out.append(("Carga AC completada", self.AC_COMPLETED_COLOR, "notice"))
        if "stop" in ev:
            out.append(("Carga AC detenida", self.AC_STOP_COLOR, "notice"))
        if "restored" in ev:
            out.append(("Electricidad Restablecida", self.AC_RESTORED_COLOR, "notice"))
        if "disconnected" in ev:
            out.append(("Red AC Desconectada", self.AC_DISCONNECTED_COLOR, "notice"))
        if "limsoc" in ev:
            out.append((f"Carga limitada por Protección: {self._ev_a.get('limsoc', '--')} A", self.LIM_SOC_COLOR, "notice"))
        if "limtemp" in ev:
            out.append((f"Carga limitada por Temperatura: {self._ev_a.get('limtemp', '--')} A", self.LIM_TEMP_COLOR, "notice"))
        if "limrest" in ev:
            out.append(("Restaurada Salida AC", self.LIM_RESTORED_COLOR, "notice"))
        return out

    def ack_all(self):
        self._ev.clear()   # los avisos se apagan solos al terminar su condicion; cerrarlos solo los oculta (ver VisState)


_ac_notice_singleton = _ac_NoticeState()


def _ac_get_notices():
    """Estado UNICO de avisos (batería baja / consumo alto): lo comparten la app y el widget,
    así los dos muestran lo mismo y cerrar el aviso en uno lo cierra en el otro."""
    return _ac_notice_singleton


_ac_SEV_NAMES = ("ok", "warning", "critical")


def _ac_alert_id(text):
    """Identifica el aviso por su texto (mismo criterio que _ac_cell_category)."""
    t = str(text).lower()
    if "salida ac apagada" in t:
        return "lim_prot"
    if "consumo al l" in t:
        return "lim_load"
    if "restaurada salida ac" in t:
        return "lim_restored"
    if "carga limitada por protec" in t:
        return "chg_lim_soc"
    if "carga limitada por temperatura" in t:
        return "chg_lim_temp"
    if "electricidad restablecida" in t:
        return "ac_restored"
    if "red ac desconectada" in t:
        return "ac_disconnected"
    if "carga ac completada" in t:
        return "ac_completed"
    if "carga ac iniciada" in t:
        return "ac_start"
    if "carga ac detenida" in t:
        return "ac_stop"
    if "apagado inmediato por bater" in t:
        return "bat_crit_3"
    if "batería cr" in t or "bateria cr" in t:
        return "bat_crit_5"  # 5% y 4% usan el mismo id base; el nivel está en el texto
    if "batería muy baja" in t or "bateria muy baja" in t:
        return "low_soc_10"
    if "protecci" in t:
        return "dc_prot"
    if "restaurada" in t:
        return "dc_restored"
    if "pico de arranque" in t:
        return "spike"
    if "reducida" in t:
        return "ac_reduced"
    if "muy caliente" in t:
        return "temp_crit"
    if "algo caliente" in t:
        return "temp_warn"
    if "celda cr" in t:
        return "cell_crit"
    if "fuera de rango" in t:
        return "cell_warn"
    if "dispersi" in t or "desbalance" in t:
        return "balance"
    if "descalibr" in t:
        return "calib"
    if "bajo voltaje" in t:
        return "v_crit"
    if "voltaje inestable" in t:
        return "v_warn"
    if "por consumo" in t:
        return "ac_cons"
    if "alta corriente dc" in t:
        return "dc_high"
    if "alta corriente ac" in t:
        return "ac_high"
    if "bater\u00eda baja" in t or "bateria baja" in t:
        return "low_soc"
    if "alto consumo" in t:
        return "load_high"
    return None


def _ac_vis_cfg(aid):
    """(segundos visible, deja triangulo) del aviso segun la configuracion."""
    sec, _n, dur, tri, _s = _ac_ALERT_META[aid]
    try:
        d = int(_ac_get(sec, aid + "_dur"))
    except Exception:
        d = dur
    t = False
    if tri is not None:
        try:
            t = bool(_ac_get(sec, aid + "_tri"))
        except Exception:
            t = tri
    return d, t


def _ac_event_secs(aid):
    """Cuanto dura un aviso puntual (pico, restaurada): su tiempo visible, o 24 h si es 'hasta cerrarlo'."""
    d = _ac_vis_cfg(aid)[0]
    return float(d) if d > 0 else 86400.0


def _ac_triangle_severity(items):
    """Severidad del triangulo a partir de TODOS los avisos activos: solo cuentan los que dejan triangulo."""
    best = 0
    for it in items:
        aid = _ac_alert_id(it[0])
        if aid is not None and _ac_vis_cfg(aid)[1]:
            best = max(best, _ac_ALERT_META[aid][4])
    return _ac_SEV_NAMES[best]


class _ac_VisState:
    """Que avisos se ven ahora. Una instancia por consumidor (widget / app): recuerda desde cuando esta
    activo cada aviso y cuales ha cerrado el usuario; al terminar la condicion se olvida de ellos."""

    def __init__(self):
        self.first = {}
        self.closed = set()

    def update(self, items):
        """Llamar con TODOS los avisos activos."""
        now = time.time()
        act = set()
        for it in items:
            aid = _ac_alert_id(it[0])
            if aid:
                act.add(aid)
                self.first.setdefault(aid, now)
        for aid in [a for a in self.first if a not in act]:
            del self.first[aid]
            self.closed.discard(aid)

    def visible(self, items, force=False):
        """Avisos a mostrar. force=True (el usuario toco el triangulo) incluye tambien los cerrados o
        vencidos que dejan triangulo."""
        now = time.time()
        out = []
        for it in items:
            aid = _ac_alert_id(it[0])
            if aid is None:
                out.append(it)
                continue
            dur, tri = _ac_vis_cfg(aid)
            gone = aid in self.closed or (dur > 0 and now - self.first.get(aid, now) >= dur)
            if not gone or (force and tri):
                out.append(it)
        return out

    def close(self, items):
        for it in items:
            aid = _ac_alert_id(it[0])
            if aid:
                self.closed.add(aid)

    def reset(self, aid):
        self.first.pop(aid, None)
        self.closed.discard(aid)


def _ac_temp_limits():
    """(aviso, alerta, histeresis) de temperatura de bateria en °C segun la configuracion."""
    try:
        return (float(_ac_get("dc_out_battery", "temp_warn_c")), float(_ac_get("dc_out_battery", "temp_crit_c")),
                float(_ac_get("dc_out_battery", "temp_hyst_c")))
    except Exception:
        return 43.0, 46.0, 0.5


alert_config = types.SimpleNamespace(
    SCHEMA=_ac_SCHEMA,
    section=_ac_section,
    get=_ac_get,
    defaults=_ac_defaults,
    set_enabled=_ac_set_enabled,
    set_values=_ac_set_values,
    save=_ac_save,
    validate=_ac_validate,
    apply_to_ble=_ac_apply_to_ble,
    mask_telemetry=_ac_mask_telemetry,
    current_limits=_ac_current_limits,
    gate_currents=_ac_gate_currents,
    widget_gate=_ac_widget_gate,
    tracker_view=_ac_tracker_view,
    blink_soc_low=_ac_blink_soc_low,
    blink_time_low=_ac_blink_time_low,
    filter_cell_alerts=_ac_filter_cell_alerts,
    filter_alert_lines=_ac_filter_alert_lines,
    NoticeState=_ac_NoticeState,
    notices=_ac_get_notices,
    VisState=_ac_VisState,
    triangle_severity=_ac_triangle_severity,
    event_secs=_ac_event_secs,
    alert_id=_ac_alert_id,
    ALERT_META=_ac_ALERT_META,
    ALERT_ON=_ac_ALERT_ON,
    temp_limits=_ac_temp_limits,
)


# UUIDs GATT Oficiales de Bluetti
BLUETTI_SERVICE_UUID = "0000ff00-0000-1000-8000-00805f9b34fb"
BLUETTI_NOTIFY_UUID  = "0000ff01-0000-1000-8000-00805f9b34fb"
BLUETTI_WRITE_UUID   = "0000ff02-0000-1000-8000-00805f9b34fb"

# Configuración por defecto para Bluetti Elite 100 V2
DEFAULT_MAC_ADDRESS  = "DC:B4:D9:54:78:1A"
KEX_MAGIC            = b"**"
AES_BLOCK_SIZE       = 16

# Registros Modbus RTU Oficiales V2
REG_BASE_STATUS      = 0x000A  # Registros base de telemetría (10)
REG_BASE_STATUS_LEN  = 34      # Longitud de palabras a leer (34 palabras = 68 bytes)
REG_CONTROL_AC       = 0x1771  # Registro 6001 - Inversor AC en Elite 100 V2 (Página 0x17 Offset 0x71)
REG_CONTROL_AC_LEG   = 0x0BBF  # Registro 3007 - Legacy AC
REG_CONTROL_DC       = 0x1772  # Registro 6002 - Salidas DC en Elite 100 V2 (Página 0x17 Offset 0x72)
REG_CONTROL_DC_LEG   = 0x0BC0  # Registro 3008 - Legacy DC
REG_CONTROL_ECO      = 0x1773  # Registro 6003 - Modo ECO en Elite 100 V2
REG_CONTROL_ECO_LEG  = 0x0B02  # Registro Legacy ECO
REG_CONTROL_PL       = 0x0B03  # Power Lifting (Modo Alta Potencia 2700W)
REG_CONTROL_CHARGE   = 0x0B04  # Velocidad de Carga AC (Silent, Standard, Turbo)
REG_CONTROL_GRID     = 2225    # Refuerzo de Red (Grid Enhancement): 0=apagado, 1=encendido (lectura y escritura 0x06).
                               # Hallado por escaneo comparativo grid_A/grid_B/grid_B2; el 0x0B05 anterior era una suposición.

# Registros de ESCRITURA confirmados para el EL100V2 (tabla de controles Patrick762 / esp32-nut-bluetti)
REG_W_SWITCH_AC      = 2011    # Encender/apagar salida AC (0/1) -- Función 0x06
REG_W_SWITCH_DC      = 2012    # Encender/apagar salida DC (0/1) -- Función 0x06
REG_W_SYSTEM_OFF     = 2013    # Apagado total del sistema (0/1) -- Función 0x06
REG_W_ECO_DC         = 2014    # Modo ECO del riel DC (0/1)
REG_W_ECO_AC         = 2017    # Modo ECO del riel AC (0/1)
REG_W_CHARGE_MODE    = 2020    # Perfil de carga: 0=standard, 1=silent, 2=turbo, 4=custom
REG_W_POWER_LIFTING  = 2021    # Power Lifting / elevación de potencia (0/1)

# Registros de LECTURA confirmados para la familia "V2 portable" (EL100V2 = Elite 100 V2)
REG_R_SOC            = 102     # State of Charge (0-100)
REG_R_RUNTIME_MIN    = 104     # Tiempo restante en minutos
REG_R_DC_OUTPUT_W    = 140     # Potencia real de salida DC (12V/USB) en Watts
REG_R_AC_OUTPUT_W    = 142     # Potencia real de salida AC (inversor) en Watts
REG_R_DC_INPUT_W     = 144     # Potencia real de entrada DC (Solar/Coche) en Watts
REG_R_AC_INPUT_W     = 146     # Potencia real de entrada AC (Red eléctrica) en Watts
REG_R_AC_INPUT_V     = 1314    # Voltaje de entrada AC, escala 0.1V
REG_R_AC_INPUT_A     = 1315    # Corriente de entrada AC, escala 0.1A
REG_R_AC_OUTPUT_V    = 1511    # Voltaje de salida AC, escala 0.1V
REG_R_SWITCH_AC      = 2011    # Estado real interruptor de salida AC (0/1)
REG_R_SWITCH_DC      = 2012    # Estado real interruptor de salida DC (0/1)
REG_R_AC_INPUT_HZ    = 1300    # Frecuencia de entrada AC, escala 0.1 Hz (0 si no hay red)
REG_R_AC_OUTPUT_HZ   = 1500    # Frecuencia de salida AC, escala 0.1 Hz (600 = 60.0 Hz)
REG_GRID_MAX_CURRENT = 2214    # Limite de corriente AC del modo de carga personalizado (A)
REG_DC_SOURCE        = 2269    # Byte bajo: 2=PV, 1=Otros. Byte alto: 1=alta corriente ON, 2/0=OFF

# --- Bloque del pack de baterias (PACK_MAIN_INFO 6000 / PACK_ITEM_INFO 6100 / PACK_SUB_PACK_INFO 6300) ---
REG_BATT_VOLT        = 6003    # Voltaje total del pack, escala 0.01 V (5292 = 52.92 V). Se lee junto con 6004.
                               # 6004 = corriente del banco (0.1 A), SIEMPRE POSITIVA (magnitud): vale igual al
                               # cargar que al descargar (CONFIRMADO con el registro del 4-5 oct). El sentido
                               # sale de la potencia neta (entrada - salida), no de este registro.
REG_CELLS_1          = 6302    # Celdas 1-8 (mV)
REG_CELLS_2          = 6310    # Celdas 9-16 (mV)
REG_TEMPS            = 6320    # Sensores de temperatura de celdas (4): valor = °C + 40 (79 -> 39 °C).
                               # Confirmado por comportamiento: varian 1-2 °C con el ambiente y la carga.
                               # (El Reg 6008 se descarto: se queda fijo en 34.)
CELL_COUNT           = 16
TEMP_OFFSET          = 40

# Capacidad del pack
REG_CAP_RATED        = 6131    # 20000 -> 20.000 Ah nominales (20 Ah x 51.2 V = 1024 Wh, coincide con la ficha)
# DESCARTADO (4-5 oct): 6318/6319 NO son capacidad. Cada palabra empaqueta dos sensores de temperatura
# (byte alto y bajo, valor = °C + 40); su media coincide con la temperatura de 6320-6323. Ya no se leen.
REG_UNKNOWN_6143     = 6143    # 83 -> 87 entre dos dias; probablemente otra temperatura (°C + 40). Solo dato crudo (CSV)
# Escaneos del 4-oct (bat_b / b77 / bat70). 2211/2212/2233 valen 0 (no sirven) y se dejaron de leer.
REG_BMS_LIMITS       = 6010    # 6010=568 (0.1 V) -> 56.8 V = 3.55 V/celda: posible tension objetivo de carga del BMS (SIN CONFIRMAR)
                               # 6011=40 y 6012=600: sin identificar
REG_BMS_ENERGY       = 6019    # 6019 = energia acumulada CARGADA, 6021 = DESCARGADA (3733 / 3213 -> 3220).
                               # Unidad ~10 Wh (0.01 kWh): +7 cuentas en 41 min a ~90 W = ~65 Wh.
ENERGY_COUNTER_WH    = 10.0
NOMINAL_PACK_V       = 51.2    # 16 celdas LiFePO4 x 3.2 V
RATED_WH             = 1024.0  # Capacidad nominal del Elite 100 V2
CHARGE_EFFICIENCY    = 0.855   # medido (4-5 oct, V x I del banco vs potencia neta): ~85,5 % de lo que entra llega a la bateria
                               # (incluye cargador AC + perdidas propias; antes 0.92, sobreestimaba)
CYCLE_LIFE_CYCLES    = 4000    # ficha oficial Bluetti: "4,000+ ciclos hasta el 80 % de la capacidad original"
CYCLE_LIFE_END_PCT   = 80

# --- Umbrales de celdas (LiFePO4), en mV ---
# Alertas por voltaje de celda: dos niveles. "warn" (ambar) avisa con margen; "crit" (rojo)
# es peligro real. La alerta se mantiene hasta que el voltaje vuelve CELL_ALERT_HYST_MV
# dentro del rango (histeresis) para que no parpadee.
CELL_WARN_HIGH_MV    = 3600    # aviso: celda alta (criterio del usuario; ojo: ya se vio 3.59 V en una carga normal)
CELL_CRIT_HIGH_MV    = 3650    # critica: maximo absoluto de una celda LiFePO4 (ficha tipica / proteccion de sobrevoltaje)
CELL_WARN_LOW_MV     = 2900    # aviso: celda baja
CELL_CRIT_LOW_MV     = 2600    # critica: celda muy baja (criterio del usuario; corte de ficha 2.5 V)
CELL_ALERT_HYST_MV   = 20

# Equilibrio entre celdas. La importancia del Δ depende de la zona de la curva LiFePO4:
#   meseta (~3.20-3.40 V): casi plana, un Δ alto delata resistencia/conexion/temperatura
#   codo superior / inferior: curva empinada, el Δ refleja desequilibrio real de carga
# Zona determinada por VOLTAJE (no por el % del equipo, que puede no cuadrar), con histeresis.
ZONE_TOP_MV          = 3420    # vmax por encima -> codo superior
ZONE_TOP_EXIT_MV     = 3400
ZONE_BOTTOM_MV       = 3100    # vmin por debajo -> codo inferior
ZONE_BOTTOM_EXIT_MV  = 3120
# Limites de Δ (mV) para cada grado, antes de compensar por potencia:
#   (optimo, bueno, aceptable, regular); por encima de "regular" = malo
# Referencias: celdas sanas en meseta <15 mV; un BMS no equilibra hasta ~3.40-3.45 V y >10-15 mV
# arriba "es mas que correcto"; en los codos la curva es empinada y el Δ crece de forma normal.
BALANCE_GRADES_MV = {"plateau": (12, 25, 40, 70), "top": (30, 50, 80, 120), "bottom": (40, 70, 100, 150)}
BALANCE_GRADE_NAMES = ("excellent", "good", "acceptable", "fair", "bad")
BALANCE_POWER_MV_PER_100W = 2.5   # el Δ tolerado sube 2.5 mV por cada 100 W de potencia neta (caida IR)...
BALANCE_POWER_MAX_MV      = 30.0  # ...con un tope
BALANCE_RELIABLE_MAX_W    = 1500  # con mas potencia el Δ esta demasiado distorsionado
BALANCE_CONFIRM_COUNT     = 12    # evaluaciones consecutivas (~30 s) para cambiar de estado (evita falsas alertas)
BALANCE_SETTLE_S          = 1800  # tras cargar a fondo las celdas tardan en asentarse: durante este tiempo no se avisa de dispersion/descalibracion
BALANCE_SETTLE_SOC        = 90    # se considera 'carga a fondo' si se carga (>=30 W netos) con SOC >= esto, o en el codo superior

# --- Salud: capacidad medida por conteo de energia ---
# Se integra la energia que entra/sale de la bateria mientras el SOC avanza CAP_WINDOW_SOC
# puntos (de transicion a transicion del entero, para minimizar el error de cuantizacion).
CAP_WINDOW_SOC       = 15      # puntos de SOC por ventana de medida
CAP_SOC_MIN          = 8       # fuera de [8, 95] % no se mide (extremos: carga CV / corte)
CAP_SOC_MAX          = 95
CAP_MIN_POWER_W      = 60      # potencia media minima (por debajo, el consumo propio distorsiona)
CAP_DISCHARGE_FACTOR = 1.13    # medido (4-5 oct): al descargar la bateria entrega ~13 % mas de lo que sale por AC/DC
                               # (inversor >90 % segun Bluetti + consumo propio ~10 W + perdidas DC); antes 1.08
CAP_MAX_GAP_S        = 15      # hueco de datos que invalida la ventana
CAP_VALID_RANGE_WH   = (500.0, 1500.0)
CAP_MAX_SAMPLES      = 12      # muestras historicas guardadas (mediana)
CALIB_LOW_SOC        = 10      # SOC "casi vacio" pero celdas altas -> posible descalibracion
CALIB_LOW_SOC_MIN_MV = 3300
CALIB_HIGH_SOC       = 98      # SOC "casi lleno" pero celdas bajas -> posible descalibracion
CALIB_HIGH_SOC_MAX_MV = 3280
CALIB_CONFIRM_COUNT  = 5       # evaluaciones consecutivas necesarias para declararla

# --- Escrituras DC / limite AC (primera fase con escritura) ---
GRID_LIMIT_MIN_A       = 1     # Rango del limite AC personalizado (A). SIN VERIFICAR contra el equipo:
GRID_LIMIT_MAX_A       = 15    # ajustar aqui si la app oficial permite otro rango.
DC_WRITE_MIN_INTERVAL_S = 5.0   # tiempo minimo entre dos escrituras al Reg 2269 (los cambios
                                # intermedios se ponen en cola y solo se aplica el ultimo)


# --- Protecciones automaticas por alta corriente ---
PROT_DC_ALERT_A         = 15.0    # entrada DC (panel) a partir de la cual hay alerta
PROT_DC_BAND_LOW_A      = 14.0    # regla A: fluctuar entre 14 y 15 A NO reinicia el temporizador
PROT_DC_A_SECONDS       = 180.0   # regla A: >=15 A sostenidos (3 min)
PROT_DC_B_LOW_A         = 11.0    # regla B: la corriente no debe bajar de aqui...
PROT_DC_B_SECONDS       = 600.0   #   ...durante 10 min (desde que llego a 15 A)
PROT_DC_B_DIP_TOL_S     = 5.0     # regla B: bajadas de menos de esto no cuentan
PROT_DC_REFRESH_S       = 60.0    # tiempo en modo "Otros" antes de volver a Panel
PROT_DC_RETRY_S         = 12.0    # reintento si el cambio de fuente no se confirma
PROT_DC_GIVEUP_S        = 45.0    # se abandona si el cambio a "Otros" nunca se confirma
PROT_AC_ALERT_A         = 12.0    # entrada AC a partir de la cual hay alerta
PROT_AC_RELEASE_A       = 11.5    # histeresis (igual que el widget)
PROT_AC_SECONDS         = 300.0   # 5 min sostenidos -> modo de carga Silencioso
PROT_MARKER_MAX_AGE_S   = 900.0   # un marcador de restauracion mas viejo se descarta
PROT_LIM_CHECK_S        = 30.0    # tras restaurar la salida AC: si el consumo sigue alto este tiempo, se apaga definitivamente
# --- Carga AC limitada por proteccion (% de bateria) y por temperatura ---
CHG_MIN_WRITE_S         = 10.0    # tiempo minimo entre dos cambios del limite de corriente
CHG_START_HOLD_S        = 6.0     # la regla debe cumplirse este tiempo antes de empezar a limitar
CHG_UP_HOLD_S           = 60.0    # para SUBIR el limite (antes del 90 %) la regla debe permitirlo este tiempo
CHG_NONE_RESTORE_S      = 60.0    # sin limite necesario durante tanto -> se vuelve al modo anterior
CHG_VERIFY_S            = 15.0    # tras escribir, tiempo antes de comprobar que el equipo tiene nuestro valor
CHG_RETRY_MAX           = 2       # reescrituras antes de dar el cambio por hecho por el usuario (otra app)
CHG_MARKER_MAX_AGE_S    = 43200.0 # sesion guardada en disco mas vieja que esto se descarta

# --- Muestreo rapido de potencia y deteccion de "Pico de Arranque" ---
# El bloque de potencias (Reg 140-147) se relee, intercalado entre las demas lecturas,
# como mucho cada FAST_POWER_INTERVAL_S (0 = desactivado). El pico se detecta sobre el
# CONSUMO = salida AC + salida DC (la entrada se ignora).
FAST_POWER_INTERVAL_S   = 0.5
SPIKE_BASE_WINDOW_S     = 6.0     # consumo "de antes": mediana de las muestras de los ultimos X s
SPIKE_MIN_RISE_W        = 100.0   # subida minima sobre ese consumo para que empiece un evento
SPIKE_MIN_PEAK_W        = 150.0   # pico minimo (sobre el consumo de antes) para notificar
SPIKE_FACTOR            = 2.0     # el pico debe ser >= FACTOR x el consumo que se queda sumado
SPIKE_S_FLOOR_W         = 40.0    # suelo del consumo sumado (picos que no dejan carga)
SPIKE_SETTLE_S          = 2.0     # el evento termina cuando los ultimos X s estan estables...
SPIKE_MAX_EVENT_S       = 8.0     # ...o a los X s como maximo
SPIKE_LOG_ENABLED       = True    # guarda cada evento en spike_log.csv para calibrar con datos reales
SPIKE_CHARGE_RISE_W     = 50.0    # una subida de la entrada AC (empieza a cargar / se conecta la red) de al menos esto...
SPIKE_MUTE_S            = 10.0    # ...silencia la deteccion de picos durante tanto tiempo (la lectura de salida se distorsiona)
SPIKE_MAX_HIGH_S        = 3.0     # un pico de arranque decae rapido: si pasa mas de esto por encima de la mitad del pico, es una carga (microondas, etc.)
AC_VOLT_MIN_PLAUSIBLE_V = 50.0    # una lectura de red entre 0 y 50 V es corrupta y se descarta

# --- Registro de salud individual de celdas (cell_events.csv + cell_stats.json) ---
CELL_LOG_ENABLED        = True
CELL_LOG_MIN_DELTA_MV   = 3       # con menos dispersión que esto, "la más baja/alta" es un empate técnico: no se registra
CELL_LOG_CONFIRM        = 3       # lecturas seguidas para dar por cambiada la celda más baja/alta (evita el parpadeo)
CELL_LOG_SAVE_S         = 60.0    # cada cuánto se guarda cell_stats.json (los eventos se guardan al instante)
CELL_LOG_MAX_CSV_BYTES  = 2_000_000


LINK_STALE_S           = 20.0     # sin ninguna respuesta Modbus valida durante tanto -> se fuerza reconexion
STALL_S                = 45.0     # el ciclo de conexion no avanza durante tanto (llamada colgada) -> se cancela y se reinicia


def _prot_marker_path() -> str:
    """Archivo pequeño en %LOCALAPPDATA%\\BluettiElite100Widget (misma carpeta que
    widget_config.json). Guarda (1) que hay que volver a Panel si la app se cerro o
    se perdio la conexion durante el minuto en modo Otros y (2) que hay un aviso
    de 'Alta corriente AC Reducida' que el usuario aun no ha cerrado."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "BluettiElite100Widget")
    try:
        os.makedirs(folder, exist_ok=True)
    except Exception:
        pass
    return os.path.join(folder, "protection_state.json")


# Alarma de red del equipo (escaneos alarma_A/B/B2). El significado del valor 2 es
# una HIPOTESIS basada en un solo evento: ver el log '[ALARMA RED]'.
REG_GRID_ALARM_A     = 126     # cambia 0 -> 2 con la alarma de red
REG_GRID_ALARM_B     = 1134    # cambia 0 -> 2 con la alarma de red (bloque INV_BASE_INFO)
REG_GRID_STATUS      = 161     # cambia 2 -> 1 con la alarma (significado sin confirmar)
GRID_LOW_VOLT_MASK   = 0x02    # bit 1 = 'voltaje de red bajo' (hipotesis)


def _build_read_request(address: int, quantity: int) -> bytes:
    """Construye una trama Modbus RTU 'Read Holding Registers' (0x03) a partir
    de una dirección de registro en DECIMAL."""
    return append_crc16(bytes([
        0x01, 0x03,
        (address >> 8) & 0xFF, address & 0xFF,
        (quantity >> 8) & 0xFF, quantity & 0xFF
    ]))


class ConnConstantsV2(Enum):
    LOCAL_AES_KEY = "459FC535808941F17091E0993EE3E93D"


class ECDHUtils(Enum):
    SECP_256R1_PUBLIC_PREFIX = "3059301306072a8648ce3d020106082a8648ce3d03010703420004"


class SignatureCrypt(Enum):
    PRIVATE_KEY_L1 = "4F19A16E3E87BDD9BD24D3E5495B88041511943CBC8B969ADE9641D0F56AF337"
    PUBLIC_KEY_K2  = "3059301306072a8648ce3d020106082a8648ce3d03010703420004A73ABF5D2232C8C1C72E68304343C272495E3A8FD6F30EA96DE2F4B3CE60B251EE21AC667CF8A71E18B46B664EAEFFE3C489F24F695B6411DB7E22CCC85A8594"


def hexsum(s: bytes, sz: int = 2) -> bytes:
    checksum = sum(s)
    as_hex = f"{checksum:0{sz * 2}x}"
    return bytes.fromhex(as_hex)


def hexxor(a: bytes, b: bytes) -> bytes:
    return bytes([x ^ y for x, y in zip(a, b)])


def calculate_crc16(data: bytes) -> int:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x0001:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc


def append_crc16(packet: bytes) -> bytes:
    crc = calculate_crc16(packet)
    return packet + bytes([crc & 0xFF, (crc >> 8) & 0xFF])


def verify_crc16(packet: bytes) -> bool:
    if len(packet) < 3:
        return False
    data = packet[:-2]
    expected_crc = calculate_crc16(data)
    received_crc = packet[-2] | (packet[-1] << 8)
    return expected_crc == received_crc


def aes_decrypt(data: bytes, aes_key: bytes, iv: Optional[bytes]) -> bytes:
    """Descifra una trama completa AES-CBC."""
    data_len = (data[0] << 8) + data[1]
    if iv is None:
        iv = hashlib.md5(data[2:6]).digest()
        encrypted = memoryview(data)[6:]
    else:
        encrypted = memoryview(data[2:])

    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    decryptor = cipher.decryptor()
    decrypted = decryptor.update(encrypted) + decryptor.finalize()
    return bytes(decrypted[:data_len])


def aes_encrypt(data: bytes, aes_key: bytes, iv: Optional[bytes]) -> bytes:
    """Cifra una trama para enviar a la estación con AES-CBC."""
    message_header = int.to_bytes(len(data), 2, "big")
    if iv is None:
        iv_seed = os.urandom(4)
        iv = hashlib.md5(iv_seed).digest()
        message_header += iv_seed

    padding = (AES_BLOCK_SIZE - len(data) % AES_BLOCK_SIZE) % AES_BLOCK_SIZE
    data_padded = data + bytes(padding)

    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    encryptor = cipher.encryptor()
    encrypted = encryptor.update(data_padded) + encryptor.finalize()
    return message_header + encrypted


def pubkey_to_bytes(pubkey: ec.EllipticCurvePublicKey) -> bytes:
    out = pubkey.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return out[1:]


def pubkey_from_bytes(data: bytes) -> ec.EllipticCurvePublicKey:
    encoded_peer_pubkey = bytes.fromhex(ECDHUtils.SECP_256R1_PUBLIC_PREFIX.value) + data
    return serialization.load_der_public_key(encoded_peer_pubkey)


def raw_ecdsa_to_der(sig: bytes) -> bytes:
    seq = univ.SequenceOf()
    seq.extend([
        univ.Integer(int.from_bytes(sig[:32], "big")),
        univ.Integer(int.from_bytes(sig[32:], "big")),
    ])
    return der_encoder.encode(seq)


def der_to_raw_ecdsa(sig: bytes) -> bytes:
    seq, remainder = der_decoder.decode(sig)
    return b"".join([int.to_bytes(int(x), 0x20, "big") for x in seq])


def verify_and_extract_signed_data(message: memoryview, signed_data_suffix: bytes) -> bytes:
    data = message[:64]
    signature = message[64:]
    signed_data = data.tobytes() + signed_data_suffix
    der_signature = raw_ecdsa_to_der(bytes(signature))
    key_bytes = bytes.fromhex(SignatureCrypt.PUBLIC_KEY_K2.value)
    try:
        serialization.load_der_public_key(key_bytes).verify(
            der_signature, signed_data, ec.ECDSA(hashes.SHA256())
        )
    except Exception as e:
        logger.warning(f"Aviso de firma ECDSA: {e} (Continuando con clave pública de estación)")
    return bytes(data)


class BluettiBleManager:
    """
    Gestor principal de conexión BLE para Bluetti Elite 100 V2.
    Realiza el intercambio de claves ECDH, cifra con AES-CBC y lee telemetría continua.
    Incluye reensamblado automático de fragmentos BLE y reintento de comandos.
    """

    def __init__(
        self,
        on_telemetry_callback: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_status_callback: Optional[Callable[[str], None]] = None,
        on_devices_discovered: Optional[Callable[[List[Dict[str, str]]], None]] = None,
        poll_interval: float = 1.5,
        default_mac: str = DEFAULT_MAC_ADDRESS
    ):
        self.on_telemetry = on_telemetry_callback
        self.on_status = on_status_callback
        self.on_devices = on_devices_discovered
        self.poll_interval = poll_interval
        self.device_address: str = default_mac

        self.client: Optional[BleakClient] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.worker_thread: Optional[threading.Thread] = None
        self.command_queue: Optional[asyncio.Queue] = None

        # Claves de Sesión Criptográfica
        self.unsecure_aes_key: Optional[bytes] = None
        self.unsecure_aes_iv: Optional[bytes] = None
        self.secure_aes_key: Optional[bytes] = None
        self.peer_pubkey: Optional[ec.EllipticCurvePublicKey] = None
        self.my_privkey: Optional[ec.EllipticCurvePrivateKey] = None
        self.my_pubkey: Optional[ec.EllipticCurvePublicKey] = None
        self.ecdh_ready_event: Optional[asyncio.Event] = None

        # Búferes de recepción y reensamblado
        self.rx_raw_buffer = bytearray()
        self.rx_modbus_buffer = bytearray()
        self._current_query_reg: Optional[int] = None
        self._current_query_future: Optional[asyncio.Future] = None
        self.is_running = False
        self.is_paused = False
        self._pending_disconnect_future: Optional[concurrent.futures.Future] = None
        self.simulation_mode = False
        self.last_rx_timestamp = time.time()
        self._last_dc_write = 0.0   # instante de la ultima escritura al Reg 2269
        # Cambio DC pendiente (cola de 1 elemento, "gana el ultimo"):
        #   {"source": "pv"|"other"|None, "high": True|False|None}
        # Lo escribe la interfaz (otro hilo) y lo consume el bucle BLE cuando
        # han pasado DC_WRITE_MIN_INTERVAL_S desde la ultima escritura.
        self._dc_pending: Optional[Dict[str, Any]] = None
        self._dc_pending_lock = threading.Lock()
        # Buffer de las 16 celdas (se rellena en dos lecturas de 8) y contador
        # de evaluaciones consecutivas sospechosas de descalibracion del SOC.
        self._cells_buf: List[Optional[int]] = [None] * CELL_COUNT
        self._calib_bad_count = 0
        # Equilibrio: zona actual, estado confirmado y candidato (antirrebote); alertas con histeresis
        self._cell_zone = "plateau"
        self._bal_candidate: Optional[str] = None
        self._bal_count = 0
        # Salud: ventana de medida por conteo de energia + muestras historicas persistentes
        self._cc: Optional[Dict[str, Any]] = None
        self._cap_dirs: List[int] = []   # sentido de cada muestra: +1 carga, -1 descarga, 0 desconocido (historicas)
        self._cap_samples: List[float] = self._load_cap_samples()
        self._capc: Dict[str, List[float]] = {"dis": [], "chg": []}   # capacidad por contador (contraste)
        self._register_snapshot_requests: queue.Queue[
            tuple[list[tuple[int, int]], concurrent.futures.Future]
        ] = queue.Queue()

        # Estado completo de la estación
        self.state: Dict[str, Any] = {
            # None hasta la primera lectura real del registro de batería --
            # antes quedaba en 84 como marcador, pero se veía como un dato
            # real mientras la app/widget seguían conectando.
            "soc": None,
            "runtime_minutes": None,
            "dc_input_watts": 0,
            "dc_input_volts": 0.0,
            "dc_input_volts_confirmed": False,
            "dc_input_current": 0.0,
            "ac_input_watts": 0,
            "ac_input_volts": 0.0,
            "ac_input_current": 0.0,
            "ac_output_watts": 0,
            "ac_output_volts": 0.0,
            "dc_output_watts": 0,
            "battery_volts": None,
            "battery_current": None,
            # --- Pack de baterias (Reg 6003 / 6302-6317 / 6320-6323 / capacidad) ---
            "pack_volts": None,            # V (Reg 6003 / 100)
            "batt_current_cand": None,     # A (Reg 6004 / 10), magnitud (siempre >= 0, confirmado)
            "batt_temps": [],              # °C de cada sensor (Reg 6320-6323 - 40)
            "batt_temp": None,             # °C, promedio de los sensores
            "batt_temp_min": None,
            "batt_temp_max": None,
            "cell_mv": None,               # lista de 16 valores en mV
            "cell_min_mv": None,
            "cell_max_mv": None,
            "cell_min_idx": None,          # 1..16
            "cell_max_idx": None,          # 1..16
            "cell_delta_mv": None,
            "cell_avg_mv": None,
            "cell_balance": None,          # "balanced" | "acceptable" | "slight" | "unbalanced"
            "cell_balance_reliable": False,
            "cell_alert": "ok",            # "ok" | "low" | "high" (critica)
            "cell_alert_cells": [],        # indices (1..16) de las celdas criticas
            "soc_calibration": "unknown",  # "unknown" | "ok" | "suspect"
            # --- Capacidad y salud estimadas (ver _update_capacity_coulomb) ---
            "cap_rated_ah": None,          # Reg 6131 / 1000
            "cap_est_wh": None,            # capacidad medida (mediana de muestras por energia, sin recortar)
            "cap_health_pct": None,        # salud estimada (puede superar 100)
            "cap_health_samples": 0,       # muestras de capacidad acumuladas (conteo de energia)
            "cap_window_progress": None,   # puntos de SOC avanzados en la ventana actual (de CAP_WINDOW_SOC)
            "cap_chg_wh": None, "cap_chg_n": 0,   # mediana de capacidad medida solo cargando
            "cap_dis_wh": None, "cap_dis_n": 0,   # idem solo descargando
            "cap_range_wh": None,          # (min, max) de las muestras
            "cell_zone": "plateau",        # "plateau" | "top" | "bottom"
            "cell_balance_limits": None,   # (optimo, bueno, aceptable, regular) en mV, ya compensados
            "cell_grade": None,            # "excellent" | "good" | "acceptable" | "fair" | "bad"
            "cell_peak_mv": None,          # celda mas alta vista en esta sesion
            "cell_trough_mv": None,        # celda mas baja vista en esta sesion
            "bms_v_target": None,          # Reg 6010 / 10 (V): posible tension objetivo de carga (sin confirmar)
            "bms_reg_6011": None, "bms_reg_6012": None,
            "energy_chg_raw": None, "energy_dis_raw": None,   # Reg 6019 / 6021 (cuentas de ~10 Wh)
            "energy_chg_kwh": None, "energy_dis_kwh": None, "cycles_eq": None,
            "cycles_chg_eq": None, "cycles_life_pct": None,   # ciclos equivalentes por carga / % de vida usada (ref. 4000)
            "cap_counter_dis_wh": None, "cap_counter_dis_n": 0,   # capacidad por contador (solo contraste)
            "cap_counter_chg_wh": None, "cap_counter_chg_n": 0,
            "cell_offset_mv": 0.0,         # compensacion aplicada por potencia
            "cell_warn": "ok",             # "ok" | "low" | "high" (nivel de aviso, ambar)
            "cell_warn_cells": [],
            "reg_6143": None,              # dato crudo sin identificar
            "ac_output_on": False,
            "dc_output_on": False,
            "eco_mode_on": False,
            "eco_dc_mode_on": False,
            "eco_ac_mode_on": False,
            "power_lifting_on": False,
            "grid_enhancement_on": False,
            "ac_input_hz": 0.0,
            "ac_output_hz": 0.0,
            "grid_max_current": None,
            "dc_source": None,
            "dc_source_raw": None,
            "dc_high_current_on": False,
            "charge_mode": "standard",
            "connected": False,
            "device_name": "No conectado",
            "device_address": self.device_address,
            "encryption_active": False,
            "last_update": time.strftime("%H:%M:%S")
        }
        self._prot_init()
        self._spike_init()
        self._cell_log_init()

    def _touch(self):
        """Marca progreso del ciclo de conexion (lo vigila _main_supervisor)."""
        self._progress_ts = time.time()

    async def _force_release(self):
        """Libera el enlace aunque el ciclo anterior se haya quedado colgado (todo con tiempo limite)."""
        c, self.client = self.client, None
        self.secure_aes_key = None
        self.unsecure_aes_key = None
        if c is not None:
            try:
                await asyncio.wait_for(c.disconnect(), timeout=6.0)
            except Exception as exc:
                logger.warning("Liberacion forzada del enlace: %s", exc)
        self.state["connected"] = False
        self.state["encryption_active"] = False
        self.state["device_name"] = "Desconectado"
        self._prot_on_disconnect()
        with self._dc_pending_lock:
            self._dc_pending = None
        fut = self._current_query_future
        if fut is not None and not fut.done():
            fut.cancel()
        self._notify_status("Reconectando (enlace sin respuesta)...")
        if self.on_telemetry:
            self.on_telemetry(dict(self.state))

    def start(self, device_address: Optional[str] = None):
        """Inicia el bucle en segundo plano."""
        if self.is_running:
            return
        if device_address:
            self.device_address = device_address
            self.state["device_address"] = device_address

        self.is_running = True
        self.is_paused = False
        self.worker_thread = threading.Thread(target=self._run_event_loop, daemon=True)
        self.worker_thread.start()
        logger.info(
            "Gestor BLE iniciado desde %s con destino: %s",
            os.path.abspath(__file__),
            self.device_address,
        )

    def stop(self):
        """Detiene limpiamente la conexión."""
        self.is_running = False
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._cleanup(), self.loop)

    def pause_and_release(self):
        """Libera temporalmente el Bluetooth para usar la app móvil."""
        self.is_paused = True
        logger.info("Liberando BLE para permitir uso en teléfono móvil...")
        self._notify_status("Desconectado (BLE liberado)")
        if self.loop and self.loop.is_running():
            # Se guarda el future en vez de dispararlo y olvidarlo: el bucle
            # principal (_main_supervisor) lo espera antes de intentar una
            # reconexión nueva. Si no se esperara, un clic rápido en
            # "Reconectar" podía hacer que el intento de reconexión arrancara
            # MIENTRAS esta desconexión todavía seguía en marcha -- las dos
            # corutinas pisándose self.client/self.secure_aes_key/los búfers
            # a la vez, dejando la reconexión colgada para siempre (parecía
            # "atascada en Conectando..." sin ningún error visible).
            self._pending_disconnect_future = asyncio.run_coroutine_threadsafe(self._disconnect_client(), self.loop)

    def resume_connection(self):
        """Reanuda la conexión oficial."""
        logger.info("Reanudando conexión con la estación...")
        self.is_paused = False
        self._touch()
        self._notify_status("Reconectando...")

    def request_register_snapshot(
        self, ranges: list[tuple[int, int]]
    ) -> concurrent.futures.Future:
        """Solicita una captura Modbus de solo lectura en el bucle BLE activo."""
        future: concurrent.futures.Future = concurrent.futures.Future()
        if not self.is_running or self.simulation_mode:
            future.set_exception(
                RuntimeError("La captura requiere el gestor BLE real en ejecución.")
            )
            return future

        for start, count in ranges:
            if not 0 <= start <= 0xFFFF or not 1 <= count <= 125:
                future.set_exception(
                    ValueError(f"Rango Modbus inválido: inicio={start}, cantidad={count}")
                )
                return future
            if start + count > 0x10000:
                future.set_exception(
                    ValueError(f"El rango Modbus excede 0xFFFF: inicio={start}, cantidad={count}")
                )
                return future

        if not ranges:
            future.set_exception(ValueError("Debe indicarse al menos un rango Modbus."))
            return future

        self._register_snapshot_requests.put((ranges, future))
        return future

    def set_device_address(self, address: str):
        """Actualiza la dirección MAC objetivo."""
        self.device_address = address
        self.state["device_address"] = address
        logger.info(f"Nueva dirección MAC asignada: {address}")
        if self.client and self.client.is_connected and self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._disconnect_client(), self.loop)

    def toggle_simulation_mode(self, enabled: Optional[bool] = None):
        """Activa el modo de prueba sin necesidad de hardware conectado."""
        if enabled is None:
            self.simulation_mode = not self.simulation_mode
        else:
            self.simulation_mode = enabled

        if self.simulation_mode:
            self.state["connected"] = True
            self.state["device_name"] = "Elite 100 V2 (Modo Demo)"
            self.state["encryption_active"] = True
            self._notify_status("Conectado (Modo Simulación)")
            if self.on_telemetry:
                self.on_telemetry(dict(self.state))
        else:
            self.state["connected"] = False
            self.state["device_name"] = "No conectado"
            self.state["encryption_active"] = False
            self._notify_status("Desconectado")

    # -------------------------------------------------------------------------
    # CONTROL DE HARDWARE Y CONMUTADORES (BLUETTI ELITE 100 V2)
    # -------------------------------------------------------------------------
    AC_HOLD_S = 10.0   # tras ordenar encender/apagar AC, las lecturas heuristicas (W, V) no pueden contradecir la orden

    def _ac_set(self, val: bool, src: str):
        """Unico punto donde las lecturas cambian ac_output_on. Durante el margen posterior a una orden del
        usuario se ignoran las lecturas que la contradicen (W y V tardan unos segundos en caer a 0 al apagar,
        y una lectura anterior a la escritura podia devolver el switch a su estado viejo)."""
        val = bool(val)
        if time.time() < getattr(self, "_ac_hold_until", 0.0) and val != getattr(self, "_ac_hold_val", val):
            logger.info("[AC] lectura %s=%s ignorada (orden reciente del usuario: %s)", src, val, self._ac_hold_val)
            return
        if self.state.get("ac_output_on") != val:
            logger.info("[AC] estado %s -> %s (origen=%s, V_sal=%s, W_sal=%s)", self.state.get("ac_output_on"), val, src,
                        self.state.get("ac_output_volts"), self.state.get("ac_output_watts"))
        self.state["ac_output_on"] = val

    def set_ac_output(self, turn_on: bool, _internal: bool = False):
        """Enciende o apaga el inversor AC (1800W) mediante Modbus 0x06."""
        if not _internal:
            self._prot_lim_user_override()
        self._ac_hold_val = bool(turn_on)
        self._ac_hold_until = time.time() + self.AC_HOLD_S
        self.state["ac_output_on"] = turn_on
        if self.simulation_mode:
            self.state["ac_output_watts"] = 150 if turn_on else 0
            if self.on_telemetry:
                self.on_telemetry(dict(self.state))
            return

        val = 0x0001 if turn_on else 0x0000
        # Función 0x06 (Write Single Register) -> Reg 2011, confirmado para EL100V2
        pkt_06_v2 = append_crc16(bytes([
            0x01, 0x06,
            (REG_W_SWITCH_AC >> 8) & 0xFF, REG_W_SWITCH_AC & 0xFF,
            (val >> 8) & 0xFF, val & 0xFF
        ]))
        self._enqueue_command(pkt_06_v2, f"Inversor AC (Reg 2011, 0x06) -> {'ENCENDIDO' if turn_on else 'APAGADO'}")

    def set_dc_output(self, turn_on: bool):
        """Enciende o apaga las salidas DC (USB-C 140W/100W y 12V) mediante Modbus 0x06."""
        self.state["dc_output_on"] = turn_on
        if self.simulation_mode:
            self.state["dc_output_watts"] = 35 if turn_on else 0
            if self.on_telemetry:
                self.on_telemetry(dict(self.state))
            return

        val = 0x0001 if turn_on else 0x0000
        # Función 0x06 (Write Single Register) -> Reg 2012, confirmado para EL100V2
        pkt_06_v2 = append_crc16(bytes([
            0x01, 0x06,
            (REG_W_SWITCH_DC >> 8) & 0xFF, REG_W_SWITCH_DC & 0xFF,
            (val >> 8) & 0xFF, val & 0xFF
        ]))
        self._enqueue_command(pkt_06_v2, f"Salidas DC (Reg 2012, 0x06) -> {'ENCENDIDO' if turn_on else 'APAGADO'}")

    def set_system_power_off(self):
        """Apaga completamente el sistema (registro 2013). Acción irreversible."""
        logger.warning("APAGADO TOTAL DEL SISTEMA (REG 2013)")
        if self.simulation_mode:
            self.state["ac_output_on"] = False
            self.state["dc_output_on"] = False
            self.state["ac_output_watts"] = 0
            self.state["dc_output_watts"] = 0
            if self.on_telemetry:
                self.on_telemetry(dict(self.state))
            return

        val = 0x0001
        # Función 0x06 (Write Single Register) -> Reg 2013
        pkt_06_v2 = append_crc16(bytes([
            0x01, 0x06,
            (REG_W_SYSTEM_OFF >> 8) & 0xFF, REG_W_SYSTEM_OFF & 0xFF,
            (val >> 8) & 0xFF, val & 0xFF
        ]))
        self._enqueue_command(pkt_06_v2, "Apagado total del sistema (Reg 2013, 0x06)")

    def set_eco_mode(self, turn_on: bool):
        """Activa o desactiva el modo de ahorro ECO."""
        self.state["eco_mode_on"] = turn_on
        self.state["eco_dc_mode_on"] = turn_on
        self.state["eco_ac_mode_on"] = turn_on
        if self.simulation_mode:
            if self.on_telemetry:
                self.on_telemetry(dict(self.state))
            return

        val = 0x0001 if turn_on else 0x0000
        pkt_dc = append_crc16(bytes([
            0x01, 0x06,
            (REG_W_ECO_DC >> 8) & 0xFF, REG_W_ECO_DC & 0xFF,
            (val >> 8) & 0xFF, val & 0xFF
        ]))
        self._enqueue_command(pkt_dc, f"Modo ECO DC (Reg 2014) -> {'ACTIVADO' if turn_on else 'DESACTIVADO'}")

        pkt_ac = append_crc16(bytes([
            0x01, 0x06,
            (REG_W_ECO_AC >> 8) & 0xFF, REG_W_ECO_AC & 0xFF,
            (val >> 8) & 0xFF, val & 0xFF
        ]))
        self._enqueue_command(pkt_ac, f"Modo ECO AC (Reg 2017) -> {'ACTIVADO' if turn_on else 'DESACTIVADO'}")

    def set_power_lifting(self, turn_on: bool):
        """Activa o desactiva el modo de elevación de potencia (2700W resistivos)."""
        self.state["power_lifting_on"] = turn_on
        if self.simulation_mode:
            if self.on_telemetry:
                self.on_telemetry(dict(self.state))
            return

        val = 0x0001 if turn_on else 0x0000
        packet = append_crc16(bytes([
            0x01, 0x06,
            (REG_W_POWER_LIFTING >> 8) & 0xFF, REG_W_POWER_LIFTING & 0xFF,
            (val >> 8) & 0xFF, val & 0xFF
        ]))
        self._enqueue_command(packet, f"Power Lifting (Reg 2021) -> {'ACTIVADO' if turn_on else 'DESACTIVADO'}")

    def set_charge_mode(self, mode: str, _internal: bool = False):
        """Cambia el perfil de carga AC ('silent', 'standard', 'turbo'). _internal=True solo lo usa la proteccion de carga."""
        if not _internal:
            self._chg_user_override()   # un cambio manual cancela la carga limitada (no se restaura nada)
        self.state["charge_mode"] = mode
        if self.simulation_mode:
            if mode == "silent":
                self.state["ac_input_watts"] = 400
            elif mode == "standard":
                self.state["ac_input_watts"] = 800
            else:
                self.state["ac_input_watts"] = 1200
            self.state["ac_input_current"] = round(self.state["ac_input_watts"] / 120.0, 1)
            if self.on_telemetry:
                self.on_telemetry(dict(self.state))
            return

        mode_val = {"standard": 0x0000, "silent": 0x0001, "turbo": 0x0002, "custom": 0x0004}.get(mode, 0x0000)
        packet = append_crc16(bytes([
            0x01, 0x06,
            (REG_W_CHARGE_MODE >> 8) & 0xFF, REG_W_CHARGE_MODE & 0xFF,
            (mode_val >> 8) & 0xFF, mode_val & 0xFF
        ]))
        self._enqueue_command(packet, f"Perfil de Carga (Reg 2020) -> {mode.upper()}")

    def set_grid_enhancement(self, turn_on: bool):
        """Activa o desactiva el refuerzo de red (adaptación automática para generador o red inestable)."""
        self.state["grid_enhancement_on"] = turn_on
        if self.simulation_mode:
            if self.on_telemetry:
                self.on_telemetry(dict(self.state))
            return

        val = 0x0001 if turn_on else 0x0000
        packet = append_crc16(bytes([
            0x01, 0x06,
            (REG_CONTROL_GRID >> 8) & 0xFF, REG_CONTROL_GRID & 0xFF,
            (val >> 8) & 0xFF, val & 0xFF
        ]))
        self._enqueue_command(packet, f"Refuerzo de Red -> {'ACTIVADO' if turn_on else 'DESACTIVADO'}")

    # -------------------------------------------------------------------------
    # ESCRITURAS: FUENTE DC / ALTA CORRIENTE (Reg 2269) Y LIMITE AC (Reg 2214)
    # Protecciones del Reg 2269: se relee el valor FRESCO antes de escribir, solo se
    # cambia el byte pedido (el otro se conserva), no se escribe sobre valores
    # desconocidos, alta corriente solo con fuente PV, intervalo minimo entre
    # escrituras (DC_WRITE_MIN_INTERVAL_S) y relectura posterior para verificar.
    #
    # COLA: si se pide un cambio antes de que pase el intervalo minimo, NO se
    # descarta ni se avisa: se guarda en una cola de un solo elemento (gana el
    # ultimo pedido) y el bucle de polling lo aplica en cuanto toca. Asi varios
    # clics seguidos (Otros -> Panel -> Otros) terminan en UNA sola escritura,
    # y la lectura de datos nunca se bloquea esperando.
    # -------------------------------------------------------------------------
    def set_dc_source(self, source: str, _internal: bool = False):
        """source: 'pv' | 'other'. _internal=True solo lo usa la proteccion automatica."""
        if source not in ("pv", "other"):
            return
        if not _internal:
            self._prot_user_override()   # un cambio manual cancela la proteccion DC en curso
        if self.simulation_mode:
            self.state["dc_source"] = source
            if source != "pv":
                self.state["dc_high_current_on"] = False
            if self.on_telemetry:
                self.on_telemetry(dict(self.state))
            return
        with self._dc_pending_lock:
            pend = dict(self._dc_pending or {"source": None, "high": None})
            pend["source"] = source
            if source == "other":
                pend["high"] = None   # la alta corriente solo existe con Panel Solar
            self._dc_pending = pend
        logger.info("Cambio DC en cola: fuente -> %s", source.upper())

    def set_dc_high_current(self, turn_on: bool):
        if self.simulation_mode:
            if self.state.get("dc_source") == "pv":
                self.state["dc_high_current_on"] = bool(turn_on)
            if self.on_telemetry:
                self.on_telemetry(dict(self.state))
            return
        with self._dc_pending_lock:
            pend = dict(self._dc_pending or {"source": None, "high": None})
            if pend.get("source") == "other":
                # Hay un cambio a "Otros" en cola: la alta corriente no aplica.
                logger.info("Alta corriente ignorada: hay un cambio a fuente 'Otros' en cola")
                return
            pend["high"] = bool(turn_on)
            self._dc_pending = pend
        logger.info("Cambio DC en cola: alta corriente -> %s", "ON" if turn_on else "OFF")

    def set_grid_max_current(self, amps: int, _internal: bool = False):
        if not _internal:
            self._chg_user_override()
        amps = int(max(GRID_LIMIT_MIN_A, min(GRID_LIMIT_MAX_A, amps)))
        if self.simulation_mode:
            self.state["grid_max_current"] = amps
            if self.on_telemetry:
                self.on_telemetry(dict(self.state))
            return
        self._enqueue_command(
            lambda: self._write_register_verified(REG_GRID_MAX_CURRENT, amps),
            f"Limite AC personalizado (Reg 2214) -> {amps} A",
        )

    # -------------------------------------------------------------------------
    # PROTECCIONES AUTOMATICAS POR ALTA CORRIENTE
    #   DC regla A: >=15 A durante 3 min seguidos (fluctuar 14-15 A no reinicia)
    #   DC regla B: desde que llega a 15 A, no baja de 11 A durante 10 min
    #               (bajadas de menos de 5 s no cuentan; mas tiempo reinicia)
    #   -> fuente "Otros" durante 1 min y vuelta automatica a Panel (alta
    #      corriente siempre OFF al volver).
    #   AC: >=12 A sostenidos 5 min -> modo de carga Silencioso (permanente);
    #       si ya estaba en Silencioso y sigue alto otros 5 min -> aviso "por Consumo".
    # Se evalua una vez por ciclo de polling (cada ~2-3 s). Los avisos los muestran
    # el widget y la app leyendo las claves prot_* de self.state.
    # -------------------------------------------------------------------------
    def _prot_init(self):
        self._prot_path = _prot_marker_path()
        self._prot_dc_phase = "idle"       # idle | switching | refreshing | restoring
        self._prot_dc_begin_ts = 0.0
        self._prot_dc_req_ts = 0.0
        self._prot_a_since: Optional[float] = None
        self._prot_b_since: Optional[float] = None
        self._prot_b_dip_since: Optional[float] = None
        self._prot_ac_since: Optional[float] = None
        self._prot_ac_high = False
        self._prot_ac_consump_fired = False
        self._prot_lim_phase = "idle"      # idle | off (salida AC apagada por la proteccion) | restoring | check | final
        self._prot_lim_since: Optional[float] = None
        self._prot_lim_end_ts = 0.0
        self._prot_lim_req_ts = 0.0
        # Batería Crítica: 5% -> 4% -> 3%
        self._prot_bat_crit_phase = "idle"  # idle | armed_5 | armed_4 | armed_3 | countdown | shutdown
        self._prot_bat_crit_level = 0       # 5, 4, 3
        self._prot_bat_crit_end_ts = 0.0
        self._prot_bat_crit_fire_ts = 0.0
        self._prot_bat_crit_dismissed = False
        self._prot_marker: Dict[str, Any] = self._prot_load_marker()
        self._chg_init()
        self.state.update({
            "prot_dc_active": False,        # hay una proteccion DC en curso (cuenta atras)
            "prot_dc_end_ts": None,         # instante (time.time) en que termina el minuto en "Otros"
            "prot_dc_reason": None,         # "A" | "B" | "restore"
            "prot_dc_restored_ts": 0.0,     # instante de la ultima restauracion a Panel
            "prot_ac_reduced_pending": bool(self._prot_marker.get("ac_notice_pending", False)),
            "prot_ac_reduced_ts": 0.0,      # instante del ultimo cambio automatico a Silencioso
            "prot_ac_consumption_ts": 0.0,  # instante del ultimo aviso "Alta corriente por Consumo"
            "prot_lim_active": False,       # salida AC apagada por consumo al limite (cuenta atras)
            "prot_lim_end_ts": None,        # instante (time.time) en que vuelve la salida AC
            "prot_lim_fire_ts": 0.0,        # instante de la ultima activacion de la proteccion
            "prot_lim_restored_ts": 0.0,    # instante de la ultima restauracion de la salida AC
            "prot_bat_crit_active": False,  # protección batería crítica activa
            "prot_bat_crit_level": 0,       # 5, 4, o 3
            "prot_bat_crit_end_ts": None,   # fin de cuenta atrás (1 min)
            "prot_bat_crit_fire_ts": 0.0,   # instante de activación
        })

    def _prot_load_marker(self) -> Dict[str, Any]:
        try:
            with open(self._prot_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return {}
        except Exception:
            return {}
        ts = data.get("dc_restore_ts")
        if ts is not None and (not isinstance(ts, (int, float)) or time.time() - ts > PROT_MARKER_MAX_AGE_S):
            data.pop("dc_restore_ts", None)
        return data

    def _prot_save_marker(self):
        try:
            clean = {k: v for k, v in self._prot_marker.items() if v not in (None, False)}
            with open(self._prot_path, "w", encoding="utf-8") as f:
                json.dump(clean, f)
        except Exception:
            pass

    def ack_ac_reduced_notice(self):
        """El usuario cerro (x) el aviso 'Alta corriente AC Reducida'."""
        self.state["prot_ac_reduced_pending"] = False
        self._prot_marker.pop("ac_notice_pending", None)
        self._prot_save_marker()

    def ack_bat_crit_notice(self, level: int):
        """El usuario cerró el aviso de batería crítica (5% o 4%) para postergar al siguiente nivel."""
        if level in (5, 4) and self._prot_bat_crit_phase == f"countdown_{level}":
            logger.info("BATERÍA CRÍTICA %d%%: usuario pospuso aviso -> siguiente nivel", level)
            self._prot_bat_crit_dismissed = True

    def _prot_reset_dc_timers(self):
        self._prot_a_since = None
        self._prot_b_since = None
        self._prot_b_dip_since = None

    def _prot_dc_finish(self, restored: bool):
        self._prot_dc_phase = "idle"
        self.state["prot_dc_active"] = False
        self.state["prot_dc_end_ts"] = None
        self.state["prot_dc_reason"] = None
        self._prot_marker.pop("dc_restore_ts", None)
        self._prot_save_marker()
        self._prot_reset_dc_timers()
        if restored:
            self.state["prot_dc_restored_ts"] = time.time()

    def _prot_user_override(self):
        """Cambio manual de la fuente DC (app/widget): se cancela la proteccion
        y NO se fuerza la vuelta a Panel."""
        if getattr(self, "_prot_dc_phase", "idle") != "idle":
            logger.info("Proteccion DC cancelada: cambio manual de la fuente DC")
            self._prot_dc_finish(restored=False)

    def _prot_on_disconnect(self):
        """Sin conexion no se puede escribir: se detienen los temporizadores pero el
        marcador en disco se conserva, para volver a Panel al reconectar."""
        self._prot_dc_phase = "idle"
        self.state["prot_dc_active"] = False
        self.state["prot_dc_end_ts"] = None
        self.state["prot_dc_reason"] = None
        self._prot_reset_dc_timers()
        self._prot_ac_since = None
        self._prot_ac_high = False
        self._prot_ac_consump_fired = False
        self._prot_lim_phase = "idle"
        self._prot_lim_since = None
        self.state["prot_lim_active"] = False
        self.state["prot_lim_end_ts"] = None
        self._prot_bat_crit_phase = "idle"
        self._prot_bat_crit_level = 0
        self._prot_bat_crit_end_ts = 0.0
        self._prot_bat_crit_fire_ts = 0.0
        self._prot_bat_crit_dismissed = False
        self.state["prot_bat_crit_active"] = False
        self.state["prot_bat_crit_level"] = 0
        self.state["prot_bat_crit_end_ts"] = None
        self.state["prot_bat_crit_fire_ts"] = 0.0
        self._spike_reset()
        self._chg_full_since = None
        self._chg_noac_since = None
        self._chg_none_since = None
        self._chg_up_since = None
        self._chg_start_since = None
        self._chg_marker_done = False

    # -------------------------------------------------------------------------
    # CARGA AC LIMITADA POR PROTECCION (% de bateria) Y POR TEMPERATURA
    #   Cerca del 100 % (o con la bateria caliente) se pasa al modo de carga Personalizado y se baja el limite de
    #   corriente de entrada AC por escalones (por defecto 4 / 3 / 2 A; con alerta de temperatura, 1 A).
    #   Fin: 100 % mantenido chgp_end_s, o sin red AC chgp_idle_min -> se restaura el modo (y limite) anteriores.
    #   Un cambio manual del modo o del limite cancela la proteccion sin restaurar nada.
    #   Los avisos salen por state["chg_lim_*"] (los muestran el widget y la app).
    # -------------------------------------------------------------------------
    def _chg_init(self):
        self._chg_active = False
        self._chg_prev: Optional[Dict[str, Any]] = None   # {"mode": modo anterior, "amps": limite anterior}
        self._chg_applied: Optional[int] = None
        self._chg_reason: Optional[str] = None
        self._chg_armed = True          # tras terminar una carga completa no se vuelve a limitar hasta una carga nueva
        self._chg_ratchet = False       # desde el tercer escalon el limite ya no vuelve a subir
        self._chg_tlevel = "ok"
        self._chg_last_write = 0.0
        self._chg_retries = 0
        self._chg_full_since = None
        self._chg_noac_since = None
        self._chg_none_since = None
        self._chg_up_since = None
        self._chg_start_since = None
        self._chg_marker_done = False
        self.state.update({
            "chg_lim_a": None,          # limite de corriente aplicado por la proteccion (None = sin proteccion)
            "chg_lim_reason": None,     # "soc" | "temp"
            "chg_lim_ts": 0.0,          # instante del ultimo cambio de limite (aviso)
            "chg_lim_ev_a": 0,          # amperios del ultimo cambio
            "chg_lim_ev_kind": None,    # motivo del ultimo cambio: "soc" | "temp"
        })

    def _chg_temp_level(self, temp) -> str:
        """ok / warn / crit con histeresis (mismo criterio que el aviso de temperatura de la app)."""
        if not isinstance(temp, (int, float)):
            self._chg_tlevel = "ok"
            return "ok"
        warn, crit, hyst = alert_config.temp_limits()
        lvl = self._chg_tlevel
        new = None
        if lvl == "crit":
            if temp > crit - hyst:
                new = "crit"
        elif temp >= crit:
            new = "crit"
        if new is None:
            if lvl in ("warn", "crit"):
                new = "warn" if temp > warn - hyst else "ok"
            else:
                new = "warn" if temp >= warn else "ok"
        self._chg_tlevel = new
        return new

    @staticmethod
    def _chg_plan(soc, warn: bool, crit: bool, b) -> Optional[int]:
        """Limite (A) que corresponde al % de bateria y al estado de temperatura; None = sin limite."""
        if soc >= b["chgp_soc4"]:
            r = b["chgp_a3"]
        elif soc >= b["chgp_soc3"]:
            r = b["chgp_a3"] if warn else b["chgp_a2"]
        elif soc >= b["chgp_soc2"]:
            r = b["chgp_a2"] if warn else b["chgp_a1"]
        elif soc >= b["chgp_soc_start"]:
            r = b["chgp_a1"]
        elif soc >= b["chgp_soc_warn"] and warn:
            r = b["chgp_a1"]
        else:
            return None
        if crit and soc >= b["chgp_soc3"]:
            r = min(r, b["chgp_a_alert"])
        return int(r)

    def _chg_write(self, amps: int):
        if self.state.get("charge_mode") != "custom":
            self.set_charge_mode("custom", _internal=True)
        self.set_grid_max_current(amps, _internal=True)
        self._chg_last_write = time.time()

    def _chg_set(self, amps: int, reason: str, now: float):
        self._chg_write(amps)
        changed = amps != self._chg_applied
        self._chg_applied, self._chg_reason = amps, reason
        st = self.state
        st["chg_lim_a"], st["chg_lim_reason"] = amps, reason
        if changed:
            st["chg_lim_ev_a"], st["chg_lim_ev_kind"], st["chg_lim_ts"] = amps, reason, now
        prev = self._chg_prev or {}
        self._prot_marker["chg_restore"] = {"mode": prev.get("mode"), "amps": prev.get("amps"), "applied": amps, "ts": now}
        self._prot_save_marker()
        logger.warning("CARGA AC LIMITADA a %d A (motivo: %s)", amps, "temperatura" if reason == "temp" else "proteccion")

    def _chg_clear(self):
        self._chg_active = False
        self._chg_prev = None
        self._chg_applied = None
        self._chg_reason = None
        self._chg_ratchet = False
        self._chg_retries = 0
        self._chg_full_since = self._chg_noac_since = self._chg_none_since = None
        self._chg_up_since = self._chg_start_since = None
        self.state["chg_lim_a"] = None
        self.state["chg_lim_reason"] = None
        self._prot_marker.pop("chg_restore", None)
        self._prot_save_marker()

    def _chg_end(self, why: str):
        """Fin de la proteccion: se vuelve al modo de carga (y limite) de antes."""
        prev = self._chg_prev or {}
        mode, amps = prev.get("mode"), prev.get("amps")
        logger.warning("CARGA AC: fin de la proteccion (%s); se restaura el modo anterior: %s %s", why, mode, amps or "")
        if self.state.get("connected"):
            if mode and mode != "custom":
                self.set_charge_mode(mode, _internal=True)
            elif mode == "custom" and isinstance(amps, (int, float)) and amps > 0:
                self.set_grid_max_current(int(amps), _internal=True)
        if why == "completa":
            self._chg_armed = False
        self._chg_clear()

    def _chg_user_override(self):
        if getattr(self, "_chg_active", False):
            logger.info("Carga AC limitada cancelada: cambio manual del modo / limite de carga")
            self._chg_armed = False
            self._chg_clear()

    def _prot_tick_chg(self):
        st = self.state
        if not st.get("connected"):
            return
        soc = st.get("soc")
        if not isinstance(soc, (int, float)):
            return
        now = time.time()
        b = _ac_section("dc_out_battery")
        volts = st.get("ac_input_volts") or 0.0
        has_ac = volts > 0 or (st.get("ac_input_watts") or 0) > 5

        # Reanudar una sesion guardada (la app se cerro o se perdio la conexion con la proteccion activa)
        if not self._chg_marker_done:
            m = self._prot_marker.get("chg_restore")
            if self._chg_active or not isinstance(m, dict):
                self._chg_marker_done = True
            elif st.get("grid_max_current") is not None and st.get("charge_mode") is not None:
                self._chg_marker_done = True
                if (st.get("charge_mode") == "custom" and st.get("grid_max_current") == m.get("applied")
                        and now - float(m.get("ts") or 0.0) < CHG_MARKER_MAX_AGE_S):
                    self._chg_active = True
                    self._chg_prev = {"mode": m.get("mode"), "amps": m.get("amps")}
                    self._chg_applied = int(m["applied"])
                    self._chg_last_write = now
                    self._chg_ratchet = soc >= b["chgp_soc3"]
                    st["chg_lim_a"] = self._chg_applied
                    logger.info("Carga AC limitada: se reanuda la proteccion guardada (%d A)", self._chg_applied)
                else:
                    self._prot_marker.pop("chg_restore", None)
                    self._prot_save_marker()

        if not b["chgp_on"]:
            if self._chg_active:
                self._chg_end("desactivada")
            return
        if not self._chg_armed and soc < b["chgp_soc_warn"] - 2:
            self._chg_armed = True    # bateria baja de nuevo: carga nueva

        lvl = self._chg_temp_level(st.get("batt_temp"))
        use_t = bool(b["chgt_on"])
        warn, crit = use_t and lvl in ("warn", "crit"), use_t and lvl == "crit"
        lim = self._chg_plan(soc, warn, crit, b)
        lim_soc = self._chg_plan(soc, False, False, b)
        reason = "temp" if (lim is not None and (lim_soc is None or lim < lim_soc)) else "soc"

        if not self._chg_active:
            if self._chg_armed and has_ac and lim is not None:
                if self._chg_start_since is None:
                    self._chg_start_since = now
                if now - self._chg_start_since >= CHG_START_HOLD_S and st.get("charge_mode") is not None:
                    prev_mode, prev_amps = st.get("charge_mode"), st.get("grid_max_current")
                    sp, sa = st.get("smart_prev"), st.get("smart_applied")
                    if isinstance(sp, dict) and sa is not None and prev_mode == "custom" and prev_amps == sa:
                        # ese limite lo puso la Carga inteligente (temporal): lo que el usuario tenia es lo de antes de ella
                        prev_mode, prev_amps = sp.get("mode") or prev_mode, sp.get("amps") if sp.get("mode") else prev_amps
                    self._chg_prev = {"mode": prev_mode, "amps": prev_amps}
                    target = lim
                    if prev_mode == "custom" and isinstance(prev_amps, (int, float)) and prev_amps > 0:
                        target = min(lim, int(prev_amps))   # nunca por encima de lo que ya tenia configurado
                    self._chg_active = True
                    self._chg_ratchet = soc >= b["chgp_soc3"]
                    self._chg_set(target, reason, now)
            else:
                self._chg_start_since = None
            return

        # --- sesion activa ---
        if soc >= 100:
            if self._chg_full_since is None:
                self._chg_full_since = now
            if now - self._chg_full_since >= b["chgp_end_s"]:
                self._chg_end("completa")
                return
        else:
            self._chg_full_since = None
        if not has_ac:
            if self._chg_noac_since is None:
                self._chg_noac_since = now
            if now - self._chg_noac_since >= b["chgp_idle_min"] * 60:
                self._chg_end("sin red")
                return
        else:
            self._chg_noac_since = None
        if lim is None:
            if self._chg_none_since is None:
                self._chg_none_since = now
            if now - self._chg_none_since >= CHG_NONE_RESTORE_S:
                self._chg_end("sin limite necesario")
            return
        self._chg_none_since = None

        # el equipo debe tener nuestro valor; si no, se reescribe y, si sigue distinto, lo cambio otra app: se cancela
        if now - self._chg_last_write > CHG_VERIFY_S:
            gm = st.get("grid_max_current")
            if st.get("charge_mode") != "custom" or (gm is not None and gm != self._chg_applied):
                self._chg_retries += 1
                if self._chg_retries > CHG_RETRY_MAX:
                    logger.info("Carga AC limitada cancelada: el modo / limite lo cambio otra aplicacion")
                    self._chg_armed = False
                    self._chg_clear()
                    return
                self._chg_write(self._chg_applied)
            else:
                self._chg_retries = 0

        target = lim
        prev = self._chg_prev or {}
        if prev.get("mode") == "custom" and isinstance(prev.get("amps"), (int, float)) and prev["amps"] > 0:
            target = min(target, int(prev["amps"]))
        if soc >= b["chgp_soc3"]:
            self._chg_ratchet = True
        applied = self._chg_applied if self._chg_applied is not None else target
        if self._chg_ratchet and target > applied:
            target = applied
        if now - self._chg_last_write < CHG_MIN_WRITE_S:
            return
        if target < applied:
            self._chg_up_since = None
            self._chg_set(target, reason, now)
        elif target > applied:
            if self._chg_up_since is None:
                self._chg_up_since = now
            elif now - self._chg_up_since >= CHG_UP_HOLD_S:
                self._chg_up_since = None
                self._chg_set(target, reason, now)
        else:
            self._chg_up_since = None

    def _protection_tick(self):
        try:
            self._prot_tick_dc()
            self._prot_tick_ac()
            self._prot_tick_lim()
            self._prot_tick_bat_crit()
            self._prot_tick_chg()
        except Exception as exc:
            logger.warning("Error en protecciones de alta corriente: %s", exc)

    def _prot_dc_start(self, rule: str):
        now = time.time()
        logger.warning("PROTECCION ALTA CORRIENTE DC (regla %s): fuente 'Otros' durante %d s", rule, PROT_DC_REFRESH_S)
        self._prot_dc_phase = "switching"
        self._prot_dc_begin_ts = now
        self._prot_dc_req_ts = now
        self.state["prot_dc_active"] = True
        self.state["prot_dc_end_ts"] = None     # la cuenta atras empieza al confirmarse el cambio
        self.state["prot_dc_reason"] = rule
        self._prot_marker["dc_restore_ts"] = now
        self._prot_marker["dc_restore_hc"] = bool(self.state.get("dc_high_current_on"))  # [alertas-patch]
        self._prot_save_marker()
        self._prot_reset_dc_timers()
        self.set_dc_source("other", _internal=True)

    def _prot_tick_dc(self):
        st = self.state
        now = time.time()
        src = st.get("dc_source")
        phase = self._prot_dc_phase

        if phase == "idle":
            # Restauracion pendiente de una sesion anterior (app cerrada o conexion
            # perdida durante el minuto en "Otros").
            if self._prot_marker.get("dc_restore_ts") is not None and src is not None:
                if src == "other":
                    logger.warning("Marcador de proteccion DC pendiente: se restaura la fuente Panel")
                    self._prot_dc_phase = "restoring"
                    self._prot_dc_begin_ts = now
                    self._prot_dc_req_ts = now
                    st["prot_dc_active"] = True
                    st["prot_dc_end_ts"] = now
                    st["prot_dc_reason"] = "restore"
                    self.set_dc_source("pv", _internal=True)
                else:
                    self._prot_marker.pop("dc_restore_ts", None)
                    self._prot_save_marker()
                return

            cur = st.get("dc_input_current") or 0.0
            if (not st.get("connected")) or src != "pv" or (st.get("dc_high_current_on") and not alert_config.get("dc_in_current", "prot_with_hc")):  # [alertas-patch]
                # Con alta corriente activada el limite es otro (hasta 20 A): no aplica.
                self._prot_reset_dc_timers()
                return

            # Regla A: >=15 A seguidos; una vez iniciada, fluctuar entre 14 y 15 A no reinicia.
            if cur >= PROT_DC_ALERT_A:
                if self._prot_a_since is None:
                    self._prot_a_since = now
                if self._prot_b_since is None:
                    self._prot_b_since = now   # la regla B arranca al llegar a 15 A
            elif cur >= PROT_DC_BAND_LOW_A and self._prot_a_since is not None:
                pass
            else:
                self._prot_a_since = None

            # Regla B: sin bajar de 11 A; bajadas < 5 s no cuentan, mas tiempo reinicia.
            if self._prot_b_since is not None:
                if cur >= PROT_DC_B_LOW_A:
                    self._prot_b_dip_since = None
                elif self._prot_b_dip_since is None:
                    self._prot_b_dip_since = now
                elif now - self._prot_b_dip_since >= PROT_DC_B_DIP_TOL_S:
                    self._prot_b_since = None
                    self._prot_b_dip_since = None

            if self._prot_a_since is not None and now - self._prot_a_since >= PROT_DC_A_SECONDS:
                self._prot_dc_start("A")
            elif self._prot_b_since is not None and now - self._prot_b_since >= PROT_DC_B_SECONDS:
                self._prot_dc_start("B")

        elif phase == "switching":
            if src == "other":
                self._prot_dc_phase = "refreshing"
                st["prot_dc_end_ts"] = now + PROT_DC_REFRESH_S
            elif now - self._prot_dc_begin_ts > PROT_DC_GIVEUP_S:
                logger.error("No se pudo confirmar el cambio a fuente 'Otros'; se abandona la proteccion DC")
                self._prot_dc_finish(restored=False)
            elif now - self._prot_dc_req_ts >= PROT_DC_RETRY_S:
                self._prot_dc_req_ts = now
                self.set_dc_source("other", _internal=True)

        elif phase == "refreshing":
            if src == "pv":
                # Alguien volvio a Panel por fuera (p. ej. app movil): se respeta.
                logger.info("Proteccion DC cancelada: la fuente volvio a Panel por otro medio")
                self._prot_dc_finish(restored=False)
            elif now >= (st.get("prot_dc_end_ts") or now):
                logger.info("Fin del minuto en 'Otros': se restaura la fuente Panel")
                self._prot_dc_phase = "restoring"
                self._prot_dc_req_ts = now
                self.set_dc_source("pv", _internal=True)

        elif phase == "restoring":
            if src == "pv":
                want_hc = (alert_config.get("dc_in_current", "restore_hc") == "previous"  # [alertas-patch]
                           and bool(self._prot_marker.get("dc_restore_hc")))
                if bool(st.get("dc_high_current_on")) != want_hc:
                    self.set_dc_high_current(want_hc)
                logger.info("Fuente Panel restaurada tras la proteccion de alta corriente DC")
                self._prot_dc_finish(restored=True)
            elif now - self._prot_dc_req_ts >= PROT_DC_RETRY_S:
                self._prot_dc_req_ts = now   # se insiste: dejarlo en "Otros" seria peor
                self.set_dc_source("pv", _internal=True)

    # -------------------------------------------------------------------------
    # CONSUMO AL LIMITE (salida AC + DC sostenida por encima de lim_w): se apaga la salida AC lim_off_s y
    #   se restaura. Tras restaurar se vigila PROT_LIM_CHECK_S: si el consumo sigue alto, la salida AC se apaga
    #   definitivamente. El marcador "lim_restore_ts" guarda el instante de restauracion por si la app se cierra
    #   con la salida AC apagada; "lim_final" recuerda el apagado definitivo.
    #   Un cambio manual de la salida AC cancela la proteccion sin forzar nada.
    # -------------------------------------------------------------------------
    def _prot_lim_user_override(self):
        if self._prot_lim_phase != "idle":
            logger.info("Proteccion por consumo al limite cancelada: cambio manual de la salida AC")
            self._prot_lim_finish()

    def _prot_lim_finish(self):
        self._prot_lim_phase = "idle"
        self._prot_lim_since = None
        self.state["prot_lim_active"] = False
        self.state["prot_lim_end_ts"] = None
        self._prot_marker.pop("lim_restore_ts", None)
        self._prot_marker.pop("lim_final", None)
        self._prot_save_marker()

    def _prot_lim_restored(self, now: float):
        self._prot_lim_phase = "check"
        self._prot_lim_since = None
        self.state["prot_lim_active"] = False
        self.state["prot_lim_end_ts"] = None
        self.state["prot_lim_restored_ts"] = now
        self._prot_marker.pop("lim_restore_ts", None)
        self._prot_save_marker()

    def _prot_lim_final(self, now: float):
        logger.warning("PROTECCION CONSUMO AL LIMITE: consumo sin normalizar tras restaurar -> salida AC apagada definitivamente")
        self._prot_lim_phase = "final"
        self._prot_lim_since = None
        self._prot_lim_req_ts = now
        self.state["prot_lim_active"] = True
        self.state["prot_lim_end_ts"] = None
        self.state["prot_lim_fire_ts"] = now
        self._prot_marker["lim_final"] = True
        self._prot_save_marker()
        self.set_ac_output(False, _internal=True)

    def _prot_lim_start(self, now: float):
        off_s = int(_ac_get("ac_out_current", "lim_off_s"))
        logger.warning("PROTECCION CONSUMO AL LIMITE: salida AC apagada %d s", off_s)
        self._prot_lim_phase = "off"
        self._prot_lim_since = None
        self._prot_lim_end_ts = now + off_s
        self._prot_lim_req_ts = now
        self.state["prot_lim_active"] = True
        self.state["prot_lim_end_ts"] = self._prot_lim_end_ts
        self.state["prot_lim_fire_ts"] = now
        self._prot_marker["lim_restore_ts"] = self._prot_lim_end_ts
        self._prot_save_marker()
        self.set_ac_output(False, _internal=True)

    def _prot_tick_lim(self):
        st = self.state
        now = time.time()
        if not st.get("connected"):
            return
        a = _ac_section("ac_out_current")
        b = _ac_section("dc_out_battery")

        if self._prot_lim_phase == "idle":
            rts = self._prot_marker.get("lim_restore_ts")
            if self._prot_marker.get("lim_final"):
                self._prot_lim_phase = "final"
                st["prot_lim_active"] = True
                st["prot_lim_end_ts"] = None
            elif rts is not None:
                logger.warning("Marcador de proteccion por consumo al limite pendiente: se restaura la salida AC")
                self._prot_lim_phase = "off"
                self._prot_lim_end_ts = float(rts)
                st["prot_lim_active"] = True
                st["prot_lim_end_ts"] = self._prot_lim_end_ts
            else:
                out_w = _ac_out_w(st)
                applies = bool(st.get("ac_output_on")) and (not b["load_only_no_ac"] or (st.get("ac_input_watts") or 0) <= 5)
                if not a["lim_prot_on"] or not applies or out_w <= a["lim_w"] - a["lim_hyst_w"]:
                    self._prot_lim_since = None
                elif out_w > a["lim_w"]:
                    if self._prot_lim_since is None:
                        self._prot_lim_since = now
                    elif now - self._prot_lim_since >= a["lim_s"]:
                        self._prot_lim_start(now)
                return

        if self._prot_lim_phase == "off":
            if now >= self._prot_lim_end_ts:
                logger.warning("Fin de la proteccion por consumo al limite: se restaura la salida AC")
                self._prot_lim_phase = "restoring"
                self._prot_lim_req_ts = now
                self.set_ac_output(True, _internal=True)
            elif st.get("ac_output_on") and now - self._prot_lim_req_ts >= self.AC_HOLD_S:
                self._prot_lim_req_ts = now
                self.set_ac_output(False, _internal=True)
        elif self._prot_lim_phase == "restoring":
            if st.get("ac_output_on"):
                if now - self._prot_lim_req_ts >= self.AC_HOLD_S:
                    self._prot_lim_restored(now)
            elif now - self._prot_lim_req_ts >= self.AC_HOLD_S:
                self._prot_lim_req_ts = now
                self.set_ac_output(True, _internal=True)
        elif self._prot_lim_phase == "check":
            out_w = _ac_out_w(st)
            ac_in = st.get("ac_input_watts") or 0
            applies = bool(st.get("ac_output_on")) and (not b["load_only_no_ac"] or ac_in <= 5)
            if not a["lim_prot_on"] or not applies or out_w <= a["lim_w"] - a["lim_hyst_w"]:
                self._prot_lim_phase = "idle"
                self._prot_lim_since = None
            elif out_w > a["lim_w"]:
                if self._prot_lim_since is None:
                    self._prot_lim_since = now
                elif now - self._prot_lim_since >= PROT_LIM_CHECK_S:
                    self._prot_lim_final(now)
        elif self._prot_lim_phase == "final":
            if st.get("ac_output_on") and now - self._prot_lim_req_ts >= self.AC_HOLD_S:
                self._prot_lim_req_ts = now
                self.set_ac_output(False, _internal=True)

    # -------------------------------------------------------------------------
    # BATERÍA CRÍTICA: 5% -> 4% -> 3%
    #   5%: si consumo > 300W -> apaga AC+DC irreversible; si <= 300W -> aviso cerrable (postergar a 4%)
    #   4%: si consumo > 200W -> apaga AC+DC irreversible; si <= 200W -> aviso cerrable (postergar a 3%)
    #   3%: SIEMPRE -> apagado total sistema (reg 2013) en 5 s
    # El marcador "bat_crit_dismissed" recuerda si el usuario cerró el aviso para postergar.
    # -------------------------------------------------------------------------
    def _prot_tick_bat_crit(self):
        st = self.state
        now = time.time()
        if not st.get("connected"):
            return
        b = _ac_section("dc_out_battery")
        if not b.get("bat_crit_on"):
            return

        soc = st.get("soc")
        if not isinstance(soc, (int, float)):
            return
        out_w = _ac_out_w(st)

        # Fases: idle -> armed_5 -> countdown_5 -> (apaga o armed_4) -> countdown_4 -> (apaga o armed_3) -> shutdown
        phase = self._prot_bat_crit_phase
        level = self._prot_bat_crit_level

        if phase == "idle":
            # Detectar entrada en zona crítica
            if b.get("bat_crit_5_on") and soc <= 5:
                self._prot_bat_crit_phase = "countdown_5"
                self._prot_bat_crit_level = 5
                self._prot_bat_crit_fire_ts = now
                self._prot_bat_crit_end_ts = now + 60.0
                self._prot_bat_crit_dismissed = False
                st["prot_bat_crit_active"] = True
                st["prot_bat_crit_level"] = 5
                st["prot_bat_crit_end_ts"] = self._prot_bat_crit_end_ts
                st["prot_bat_crit_fire_ts"] = now
            elif b.get("bat_crit_4_on") and soc <= 4:
                self._prot_bat_crit_phase = "countdown_4"
                self._prot_bat_crit_level = 4
                self._prot_bat_crit_fire_ts = now
                self._prot_bat_crit_end_ts = now + 60.0
                self._prot_bat_crit_dismissed = False
                st["prot_bat_crit_active"] = True
                st["prot_bat_crit_level"] = 4
                st["prot_bat_crit_end_ts"] = self._prot_bat_crit_end_ts
                st["prot_bat_crit_fire_ts"] = now
            elif b.get("bat_crit_3_on") and soc <= 3:
                self._prot_bat_crit_phase = "shutdown"
                self._prot_bat_crit_level = 3
                self._prot_bat_crit_fire_ts = now
                self._prot_bat_crit_end_ts = now + 5.0
                st["prot_bat_crit_active"] = True
                st["prot_bat_crit_level"] = 3
                st["prot_bat_crit_end_ts"] = self._prot_bat_crit_end_ts
                st["prot_bat_crit_fire_ts"] = now
            return

        if phase == "countdown_5":
            # 5%: si consumo > 300W -> apaga AC+DC irreversible; si <= 300W y usuario despide -> armed_4
            if out_w > b.get("bat_crit_5_w", 300):
                logger.warning("BATERÍA CRÍTICA 5%%: consumo %dW > %dW -> apaga AC+DC irreversible", out_w, b.get("bat_crit_5_w", 300))
                self.set_ac_output(False, _internal=True)
                self.set_dc_output(False)
                self._prot_bat_crit_phase = "final_5"
                self._prot_bat_crit_dismissed = True  # irreversible
                st["prot_bat_crit_end_ts"] = None
            elif now >= self._prot_bat_crit_end_ts:
                # Tiempo agotado sin acción del usuario -> apaga
                logger.warning("BATERÍA CRÍTICA 5%%: tiempo agotado -> apaga AC+DC")
                self.set_ac_output(False, _internal=True)
                self.set_dc_output(False)
                self._prot_bat_crit_phase = "final_5"
                self._prot_bat_crit_dismissed = True
                st["prot_bat_crit_end_ts"] = None
            # Si el usuario cierra el aviso (dismissed), pasa a armed_4
            elif self._prot_bat_crit_dismissed:
                logger.info("BATERÍA CRÍTICA 5%%: aviso despedido -> postergar a 4%%")
                self._prot_bat_crit_phase = "armed_4"
                self._prot_bat_crit_dismissed = False
            return

        if phase == "armed_4":
            # Esperando bajar a 4%
            if b.get("bat_crit_4_on") and soc <= 4:
                self._prot_bat_crit_phase = "countdown_4"
                self._prot_bat_crit_level = 4
                self._prot_bat_crit_fire_ts = now
                self._prot_bat_crit_end_ts = now + 60.0
                self._prot_bat_crit_dismissed = False
                st["prot_bat_crit_level"] = 4
                st["prot_bat_crit_end_ts"] = self._prot_bat_crit_end_ts
                st["prot_bat_crit_fire_ts"] = now
            elif soc > 5:
                # Subió por encima de 5% -> rearma todo
                self._prot_bat_crit_phase = "idle"
                self._prot_bat_crit_level = 0
                self._prot_bat_crit_dismissed = False
                st["prot_bat_crit_active"] = False
                st["prot_bat_crit_level"] = 0
                st["prot_bat_crit_end_ts"] = None
            return

        if phase == "countdown_4":
            # 4%: si consumo > 200W -> apaga AC+DC irreversible; si <= 200W y usuario despide -> armed_3
            if out_w > b.get("bat_crit_4_w", 200):
                logger.warning("BATERÍA CRÍTICA 4%%: consumo %dW > %dW -> apaga AC+DC irreversible", out_w, b.get("bat_crit_4_w", 200))
                self.set_ac_output(False, _internal=True)
                self.set_dc_output(False)
                self._prot_bat_crit_phase = "final_4"
                self._prot_bat_crit_dismissed = True
                st["prot_bat_crit_end_ts"] = None
            elif now >= self._prot_bat_crit_end_ts:
                logger.warning("BATERÍA CRÍTICA 4%%: tiempo agotado -> apaga AC+DC")
                self.set_ac_output(False, _internal=True)
                self.set_dc_output(False)
                self._prot_bat_crit_phase = "final_4"
                self._prot_bat_crit_dismissed = True
                st["prot_bat_crit_end_ts"] = None
            elif self._prot_bat_crit_dismissed:
                logger.info("BATERÍA CRÍTICA 4%%: aviso despedido -> postergar a 3%%")
                self._prot_bat_crit_phase = "armed_3"
                self._prot_bat_crit_dismissed = False
            return

        if phase == "armed_3":
            # Esperando bajar a 3%
            if b.get("bat_crit_3_on") and soc <= 3:
                self._prot_bat_crit_phase = "shutdown"
                self._prot_bat_crit_level = 3
                self._prot_bat_crit_fire_ts = now
                self._prot_bat_crit_end_ts = now + 5.0
                st["prot_bat_crit_level"] = 3
                st["prot_bat_crit_end_ts"] = self._prot_bat_crit_end_ts
                st["prot_bat_crit_fire_ts"] = now
            elif soc > 4:
                self._prot_bat_crit_phase = "idle"
                self._prot_bat_crit_level = 0
                self._prot_bat_crit_dismissed = False
                st["prot_bat_crit_active"] = False
                st["prot_bat_crit_level"] = 0
                st["prot_bat_crit_end_ts"] = None
            return

        if phase == "shutdown":
            # 3%: cuenta atrás 5 s y apaga sistema (reg 2013)
            if now >= self._prot_bat_crit_end_ts:
                logger.warning("BATERÍA CRÍTICA 3%%: apagado total del sistema (REG 2013)")
                self.set_system_power_off()
                self._prot_bat_crit_phase = "final_3"
                st["prot_bat_crit_end_ts"] = None
            return

        # Fases finales (irreversibles): solo mantenemos salidas apagadas
        if phase in ("final_5", "final_4", "final_3"):
            if st.get("ac_output_on"):
                self.set_ac_output(False, _internal=True)
            if st.get("dc_output_on"):
                self.set_dc_output(False)
            return

    def _prot_tick_ac(self):
        st = self.state
        now = time.time()
        volts = st.get("ac_input_volts") or 0.0
        cur = st.get("ac_input_current") or 0.0
        if (not st.get("connected")) or volts <= 0:
            self._prot_ac_since = None
            self._prot_ac_high = False
            self._prot_ac_consump_fired = False
            return

        if self._prot_ac_high:
            self._prot_ac_high = cur >= PROT_AC_RELEASE_A
        else:
            self._prot_ac_high = cur >= PROT_AC_ALERT_A
        if not self._prot_ac_high:
            self._prot_ac_since = None
            self._prot_ac_consump_fired = False
            return

        if self._prot_ac_since is None:
            self._prot_ac_since = now
            return
        if now - self._prot_ac_since < PROT_AC_SECONDS:
            return

        if st.get("charge_mode") != "silent":
            logger.warning("PROTECCION ALTA CORRIENTE AC: >=%.1f A durante %d s -> modo de carga Silencioso",
                           PROT_AC_ALERT_A, PROT_AC_SECONDS)
            self.set_charge_mode("silent")
            st["prot_ac_reduced_pending"] = True
            st["prot_ac_reduced_ts"] = now
            self._prot_marker["ac_notice_pending"] = True
            self._prot_save_marker()
            self._prot_ac_since = now   # si sigue alto otros 5 min -> "por Consumo"
        elif not self._prot_ac_consump_fired:
            self._prot_ac_consump_fired = True
            st["prot_ac_consumption_ts"] = now
            logger.warning("Alta corriente AC sostenida con modo Silencioso: es consumo, no carga")

    # -------------------------------------------------------------------------
    # PICO DE ARRANQUE
    # Consumo = salida AC + salida DC (la entrada se ignora). Se muestrea el bloque de
    # potencias cada ~FAST_POWER_INTERVAL_S (ver query_modbus).
    #   1. "consumo de antes" = mediana de las muestras de los ultimos SPIKE_BASE_WINDOW_S.
    #   2. Una muestra que lo supera en >= SPIKE_MIN_RISE_W abre un evento: se sigue el
    #      maximo (pico) hasta que el consumo se estabiliza (o pasan SPIKE_MAX_EVENT_S).
    #   3. Al cerrarse: P = pico - consumo de antes; S = consumo estable - consumo de antes
    #      (lo que se queda sumado). Se notifica si P >= SPIKE_MIN_PEAK_W y P >= FACTOR x S.
    #      Ej.: antes 240 W, la nevera deja 150 W (390 W); pico de 540 W -> P = 300 W,
    #      S = 150 W -> 300 >= 2 x 150 -> "Pico de Arranque de 300 W".
    # Un encendido "plano" (hervidor: P == S) no se notifica.
    # -------------------------------------------------------------------------
    def _spike_init(self):
        self._spike_samples = collections.deque()   # (ts, consumo W)
        self._spike_event: Optional[Dict[str, Any]] = None
        self._spike_last_end = 0.0
        self._spike_path = os.path.join(os.path.dirname(_prot_marker_path()), "spike_log.csv")
        self._last_power_ts = 0.0
        self._in_fast_sample = False
        self._current_query_bytes: Optional[int] = None
        self._diag_log_ts = 0.0
        self._tele_log_ts = 0.0
        self.state.update({
            "spike_ts": 0.0,          # instante del ultimo pico NOTIFICADO
            "spike_w": 0,             # su magnitud (W sobre el consumo de antes)
            "spike_base_w": 0,
            "spike_settled_w": 0,
        })

    def _spike_reset(self):
        self._spike_event = None
        self._spike_samples.clear()

    @staticmethod
    def _median(vals):
        s = sorted(vals)
        n = len(s)
        return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0

    def _spike_feed(self):
        st = self.state
        now = time.time()
        load = float((st.get("ac_output_watts") or 0) + (st.get("dc_output_watts") or 0))
        buf = self._spike_samples
        if buf and now - buf[-1][0] < 0.12:
            return   # lecturas casi simultaneas: no aportan nada
        in_w = float(st.get("ac_input_watts") or 0)   # solo la entrada AC (la solar sube y baja sola todo el dia)
        prev_in = getattr(self, "_spike_prev_in", in_w)
        self._spike_prev_in = in_w
        if in_w - prev_in >= SPIKE_CHARGE_RISE_W:
            self._spike_mute_until = now + SPIKE_MUTE_S   # empieza a cargar por AC / vuelve la red: no es un arranque
        if now < getattr(self, "_spike_mute_until", 0.0):
            self._spike_event = None
            self._spike_last_end = now
            buf.append((now, load))
            while buf and now - buf[0][0] > 15.0:
                buf.popleft()
            return
        ev = self._spike_event
        if ev is None:
            hist = [w for ts, w in buf if now - ts <= SPIKE_BASE_WINDOW_S and ts > self._spike_last_end]
            if len(hist) >= 2:
                base = self._median(hist)
                if load - base >= SPIKE_MIN_RISE_W:
                    pre = [(ts, w) for ts, w in buf if now - ts <= 2.5 and ts > self._spike_last_end][-4:]
                    self._spike_event = {"base": base, "peak": load, "t0": now, "pts": [(now, load)], "pre": pre}
        else:
            ev["pts"].append((now, load))
            if load > ev["peak"]:
                ev["peak"] = load
            self._spike_check_end(ev, now)
        buf.append((now, load))
        while buf and now - buf[0][0] > 15.0:
            buf.popleft()

    def _spike_check_end(self, ev: Dict[str, Any], now: float):
        age = now - ev["t0"]
        done = age >= SPIKE_MAX_EVENT_S
        if not done and age >= SPIKE_SETTLE_S:
            recent = [w for ts, w in ev["pts"] if now - ts <= SPIKE_SETTLE_S]
            if len(recent) >= 3:
                level = sum(recent) / len(recent)
                done = (max(recent) - min(recent)) <= max(40.0, 0.12 * level)
        if not done:
            return
        settled = self._median([w for _, w in ev["pts"][-3:]])
        base, peak = ev["base"], ev["peak"]
        p_w = peak - base
        s_w = max(0.0, settled - base)
        notify = p_w >= SPIKE_MIN_PEAK_W and p_w >= SPIKE_FACTOR * max(s_w, SPIKE_S_FLOOR_W)
        if notify:
            hi = [ts for ts, w in ev["pts"] if w - base >= 0.5 * p_w]
            if hi and max(hi) - min(hi) > SPIKE_MAX_HIGH_S:
                notify = False   # se mantiene alto varios segundos: es una carga (microondas), no un arranque
        self._spike_log_event(ev, settled, s_w, p_w, notify)
        self._spike_event = None
        self._spike_last_end = now
        if notify:
            self.state["spike_ts"] = now
            self.state["spike_w"] = int(round(p_w / 10.0) * 10)
            self.state["spike_base_w"] = int(round(base))
            self.state["spike_settled_w"] = int(round(settled))
            logger.warning("PICO DE ARRANQUE: %d W (antes %d W, se queda en %d W, pico %d W)",
                           self.state["spike_w"], round(base), round(settled), round(peak))

    def _spike_log_event(self, ev, settled, s_w, p_w, notified):
        """Cada evento (notificado o no) con sus muestras, para calibrar los umbrales."""
        if not SPIKE_LOG_ENABLED:
            return
        try:
            if os.path.exists(self._spike_path) and os.path.getsize(self._spike_path) > 300_000:
                os.replace(self._spike_path, self._spike_path + ".old")
            new = not os.path.exists(self._spike_path)
            t0 = ev["t0"]
            pts = ";".join(f"{ts - t0:.2f}:{int(w)}" for ts, w in list(ev["pre"]) + list(ev["pts"]))
            with open(self._spike_path, "a", encoding="utf-8") as f:
                if new:
                    f.write("fecha,consumo_antes_w,pico_w,asentado_w,P_w,S_w,notificado,muestras_segundos:watios\n")
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')},{ev['base']:.0f},{ev['peak']:.0f},{settled:.0f},"
                        f"{p_w:.0f},{s_w:.0f},{'SI' if notified else 'no'},{pts}\n")
        except Exception:
            pass

    async def _apply_pending_dc(self):
        """Aplica el cambio DC en cola si ya paso el intervalo minimo. Se llama
        una vez por ciclo de polling; no espera nunca (si todavia no toca, sale
        al instante y el polling sigue)."""
        with self._dc_pending_lock:
            pend = self._dc_pending
            if not pend:
                return
            if time.time() - self._last_dc_write < DC_WRITE_MIN_INTERVAL_S:
                return
            self._dc_pending = None
        await self._write_dc_word(source=pend.get("source"), high=pend.get("high"))
        await asyncio.sleep(0.4)
        self.rx_modbus_buffer.clear()

    async def _write_register_verified(self, reg: int, value: int) -> bool:
        """Escribe un registro (0x06), espera y lo relee para verificar. Actualiza
        self.state con lo que el equipo realmente devuelve."""
        pkt = append_crc16(bytes([
            0x01, 0x06,
            (reg >> 8) & 0xFF, reg & 0xFF,
            (value >> 8) & 0xFF, value & 0xFF,
        ]))
        enc = aes_encrypt(pkt, self.secure_aes_key, None)
        if not await self._write_ble_safe(enc, f"Reg {reg} <- {value}"):
            logger.warning("No se pudo enviar la escritura al Reg %d", reg)
            return False
        await asyncio.sleep(0.4)
        self.rx_modbus_buffer.clear()
        p = await self.query_modbus(reg, 1, timeout=0.8)
        if not p or len(p) != 2:
            logger.warning("Reg %d: sin respuesta al verificar tras escribir %d", reg, value)
            return False
        self._parse_telemetry_payload(p, 0x00, reg)
        got = int.from_bytes(p, "big")
        if got == value:
            logger.info("Reg %d verificado = %d", reg, got)
            return True
        logger.warning("Reg %d: se escribio %d pero el equipo devuelve %d", reg, value, got)
        return False

    async def _write_dc_word(self, source: Optional[str], high: Optional[bool]):
        # El intervalo minimo ya lo garantiza _apply_pending_dc(); aqui solo se
        # revalida por seguridad y, si por algun motivo aun no toca, se vuelve a
        # poner en cola (sin avisos, sin perder el cambio).
        if time.time() - self._last_dc_write < DC_WRITE_MIN_INTERVAL_S:
            with self._dc_pending_lock:
                if self._dc_pending is None:
                    self._dc_pending = {"source": source, "high": high}
            return
        payload = await self.query_modbus(REG_DC_SOURCE, 1, timeout=0.8)
        if not payload or len(payload) != 2:
            logger.warning("Reg 2269: no se pudo leer el valor actual; no se escribe nada")
            return
        raw = int.from_bytes(payload, "big")
        lo, hi = raw & 0xFF, (raw >> 8) & 0xFF
        if lo not in (1, 2):
            logger.warning("Reg 2269 con valor inesperado (0x%04X); no se escribe nada", raw)
            return
        new_lo, new_hi = lo, hi
        if source == "pv":
            new_lo = 2
        elif source == "other":
            new_lo = 1
            if hi == 1:          # al salir de PV se apaga antes la alta corriente
                new_hi = 2
        if high is not None:
            if new_lo != 2:
                logger.warning("Alta corriente rechazada: la fuente no es Panel Solar")
                return
            new_hi = 1 if high else 2
        if (new_lo, new_hi) == (lo, hi):
            return
        new_raw = (new_hi << 8) | new_lo
        logger.info("Reg 2269: 0x%04X -> 0x%04X", raw, new_raw)
        self._last_dc_write = time.time()
        await self._write_register_verified(REG_DC_SOURCE, new_raw)

    def _enqueue_command(self, packet: bytes, description: str):
        if self.loop and self.loop.is_running() and self.command_queue is not None:
            self.loop.call_soon_threadsafe(self.command_queue.put_nowait, (packet, description))

    def _notify_status(self, msg: str):
        if self.on_status:
            self.on_status(msg)

    # -------------------------------------------------------------------------
    # SUPERVISOR Y BUCLE PRINCIPAL ASYNCIO
    # -------------------------------------------------------------------------
    def _run_event_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.command_queue = asyncio.Queue()
        self.ecdh_ready_event = asyncio.Event()
        self.loop.run_until_complete(self._main_supervisor())

    async def _capture_register_ranges(
        self, ranges: list[tuple[int, int]]
    ) -> Dict[int, Optional[int]]:
        snapshot: Dict[int, Optional[int]] = {}
        for start, count in ranges:
            for chunk_start in range(start, start + count, 8):
                chunk_count = min(8, start + count - chunk_start)
                payload = await self.query_modbus(chunk_start, chunk_count, timeout=1.2)
                expected_length = chunk_count * 2
                if payload is None or len(payload) != expected_length:
                    logger.warning(
                        "Captura: sin respuesta completa para registros %d-%d; "
                        "se continúa con el siguiente grupo.",
                        chunk_start,
                        chunk_start + chunk_count - 1,
                    )
                    for register in range(chunk_start, chunk_start + chunk_count):
                        snapshot[register] = None
                    continue

                for index in range(chunk_count):
                    word_offset = index * 2
                    snapshot[chunk_start + index] = int.from_bytes(
                        payload[word_offset:word_offset + 2], "big"
                    )
        return snapshot

    async def _cleanup(self):
        if getattr(self, "_cl_dirty", False):
            self._cell_log_save()
        await self._disconnect_client()
        if self.loop:
            self.loop.stop()

    async def _disconnect_client(self):
        if self.client and self.client.is_connected:
            try:
                await asyncio.wait_for(self.client.disconnect(), timeout=8.0)
            except Exception as e:
                logger.warning(f"Error al desconectar BLE: {e}")
            finally:
                self.client = None
                self.secure_aes_key = None
                self.state["connected"] = False
                self.state["encryption_active"] = False
                self.state["device_name"] = "Desconectado"
                self._prot_on_disconnect()
                self.state["ac_input_hz"] = 0.0
                self.state["ac_output_hz"] = 0.0
                self.state["dc_input_watts"] = 0
                self.state["dc_input_volts"] = 0.0
                self.state["dc_input_current"] = 0.0
                self.state["ac_input_watts"] = 0
                self.state["ac_input_volts"] = 0.0
                self.state["ac_input_current"] = 0.0
                self.state["ac_output_watts"] = 0
                self.state["dc_output_watts"] = 0
                with self._dc_pending_lock:
                    self._dc_pending = None   # no aplicar cambios viejos tras reconectar
                if self._current_query_future and not self._current_query_future.done():
                    self._current_query_future.cancel()
                if self.on_telemetry:
                    self.on_telemetry(dict(self.state))

    async def _write_ble_safe(self, data: bytes, desc: str = "") -> bool:
        """Escribe datos asegurando tolerancia con y sin respuesta según MTU y stack."""
        if not self.client or not self.client.is_connected:
            return False
        target_char = getattr(self, "write_char_uuid", BLUETTI_WRITE_UUID) or BLUETTI_WRITE_UUID
        try:
            await asyncio.wait_for(self.client.write_gatt_char(target_char, data, response=False), timeout=5.0)
            return True
        except Exception:
            try:
                await asyncio.wait_for(self.client.write_gatt_char(target_char, data, response=True), timeout=5.0)
                return True
            except Exception as e2:
                logger.error(f"Error escribiendo en BLE ({desc}): {e2}")
                return False

    async def query_modbus(self, start_reg: int, count: int, timeout: float = 0.6) -> Optional[bytes]:
        """Lectura Modbus 0x03. Antes de cada lectura (salvo la propia de potencias) intercala,
        si toca, una lectura rapida del bloque de potencias: asi el consumo se muestrea cada
        ~FAST_POWER_INTERVAL_S en vez de una vez por ciclo completo (~2-3 s), que es lo que
        permite ver un pico de arranque."""
        if (FAST_POWER_INTERVAL_S > 0 and start_reg != 140 and not self._in_fast_sample
                and self.secure_aes_key is not None
                and time.time() - self._last_power_ts >= FAST_POWER_INTERVAL_S):
            self._in_fast_sample = True
            self._last_power_ts = time.time()   # limita el ritmo aunque esta lectura falle
            try:
                p_fast = await self._query_modbus_raw(140, 8, timeout=0.4)
                if p_fast:
                    self._parse_telemetry_payload(p_fast, 0x00, 140)
            except Exception as exc:
                logger.debug("Muestreo rapido de potencia: %s", exc)
            finally:
                self._in_fast_sample = False
        return await self._query_modbus_raw(start_reg, count, timeout)

    async def _query_modbus_raw(self, start_reg: int, count: int, timeout: float = 0.6) -> Optional[bytes]:
        """Envía una petición Modbus 0x03 y espera su respuesta de forma síncrona y atómica.
        Evita al 100% cualquier tipo de desincronización o desfase por paquetes en colas FIFO."""
        if not self.client or not self.client.is_connected or not self.secure_aes_key or not self.loop:
            return None

        # Cancelar cualquier consulta previa que siga colgada
        if self._current_query_future and not self._current_query_future.done():
            self._current_query_future.cancel()

        self._current_query_reg = start_reg
        self._current_query_bytes = count * 2   # tamaño de respuesta esperado
        self._current_query_future = self.loop.create_future()

        req_packet = _build_read_request(start_reg, count)
        enc_req = aes_encrypt(req_packet, self.secure_aes_key, None)
        ok = await self._write_ble_safe(enc_req, f"Query Reg {start_reg} ({count} words)")
        if not ok:
            self._current_query_future = None
            return None

        try:
            payload = await asyncio.wait_for(self._current_query_future, timeout=timeout)
            if payload is not None:
                self._last_rx_ok = time.time()
                self._touch()
            return payload
        except asyncio.TimeoutError:
            logger.debug(f"Petición Modbus reg {start_reg} sin respuesta (timeout {timeout}s)")
            # Respuesta tardia (el equipo estaba ocupado, p. ej. durante un pico de carga):
            # se deja un margen para que llegue y se DESCARTE antes de la siguiente lectura;
            # si no, podria tomarse como la respuesta de esa otra lectura (valores absurdos).
            await asyncio.sleep(0.12)
            self.rx_modbus_buffer.clear()
            return None
        except asyncio.CancelledError:
            logger.debug(f"Petición Modbus reg {start_reg} cancelada")
            return None
        finally:
            self._current_query_future = None
            self._current_query_reg = None
            self._current_query_bytes = None

    async def _interruptible_sleep(self, seconds: float):
        """Como asyncio.sleep, pero se corta en cuanto se pausa/cancela (por
        ej. el usuario cancela un intento de conexión desde el widget): así
        la cancelación se nota enseguida en vez de esperar todo el backoff."""
        end = time.time() + seconds
        while self.is_running and not self.is_paused:
            remaining = end - time.time()
            if remaining <= 0:
                break
            self._touch()
            await asyncio.sleep(min(0.25, remaining))

    async def _wait_ecdh(self, timeout: float) -> bool:
        """Espera el handshake ECDH, pero abandona apenas se pausa/cancela
        (True = handshake listo, False = timeout o cancelado)."""
        end = time.time() + timeout
        while not self.is_paused:
            remaining = end - time.time()
            if remaining <= 0:
                return False
            try:
                await asyncio.wait_for(self.ecdh_ready_event.wait(), timeout=min(0.5, remaining))
                return True
            except asyncio.TimeoutError:
                continue
        return False

    async def _main_supervisor(self):
        """Vigilante: si el ciclo de conexion se cuelga (una llamada de Windows/Bleak que no vuelve tras
        hibernar o reconectar y deja el Bluetooth del equipo ocupado), lo cancela, libera el enlace y lo reinicia."""
        inner = asyncio.ensure_future(self._supervisor_inner())
        self._touch()
        while self.is_running:
            await asyncio.sleep(1.0)
            if inner.done():
                if not self.is_running:
                    break
                try:
                    exc = inner.exception()
                except asyncio.CancelledError:
                    exc = None
                if exc is not None:
                    logger.error("Ciclo de conexion terminado por error: %s", exc)
                inner = asyncio.ensure_future(self._supervisor_inner())
                self._touch()
                continue
            if self.is_paused or self.simulation_mode:
                continue
            if time.time() - getattr(self, "_progress_ts", time.time()) > STALL_S:
                logger.error("Ciclo de conexion BLE sin progreso %.0f s: se cancela, se libera el enlace y se reinicia", STALL_S)
                inner.cancel()
                try:
                    await asyncio.wait({inner}, timeout=10.0)
                except Exception:
                    pass
                await self._force_release()
                inner = asyncio.ensure_future(self._supervisor_inner())
                self._touch()
        if not inner.done():
            inner.cancel()

    async def _supervisor_inner(self):
        backoff = 2.0
        while self.is_running:
            self._touch()
            if self.simulation_mode:
                await asyncio.sleep(1.2)
                if self.state["ac_output_on"]:
                    self.state["ac_output_watts"] = random.randint(140, 165)
                if self.state["dc_output_on"]:
                    self.state["dc_output_watts"] = random.randint(28, 42)
                if self.on_telemetry:
                    self.on_telemetry(dict(self.state))
                continue

            if self.is_paused:
                await asyncio.sleep(1.0)
                continue

            # Si venimos de un pause_and_release() reciente, esperar a que
            # esa desconexión termine de verdad antes de escanear/conectar
            # de nuevo -- si no, esta corutina nueva y la desconexión vieja
            # pisan self.client/self.secure_aes_key/los búfers al mismo
            # tiempo y la reconexión queda colgada sin ningún error visible.
            pending = self._pending_disconnect_future
            if pending is not None:
                try:
                    await asyncio.wait_for(asyncio.wrap_future(pending), timeout=10.0)
                except Exception as e:
                    logger.warning("La desconexión previa terminó con un error (se ignora, se sigue igual): %s", e)
                finally:
                    self._pending_disconnect_future = None

            try:
                self._notify_status("Buscando Bluetti Elite 100 V2...")
                target_device = None

                # 1. Escaneo rápido de periféricos BLE
                devices = await asyncio.wait_for(BleakScanner.discover(timeout=4.0), timeout=15.0)
                self._touch()
                if self.is_paused:
                    # Cancelado por el usuario mientras escaneaba: no seguir
                    # adelante con la conexión (el bucle vuelve arriba y queda
                    # inactivo hasta que se reanude).
                    continue
                discovered_list = []
                for d in devices:
                    d_name = d.name or "Dispositivo sin nombre"
                    discovered_list.append({"name": d_name, "address": d.address})
                    if self.device_address and d.address.upper() == self.device_address.upper():
                        target_device = d
                    elif not target_device and any(k in d_name.upper() for k in ("EL100", "BLUETTI", "EB", "AC1", "AC2")):
                        target_device = d

                if self.on_devices and discovered_list:
                    self.on_devices(discovered_list)

                if not target_device and self.device_address:
                    target_device = self.device_address

                if not target_device:
                    self._notify_status("Estación no detectada. Verifique que esté encendida.")
                    await self._interruptible_sleep(backoff)
                    backoff = min(backoff * 1.5, 8.0)
                    continue

                dev_title = getattr(target_device, 'name', None) or str(target_device)
                self._notify_status(f"Conectando a {dev_title}...")

                async with BleakClient(target_device, disconnected_callback=self._on_disconnected, timeout=15.0) as client:
                    if self.is_paused:
                        # El usuario canceló mientras se establecía el enlace:
                        # salir ya (el async with lo desconecta) en vez de
                        # completar el handshake y dejar la estación tomada.
                        logger.info("Intento de conexión cancelado por el usuario; se libera el enlace.")
                        continue
                    self.client = client
                    self.unsecure_aes_key = None
                    self.unsecure_aes_iv = None
                    self.secure_aes_key = None
                    self.ecdh_ready_event.clear()
                    self.rx_raw_buffer.clear()
                    self.rx_modbus_buffer.clear()

                    # Asegurar resolución de servicios GATT en Windows
                    try:
                        if not client.services:
                            await asyncio.wait_for(client.get_services(), timeout=10.0)
                    except Exception:
                        pass

                    notify_char = BLUETTI_NOTIFY_UUID
                    write_char = BLUETTI_WRITE_UUID

                    if client.services:
                        for service in client.services:
                            for char in service.characteristics:
                                u = char.uuid.lower()
                                if "ff01" in u:
                                    notify_char = char.uuid
                                elif "ff02" in u:
                                    write_char = char.uuid

                    self.write_char_uuid = write_char

                    logger.info(f"Enlace Bluetooth establecido. Suscribiendo notificaciones ({notify_char})...")
                    self._notify_status("Autenticando enlace criptográfico...")

                    await asyncio.wait_for(client.start_notify(notify_char, self._on_rx_raw_packet), timeout=10.0)
                    self._touch()

                    # Esperar Handshake ECDH (máx 12 segundos)
                    if not await self._wait_ecdh(12.0):
                        if self.is_paused:
                            continue  # cancelado por el usuario, no es un error de autenticación
                        logger.error("Tiempo de espera agotado en Handshake ECDH. Reintentando...")
                        self._notify_status("Reintentando autenticación criptográfica...")
                        continue

                    # Conexión cifrada 100% activa
                    self.state["connected"] = True
                    self.state["encryption_active"] = True
                    self.state["device_name"] = dev_title
                    for _k in ("grid_alarm_126", "grid_alarm_1134", "grid_status_161"):
                        self.state[_k] = None
                    self.state["grid_low_voltage_alarm"] = False
                    backoff = 2.0
                    self._notify_status(f"Enlace Seguro Activo: {dev_title}")
                    logger.info("Sesión segura AES lista. Iniciando polling continuo de telemetría...")
                    self._slow_cycle = 0   # releer los datos lentos/estáticos (capacidad) nada más conectar
                    self._last_rx_ok = time.time()
                    self._touch()

                    # Bucle principal de polling y envío de comandos
                    while self.is_running and not self.is_paused and client.is_connected:
                        # Vigilante de enlace: tras hibernar el enlace puede quedar "vivo" para Windows pero sin
                        # respuestas. Si pasa LINK_STALE_S sin ninguna lectura valida, se sale para reconectar.
                        if time.time() - self._last_rx_ok > LINK_STALE_S:
                            logger.warning("Enlace sin respuestas validas %.0f s (hibernacion?): se reconecta", LINK_STALE_S)
                            self.state["connected"] = False
                            self.state["encryption_active"] = False
                            self._notify_status("Reconectando (enlace sin respuesta)...")
                            if self.on_telemetry:
                                self.on_telemetry(dict(self.state))
                            break
                        try:
                            ranges, snapshot_future = self._register_snapshot_requests.get_nowait()
                        except queue.Empty:
                            pass
                        else:
                            if not snapshot_future.cancelled():
                                try:
                                    snapshot = await self._capture_register_ranges(ranges)
                                except Exception as exc:
                                    snapshot_future.set_exception(exc)
                                    logger.warning("Falló la captura Modbus solicitada: %s", exc)
                                else:
                                    snapshot_future.set_result(snapshot)

                        # 0. Cambio DC en cola (Reg 2269): se aplica solo cuando ya paso
                        # el intervalo minimo; si todavia no toca, no espera ni bloquea.
                        await self._apply_pending_dc()

                        # 1. Enviar comandos pendientes de hardware (Modbus 0x06) con prioridad aislada
                        if not self.command_queue.empty():
                            while not self.command_queue.empty():
                                cmd_packet, desc = await self.command_queue.get()
                                logger.info(f"Enviando comando Modbus cifrado: {desc}")
                                if callable(cmd_packet):
                                    # Operacion compuesta (leer-modificar-escribir-verificar)
                                    try:
                                        await cmd_packet()
                                    except Exception as exc:
                                        logger.warning(f"Fallo en operacion '{desc}': {exc}")
                                else:
                                    enc_cmd = aes_encrypt(cmd_packet, self.secure_aes_key, None)
                                    await self._write_ble_safe(enc_cmd, desc)
                                # Pausa de protección de 400ms para permitir que el relé físico conmute y el hardware se estabilice
                                await asyncio.sleep(0.4)
                            # Purgar cualquier dato residual en búfer tras conmutación
                            self.rx_modbus_buffer.clear()
                            await asyncio.sleep(0.2)

                        # 2. Polling escalonado síncrono e inmune a desfases
                        # A. Batería: SoC (Reg 102) + Tiempo restante en minutos (Reg 104)
                        p_soc = await self.query_modbus(100, 6, timeout=0.6)
                        if p_soc:
                            self._parse_telemetry_payload(p_soc, 0x00, 100)

                        # B. Potencias reales: DC Out=140, AC Out=142, DC In (Solar)=144, AC In=146
                        p_pwr = await self.query_modbus(140, 8, timeout=0.6)
                        if p_pwr:
                            self._parse_telemetry_payload(p_pwr, 0x00, 140)

                        # C. Voltaje y corriente reales de entrada AC (Reg 1314-1315)
                        p_acv = await self.query_modbus(1314, 2, timeout=0.5)
                        if p_acv:
                            self._parse_telemetry_payload(p_acv, 0x00, 1314)

                        # D. Voltaje real de salida AC (Reg 1511)
                        p_acov = await self.query_modbus(1511, 2, timeout=0.5)
                        if p_acov:
                            self._parse_telemetry_payload(p_acov, 0x00, 1511)

                        # E. Interruptores reales: registros 2011-2012 y 48-49
                        p_sw = await self.query_modbus(2011, 2, timeout=0.5)
                        if p_sw:
                            self._parse_telemetry_payload(p_sw, 0x00, 2011)

                        p_sw_base = await self.query_modbus(48, 2, timeout=0.5)
                        if p_sw_base:
                            self._parse_telemetry_payload(p_sw_base, 0x00, 48)

                        # F. Modos ECO por riel, Power Lifting, refuerzo de red y Perfil
                        # de Carga (Turbo/Silencioso/Estándar): sincronizar con el estado
                        # real del equipo. El perfil de carga antes NUNCA se releía --
                        # solo se actualizaba localmente cuando nosotros mismos lo
                        # cambiábamos, así que si se activaba por cualquier otro medio
                        # (app oficial, sesión anterior) el widget nunca se enteraba.
                        for reg in (REG_W_ECO_DC, REG_W_ECO_AC, REG_W_POWER_LIFTING, REG_CONTROL_GRID, REG_W_CHARGE_MODE):
                            p_mode = await self.query_modbus(reg, 1, timeout=0.5)
                            if p_mode:
                                self._parse_telemetry_payload(p_mode, 0x00, reg)

                        # G. Entrada solar (bloque INV_PV_INFO): Reg 1212 = potencia (W),
                        # Reg 1213 = voltaje del panel (0.1V), Reg 1214 = corriente (0.1A)
                        p_pv = await self.query_modbus(1212, 3, timeout=0.5)
                        if not p_pv:
                            # Respaldo: si la lectura de 3 registros falla, leer solo W y V
                            # (la corriente se calculara como W / V en el parser).
                            p_pv = await self.query_modbus(1212, 2, timeout=0.5)
                        if p_pv:
                            self._parse_telemetry_payload(p_pv, 0x00, 1212)

                        # H. Lecturas lentas (cada 3 ciclos): frecuencia de red/salida,
                        # limite AC personalizado (2214), fuente DC / alta corriente (2269)
                        # y datos del pack (voltaje total, 16 celdas, temperaturas).
                        # SOLO LECTURA: no se escribe nada en el equipo.
                        slow = getattr(self, "_slow_cycle", 0)
                        self._slow_cycle = slow + 1
                        if slow % 3 == 0:
                            for reg in (REG_R_AC_INPUT_HZ, REG_R_AC_OUTPUT_HZ, REG_GRID_MAX_CURRENT, REG_DC_SOURCE):
                                p_slow = await self.query_modbus(reg, 1, timeout=0.5)
                                if p_slow:
                                    self._parse_telemetry_payload(p_slow, 0x00, reg)

                            # Pack: 6003 (voltaje total) y 6004 (corriente, candidata)
                            p_bv = await self.query_modbus(REG_BATT_VOLT, 2, timeout=0.5)
                            if p_bv:
                                self._parse_telemetry_payload(p_bv, 0x00, REG_BATT_VOLT)
                            # Celdas 1-8 y 9-16 (dos lecturas de 8 palabras, como el resto del polling)
                            p_c1 = await self.query_modbus(REG_CELLS_1, 8, timeout=0.5)
                            if p_c1:
                                self._parse_telemetry_payload(p_c1, 0x00, REG_CELLS_1)
                            p_c2 = await self.query_modbus(REG_CELLS_2, 8, timeout=0.5)
                            if p_c2:
                                self._parse_telemetry_payload(p_c2, 0x00, REG_CELLS_2)
                            # Temperaturas: 4 sensores (valor - 40), se promedian
                            p_tb = await self.query_modbus(REG_TEMPS, 4, timeout=0.5)
                            if p_tb:
                                self._parse_telemetry_payload(p_tb, 0x00, REG_TEMPS)

                        # I. Datos casi estáticos (cada 30 ciclos, y nada más conectar):
                        # capacidad nominal y el registro 6143 (solo para el CSV).
                        if slow % 10 == 0:
                            p_en = await self.query_modbus(REG_BMS_ENERGY, 3, timeout=0.5)
                            if p_en:
                                self._parse_telemetry_payload(p_en, 0x00, REG_BMS_ENERGY)
                        if slow % 30 == 0:
                            for reg, qty in ((REG_CAP_RATED, 1), (REG_UNKNOWN_6143, 1),
                                             (REG_BMS_LIMITS, 3)):
                                p_cap = await self.query_modbus(reg, qty, timeout=0.5)
                                if p_cap:
                                    self._parse_telemetry_payload(p_cap, 0x00, reg)

                        # Protecciones automaticas por alta corriente (una evaluacion por ciclo)
                        self._protection_tick()

                        # I. Alarma de red del equipo (SOLO LECTURA): 1134 cada ciclo; 126 y 161
                        # cada 3 ciclos. Va aparte del resto para que un fallo aqui no afecte
                        # a las demas lecturas.
                        self._grid_alarm_cycle = getattr(self, "_grid_alarm_cycle", 0) + 1
                        _alarm_regs = [REG_GRID_ALARM_B]
                        if self._grid_alarm_cycle % 3 == 0:
                            _alarm_regs += [REG_GRID_ALARM_A, REG_GRID_STATUS]
                        for reg in _alarm_regs:
                            p_alm = await self.query_modbus(reg, 1, timeout=0.5)
                            if p_alm:
                                self._parse_telemetry_payload(p_alm, 0x00, reg)

                        # Emitir telemetría consolidada al finalizar el ciclo
                        if self.on_telemetry:
                            self.on_telemetry(dict(self.state))

                        await asyncio.sleep(0.2)

            except Exception as e:
                logger.error(f"Excepción en bucle BLE: {e}")
                self.state["connected"] = False
                self.state["encryption_active"] = False
                self._notify_status(f"Reconectando ({e.__class__.__name__})...")
                await self._interruptible_sleep(backoff)
                backoff = min(backoff * 1.5, 10.0)

    def _on_disconnected(self, client: BleakClient):
        logger.warning("Estación Bluetti desconectada del enlace BLE")
        self.state["connected"] = False
        self.state["encryption_active"] = False
        self.state["dc_input_watts"] = 0
        self.state["dc_input_volts"] = 0.0
        self.state["dc_input_current"] = 0.0
        self.state["ac_input_watts"] = 0
        self.state["ac_input_volts"] = 0.0
        self.state["ac_input_current"] = 0.0
        self.state["ac_output_watts"] = 0
        self.state["dc_output_watts"] = 0
        with self._dc_pending_lock:
            self._dc_pending = None   # no aplicar cambios viejos tras reconectar
        if self._current_query_future and not self._current_query_future.done():
            self._current_query_future.cancel()
        if self.on_telemetry:
            self.on_telemetry(dict(self.state))
        self.state["ac_input_hz"] = 0.0
        self.state["ac_output_hz"] = 0.0
        self._prot_on_disconnect()
        self._notify_status("Desconectado. Reconectando automáticamente...")

    # -------------------------------------------------------------------------
    # PROCESAMIENTO CRIPTOGRÁFICO DE PAQUETES RECIBIDOS (RX) CON REENSAMBLADO
    # -------------------------------------------------------------------------
    def _on_rx_raw_packet(self, sender: BleakGATTCharacteristic, data: bytearray):
        """Despacha fragmentos crudos al bucle asyncio para reensamblado seguro."""
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._process_rx_chunk(bytes(data)), self.loop)

    async def _process_rx_chunk(self, chunk: bytes):
        """Reensambla fragmentos BLE antes de procesar o descifrar tramas."""
        self.rx_raw_buffer.extend(chunk)

        while len(self.rx_raw_buffer) > 0:
            # Caso A: Tramas de Negociación Inicial KEX_MAGIC (2A 2A) sin cifrar
            if self.rx_raw_buffer[:2] == KEX_MAGIC:
                if len(self.rx_raw_buffer) < 4:
                    break
                cmd_type = self.rx_raw_buffer[2]
                payload_len = self.rx_raw_buffer[3]
                total_kex_len = 4 + payload_len + 2
                if len(self.rx_raw_buffer) < total_kex_len:
                    break

                kex_frame = bytes(self.rx_raw_buffer[:total_kex_len])
                self.rx_raw_buffer = self.rx_raw_buffer[total_kex_len:]
                await self._handle_kex_unencrypted(kex_frame)
                continue

            # Caso B: Tramas cifradas con AES (Handshake avanzado y Modbus de telemetría)
            if self.unsecure_aes_key is not None:
                if len(self.rx_raw_buffer) < 2:
                    break
                data_len = (self.rx_raw_buffer[0] << 8) | self.rx_raw_buffer[1]

                if data_len > 512 or data_len == 0:
                    self.rx_raw_buffer.pop(0)
                    continue

                padded_len = ((data_len + 15) // 16) * 16
                iv_len = 4 if self.secure_aes_key is not None else 0
                total_enc_len = 2 + iv_len + padded_len

                if len(self.rx_raw_buffer) < total_enc_len:
                    break

                enc_frame = bytes(self.rx_raw_buffer[:total_enc_len])
                self.rx_raw_buffer = self.rx_raw_buffer[total_enc_len:]

                key = self.secure_aes_key if self.secure_aes_key is not None else self.unsecure_aes_key
                iv = None if self.secure_aes_key is not None else self.unsecure_aes_iv

                try:
                    decrypted = aes_decrypt(enc_frame, key, iv)
                    await self._handle_decrypted_frame(decrypted)
                except Exception as e:
                    logger.error(f"Fallo al descifrar paquete AES ({len(enc_frame)} bytes): {e}")
                continue

            self.rx_raw_buffer.pop(0)

    async def _handle_kex_unencrypted(self, raw: bytes):
        """Maneja desafíos iniciales de intercambio de claves."""
        cmd_type = raw[2]
        payload = raw[4:-2]

        if cmd_type == 0x01:  # DESAFÍO BLUETTI
            logger.info(f"Desafío recibido de estación: {raw.hex(' ').upper()}")
            self.unsecure_aes_iv = hashlib.md5(payload[::-1]).digest()
            static_key = bytes.fromhex(ConnConstantsV2.LOCAL_AES_KEY.value)
            self.unsecure_aes_key = hexxor(self.unsecure_aes_iv, static_key)

            body = bytes.fromhex("0204") + self.unsecure_aes_iv[8:12]
            resp = b"".join([KEX_MAGIC, body, hexsum(body, 2)])
            logger.info(f"Enviando respuesta a desafío: {resp.hex(' ').upper()}")
            await self._write_ble_safe(resp, "Respuesta Desafío 0x02")

        elif cmd_type == 0x03:  # DESAFÍO ACEPTADO
            logger.info("Desafío aceptado por Bluetti. Negociando intercambio público ECDH...")

    async def _handle_decrypted_frame(self, decrypted: bytes):
        """Procesa tramas una vez descifradas con éxito."""
        if decrypted.startswith(KEX_MAGIC):
            cmd_type = decrypted[2]
            payload = memoryview(decrypted)[4:-2]

            if cmd_type == 0x04:  # Clave Pública de la Estación (PEER_PUBKEY)
                logger.info("Clave pública de estación recibida. Verificando firma y generando clave local...")
                data = verify_and_extract_signed_data(payload, self.unsecure_aes_iv)
                self.peer_pubkey = pubkey_from_bytes(data)

                self.my_privkey = ec.generate_private_key(ec.SECP256R1())
                self.my_pubkey = self.my_privkey.public_key()
                my_pub_bytes = pubkey_to_bytes(self.my_pubkey)

                signing_secret = int.from_bytes(bytes.fromhex(SignatureCrypt.PRIVATE_KEY_L1.value), "big")
                signing_key = ec.derive_private_key(signing_secret, ec.SECP256R1())
                to_sign = my_pub_bytes + self.unsecure_aes_iv
                signature = signing_key.sign(to_sign, ec.ECDSA(hashes.SHA256()))
                raw_sig = der_to_raw_ecdsa(signature)

                body = b"".join([bytes.fromhex("0580"), my_pub_bytes, raw_sig])
                msg = b"".join([KEX_MAGIC, body, hexsum(body, 2)])
                encrypted_msg = aes_encrypt(msg, self.unsecure_aes_key, self.unsecure_aes_iv)

                logger.info(f"Enviando clave pública firmada local ({len(encrypted_msg)} bytes)...")
                await self._write_ble_safe(encrypted_msg, "Clave Pública Firmada 0x05")
                return

            elif cmd_type == 0x06:  # Clave Aceptada (PUBKEY_ACCEPTED)
                self.secure_aes_key = self.my_privkey.exchange(ec.ECDH(), self.peer_pubkey)
                logger.info(f"¡Handshake ECDH Exitoso! Clave AES de Sesión: {self.secure_aes_key.hex()[:16]}...")
                self.ecdh_ready_event.set()
                return

        self._handle_modbus_decrypted(decrypted)

    def _handle_modbus_decrypted(self, data: bytes):
        """Procesa paquetes Modbus descifrados asegurando integridad de CRC16."""
        self.rx_modbus_buffer.extend(data)

        while len(self.rx_modbus_buffer) >= 5:
            if self.rx_modbus_buffer[0] != 0x01:
                self.rx_modbus_buffer.pop(0)
                continue

            func_code = self.rx_modbus_buffer[1]

            # Manejo de Excepciones Modbus devueltas por la estación
            if func_code & 0x80:
                if len(self.rx_modbus_buffer) < 5:
                    break
                exc_pkt = bytes(self.rx_modbus_buffer[:5])
                self.rx_modbus_buffer = self.rx_modbus_buffer[5:]
                exc_code = exc_pkt[2]
                logger.warning(f"Excepción Modbus de estación: Función 0x{func_code:02X}, Código Error: 0x{exc_code:02X}")
                if self._current_query_future and not self._current_query_future.done():
                    self._current_query_future.set_result(None)
                continue

            if func_code == 0x03:  # Read Holding Registers
                if len(self.rx_modbus_buffer) < 3:
                    break
                byte_count = self.rx_modbus_buffer[2]
                total_len = 3 + byte_count + 2

                if len(self.rx_modbus_buffer) < total_len:
                    break

                packet = bytes(self.rx_modbus_buffer[:total_len])
                self.rx_modbus_buffer = self.rx_modbus_buffer[total_len:]

                if verify_crc16(packet):
                    payload = packet[3: 3 + byte_count]
                    expected = getattr(self, "_current_query_bytes", None)
                    if (expected is not None and byte_count != expected
                            and self._current_query_future and not self._current_query_future.done()):
                        # Es la respuesta TARDIA de otra lectura: interpretarla como la actual
                        # mostraria valores absurdos durante un instante (p. ej. tension de red 2 V).
                        logger.warning("Respuesta Modbus de %d bytes descartada: se esperaban %d "
                                       "(respuesta tardia de otra lectura)", byte_count, expected)
                    elif self._current_query_future and not self._current_query_future.done():
                        self._current_query_future.set_result(payload)
                    else:
                        logger.debug(f"Respuesta Modbus 0x03 ({byte_count} bytes) recibida fuera de ventana síncrona.")
                else:
                    logger.warning("Fallo de CRC16 en respuesta Modbus 0x03")

            elif func_code == 0x06:  # Write Single Register Confirmación
                if len(self.rx_modbus_buffer) < 8:
                    break
                packet = bytes(self.rx_modbus_buffer[:8])
                self.rx_modbus_buffer = self.rx_modbus_buffer[8:]

                if verify_crc16(packet):
                    reg = (packet[2] << 8) | packet[3]
                    val = (packet[4] << 8) | packet[5]
                    logger.info(f"¡Comando Modbus 0x06 aceptado por Bluetti! Registro 0x{reg:04X} = {val}")
                    if reg in (REG_W_SWITCH_AC, REG_CONTROL_AC, REG_CONTROL_AC_LEG):
                        self._ac_hold_val = (val == 0x0001)
                        self._ac_hold_until = max(getattr(self, "_ac_hold_until", 0.0), time.time() + 6.0)
                        self.state["ac_output_on"] = (val == 0x0001)
                    elif reg in (REG_W_SWITCH_DC, REG_CONTROL_DC, REG_CONTROL_DC_LEG):
                        self.state["dc_output_on"] = (val == 0x0001)
                    elif reg == REG_W_ECO_DC:
                        self.state["eco_dc_mode_on"] = (val == 0x0001)
                        self.state["eco_mode_on"] = (
                            self.state["eco_dc_mode_on"] or self.state["eco_ac_mode_on"]
                        )
                    elif reg == REG_W_ECO_AC:
                        self.state["eco_ac_mode_on"] = (val == 0x0001)
                        self.state["eco_mode_on"] = (
                            self.state["eco_dc_mode_on"] or self.state["eco_ac_mode_on"]
                        )
                    elif reg in (REG_CONTROL_ECO, REG_CONTROL_ECO_LEG):
                        self.state["eco_mode_on"] = (val == 0x0001)
                        self.state["eco_dc_mode_on"] = self.state["eco_mode_on"]
                        self.state["eco_ac_mode_on"] = self.state["eco_mode_on"]
                    elif reg in (REG_W_POWER_LIFTING, REG_CONTROL_PL):
                        self.state["power_lifting_on"] = (val == 0x0001)
                    elif reg in (REG_W_CHARGE_MODE, REG_CONTROL_CHARGE):
                        modes = {0: "standard", 1: "silent", 2: "turbo", 4: "custom"}
                        self.state["charge_mode"] = modes.get(val, "standard")
                    elif reg in (REG_CONTROL_GRID,):
                        self.state["grid_enhancement_on"] = (val == 0x0001)
                    if self.on_telemetry:
                        self.on_telemetry(dict(self.state))
                else:
                    logger.warning("Fallo de CRC16 en respuesta Modbus 0x06")

            elif func_code == 0x10:  # Write Multiple Registers Confirmación
                if len(self.rx_modbus_buffer) < 8:
                    break
                packet = bytes(self.rx_modbus_buffer[:8])
                self.rx_modbus_buffer = self.rx_modbus_buffer[8:]

                if verify_crc16(packet):
                    reg = (packet[2] << 8) | packet[3]
                    logger.info(f"¡Comando Modbus 0x10 aceptado por Bluetti! Registro 0x{reg:04X}")
                else:
                    logger.warning("Fallo de CRC16 en respuesta Modbus 0x10")
            else:
                self.rx_modbus_buffer.pop(0)

    # -------------------------------------------------------------------------
    # ANALISIS DE CELDAS (balance, alertas criticas, calibracion del SOC)
    # -------------------------------------------------------------------------
    def _update_cell_analysis(self):
        """Se llama cada vez que llega una mitad de las celdas. Solo analiza
        cuando ya hay las 16 lecturas y todas son plausibles."""
        cells = list(self._cells_buf)
        if any(c is None for c in cells):
            return
        if not all(1500 <= c <= 4500 for c in cells):
            logger.warning("Celdas con valores implausibles, se ignora la lectura: %s", cells)
            return

        mn, mx = min(cells), max(cells)
        delta = mx - mn
        avg = sum(cells) / len(cells)
        soc = self.state.get("soc")

        self.state["cell_mv"] = cells
        self.state["cell_min_mv"] = mn
        self.state["cell_max_mv"] = mx
        self.state["cell_min_idx"] = cells.index(mn) + 1
        self.state["cell_max_idx"] = cells.index(mx) + 1
        self.state["cell_delta_mv"] = delta
        self.state["cell_avg_mv"] = round(avg, 1)

        net = (self.state.get("dc_input_watts", 0) + self.state.get("ac_input_watts", 0)
               - self.state.get("ac_output_watts", 0) - self.state.get("dc_output_watts", 0))
        self.state["cell_peak_mv"] = max(mx, self.state.get("cell_peak_mv") or 0)
        self.state["cell_trough_mv"] = min(mn, self.state.get("cell_trough_mv") or 99999)
        if ((soc is not None and soc >= BALANCE_SETTLE_SOC) or self._cell_zone == "top") and net >= 30:
            self._top_chg_ts = time.time()   # cargando a fondo: las celdas se estan asentando
        self._evaluate_balance(cells, mn, mx, delta, net)
        self._evaluate_cell_alerts(cells)

        self._evaluate_soc_calibration(avg, soc)
        try:
            self._cell_log_update(cells, mn, mx, delta, net)
        except Exception as exc:
            logger.debug("Registro de celdas: %s", exc)

    # -------------------------------------------------------------------------
    # REGISTRO DE SALUD INDIVIDUAL DE CELDAS
    # cell_events.csv: un evento cada vez que una celda pasa a ser la más baja/alta del pack
    # (confirmada CELL_LOG_CONFIRM lecturas seguidas y con dispersión >= CELL_LOG_MIN_DELTA_MV)
    # o entra en aviso/alerta, con SOC, potencia y zona de ese momento.
    # cell_stats.json: acumulado por celda (veces más baja/alta, segundos como tal, extremos, alertas).
    # Solo lectura de datos que ya se tienen: no escribe nada en el equipo.
    # -------------------------------------------------------------------------
    @staticmethod
    def _cell_stat_blank() -> Dict[str, Any]:
        return {"min_count": 0, "max_count": 0, "min_s": 0.0, "max_s": 0.0,
                "warn_count": 0, "alert_count": 0,
                "lowest_mv": None, "lowest_ts": None, "highest_mv": None, "highest_ts": None,
                "last_min": None, "last_max": None}

    def _cell_log_init(self):
        folder = os.path.dirname(_prot_marker_path())
        self._cl_csv = os.path.join(folder, "cell_events.csv")
        self._cl_json = os.path.join(folder, "cell_stats.json")
        self._cl_stats = {str(i): self._cell_stat_blank() for i in range(1, 17)}
        self._cl_since = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with open(self._cl_json, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._cl_since = data.get("since", self._cl_since)
            for k, v in (data.get("cells") or {}).items():
                if k in self._cl_stats and isinstance(v, dict):
                    self._cl_stats[k].update(v)
        except Exception:
            pass
        self._cl_hold = {"min": None, "max": None}
# ===== [sync-datos] =====
        # [sync-datos] cuentas recibidas de OTRA PC (cell_stats.json): nunca se pierden
        try:
            self._cl_peers = (data.get("peers") if isinstance(data.get("peers"), dict) else {})
        except Exception:
            self._cl_peers = {}
        self._cl_cand = {"min": (None, 0), "max": (None, 0)}
        self._cl_last_ts: Optional[float] = None
        self._cl_prev_flags: set = set()
        self._cl_dirty = False
        self._cl_save_ts = time.time()

    def _cell_log_save(self):
        try:
            tmp = self._cl_json + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"since": self._cl_since, "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
                           # [sync-datos] las cuentas de otras PC se guardan intactas
                           "cells": self._cl_stats,
                           "peers": self._cl_peers_now()}, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self._cl_json)
            self._cl_dirty = False
        except Exception:
            pass
        self._cl_save_ts = time.time()

    def _cell_log_event(self, event: str, idx: int, mv: int, other_idx: Optional[int], delta: int, net: float):
        try:
            if os.path.exists(self._cl_csv) and os.path.getsize(self._cl_csv) > CELL_LOG_MAX_CSV_BYTES:
                os.replace(self._cl_csv, self._cl_csv + ".old")
            new = not os.path.exists(self._cl_csv)
            st = self.state
            temp = st.get("batt_temp")
            with open(self._cl_csv, "a", encoding="utf-8") as f:
                if new:
                    f.write("fecha,evento,celda,mV,otra_celda,delta_mV,soc,potencia_neta_W,zona,temp_C\n")
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')},{event},{idx},{mv},"
                        f"{other_idx if other_idx else ''},{delta},{st.get('soc', '')},{net:.0f},"
                        f"{st.get('cell_zone', '')},{'' if temp is None else temp}\n")
        except Exception:
            pass

    def _cell_log_update(self, cells: List[int], mn: int, mx: int, delta: int, net: float):
        if not CELL_LOG_ENABLED:
            return
        now = time.time()
        dt = 0.0 if self._cl_last_ts is None else min(max(now - self._cl_last_ts, 0.0), 5.0)
        self._cl_last_ts = now
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        mn_i, mx_i = cells.index(mn) + 1, cells.index(mx) + 1
        S = self._cl_stats

        # Extremos absolutos por celda (lectura a lectura)
        lo = S[str(mn_i)]
        if lo["lowest_mv"] is None or mn < lo["lowest_mv"]:
            lo["lowest_mv"], lo["lowest_ts"] = mn, stamp
            self._cl_dirty = True
        hi = S[str(mx_i)]
        if hi["highest_mv"] is None or mx > hi["highest_mv"]:
            hi["highest_mv"], hi["highest_ts"] = mx, stamp
            self._cl_dirty = True

        # Más baja / más alta del pack, con antirrebote y solo si la dispersión es significativa
        for kind, idx, val, other in (("min", mn_i, mn, mx_i), ("max", mx_i, mx, mn_i)):
            if delta < CELL_LOG_MIN_DELTA_MV:
                self._cl_cand[kind] = (None, 0)
                continue
            if idx == self._cl_hold[kind]:
                self._cl_cand[kind] = (None, 0)
                S[str(idx)][f"{kind}_s"] = round(S[str(idx)][f"{kind}_s"] + dt, 1)
                self._cl_dirty = True
                continue
            c_idx, c_n = self._cl_cand[kind]
            c_n = c_n + 1 if c_idx == idx else 1
            self._cl_cand[kind] = (idx, c_n)
            if c_n >= CELL_LOG_CONFIRM:
                self._cl_hold[kind] = idx
                self._cl_cand[kind] = (None, 0)
                S[str(idx)][f"{kind}_count"] += 1
                S[str(idx)][f"last_{kind}"] = stamp
                self._cell_log_event("MAS_BAJA" if kind == "min" else "MAS_ALTA", idx, val, other, delta, net)
                self._cell_log_save()

        # Entradas en aviso / alerta por voltaje
        st = self.state
        flags = set()
        for i in st.get("cell_alert_cells", []) or []:
            flags.add(("ALERTA_BAJA" if st.get("cell_alert") == "low" else "ALERTA_ALTA", i))
        for i in st.get("cell_warn_cells", []) or []:
            flags.add(("AVISO_BAJA" if st.get("cell_warn") == "low" else "AVISO_ALTA", i))
        for ev, i in sorted(flags - self._cl_prev_flags):
            S[str(i)]["alert_count" if ev.startswith("ALERTA") else "warn_count"] += 1
            self._cell_log_event(ev, i, cells[i - 1], None, delta, net)
            self._cell_log_save()
        self._cl_prev_flags = flags

        if self._cl_dirty and now - self._cl_save_ts >= CELL_LOG_SAVE_S:
            self._cell_log_save()

    def _balance_settling(self) -> bool:
        """True mientras dura el asentamiento tras cargar a fondo (SOC alto / codo superior)."""
        return time.time() - getattr(self, "_top_chg_ts", 0.0) < BALANCE_SETTLE_S

    def _evaluate_balance(self, cells: List[int], mn: int, mx: int, delta: int, net: float):
        """Estado de equilibrio segun la zona de la curva (por voltaje), con el Δ tolerado
        compensado por la potencia (2.5 mV por cada 100 W, con tope) y antirrebote."""
        st = self.state
        # Zona con histeresis
        zone = self._cell_zone
        if zone == "top":
            if mx < ZONE_TOP_EXIT_MV:
                zone = "plateau"
        elif zone == "bottom":
            if mn > ZONE_BOTTOM_EXIT_MV:
                zone = "plateau"
        if zone == "plateau":
            if mx > ZONE_TOP_MV:
                zone = "top"
            elif mn < ZONE_BOTTOM_MV:
                zone = "bottom"
        self._cell_zone = zone

        base = BALANCE_GRADES_MV[zone]
        offset = min(abs(net) / 100.0 * BALANCE_POWER_MV_PER_100W, BALANCE_POWER_MAX_MV)
        limits = tuple(v + offset for v in base)
        grade = BALANCE_GRADE_NAMES[-1]
        for name, lim in zip(BALANCE_GRADE_NAMES, limits):
            if delta <= lim:
                grade = name
                break
        settling = self._balance_settling()
        st["cell_settling"] = settling
        if settling and grade in ("fair", "bad"):
            grade = "acceptable"   # asentandose tras cargar a fondo: la dispersion es transitoria, no se avisa
        # Estado de 4 niveles (compatible con el widget): optimo/bueno -> balanced
        cand = {"excellent": "balanced", "good": "balanced", "acceptable": "acceptable",
                "fair": "slight", "bad": "unbalanced"}[grade]

        # Antirrebote: el estado solo cambia tras BALANCE_CONFIRM_COUNT evaluaciones iguales
        if st.get("cell_balance") is None:
            st["cell_balance"] = cand
            self._bal_candidate, self._bal_count = None, 0
        elif cand == st["cell_balance"]:
            self._bal_candidate, self._bal_count = None, 0
        else:
            if cand == self._bal_candidate:
                self._bal_count += 1
            else:
                self._bal_candidate, self._bal_count = cand, 1
            if self._bal_count >= BALANCE_CONFIRM_COUNT:
                st["cell_balance"] = cand
                self._bal_candidate, self._bal_count = None, 0

        st["cell_zone"] = zone
        st["cell_offset_mv"] = round(offset, 1)
        st["cell_balance_limits"] = tuple(round(v) for v in limits)
        st["cell_grade"] = self._grade_confirmed(grade)
        st["cell_balance_reliable"] = abs(net) <= BALANCE_RELIABLE_MAX_W

    def _grade_confirmed(self, grade: str) -> str:
        """El grado (5 niveles) usa el mismo antirrebote que el estado: solo cambia cuando el
        estado de 4 niveles cambio de forma confirmada, o cuando sigue en el mismo estado."""
        st = self.state
        cur = st.get("cell_grade")
        mapping = {"excellent": "balanced", "good": "balanced", "acceptable": "acceptable",
                   "fair": "slight", "bad": "unbalanced"}
        if cur is None or mapping[cur] != st.get("cell_balance"):
            # el estado confirmado manda: tomar el grado actual si coincide, si no el mas cercano
            if mapping[grade] == st.get("cell_balance"):
                return grade
            return {"balanced": "good", "acceptable": "acceptable", "slight": "fair",
                    "unbalanced": "bad"}.get(st.get("cell_balance"), grade)
        # mismo estado: permitir afinar entre excellent/good
        return grade if mapping[grade] == mapping[cur] else cur

    def _evaluate_cell_alerts(self, cells: List[int]):
        """Alertas por voltaje de celda en dos niveles (aviso / critica), con histeresis:
        una alerta activa no se apaga hasta que el voltaje vuelve CELL_ALERT_HYST_MV dentro."""
        st = self.state
        h = CELL_ALERT_HYST_MV

        def pick(prev_active: bool, enter_fn, stay_fn):
            fn = stay_fn if prev_active else enter_fn
            return [i + 1 for i, c in enumerate(cells) if fn(c)]

        prev_crit, prev_warn = st.get("cell_alert", "ok"), st.get("cell_warn", "ok")
        crit_low = pick(prev_crit == "low", lambda c: c <= CELL_CRIT_LOW_MV, lambda c: c <= CELL_CRIT_LOW_MV + h)
        crit_high = pick(prev_crit == "high", lambda c: c >= CELL_CRIT_HIGH_MV, lambda c: c >= CELL_CRIT_HIGH_MV - h)
        if crit_low:
            st["cell_alert"], st["cell_alert_cells"] = "low", crit_low
        elif crit_high:
            st["cell_alert"], st["cell_alert_cells"] = "high", crit_high
        else:
            st["cell_alert"], st["cell_alert_cells"] = "ok", []

        warn_low = pick(prev_warn == "low", lambda c: c <= CELL_WARN_LOW_MV, lambda c: c <= CELL_WARN_LOW_MV + h)
        warn_high = pick(prev_warn == "high", lambda c: c >= CELL_WARN_HIGH_MV, lambda c: c >= CELL_WARN_HIGH_MV - h)
        if st["cell_alert"] != "ok":
            st["cell_warn"], st["cell_warn_cells"] = "ok", []
        elif warn_low:
            st["cell_warn"], st["cell_warn_cells"] = "low", warn_low
        elif warn_high:
            st["cell_warn"], st["cell_warn_cells"] = "high", warn_high
        else:
            st["cell_warn"], st["cell_warn_cells"] = "ok", []

    def _evaluate_soc_calibration(self, avg_mv: float, soc: Optional[int]):
        """'Descalibrada' = el % que marca no cuadra con el voltaje real. Solo es
        comprobable en los extremos (LiFePO4 tiene curva plana en medio), y no
        mientras el voltaje esta distorsionado por carga/descarga fuerte."""
        st = self.state
        if soc is None:
            st["soc_calibration"] = "unknown"
            self._calib_bad_count = 0
            return
        net = (st.get("dc_input_watts", 0) + st.get("ac_input_watts", 0)
               - st.get("ac_output_watts", 0) - st.get("dc_output_watts", 0))
        suspect: Optional[bool] = None   # None = no evaluable ahora
        if soc <= CALIB_LOW_SOC and net <= 5:
            # Casi vacio segun el equipo, pero celdas altas y sin estar cargando
            suspect = avg_mv >= CALIB_LOW_SOC_MIN_MV
        elif soc >= CALIB_HIGH_SOC and net >= -200:
            # Casi lleno segun el equipo, pero celdas bajas y sin carga fuerte
            suspect = avg_mv <= CALIB_HIGH_SOC_MAX_MV

        if suspect is not None and self._balance_settling():
            suspect = None   # recien cargada a fondo: el voltaje aun no refleja el SOC
        if suspect is None:
            st["soc_calibration"] = "unknown"
            self._calib_bad_count = 0
        elif suspect:
            self._calib_bad_count += 1
            if self._calib_bad_count >= CALIB_CONFIRM_COUNT:
                st["soc_calibration"] = "suspect"
        else:
            self._calib_bad_count = 0
            st["soc_calibration"] = "ok"

    # -------------------------------------------------------------------------
    # CAPACIDAD Y SALUD ESTIMADAS
    # -------------------------------------------------------------------------
    @staticmethod
    def _cap_store_path() -> str:
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        folder = os.path.join(base, "BluettiElite100Widget")
        try:
            os.makedirs(folder, exist_ok=True)
        except Exception:
            pass
        return os.path.join(folder, "capacity_samples.json")

    def _load_cap_samples(self) -> List[float]:
        try:
            with open(self._cap_store_path(), "r", encoding="utf-8") as f:
                data = json.load(f)
            raw = [float(v) for v in data.get("samples", [])]
            dirs = [int(d) for d in data.get("dirs", [])]
            if len(dirs) != len(raw):
                dirs = [0] * len(raw)   # archivo antiguo (sin sentido): se conservan como "desconocido"
            keep = [(v, d) for v, d in zip(raw, dirs) if CAP_VALID_RANGE_WH[0] <= v <= CAP_VALID_RANGE_WH[1]]
            keep = keep[-CAP_MAX_SAMPLES:]
            self._cap_dirs = [d for _, d in keep]
            return [v for v, _ in keep]
        except Exception:
            self._cap_dirs = []
            return []

    def _save_cap_samples(self):
        try:
            with open(self._cap_store_path(), "w", encoding="utf-8") as f:
                json.dump({"samples": [round(v, 1) for v in self._cap_samples], "dirs": self._cap_dirs}, f)
        except Exception:
            pass

# ===== [sync-datos] =====
    # [sync-datos] recargar la capacidad medida tras recibirla de la otra PC (no reiniciar)
    def _cl_peers_now(self):
        """Cuentas de otras PC: lo que haya en disco ahora (la sincronizacion puede haberlas anadido)."""
        try:
            with open(self._cl_json, "r", encoding="utf-8") as f0:
                pe = json.load(f0).get("peers")
            if isinstance(pe, dict):
                self._cl_peers = pe
        except Exception:
            pass
        return getattr(self, "_cl_peers", {})

    def refresh_cap_samples(self):
        try:
            vals = self._load_cap_samples()
            self._cap_samples = vals
            self._publish_capacity()
        except Exception:
            pass

    def _publish_capacity(self):
        """Capacidad y salud mostradas = MEDIANA de las muestras medidas por conteo de energia
        (robusta ante una ventana mala). Salud limitada a 100 %."""
        st = self.state
        n = len(self._cap_samples)
        st["cap_health_samples"] = n
        if n == 0:
            return
        srt = sorted(self._cap_samples)
        med = srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2.0
        st["cap_est_wh"] = round(med)   # sin recortar: puede superar la nominal (carga incluye perdidas internas)
        st["cap_health_pct"] = round(med / RATED_WH * 100.0)
        st["cap_range_wh"] = (round(srt[0]), round(srt[-1]))
        # Mediana por sentido (solo muestras con sentido conocido), para ver si carga y descarga difieren
        for key, sg in (("chg", 1), ("dis", -1)):
            vs = sorted(v for v, d in zip(self._cap_samples, self._cap_dirs) if d == sg)
            m = len(vs)
            st[f"cap_{key}_n"] = m
            st[f"cap_{key}_wh"] = (round(vs[m // 2] if m % 2 else (vs[m // 2 - 1] + vs[m // 2]) / 2.0) if m else None)

    def _log_csv(self):
        """Registro pasivo (1 fila por minuto) para confirmar los registros candidatos:
        %LOCALAPPDATA%/BluettiElite100Widget/registro_bateria.csv"""
        now = time.time()
        if now - getattr(self, "_csv_last", 0.0) < 60.0:
            return
        st = self.state
        if st.get("soc") is None or not st.get("connected"):
            return
        self._csv_last = now
        try:
            path = os.path.join(os.path.dirname(self._cap_store_path()), "registro_bateria.csv")
            new = not os.path.exists(path) or os.path.getsize(path) > 3_000_000
            if not new:
                # Formato de columnas cambiado (se quitaron 6318/6319): archivo antiguo -> .old
                with open(path, "r", encoding="utf-8") as f0:
                    if "reg6318" in f0.readline():
                        os.replace(path, path + ".old")
                        new = True
            net = (st.get("dc_input_watts", 0) + st.get("ac_input_watts", 0)
                   - st.get("ac_output_watts", 0) - st.get("dc_output_watts", 0))
            row = [time.strftime("%Y-%m-%d %H:%M:%S"), st.get("soc"), net, st.get("pack_volts"), st.get("cell_avg_mv"),
                   st.get("cell_min_mv"), st.get("cell_max_mv"), st.get("batt_temp"), st.get("batt_current_cand"),
                   st.get("cap_rated_ah"), st.get("reg_6143"),
                   st.get("bms_v_target"), st.get("energy_chg_raw"), st.get("energy_dis_raw"), st.get("cap_est_wh")]
            with open(path, "w" if new else "a", encoding="utf-8") as f:
                if new:
                    f.write("hora,soc,potencia_neta_w,pack_v,celda_media_mv,celda_min_mv,celda_max_mv,temp_c,"
                            "reg6004_a,reg6131_ah,reg6143,reg6010_v,reg6019_chg,reg6021_dis,cap_medida_wh\n")
                f.write(",".join("" if v is None else str(v) for v in row) + "\n")
        except Exception:
            pass

    def _energy_now(self):
        st = self.state
        if st.get("energy_chg_raw") is None or st.get("energy_dis_raw") is None:
            return None
        return (st["energy_chg_raw"], st["energy_dis_raw"])

    def _counter_capacity(self, cc: dict, progress: int):
        """Contraste: capacidad segun los contadores de energia del BMS (Reg 6019/6021) en la misma
        ventana de SOC. Resolucion ~10 Wh -> unos +-7 % en 15 puntos. NO modifica la salud mostrada;
        sirve para comparar con la estimacion por potencia y decidir mas adelante."""
        e0, e1 = cc.get("e0"), self._energy_now()
        if not e0 or not e1 or progress <= 0:
            return
        key, delta = ("dis", e1[1] - e0[1]) if cc["dir"] < 0 else ("chg", e1[0] - e0[0])
        if delta < 5:
            return
        cap = delta * ENERGY_COUNTER_WH / (progress / 100.0)
        if not CAP_VALID_RANGE_WH[0] <= cap <= CAP_VALID_RANGE_WH[1]:
            return
        lst = self._capc[key]
        lst.append(cap)
        del lst[:-CAP_MAX_SAMPLES]
        srt = sorted(lst)
        n = len(srt)
        med = srt[n // 2] if n % 2 else (srt[n // 2 - 1] + srt[n // 2]) / 2.0
        self.state[f"cap_counter_{key}_wh"] = round(med)
        self.state[f"cap_counter_{key}_n"] = n
        logger.info("Capacidad por contador (%s): %.0f Wh (mediana %.0f, n=%d)", key, cap, med, n)

    def _cc_new(self, now: float, soc: int, sign: int):
        self._cc = {"t": now, "soc": soc, "sign": sign, "start": None, "dir": 0,
                    "energy": 0.0, "psum": 0.0, "time": 0.0, "idle": 0.0}
        self.state["cap_window_progress"] = None

    def _update_capacity_coulomb(self):
        """Capacidad por conteo de energia. Ventana: desde una transicion del SOC hasta la
        que esta CAP_WINDOW_SOC puntos despues (cargando o descargando), integrando la
        potencia neta (x rendimiento) que entra/sale de la bateria. Se descarta si cambia
        el sentido, hay huecos de datos, el SOC va en contra, o la potencia media es baja.
        capacidad(Wh) = energia / (puntos de SOC / 100). Precision esperada ~ +-10 %."""
        st = self.state
        soc = st.get("soc")
        now = time.monotonic()
        if soc is None or not (CAP_SOC_MIN <= soc <= CAP_SOC_MAX):
            self._cc = None
            st["cap_window_progress"] = None
            return
        net = (st.get("dc_input_watts", 0) + st.get("ac_input_watts", 0)
               - st.get("ac_output_watts", 0) - st.get("dc_output_watts", 0))
        sign = 1 if net >= 30 else (-1 if net <= -30 else 0)
        cc = self._cc
        if cc is None:
            self._cc_new(now, soc, sign)
            return
        dt = now - cc["t"]
        cc["t"] = now
        if dt <= 0 or dt > CAP_MAX_GAP_S:
            self._cc_new(now, soc, sign)
            return

        if sign == 0:
            cc["idle"] += dt
            if cc["idle"] > 120 or (soc != cc["soc"] and cc["start"] is not None):
                self._cc_new(now, soc, 0)   # reposo largo o el SOC se movio sin potencia: ventana invalida
            else:
                cc["soc"] = soc
            return
        cc["idle"] = 0.0
        if cc["start"] is not None and sign != cc["dir"]:
            self._cc_new(now, soc, sign)   # cambio de sentido
            return
        cc["sign"] = sign

        if cc["start"] is not None:
            bat_p = net * CHARGE_EFFICIENCY if net > 0 else net * CAP_DISCHARGE_FACTOR
            cc["energy"] += abs(bat_p) * dt / 3600.0
            cc["psum"] += abs(net) * dt
            cc["time"] += dt

        if soc != cc["soc"]:
            prev, cc["soc"] = cc["soc"], soc
            if cc["start"] is None:
                # Empezar justo en una transicion del entero (minimiza el error de cuantizacion)
                cc.update(start=soc, dir=sign, energy=0.0, psum=0.0, time=0.0, e0=self._energy_now())
            elif (soc - prev) * cc["dir"] < 0:
                self._cc_new(now, soc, sign)   # el SOC fue en contra del sentido de la potencia
                return
            progress = abs(soc - cc["start"]) if cc["start"] is not None else 0
            if progress >= CAP_WINDOW_SOC:
                avg_p = cc["psum"] / cc["time"] if cc["time"] > 0 else 0.0
                cap = cc["energy"] / (progress / 100.0)
                if (avg_p >= CAP_MIN_POWER_W and cc["time"] <= 12 * 3600
                        and CAP_VALID_RANGE_WH[0] <= cap <= CAP_VALID_RANGE_WH[1]):
                    self._cap_samples.append(cap)
                    self._cap_dirs.append(cc["dir"])
                    self._cap_samples = self._cap_samples[-CAP_MAX_SAMPLES:]
                    self._cap_dirs = self._cap_dirs[-CAP_MAX_SAMPLES:]
                    self._save_cap_samples()
                    self._publish_capacity()
                    logger.info("Capacidad medida por energia: %.0f Wh (ventana de %d puntos, %.0f W medios)",
                                cap, progress, avg_p)
                    self._counter_capacity(cc, progress)
                # Encadenar: la ventana siguiente empieza en esta misma transicion
                cc.update(start=soc, energy=0.0, psum=0.0, time=0.0, e0=self._energy_now())
                progress = 0
        else:
            progress = abs(soc - cc["start"]) if cc["start"] is not None else 0
        st["cap_window_progress"] = progress if cc["start"] is not None else None

    def _set_grid_alarm(self, key: str, value: int):
        """Guarda un registro crudo de la alarma de red y recalcula la bandera.
        Se usa 1134; 126 solo si 1134 aun no ha respondido."""
        prev = self.state.get(key)
        if prev != value:
            logger.info("[ALARMA RED] %s: %s -> %s (refuerzo_red=%s, V_red=%s)", key, prev, value,
                        self.state.get("grid_enhancement_on"), self.state.get("ac_input_volts"))
        self.state[key] = value
        src = self.state.get("grid_alarm_1134")
        if src is None:
            src = self.state.get("grid_alarm_126")
        self.state["grid_low_voltage_alarm"] = bool(src is not None and (src & GRID_LOW_VOLT_MASK))

    def _parse_telemetry_payload(self, *args, **kwargs):  # [alertas-patch]
        # Antes de interpretar cada lectura se vuelcan a las constantes del modulo
        # (PROT_*, CELL_*, SPIKE_*) los valores configurados en alert_config.py.
        alert_config.apply_to_ble(globals(), self.state)
        return self._parse_telemetry_payload_impl(*args, **kwargs)

    def _parse_telemetry_payload_impl(self, p: bytes, page: int = 0x00, offset: int = 0x00):
        """Decodifica los registros de telemetría reales de la Bluetti Elite 100 V2."""
        num_words = len(p) // 2
        if num_words < 1:
            return

        def r16(word_idx: int) -> int:
            off = word_idx * 2
            if off + 1 < len(p):
                return (p[off] << 8) | p[off + 1]
            return 0

        def r16_s(word_idx: int) -> int:
            v = r16(word_idx)
            return v - 65536 if v > 32767 else v

        # 1. Batería: SoC (Reg 102) + Tiempo restante en minutos (Reg 104)
        if offset == 100:
            if num_words >= 3:
                soc_val = r16(2)
                if 0 <= soc_val <= 100:
                    self.state["soc"] = soc_val
            if num_words >= 5:
                runtime_min = r16(4)
                if 0 < runtime_min < 100000:
                    self.state["runtime_minutes"] = runtime_min
            self._update_capacity_coulomb()
            self._publish_capacity()
            self._log_csv()

        # 2. Bloque de Potencias reales (Reg 140 DC-Out, 142 AC-Out, 144 DC-In/Solar, 146 AC-In)
        elif offset == 140:
            if num_words >= 1:
                dc_out_w = r16(0)
                # Un valor imposible (>=3000 W en DC...) se descarta y se CONSERVA el ultimo
                # bueno; antes caia a 0 W, y esa lectura corrupta se veia como un parpadeo.
                prev_dc = self.state.get("dc_output_watts", 0)
                if dc_out_w > 0:
                    self.state["dc_output_on"] = True
                    self.state["dc_output_watts"] = dc_out_w if dc_out_w < 3000 else prev_dc
                else:
                    if self.state.get("dc_output_on", False):
                        self.state["dc_output_watts"] = dc_out_w if dc_out_w < 3000 else prev_dc
                    else:
                        self.state["dc_output_watts"] = 0

            if num_words >= 3:
                ac_w = r16(2)
                self.state["ac_output_watts"] = ac_w if ac_w < 5000 else self.state.get("ac_output_watts", 0)
                if self.state["ac_output_watts"] > 5:
                    self._ac_set(True, "potencia")

            if num_words >= 5:
                sol_w = r16(4)
                self.state["dc_input_watts"] = sol_w if sol_w < 2000 else self.state.get("dc_input_watts", 0)
                # El voltaje y la corriente solar ya NO se derivan de aquí: se leen
                # del bloque INV_PV_INFO (Reg 1212-1213), ver offset == 1212.

            if num_words >= 7:
                self.state["ac_input_watts"] = r16(6)

            now_p = time.time()
            self._last_power_ts = now_p
            if num_words >= 8 and now_p - self._diag_log_ts >= 10.0:   # antes: una linea por lectura
                self._diag_log_ts = now_p
                logger.info(
                    "[DIAGNÓSTICO Reg140-147] reg141=%s reg143=%s reg145=%s reg147=%s "
                    "(valores crudos uint16, compara contra la app oficial Bluetti)",
                    r16(1), r16(3), r16(5), r16(7)
                )
            self._spike_feed()   # deteccion de picos de arranque sobre el consumo (salida AC + DC)

        # 2b. Entrada solar real: Reg 1212 = potencia (W), Reg 1213 = voltaje (0.1V).
        # La corriente se CALCULA como W / V (no se lee de ningún registro).
        elif offset == 1212:
            if num_words >= 2:
                pv_w = r16(0)
                pv_v = r16(1) / 10.0
                if 5.0 <= pv_v <= 200.0:
                    self.state["dc_input_volts"] = round(pv_v, 1)
                    self.state["dc_input_volts_confirmed"] = True
                    calc_a = round(pv_w / pv_v, 1) if pv_w > 0 else 0.0
                    measured_a = r16(2) / 10.0 if num_words >= 3 else None
                    # Corriente REAL (Reg 1214) si es plausible; si no, W / V de respaldo.
                    if measured_a is not None and 0.0 <= measured_a <= 60.0:
                        self.state["dc_input_current"] = round(measured_a, 1)
                        self.state["dc_input_current_source"] = "medida"
                        if abs(measured_a - calc_a) > 1.5:
                            logger.info(
                                "[CORRIENTE SOLAR] medida (Reg1214)=%.1fA vs calculada (W/V)=%.1fA",
                                measured_a, calc_a,
                            )
                    else:
                        self.state["dc_input_current"] = calc_a
                        self.state["dc_input_current_source"] = "calculada"
                else:
                    self.state["dc_input_volts"] = 0.0
                    self.state["dc_input_volts_confirmed"] = False
                    self.state["dc_input_current"] = 0.0
                    self.state["dc_input_current_source"] = "ninguna"

        # 3. Voltaje y corriente reales de entrada AC (Reg 1314-1315)
        elif offset == 1314:
            if num_words >= 1:
                v_in = r16(0) / 10.0
                # 0 V = sin red; entre 0 y 50 V no es una tension de red real (lectura corrupta)
                if v_in == 0.0 or v_in >= AC_VOLT_MIN_PLAUSIBLE_V:
                    self.state["ac_input_volts"] = round(v_in, 1)
                else:
                    logger.warning("Tension de entrada AC implausible (%.1f V): se descarta la lectura", v_in)
            if num_words >= 2:
                a_in = r16(1) / 10.0
                if a_in <= 60.0:
                    self.state["ac_input_current"] = round(a_in, 1)
                else:
                    logger.warning("Corriente de entrada AC implausible (%.1f A): se descarta la lectura", a_in)

        # 4. Modos ECO de los rieles DC/AC y Power Lifting
        elif offset == REG_W_ECO_DC:
            self.state["eco_dc_mode_on"] = (r16(0) == 1)
            self.state["eco_mode_on"] = (
                self.state["eco_dc_mode_on"] or self.state["eco_ac_mode_on"]
            )
        elif offset == REG_W_ECO_AC:
            self.state["eco_ac_mode_on"] = (r16(0) == 1)
            self.state["eco_mode_on"] = (
                self.state["eco_dc_mode_on"] or self.state["eco_ac_mode_on"]
            )
        elif offset == REG_W_POWER_LIFTING:
            self.state["power_lifting_on"] = (r16(0) == 1)
        elif offset == REG_CONTROL_GRID:
            self.state["grid_enhancement_on"] = (r16(0) == 1)
        elif offset == REG_W_CHARGE_MODE:
            modes = {0: "standard", 1: "silent", 2: "turbo", 4: "custom"}
            self.state["charge_mode"] = modes.get(r16(0), self.state.get("charge_mode", "standard"))

        # 2c. Frecuencias (escala 0.1 Hz; 0 = sin red / sin salida)
        elif offset == REG_R_AC_INPUT_HZ:
            raw_hz = r16(0)
            self.state["ac_input_hz"] = round(raw_hz / 10.0, 1) if 0 < raw_hz < 1000 else 0.0
        elif offset == REG_R_AC_OUTPUT_HZ:
            raw_hz = r16(0)
            self.state["ac_output_hz"] = round(raw_hz / 10.0, 1) if 0 < raw_hz < 1000 else 0.0

        # 2d. Limite AC del modo personalizado (Reg 2214, amperios enteros)
        elif offset == REG_GRID_MAX_CURRENT:
            amps = r16(0)
            if 0 < amps <= 60:
                self.state["grid_max_current"] = amps

        # 2e. Fuente DC y alta corriente (Reg 2269, dos bytes en una palabra)
        #   byte bajo: 2 = PV, 1 = Otros
        #   byte alto: 1 = alta corriente ACTIVA (solo con PV); 2 o 0 = apagada / no aplica
        elif offset == REG_DC_SOURCE:
            raw_src = r16(0)
            lo, hi = raw_src & 0xFF, (raw_src >> 8) & 0xFF
            self.state["dc_source_raw"] = raw_src
            self.state["dc_source"] = {2: "pv", 1: "other"}.get(lo)
            self.state["dc_high_current_on"] = (lo == 2 and hi == 1)

        # 2f. Pack de baterias, bloque 6003-6004:
        #   6003 = voltaje total (0.01 V); 6004 = corriente de bateria candidata (0.1 A)
        elif offset == REG_BATT_VOLT:
            pack_v = r16(0) / 100.0
            if 20.0 <= pack_v <= 120.0:
                self.state["pack_volts"] = round(pack_v, 2)
            if num_words >= 2:
                self.state["batt_current_cand"] = round(r16(1) / 10.0, 1)   # magnitud sin signo

        # 2g. Celdas 1-8 (Reg 6302) y 9-16 (Reg 6310), en mV
        elif offset in (REG_CELLS_1, REG_CELLS_2):
            base = 0 if offset == REG_CELLS_1 else 8
            for i in range(min(num_words, 8)):
                self._cells_buf[base + i] = r16(i)
            self._update_cell_analysis()

        # 2h. Temperaturas: 4 sensores (valor = °C + 40). Se muestra el promedio.
        elif offset == REG_TEMPS:
            temps = []
            for i in range(min(num_words, 4)):
                raw_t = r16(i)
                if raw_t > 0:
                    t_c = raw_t - TEMP_OFFSET
                    if -20 <= t_c <= 100:
                        temps.append(t_c)
            self.state["batt_temps"] = temps
            self.state["batt_temp"] = round(sum(temps) / len(temps), 2) if temps else None
            self.state["batt_temp_min"] = min(temps) if temps else None
            self.state["batt_temp_max"] = max(temps) if temps else None

        # 2i. Capacidad nominal (6131, mAh) y 6143 crudo
        elif offset == REG_CAP_RATED:
            self.state["cap_rated_ah"] = round(r16(0) / 1000.0, 3)
        elif offset == REG_UNKNOWN_6143:
            self.state["reg_6143"] = r16(0)
        elif offset == REG_BMS_LIMITS:
            self.state["bms_v_target"] = round(r16(0) / 10.0, 1)
            if num_words >= 2:
                self.state["bms_reg_6011"] = r16(1)
            if num_words >= 3:
                self.state["bms_reg_6012"] = r16(2)
        elif offset == REG_BMS_ENERGY and num_words >= 3:
            chg, dis = r16(0), r16(2)          # 6019 cargada, 6021 descargada (6020 = 0)
            st_ = self.state
            st_["energy_chg_raw"], st_["energy_dis_raw"] = chg, dis
            st_["energy_chg_kwh"] = round(chg * ENERGY_COUNTER_WH / 1000.0, 2)
            st_["energy_dis_kwh"] = round(dis * ENERGY_COUNTER_WH / 1000.0, 2)
            st_["cycles_eq"] = round(dis * ENERGY_COUNTER_WH / RATED_WH, 1)
            st_["cycles_chg_eq"] = round(chg * ENERGY_COUNTER_WH / RATED_WH, 1)
            st_["cycles_life_pct"] = round(min(100.0, st_["cycles_eq"] / CYCLE_LIFE_CYCLES * 100.0), 2)

        # 5. Voltaje real de salida AC (Reg 1511)
        # Alarma de red del equipo (126 / 1134 / 161)
        elif offset == REG_GRID_ALARM_A:
            self._set_grid_alarm("grid_alarm_126", r16(0))
        elif offset == REG_GRID_ALARM_B:
            self._set_grid_alarm("grid_alarm_1134", r16(0))
        elif offset == REG_GRID_STATUS:
            raw_st = r16(0)
            if self.state.get("grid_status_161") != raw_st:
                logger.info("[ALARMA RED] grid_status_161: %s -> %s (refuerzo_red=%s, V_red=%s)",
                            self.state.get("grid_status_161"), raw_st,
                            self.state.get("grid_enhancement_on"), self.state.get("ac_input_volts"))
            self.state["grid_status_161"] = raw_st

        elif offset == 1511:
            if num_words >= 1:
                v_out = r16(0) / 10.0
                if v_out >= AC_VOLT_MIN_PLAUSIBLE_V:
                    self._acv_low_n = 0
                    self.state["ac_output_volts"] = round(v_out, 1)
                elif v_out == 0.0:
                    self._acv_low_n = 0
                    self.state["ac_output_volts"] = 0.0
                else:
                    # Entre 0 y 50 V: una lectura suelta puede ser corrupta, pero si se repite es real (la salida
                    # apagada decae por esos valores). Antes se descartaban siempre y el ultimo voltaje alto
                    # (p. ej. 120 V) quedaba congelado: el switch AC se leia como encendido para siempre.
                    self._acv_low_n = getattr(self, "_acv_low_n", 0) + 1
                    if self._acv_low_n >= 2:
                        self.state["ac_output_volts"] = round(v_out, 1)
                    else:
                        logger.warning("Tension de salida AC dudosa (%.1f V): se confirma con la siguiente lectura", v_out)

                # LÓGICA INFALIBLE PARA AC OUTPUT ON/OFF:
                # El inversor AC genera ~120V cuando está físicamente encendido
                if self.state["ac_output_volts"] >= 80.0 or self.state.get("ac_output_watts", 0) > 5:
                    self._ac_set(True, "voltaje/potencia")
                elif self.state["ac_output_volts"] < 20.0 and self.state.get("ac_output_watts", 0) == 0:
                    self._ac_set(False, "voltaje/potencia")

        # 5. Interruptores reales AC/DC (Reg 2011 = AC output, 2012 = DC output)
        elif offset == 2011:
            if num_words >= 1:
                sw_ac = r16(0)
                if sw_ac in (0, 1):
                    if self.state.get("ac_output_volts", 0.0) >= 80.0:
                        self._ac_set(True, "reg2011+voltaje")
                    else:
                        self._ac_set(sw_ac == 1, "reg2011")
            if num_words >= 2:
                sw_dc = r16(1)
                if sw_dc in (0, 1):
                    self.state["dc_output_on"] = (sw_dc == 1)
                    if not self.state["dc_output_on"] and self.state.get("dc_output_watts", 0) == 0:
                        self.state["dc_output_watts"] = 0

        # 6. Registros base de switches (Reg 48 = AC, Reg 49 = DC)
        elif offset == 48:
            if num_words >= 1:
                sw_ac_base = r16(0)
                if sw_ac_base in (0, 1):
                    if self.state.get("ac_output_volts", 0.0) >= 80.0:
                        self._ac_set(True, "reg48+voltaje")
                    else:
                        self._ac_set(sw_ac_base == 1, "reg48")
            if num_words >= 2:
                sw_dc_base = r16(1)
                if sw_dc_base in (0, 1):
                    self.state["dc_output_on"] = (sw_dc_base == 1)
                    if not self.state["dc_output_on"] and self.state.get("dc_output_watts", 0) == 0:
                        self.state["dc_output_watts"] = 0

        self.state["connected"] = True
        self.state["last_update"] = time.strftime("%H:%M:%S")

        if time.time() - self._tele_log_ts >= 5.0:   # antes: una linea por lectura
            self._tele_log_ts = time.time()
            logger.info(
                f"[TELEMETRÍA OFICIAL] Batería: {self.state['soc']}% | "
                f"Solar: {self.state['dc_input_watts']}W | Red AC: {self.state['ac_input_watts']}W | "
                f"Salida AC: {self.state['ac_output_watts']}W ({self.state.get('ac_output_volts', 0)}V) | Salida DC: {self.state['dc_output_watts']}W | "
                f"AC Switch: {'ON' if self.state['ac_output_on'] else 'OFF'} | DC Switch: {'ON' if self.state['dc_output_on'] else 'OFF'}"
            )

        if self.on_telemetry:
            self.on_telemetry(dict(self.state))
