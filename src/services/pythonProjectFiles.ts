// Exporta todos los archivos Python y scripts del proyecto con el protocolo oficial ECDH
export interface PythonProjectFile {
  name: string;
  description: string;
  language: string;
  content: string;
}

export const BLE_MANAGER_PY = `"""
ble_manager.py - Gestor Oficial BLE con Cifrado ECDH & Modbus para Bluetti Elite 100 V2 / 200 V2
=================================================================================================
Implementa el protocolo de autenticación criptográfica oficial por curva elíptica (SECP256R1),
intercambio de claves ECDH, cifrado de sesión AES-128/256-CBC y comandos Modbus RTU.
Evita la desconexión a los pocos segundos manteniendo viva la sesión con la estación.
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

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s (BLE): %(message)s")
logger = logging.getLogger("BluettiBLE")

BLUETTI_SERVICE_UUID = "0000ff00-0000-1000-8000-00805f9b34fb"
BLUETTI_NOTIFY_UUID  = "0000ff01-0000-1000-8000-00805f9b34fb"
BLUETTI_WRITE_UUID   = "0000ff02-0000-1000-8000-00805f9b34fb"

DEFAULT_MAC_ADDRESS  = "DC:B4:D9:54:78:1A"
KEX_MAGIC            = b"**"
AES_BLOCK_SIZE       = 16

REG_BASE_STATUS      = 0x000A
REG_BASE_STATUS_LEN  = 34
REG_CONTROL_AC       = 0x0BBF  # Página 0x0B, offset 0xBF (3007) - Inversor AC (1800W)
REG_CONTROL_AC_ALT   = 0x0B00  # Offset alternativo / estándar
REG_CONTROL_DC       = 0x0BC0  # Página 0x0B, offset 0xC0 (3008) - Salidas DC (USB / 12V)
REG_CONTROL_DC_ALT   = 0x0B01  # Offset alternativo / estándar
REG_CONTROL_ECO      = 0x0B02
REG_CONTROL_PL       = 0x0B03
REG_CONTROL_CHARGE   = 0x0B04
REG_CONTROL_GRID     = 0x0B05  # Refuerzo de Red / Adaptación Automática a la Red

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
    serialization.load_der_public_key(key_bytes).verify(
        der_signature, signed_data, ec.ECDSA(hashes.SHA256())
    )
    return bytes(data)

class BluettiBleManager:
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

        self.unsecure_aes_key: Optional[bytes] = None
        self.unsecure_aes_iv: Optional[bytes] = None
        self.secure_aes_key: Optional[bytes] = None
        self.peer_pubkey: Optional[ec.EllipticCurvePublicKey] = None
        self.my_privkey: Optional[ec.EllipticCurvePrivateKey] = None
        self.my_pubkey: Optional[ec.EllipticCurvePublicKey] = None
        self.ecdh_ready_event: Optional[asyncio.Event] = None

        self.rx_buffer = bytearray()
        self.is_running = False
        self.is_paused = False
        self.simulation_mode = False

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
            "charge_mode": "standard",
            "connected": False,
            "device_name": "No conectado",
            "device_address": self.device_address,
            "encryption_active": False,
            "last_update": time.strftime("%H:%M:%S")
        }

    def start(self, device_address: Optional[str] = None):
        if self.is_running:
            return
        if device_address:
            self.device_address = device_address
            self.state["device_address"] = device_address

        self.is_running = True
        self.is_paused = False
        self.worker_thread = threading.Thread(target=self._run_event_loop, daemon=True)
        self.worker_thread.start()

    def stop(self):
        self.is_running = False
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._cleanup(), self.loop)

    def pause_and_release(self):
        self.is_paused = True
        self._notify_status("Pausado (BLE Liberado para Móvil)")
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._disconnect_client(), self.loop)

    def resume_connection(self):
        self.is_paused = False
        self._notify_status("Reconectando...")

    def set_device_address(self, address: str):
        self.device_address = address
        self.state["device_address"] = address
        if self.client and self.client.is_connected and self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._disconnect_client(), self.loop)

    def toggle_simulation_mode(self, enabled: Optional[bool] = None):
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

    def set_ac_output(self, turn_on: bool):
        self.state["ac_output_on"] = turn_on
        if self.simulation_mode:
            self.state["ac_output_watts"] = 150 if turn_on else 0
            if self.on_telemetry:
                self.on_telemetry(dict(self.state))
            return

        val = 0x0001 if turn_on else 0x0000
        packet = append_crc16(bytes([
            0x01, 0x06,
            (REG_CONTROL_AC >> 8) & 0xFF, REG_CONTROL_AC & 0xFF,
            (val >> 8) & 0xFF, val & 0xFF
        ]))
        self._enqueue_command(packet, f"Inversor AC -> {'ENCENDIDO' if turn_on else 'APAGADO'}")

    def set_dc_output(self, turn_on: bool):
        self.state["dc_output_on"] = turn_on
        if self.simulation_mode:
            self.state["dc_output_watts"] = 35 if turn_on else 0
            if self.on_telemetry:
                self.on_telemetry(dict(self.state))
            return

        val = 0x0001 if turn_on else 0x0000
        packet = append_crc16(bytes([
            0x01, 0x06,
            (REG_CONTROL_DC >> 8) & 0xFF, REG_CONTROL_DC & 0xFF,
            (val >> 8) & 0xFF, val & 0xFF
        ]))
        self._enqueue_command(packet, f"Salidas DC -> {'ENCENDIDO' if turn_on else 'APAGADO'}")

    def set_eco_mode(self, turn_on: bool):
        self.state["eco_mode_on"] = turn_on
        if self.simulation_mode:
            if self.on_telemetry:
                self.on_telemetry(dict(self.state))
            return

        val = 0x0001 if turn_on else 0x0000
        packet = append_crc16(bytes([
            0x01, 0x06,
            (REG_CONTROL_ECO >> 8) & 0xFF, REG_CONTROL_ECO & 0xFF,
            (val >> 8) & 0xFF, val & 0xFF
        ]))
        self._enqueue_command(packet, f"Modo ECO -> {'ACTIVADO' if turn_on else 'DESACTIVADO'}")

    def set_power_lifting(self, turn_on: bool):
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
            except Exception:
                pass
            finally:
                self.client = None
                self.secure_aes_key = None
                self.state["connected"] = False
                self.state["encryption_active"] = False
                self.state["device_name"] = "Desconectado"

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

                devices = await BleakScanner.discover(timeout=4.0)
                discovered_list = []
                for d in devices:
                    d_name = d.name or "Dispositivo sin nombre"
                    discovered_list.append({"name": d_name, "address": d.address})
                    if self.device_address and d.address.upper() == self.device_address.upper():
                        target_device = d
                    elif not target_device and ("EL100" in d_name.upper() or "BLUETTI" in d_name.upper()):
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
                    self.rx_buffer.clear()

                    await client.start_notify(BLUETTI_NOTIFY_UUID, self._on_rx_raw_packet)

                    try:
                        await asyncio.wait_for(self.ecdh_ready_event.wait(), timeout=10.0)
                    except asyncio.TimeoutError:
                        self._notify_status("Error: Handshake ECDH no completado")
                        continue

                    self.state["connected"] = True
                    self.state["encryption_active"] = True
                    self.state["device_name"] = dev_title
                    backoff = 2.0
                    self._notify_status(f"Enlace Seguro Activo: {dev_title}")

                    while self.is_running and not self.is_paused and client.is_connected:
                        while not self.command_queue.empty():
                            cmd_packet, desc = await self.command_queue.get()
                            enc_cmd = aes_encrypt(cmd_packet, self.secure_aes_key, None)
                            await client.write_gatt_char(BLUETTI_WRITE_UUID, enc_cmd, response=False)
                            await asyncio.sleep(0.2)

                        # Polling segmentado compatible con el límite de 8 palabras de la Elite 100 V2
                        segments = [
                            (0x000A, 8),
                            (0x0012, 8),
                            (0x001A, 8),
                            (0x0BBF, 2)
                        ]
                        for reg_start, reg_len in segments:
                            if not self.is_running or self.is_paused or not client.is_connected:
                                break
                            self.last_query_offset = reg_start
                            poll_req = append_crc16(bytes([
                                0x01, 0x03,
                                (reg_start >> 8) & 0xFF, reg_start & 0xFF,
                                (reg_len >> 8) & 0xFF, reg_len & 0xFF
                            ]))
                            enc_poll = aes_encrypt(poll_req, self.secure_aes_key, None)
                            await client.write_gatt_char(BLUETTI_WRITE_UUID, enc_poll, response=False)
                            await asyncio.sleep(0.18)

                        await asyncio.sleep(max(0.4, self.poll_interval - 0.72))

            except Exception as e:
                self.state["connected"] = False
                self.state["encryption_active"] = False
                self._notify_status(f"Reconectando ({e.__class__.__name__})...")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 1.5, 10.0)

    def _on_disconnected(self, client: BleakClient):
        self.state["connected"] = False
        self.state["encryption_active"] = False
        self._notify_status("Desconectado. Reconectando automáticamente...")

    def _on_rx_raw_packet(self, sender: BleakGATTCharacteristic, data: bytearray):
        if self.loop and self.loop.is_running():
            asyncio.run_coroutine_threadsafe(self._process_rx_packet(bytes(data)), self.loop)

    async def _process_rx_packet(self, raw: bytes):
        try:
            if raw.startswith(KEX_MAGIC):
                cmd_type = raw[2]
                payload = raw[4:-2]

                if cmd_type == 0x01:
                    self.unsecure_aes_iv = hashlib.md5(payload[::-1]).digest()
                    static_key = bytes.fromhex(ConnConstantsV2.LOCAL_AES_KEY.value)
                    self.unsecure_aes_key = hexxor(self.unsecure_aes_iv, static_key)

                    body = bytes.fromhex("0204") + self.unsecure_aes_iv[8:12]
                    resp = b"".join([KEX_MAGIC, body, hexsum(body, 2)])
                    await self.client.write_gatt_char(BLUETTI_WRITE_UUID, resp, response=False)
                    return

                elif cmd_type == 0x03:
                    return

            if self.unsecure_aes_key:
                key = self.unsecure_aes_key if self.secure_aes_key is None else self.secure_aes_key
                iv = self.unsecure_aes_iv if self.secure_aes_key is None else None
                decrypted = aes_decrypt(raw, key, iv)

                if decrypted.startswith(KEX_MAGIC):
                    cmd_type = decrypted[2]
                    payload = memoryview(decrypted)[4:-2]

                    if cmd_type == 0x04:
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
                        await self.client.write_gatt_char(BLUETTI_WRITE_UUID, encrypted_msg, response=False)
                        return

                    elif cmd_type == 0x06:
                        self.secure_aes_key = self.my_privkey.exchange(ec.ECDH(), self.peer_pubkey)
                        self.ecdh_ready_event.set()
                        return

                self._handle_modbus_decrypted(decrypted)

        except Exception as e:
            logger.error(f"Error procesando paquete: {e}")

    def _handle_modbus_decrypted(self, data: bytes):
        self.rx_buffer.extend(data)

        while len(self.rx_buffer) >= 5:
            if self.rx_buffer[0] != 0x01:
                self.rx_buffer.pop(0)
                continue

            func_code = self.rx_buffer[1]

            if func_code == 0x03:
                if len(self.rx_buffer) < 3:
                    break
                byte_count = self.rx_buffer[2]
                total_len = 3 + byte_count + 2

                if len(self.rx_buffer) < total_len:
                    break

                packet = bytes(self.rx_buffer[:total_len])
                self.rx_buffer = self.rx_buffer[total_len:]

                if verify_crc16(packet):
                    payload = packet[3: 3 + byte_count]
                    self._parse_telemetry_payload(payload)

            elif func_code == 0x06:
                if len(self.rx_buffer) < 8:
                    break
                packet = bytes(self.rx_buffer[:8])
                self.rx_buffer = self.rx_buffer[8:]
                if verify_crc16(packet):
                    reg = (packet[2] << 8) | packet[3]
                    val = (packet[4] << 8) | packet[5]
                    if reg in (REG_CONTROL_AC, REG_CONTROL_AC_ALT, 0x0BBF, 0x0B00):
                        self.state["ac_output_on"] = (val == 0x0001)
                    elif reg in (REG_CONTROL_DC, REG_CONTROL_DC_ALT, 0x0BC0, 0x0B01):
                        self.state["dc_output_on"] = (val == 0x0001)
                    elif reg in (REG_CONTROL_ECO, 0x0B02):
                        self.state["eco_mode_on"] = (val == 0x0001)
                    elif reg in (REG_CONTROL_PL, 0x0B03):
                        self.state["power_lifting_on"] = (val == 0x0001)
                    elif reg in (REG_CONTROL_CHARGE, 0x0B04):
                        modes = {0: "silent", 1: "standard", 2: "turbo"}
                        self.state["charge_mode"] = modes.get(val, "standard")
                    elif reg in (REG_CONTROL_GRID, 0x0B05):
                        self.state["grid_enhancement_on"] = (val == 0x0001)

            else:
                self.rx_buffer.pop(0)

    def _parse_telemetry_payload(self, p: bytes):
        num_words = len(p) // 2
        if num_words < 6:
            return

        def r16(word_idx: int) -> int:
            offset = word_idx * 2
            if offset + 1 < len(p):
                return (p[offset] << 8) | p[offset + 1]
            return 0

        def r16_s(word_idx: int) -> int:
            v = r16(word_idx)
            return v - 65536 if v > 32767 else v

        if num_words >= 30:
            volt_raw = r16(10)
            batt_v = round(volt_raw * 0.1, 1) if volt_raw > 0 else 51.2
            batt_curr = round(r16_s(11) * 0.1, 1)

            dc_in_w = r16(26)
            ac_in_w = r16(27)
            ac_out_w = r16(28)
            dc_out_w = r16(29)

            soc = self.state["soc"]
            if num_words >= 34:
                soc_raw = r16(33) & 0xFF
                if 0 < soc_raw <= 100:
                    soc = soc_raw

            dc_v = 38.4 if dc_in_w > 0 else 0.0
            dc_a = round(dc_in_w / dc_v, 1) if dc_v > 0 else 0.0
            ac_v = 120.0 if ac_in_w > 0 else 0.0
            ac_a = round(ac_in_w / ac_v, 1) if ac_v > 0 else 0.0

            self.state.update({
                "soc": min(100, max(0, soc)),
                "dc_input_watts": dc_in_w,
                "dc_input_volts": dc_v,
                "dc_input_current": dc_a,
                "ac_input_watts": ac_in_w,
                "ac_input_volts": ac_v,
                "ac_input_current": ac_a,
                "ac_output_watts": ac_out_w,
                "dc_output_watts": dc_out_w,
                "battery_volts": batt_v,
                "battery_current": batt_curr,
                "ac_output_on": ac_out_w > 0 or self.state.get("ac_output_on", False),
                "dc_output_on": dc_out_w > 0 or self.state.get("dc_output_on", False),
                "connected": True,
                "last_update": time.strftime("%H:%M:%S")
            })

        if self.on_telemetry:
            self.on_telemetry(dict(self.state))
`;

