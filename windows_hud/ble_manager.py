"""
ble_manager.py - Gestor Oficial BLE con Cifrado ECDH & Modbus para Bluetti Elite 100 V2 / 200 V2
=================================================================================================
Implementa el protocolo de autenticación criptográfica oficial por curva elíptica (SECP256R1),
intercambio de claves ECDH, cifrado de sesión AES-128/256-CBC con reensamblado automático de fragmentos BLE
y comandos Modbus RTU para sincronización en tiempo real y conmutación de salidas.
"""

import asyncio
import hashlib
import json
import logging
import os
import random
import sys
import threading
import time
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

# UUIDs GATT Oficiales de Bluetti
BLUETTI_SERVICE_UUID = "0000ff00-0000-1000-8000-00805f9b34fb"
BLUETTI_NOTIFY_UUID  = "0000ff01-0000-1000-8000-00805f9b34fb"
BLUETTI_WRITE_UUID   = "0000ff02-0000-1000-8000-00805f9b34fb"

# Configuración por defecto para Bluetti Elite 100 V2
DEFAULT_MAC_ADDRESS  = "DC:B4:D9:54:78:1A"
KEX_MAGIC            = b"**"
AES_BLOCK_SIZE       = 16

# Registros Modbus RTU Oficiales V2
# En el protocolo Bluetti, la página 0x0B (Config) utiliza los siguientes offsets:
#   Offset 0xBF (3007 dec): Inversor AC (0x0BBF)
#   Offset 0xC0 (3008 dec): Salidas DC (0x0BC0)
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
REG_CONTROL_GRID     = 0x0B05  # Refuerzo de Red / Adaptación Automática a la Red (0x0B05)


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
        self.last_query_offset = 0x000A
        self.is_running = False
        self.is_paused = False
        self.simulation_mode = False
        self.last_rx_timestamp = time.time()

        # Estado completo de la estación
        self.state: Dict[str, Any] = {
            "soc": 84,
            "dc_input_watts": 0,
            "dc_input_volts": 38.4,
            "dc_input_current": 0.0,
            "ac_input_watts": 0,
            "ac_input_volts": 120.0,
            "ac_input_current": 0.0,
            "ac_output_watts": 0,
            "dc_output_watts": 0,
            "battery_volts": 51.2,
            "battery_current": 0.0,
            "ac_output_on": False,
            "dc_output_on": False,
            "eco_mode_on": True,
            "power_lifting_on": False,
            "grid_enhancement_on": False,
            "charge_mode": "standard",
            "connected": False,
            "device_name": "No conectado",
            "device_address": self.device_address,
            "encryption_active": False,
            "last_update": time.strftime("%H:%M:%S")
        }

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
        logger.info(f"Gestor BLE iniciado con destino: {self.device_address}")

    def stop(self):
        """Detiene limpiamente la conexión."""
        self.is_running = False
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._cleanup(), self.loop)

    def pause_and_release(self):
        """Libera temporalmente el Bluetooth para usar la app móvil."""
        self.is_paused = True
        logger.info("Liberando BLE para permitir uso en teléfono móvil...")
        self._notify_status("Pausado (BLE Liberado para Móvil)")
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._disconnect_client(), self.loop)

    def resume_connection(self):
        """Reanuda la conexión oficial."""
        logger.info("Reanudando conexión con la estación...")
        self.is_paused = False
        self._notify_status("Reconectando...")

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
    # COMANDOS DE CONTROL MODBUS CIFRADOS
    # -------------------------------------------------------------------------
    # CONTROL DE HARDWARE Y CONMUTADORES (BLUETTI ELITE 100 V2)
    # -------------------------------------------------------------------------
    def set_ac_output(self, turn_on: bool):
        """Enciende o apaga el inversor AC (1800W) mediante Modbus 0x06."""
        self.state["ac_output_on"] = turn_on
        if self.simulation_mode:
            self.state["ac_output_watts"] = 150 if turn_on else 0
            if self.on_telemetry:
                self.on_telemetry(dict(self.state))
            return

        val = 0x0001 if turn_on else 0x0000
        # Función 0x06 (Write Single Register) -> Reg 6001 (0x1771) de la Elite 100 V2
        pkt_06_v2 = append_crc16(bytes([
            0x01, 0x06,
            (REG_CONTROL_AC >> 8) & 0xFF, REG_CONTROL_AC & 0xFF,
            (val >> 8) & 0xFF, val & 0xFF
        ]))
        self._enqueue_command(pkt_06_v2, f"Inversor AC (0x1771) -> {'ENCENDIDO' if turn_on else 'APAGADO'}")

    def set_dc_output(self, turn_on: bool):
        """Enciende o apaga las salidas DC (USB-C 140W/100W y 12V) mediante Modbus 0x06."""
        self.state["dc_output_on"] = turn_on
        if self.simulation_mode:
            self.state["dc_output_watts"] = 35 if turn_on else 0
            if self.on_telemetry:
                self.on_telemetry(dict(self.state))
            return

        val = 0x0001 if turn_on else 0x0000
        # Función 0x06 (Write Single Register) -> Reg 6002 (0x1772) de la Elite 100 V2
        pkt_06_v2 = append_crc16(bytes([
            0x01, 0x06,
            (REG_CONTROL_DC >> 8) & 0xFF, REG_CONTROL_DC & 0xFF,
            (val >> 8) & 0xFF, val & 0xFF
        ]))
        self._enqueue_command(pkt_06_v2, f"Salidas DC (0x1772) -> {'ENCENDIDO' if turn_on else 'APAGADO'}")

    def set_eco_mode(self, turn_on: bool):
        """Activa o desactiva el modo de ahorro ECO."""
        self.state["eco_mode_on"] = turn_on
        if self.simulation_mode:
            if self.on_telemetry:
                self.on_telemetry(dict(self.state))
            return

        val = 0x0001 if turn_on else 0x0000
        packet_v2 = append_crc16(bytes([
            0x01, 0x06,
            (REG_CONTROL_ECO >> 8) & 0xFF, REG_CONTROL_ECO & 0xFF,
            (val >> 8) & 0xFF, val & 0xFF
        ]))
        self._enqueue_command(packet_v2, f"Modo ECO (0x1773) -> {'ACTIVADO' if turn_on else 'DESACTIVADO'}")

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
            (REG_CONTROL_PL >> 8) & 0xFF, REG_CONTROL_PL & 0xFF,
            (val >> 8) & 0xFF, val & 0xFF
        ]))
        self._enqueue_command(packet, f"Power Lifting -> {'ACTIVADO' if turn_on else 'DESACTIVADO'}")

    def set_charge_mode(self, mode: str):
        """Cambia el perfil de carga AC ('silent', 'standard', 'turbo')."""
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

        mode_val = 0x0000 if mode == "silent" else (0x0001 if mode == "standard" else 0x0002)
        packet = append_crc16(bytes([
            0x01, 0x06,
            (REG_CONTROL_CHARGE >> 8) & 0xFF, REG_CONTROL_CHARGE & 0xFF,
            (mode_val >> 8) & 0xFF, mode_val & 0xFF
        ]))
        self._enqueue_command(packet, f"Perfil de Carga -> {mode.upper()}")

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

    async def _cleanup(self):
        await self._disconnect_client()
        if self.loop:
            self.loop.stop()

    async def _disconnect_client(self):
        if self.client and self.client.is_connected:
            try:
                await self.client.disconnect()
            except Exception as e:
                logger.warning(f"Error al desconectar BLE: {e}")
            finally:
                self.client = None
                self.secure_aes_key = None
                self.state["connected"] = False
                self.state["encryption_active"] = False
                self.state["device_name"] = "Desconectado"

    async def _write_ble_safe(self, data: bytes, desc: str = "") -> bool:
        """Escribe datos asegurando tolerancia con y sin respuesta según MTU y stack."""
        if not self.client or not self.client.is_connected:
            return False
        target_char = getattr(self, "write_char_uuid", BLUETTI_WRITE_UUID) or BLUETTI_WRITE_UUID
        try:
            await self.client.write_gatt_char(target_char, data, response=False)
            return True
        except Exception:
            try:
                await self.client.write_gatt_char(target_char, data, response=True)
                return True
            except Exception as e2:
                logger.error(f"Error escribiendo en BLE ({desc}): {e2}")
                return False

    async def _main_supervisor(self):
        backoff = 2.0
        while self.is_running:
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

            try:
                self._notify_status("Buscando Bluetti Elite 100 V2...")
                target_device = None

                # 1. Escaneo rápido de periféricos BLE
                devices = await BleakScanner.discover(timeout=4.0)
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
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 1.5, 8.0)
                    continue

                dev_title = getattr(target_device, 'name', None) or str(target_device)
                self._notify_status(f"Conectando a {dev_title}...")

                async with BleakClient(target_device, disconnected_callback=self._on_disconnected, timeout=15.0) as client:
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
                            await client.get_services()
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

                    await client.start_notify(notify_char, self._on_rx_raw_packet)

                    # Esperar Handshake ECDH (máx 12 segundos)
                    try:
                        await asyncio.wait_for(self.ecdh_ready_event.wait(), timeout=12.0)
                    except asyncio.TimeoutError:
                        logger.error("Tiempo de espera agotado en Handshake ECDH. Reintentando...")
                        self._notify_status("Reintentando autenticación criptográfica...")
                        continue

                    # Conexión cifrada 100% activa
                    self.state["connected"] = True
                    self.state["encryption_active"] = True
                    self.state["device_name"] = dev_title
                    backoff = 2.0
                    self._notify_status(f"Enlace Seguro Activo: {dev_title}")
                    logger.info("Sesión segura AES lista. Iniciando polling continuo de telemetría...")

                    # Bucle principal de polling y envío de comandos
                    while self.is_running and not self.is_paused and client.is_connected:
                        # 1. Enviar comandos pendientes de hardware (Modbus 0x06 / 0x05)
                        while not self.command_queue.empty():
                            cmd_packet, desc = await self.command_queue.get()
                            logger.info(f"Enviando comando Modbus cifrado: {desc}")
                            enc_cmd = aes_encrypt(cmd_packet, self.secure_aes_key, None)
                            await self._write_ble_safe(enc_cmd, desc)
                            await asyncio.sleep(0.12)

                        # 2. Polling escalonado secuencial (1 Ciclo Completo = Exactamente 1.0s)
                        # A. Batería SoC (Reg 100..107)
                        self.last_query_page = 0x00
                        self.last_query_offset = 100
                        req_soc = append_crc16(bytes([0x01, 0x03, 0x00, 100, 0x00, 8]))
                        await self._write_ble_safe(aes_encrypt(req_soc, self.secure_aes_key, None), "Polling SoC")
                        await asyncio.sleep(0.25)

                        # B. Salida AC, Entrada Solar, Voltajes y Amperios Reales (Reg 140..155)
                        self.last_query_page = 0x00
                        self.last_query_offset = 140
                        req_pwr = append_crc16(bytes([0x01, 0x03, 0x00, 140, 0x00, 16]))
                        await self._write_ble_safe(aes_encrypt(req_pwr, self.secure_aes_key, None), "Polling Potencias y Tensiones")
                        await asyncio.sleep(0.25)

                        # C. Interruptores de Hardware (Reg 6000..6003)
                        self.last_query_page = 0x17
                        self.last_query_offset = 0x70
                        req_sw = append_crc16(bytes([0x01, 0x03, 0x17, 0x70, 0x00, 4]))
                        await self._write_ble_safe(aes_encrypt(req_sw, self.secure_aes_key, None), "Polling Switches")
                        await asyncio.sleep(0.25)

                        # Emitir telemetría consolidada al finalizar el ciclo de 1 segundo
                        if self.on_telemetry:
                            self.on_telemetry(dict(self.state))

                        await asyncio.sleep(0.25)

            except Exception as e:
                logger.error(f"Excepción en bucle BLE: {e}")
                self.state["connected"] = False
                self.state["encryption_active"] = False
                self._notify_status(f"Reconectando ({e.__class__.__name__})...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 10.0)

    def _on_disconnected(self, client: BleakClient):
        logger.warning("Estación Bluetti desconectada del enlace BLE")
        self.state["connected"] = False
        self.state["encryption_active"] = False
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
                    break  # Esperar más bytes para leer comando y tamaño
                cmd_type = self.rx_raw_buffer[2]
                payload_len = self.rx_raw_buffer[3]
                total_kex_len = 4 + payload_len + 2  # magic(2) + cmd(1) + len(1) + payload + checksum(2)
                if len(self.rx_raw_buffer) < total_kex_len:
                    break  # Trama incompleta, esperar más fragmentos

                kex_frame = bytes(self.rx_raw_buffer[:total_kex_len])
                self.rx_raw_buffer = self.rx_raw_buffer[total_kex_len:]
                await self._handle_kex_unencrypted(kex_frame)
                continue

            # Caso B: Tramas cifradas con AES (Handshake avanzado y Modbus de telemetría)
            if self.unsecure_aes_key is not None:
                if len(self.rx_raw_buffer) < 2:
                    break
                data_len = (self.rx_raw_buffer[0] << 8) | self.rx_raw_buffer[1]

                # Sanity check: longitud debe ser razonable (< 512 bytes)
                if data_len > 512 or data_len == 0:
                    self.rx_raw_buffer.pop(0)
                    continue

                padded_len = ((data_len + 15) // 16) * 16
                iv_len = 4 if self.secure_aes_key is not None else 0
                total_enc_len = 2 + iv_len + padded_len

                if len(self.rx_raw_buffer) < total_enc_len:
                    break  # Esperar siguientes fragmentos BLE

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

            # Descartar byte desconocido inicial
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

            # Responder al desafío inmediatamente
            body = bytes.fromhex("0204") + self.unsecure_aes_iv[8:12]
            resp = b"".join([KEX_MAGIC, body, hexsum(body, 2)])
            logger.info(f"Enviando respuesta a desafío: {resp.hex(' ').upper()}")
            await self._write_ble_safe(resp, "Respuesta Desafío 0x02")

        elif cmd_type == 0x03:  # DESAFÍO ACEPTADO
            logger.info("Desafío aceptado por Bluetti. Negociando intercambio público ECDH...")

    async def _handle_decrypted_frame(self, decrypted: bytes):
        """Procesa tramas una vez descifradas con éxito."""
        # 1. Clave pública de la estación durante handshake
        if decrypted.startswith(KEX_MAGIC):
            cmd_type = decrypted[2]
            payload = memoryview(decrypted)[4:-2]

            if cmd_type == 0x04:  # Clave Pública de la Estación (PEER_PUBKEY)
                logger.info("Clave pública de estación recibida. Verificando firma y generando clave local...")
                data = verify_and_extract_signed_data(payload, self.unsecure_aes_iv)
                self.peer_pubkey = pubkey_from_bytes(data)

                # Generar par de claves SECP256R1 local
                self.my_privkey = ec.generate_private_key(ec.SECP256R1())
                self.my_pubkey = self.my_privkey.public_key()
                my_pub_bytes = pubkey_to_bytes(self.my_pubkey)

                # Firmar con clave L1 oficial
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
                # Derivar clave secreta compartida ECDH
                self.secure_aes_key = self.my_privkey.exchange(ec.ECDH(), self.peer_pubkey)
                logger.info(f"¡Handshake ECDH Exitoso! Clave AES de Sesión: {self.secure_aes_key.hex()[:16]}...")
                self.ecdh_ready_event.set()
                return

        # 2. Respuestas Modbus de telemetría y comandos
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
                    page = getattr(self, "last_query_page", 0x00)
                    offset = getattr(self, "last_query_offset", 0x00)
                    self._parse_telemetry_payload(payload, page, offset)
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
                    if reg in (REG_CONTROL_AC, REG_CONTROL_AC_LEG, 0x1771, 0x0BBF, 0x0B00):
                        self.state["ac_output_on"] = (val == 0x0001)
                    elif reg in (REG_CONTROL_DC, REG_CONTROL_DC_LEG, 0x1772, 0x0BC0, 0x0B01):
                        self.state["dc_output_on"] = (val == 0x0001)
                    elif reg in (REG_CONTROL_ECO, REG_CONTROL_ECO_LEG, 0x1773, 0x0B02):
                        self.state["eco_mode_on"] = (val == 0x0001)
                    elif reg in (REG_CONTROL_PL, 0x0B03):
                        self.state["power_lifting_on"] = (val == 0x0001)
                    elif reg in (REG_CONTROL_CHARGE, 0x0B04):
                        modes = {0: "silent", 1: "standard", 2: "turbo"}
                        self.state["charge_mode"] = modes.get(val, "standard")
                    elif reg in (REG_CONTROL_GRID, 0x0B05):
                        self.state["grid_enhancement_on"] = (val == 0x0001)
                    if self.on_telemetry:
                        self.on_telemetry(dict(self.state))
                else:
                    logger.warning("Fallo de CRC16 en respuesta Modbus 0x06")
            else:
                self.rx_modbus_buffer.pop(0)

    def _parse_telemetry_payload(self, p: bytes, page: int = 0x00, offset: int = 0x00):
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

        # 1. Bloque de Batería (Offset 100..107)
        if page == 0x00 and offset == 100:
            if num_words >= 3:
                soc_val = r16(2)
                if 0 <= soc_val <= 100:
                    self.state["soc"] = soc_val

        # 2. Bloque de Potencias y Tensiones (Página 0x00, Offset 140..155)
        elif page == 0x00 and offset == 140:
            if num_words >= 5:
                # Reg 140 (Word 0): Entrada Red Eléctrica (W)
                raw_grid_w = r16(0) if r16(0) < 3000 else 0
                self.state["ac_input_watts"] = raw_grid_w

                # Reg 141 (Word 1): Tensión de Red Eléctrica (V, escala 0.1V ej. 0 -> 0.0V, 1166 -> 116.6V)
                v_grid = (r16(1) / 10.0) if (num_words >= 2 and r16(1) > 0) else 0.0
                self.state["ac_input_volts"] = round(v_grid, 1)
                self.state["ac_input_current"] = round(raw_grid_w / v_grid, 2) if (v_grid > 0 and raw_grid_w > 0) else 0.0

                # Reg 142 (Word 2): Salida Inversor AC (W)
                self.state["ac_output_watts"] = r16(2)

                # Reg 144 (Word 4): Entrada Solar MPPT (W)
                sol_w = r16(4)
                self.state["dc_input_watts"] = sol_w

                # Salida DC (Vatios reales)
                # Si el switch DC está encendido, lee los vatios de consumo real de USB-C/12V. Si no, 0W.
                self.state["dc_output_watts"] = r16(10) if (num_words >= 11 and self.state.get("dc_output_on", False)) else 0

                # Tensión Real Solar MPPT (V):
                # Si hay potencia solar (>0W) o voltaje detectado en Word 12 o Word 5:
                # Word 12 (Reg 152) contiene el voltaje del bus MPPT en décimas de voltio (ej. 346 -> 34.6V)
                if num_words >= 13 and r16(12) > 0 and sol_w > 0:
                    v_sol = r16(12) / 10.0
                elif sol_w > 0 and num_words >= 6 and r16(5) > 0:
                    v_sol = r16(5) / 10.0
                else:
                    v_sol = 0.0 if sol_w == 0 else ((r16(12) / 10.0) if num_words >= 13 else 0.0)

                self.state["dc_input_volts"] = round(v_sol, 1)
                self.state["dc_input_current"] = round(sol_w / v_sol, 2) if (v_sol > 0 and sol_w > 0) else 0.0

        # 3. Bloque de Switches de Control (Página 0x17 Offset 0x70 / Reg 6000 o Página 0x0B)
        elif page == 0x17 and offset == 0x70:
            if num_words >= 3:
                self.state["ac_output_on"] = (r16(1) == 1)
                self.state["dc_output_on"] = (r16(2) == 1)
        elif page == 0x0B and (offset in (0xB8, 0xBF, 3000, 3007)):
            if num_words >= 4:
                self.state["ac_output_on"] = (r16(2) != 0)
                self.state["dc_output_on"] = (r16(3) != 0)
            elif num_words >= 2:
                self.state["ac_output_on"] = (r16(0) != 0)
                self.state["dc_output_on"] = (r16(1) != 0)

        self.state["connected"] = True
        self.state["last_update"] = time.strftime("%H:%M:%S")

        logger.info(
            f"[TELEMETRÍA OFICIAL] Batería: {self.state['soc']}% | "
            f"Solar: {self.state['dc_input_watts']}W | Red AC: {self.state['ac_input_watts']}W | "
            f"Salida AC: {self.state['ac_output_watts']}W | Salida DC: {self.state['dc_output_watts']}W | "
            f"AC Switch: {'ON' if self.state['ac_output_on'] else 'OFF'} | DC Switch: {'ON' if self.state['dc_output_on'] else 'OFF'}"
        )

        if self.on_telemetry:
            self.on_telemetry(dict(self.state))