export const DESKTOP_APP_PY = `"""
desktop_app.py - Lanzador de Escritorio Nativo Windows con Interfaz High-Tech y Servidor Local
==============================================================================================
"""

import http.server
import json
import logging
import os
import socketserver
import sys
import threading
import time
import webbrowser
from typing import Optional

from ble_manager import BluettiBleManager

logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s (Desktop): %(message)s")
logger = logging.getLogger("BluettiDesktop")

PORT = 8765

ble = BluettiBleManager(poll_interval=1.5, default_mac="DC:B4:D9:54:78:1A")

class BluettiHttpHandler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        base_dir = os.path.dirname(os.path.abspath(__file__))
        super().__init__(*args, directory=base_dir, **kwargs)

    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            base_dir = os.path.dirname(os.path.abspath(__file__))
            target_file = os.path.join(base_dir, "standalone_ui.html")
            if not os.path.exists(target_file):
                target_file = os.path.join(base_dir, "dist", "index.html")

            if os.path.exists(target_file):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                with open(target_file, "rb") as f:
                    self.wfile.write(f.read())
                return

        elif self.path == "/api/telemetry" or self.path == "/api/status":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            data = json.dumps(ble.state).encode("utf-8")
            self.wfile.write(data)
            return

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

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "state": ble.state}).encode("utf-8"))
            return

        super().do_POST()

def run_http_server(port: int = PORT):
    socketserver.TCPServer.allow_reuse_address = True
    with socketserver.TCPServer(("127.0.0.1", port), BluettiHttpHandler) as httpd:
        httpd.serve_forever()

def main():
    print("=" * 65)
    print("   BLUETTI ELITE 100 V2 - PANEL DE CONTROL & HUD GAMER")
    print("   Protocolo Criptográfico Oficial ECDH + Modbus RTU AES")
    print("=" * 65)

    ble.start()
    server_thread = threading.Thread(target=run_http_server, args=(PORT,), daemon=True)
    server_thread.start()

    target_url = f"http://127.0.0.1:{PORT}/index.html"
    time.sleep(0.5)

    try:
        import webview
        window = webview.create_window(
            title="Bluetti Elite 100 V2 • Centro de Control & HUD Gamer",
            url=target_url,
            width=1120,
            height=800,
            min_size=(750, 520),
            background_color="#080b11"
        )
        webview.start(gui="edgechromium", debug=False)
    except Exception:
        webbrowser.open(target_url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            ble.stop()

if __name__ == "__main__":
    main()
`;

export const TEST_HANDSHAKE_PY = `"""
test_handshake.py - Monitor Oficial ECDH Continuo
=================================================
"""
import asyncio
import hashlib
import os
import sys
import time
from enum import Enum
from bleak import BleakScanner, BleakClient

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from pyasn1.type import univ
import pyasn1.codec.der.decoder as der_decoder
import pyasn1.codec.der.encoder as der_encoder

BLUETTI_NOTIFY_UUID = "0000ff01-0000-1000-8000-00805f9b34fb"
BLUETTI_WRITE_UUID  = "0000ff02-0000-1000-8000-00805f9b34fb"
MAC_ADDRESS = "DC:B4:D9:54:78:1A"
KEX_MAGIC = b"**"
LOCAL_AES_KEY = bytes.fromhex("459FC535808941F17091E0993EE3E93D")
SIGNING_KEY_PRIV = "4F19A16E3E87BDD9BD24D3E5495B88041511943CBC8B969ADE9641D0F56AF337"
PUBLIC_KEY_K2 = "3059301306072a8648ce3d020106082a8648ce3d03010703420004A73ABF5D2232C8C1C72E68304343C272495E3A8FD6F30EA96DE2F4B3CE60B251EE21AC667CF8A71E18B46B664EAEFFE3C489F24F695B6411DB7E22CCC85A8594"

def hexsum(s, sz=2):
    checksum = sum(s)
    return bytes.fromhex(f"{checksum:0{sz*2}x}")

def hexxor(a, b):
    return bytes([x ^ y for x, y in zip(a, b)])

def aes_decrypt(data, aes_key, iv):
    data_len = (data[0] << 8) + data[1]
    if iv is None:
        iv = hashlib.md5(data[2:6]).digest()
        encrypted = memoryview(data)[6:]
    else:
        encrypted = memoryview(data[2:])
    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    dec = cipher.decryptor().update(encrypted) + cipher.decryptor().finalize()
    return bytes(dec[:data_len])

def aes_encrypt(data, aes_key, iv):
    header = int.to_bytes(len(data), 2, "big")
    if iv is None:
        seed = os.urandom(4)
        iv = hashlib.md5(seed).digest()
        header += seed
    pad = (16 - len(data) % 16) % 16
    data_padded = data + bytes(pad)
    cipher = Cipher(algorithms.AES(aes_key), modes.CBC(iv))
    enc = cipher.encryptor().update(data_padded) + cipher.encryptor().finalize()
    return header + enc

def pubkey_to_bytes(pubkey):
    return pubkey.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)[1:]

def pubkey_from_bytes(data):
    prefix = bytes.fromhex("3059301306072a8648ce3d020106082a8648ce3d03010703420004")
    return serialization.load_der_public_key(prefix + data)

def raw_ecdsa_to_der(sig):
    seq = univ.SequenceOf()
    seq.extend([univ.Integer(int.from_bytes(sig[:32], "big")), univ.Integer(int.from_bytes(sig[32:], "big"))])
    return der_encoder.encode(seq)

def der_to_raw_ecdsa(sig):
    seq, remainder = der_decoder.decode(sig)
    return b"".join([int.to_bytes(int(x), 0x20, "big") for x in seq])

def verify_and_extract_signed_data(message, signed_data_suffix):
    data = message[:64]
    sig = message[64:]
    der_sig = raw_ecdsa_to_der(sig)
    k2_bytes = bytes.fromhex(PUBLIC_KEY_K2)
    serialization.load_der_public_key(k2_bytes).verify(der_sig, data.tobytes() + signed_data_suffix, ec.ECDSA(hashes.SHA256()))
    return data

class BluettiEncryptedClient:
    def __init__(self, client):
        self.client = client
        self.unsecure_aes_key = None
        self.unsecure_aes_iv = None
        self.secure_aes_key = None
        self.peer_pubkey = None
        self.my_privkey = None
        self.ready_event = asyncio.Event()

    async def on_packet(self, buffer):
        raw = bytes(buffer)
        if raw.startswith(KEX_MAGIC):
            cmd_type = raw[2]
            payload = raw[4:-2]
            if cmd_type == 0x01:
                print(f"  <-- [RX BLE]: {raw.hex(' ').upper()}")
                self.unsecure_aes_iv = hashlib.md5(payload[::-1]).digest()
                self.unsecure_aes_key = hexxor(self.unsecure_aes_iv, LOCAL_AES_KEY)
                body = bytes.fromhex("0204") + self.unsecure_aes_iv[8:12]
                resp = b"".join([KEX_MAGIC, body, hexsum(body, 2)])
                print(f"  --> [TX BLE]: {resp.hex(' ').upper()}")
                await self.client.write_gatt_char(BLUETTI_WRITE_UUID, resp, response=False)
                print("[OK] Handshake criptográfico ECDH iniciado...")
                return
            elif cmd_type == 0x03:
                return

        if self.unsecure_aes_key:
            key = self.unsecure_aes_key if self.secure_aes_key is None else self.secure_aes_key
            iv = self.unsecure_aes_iv if self.secure_aes_key is None else None
            decrypted = aes_decrypt(raw, key, iv)
            if decrypted.startswith(KEX_MAGIC):
                cmd_type = decrypted[2]
                payload = memoryview(decrypted)[4:-2]
                if cmd_type == 0x04:
                    data = verify_and_extract_signed_data(payload, self.unsecure_aes_iv)
                    self.peer_pubkey = pubkey_from_bytes(data)
                    self.my_privkey = ec.generate_private_key(ec.SECP256R1())
                    my_pub_bytes = pubkey_to_bytes(self.my_privkey.public_key())
                    signing_secret = int.from_bytes(bytes.fromhex(SIGNING_KEY_PRIV), "big")
                    signing_key = ec.derive_private_key(signing_secret, ec.SECP256R1())
                    sig = signing_key.sign(my_pub_bytes + self.unsecure_aes_iv, ec.ECDSA(hashes.SHA256()))
                    raw_sig = der_to_raw_ecdsa(sig)
                    body = b"".join([bytes.fromhex("0580"), my_pub_bytes, raw_sig])
                    msg = b"".join([KEX_MAGIC, body, hexsum(body, 2)])
                    enc_msg = aes_encrypt(msg, self.unsecure_aes_key, self.unsecure_aes_iv)
                    await self.client.write_gatt_char(BLUETTI_WRITE_UUID, enc_msg, response=False)
                    return
                elif cmd_type == 0x06:
                    self.secure_aes_key = self.my_privkey.exchange(ec.ECDH(), self.peer_pubkey)
                    print("\\n=======================================================")
                    print("  ¡¡HANDSHAKE ECDH COMPLETADO CON EXITO!!")
                    print(f"  Secure Key: {self.secure_aes_key.hex()}")
                    print("=======================================================\\n")
                    self.ready_event.set()
                    return

            if len(decrypted) >= 5 and decrypted[0] == 0x01:
                # Modbus recibido
                pass

async def main():
    print(f"Conectando a {MAC_ADDRESS}...")
    async with BleakClient(MAC_ADDRESS, timeout=15.0) as client:
        print("[OK] Conectado por Bluetooth BLE.")
        bluetti = BluettiEncryptedClient(client)
        await client.start_notify(BLUETTI_NOTIFY_UUID, lambda s, d: asyncio.create_task(bluetti.on_packet(d)))
        await asyncio.wait_for(bluetti.ready_event.wait(), timeout=12.0)
        print("[OK] Sesion activa.")
        while client.is_connected:
            await asyncio.sleep(2.0)

if __name__ == "__main__":
    asyncio.run(main())
`;

export const REQUIREMENTS_TXT = `bleak>=0.22.0
cryptography>=42.0.0
pyasn1>=0.6.0
customtkinter>=5.2.2
pywebview>=5.0.0
pyinstaller>=6.5.0
`;

export const RUN_BAT = `@echo off
title Bluetti Elite 100 V2 - Panel de Control
echo ========================================================
echo   Iniciando Bluetti Elite 100 V2 Desktop HUD...
echo ========================================================
echo.
python desktop_app.py
if %errorlevel% neq 0 (
    python hud_app.py
)
pause
`;

export const PYTHON_FILES: PythonProjectFile[] = [
  {
    name: 'ble_manager.py',
    description: 'Motor oficial de conexión BLE con autenticación ECDH SECP256R1 y cifrado AES-CBC',
    language: 'python',
    content: BLE_MANAGER_PY
  },
  {
    name: 'desktop_app.py',
    description: 'Lanzador nativo de escritorio con servidor local, REST API y vista Edge Chromium',
    language: 'python',
    content: DESKTOP_APP_PY
  },
  {
    name: 'test_handshake.py',
    description: 'Script de terminal para verificar apretón de manos ECDH y telemetría continua',
    language: 'python',
    content: TEST_HANDSHAKE_PY
  },
  {
    name: 'requirements.txt',
    description: 'Dependencias oficiales requeridas (bleak, cryptography, pyasn1, customtkinter, pywebview)',
    language: 'text',
    content: REQUIREMENTS_TXT
  },
  {
    name: 'run.bat',
    description: 'Archivo ejecutable para iniciar el panel de control con un doble clic',
    language: 'bat',
    content: RUN_BAT
  }
];
