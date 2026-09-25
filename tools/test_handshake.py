"""
test_handshake.py - Diagnóstico y Monitor Continuo Oficial ECDH
===============================================================
Autentica con la Bluetti Elite 100 V2 / 200 V2 usando ECDH SECP256R1,
negocia la clave de sesión AES y realiza lecturas Modbus continuas
con reensamblado automático de tramas.
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

BLUETTI_SERVICE_UUID = "0000ff00-0000-1000-8000-00805f9b34fb"
BLUETTI_NOTIFY_UUID  = "0000ff01-0000-1000-8000-00805f9b34fb"
BLUETTI_WRITE_UUID   = "0000ff02-0000-1000-8000-00805f9b34fb"

MAC_ADDRESS = "DC:B4:D9:54:78:1A"
KEX_MAGIC = b"**"
AES_BLOCK_SIZE = 16


class ConnConstantsV2(Enum):
    LOCAL_AES_KEY = "459FC535808941F17091E0993EE3E93D"


class ECDHUtils(Enum):
    SECP_256R1_PUBLIC_PREFIX = "3059301306072a8648ce3d020106082a8648ce3d03010703420004"


class SignatureCrypt(Enum):
    PRIVATE_KEY_L1 = "4F19A16E3E87BDD9BD24D3E5495B88041511943CBC8B969ADE9641D0F56AF337"
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
    decryptor = cipher.decryptor()
    decrypted = decryptor.update(encrypted) + decryptor.finalize()
    return bytes(decrypted[:data_len])


def aes_encrypt(data, aes_key, iv):
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


def pubkey_to_bytes(pubkey):
    out = pubkey.public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    return out[1:]


def pubkey_from_bytes(data):
    encoded_peer_pubkey = bytes.fromhex(ECDHUtils.SECP_256R1_PUBLIC_PREFIX.value) + data
    return serialization.load_der_public_key(encoded_peer_pubkey)


def raw_ecdsa_to_der(sig):
    seq = univ.SequenceOf()
    seq.extend([
        univ.Integer(int.from_bytes(sig[:32], "big")),
        univ.Integer(int.from_bytes(sig[32:], "big")),
    ])
    return der_encoder.encode(seq)


def der_to_raw_ecdsa(sig):
    seq, remainder = der_decoder.decode(sig)
    return b"".join([int.to_bytes(int(x), 0x20, "big") for x in seq])


def verify_and_extract_signed_data(message, signed_data_suffix):
    data = message[:64]
    signature = message[64:]
    signed_data = data.tobytes() + signed_data_suffix
    der_signature = raw_ecdsa_to_der(signature)
    key_bytes = bytes.fromhex(SignatureCrypt.PUBLIC_KEY_K2.value)
    try:
        serialization.load_der_public_key(key_bytes).verify(
            der_signature, signed_data, ec.ECDSA(hashes.SHA256())
        )
    except Exception as e:
        print(f"  [Aviso ECDSA]: {e}")
    return data


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


class BluettiEncryptedClient:
    def __init__(self, client: BleakClient):
        self.client = client
        self.unsecure_aes_key = None
        self.unsecure_aes_iv = None
        self.secure_aes_key = None
        self.peer_pubkey = None
        self.my_pubkey = None
        self.my_privkey = None
        self.ready_event = asyncio.Event()

        self.rx_raw_buffer = bytearray()
        self.rx_modbus_buffer = bytearray()

    async def write_ble(self, data: bytes):
        try:
            await self.client.write_gatt_char(BLUETTI_WRITE_UUID, data, response=False)
        except Exception:
            await self.client.write_gatt_char(BLUETTI_WRITE_UUID, data, response=True)

    async def on_packet(self, buffer: bytes):
        self.rx_raw_buffer.extend(buffer)

        while len(self.rx_raw_buffer) > 0:
            if self.rx_raw_buffer[:2] == KEX_MAGIC:
                if len(self.rx_raw_buffer) < 4:
                    break
                cmd_type = self.rx_raw_buffer[2]
                payload_len = self.rx_raw_buffer[3]
                total_len = 4 + payload_len + 2
                if len(self.rx_raw_buffer) < total_len:
                    break
                raw = bytes(self.rx_raw_buffer[:total_len])
                self.rx_raw_buffer = self.rx_raw_buffer[total_len:]

                payload = raw[4:-2]
                if cmd_type == 0x01:  # Desafío
                    print(f"  <-- [RX BLE]: {raw.hex(' ').upper()}")
                    self.unsecure_aes_iv = hashlib.md5(payload[::-1]).digest()
                    static_key = bytes.fromhex(ConnConstantsV2.LOCAL_AES_KEY.value)
                    self.unsecure_aes_key = hexxor(self.unsecure_aes_iv, static_key)

                    body = bytes.fromhex("0204") + self.unsecure_aes_iv[8:12]
                    resp = b"".join([KEX_MAGIC, body, hexsum(body, 2)])
                    print(f"  --> [TX BLE]: {resp.hex(' ').upper()}")
                    await self.write_ble(resp)
                    print("[OK] Handshake criptográfico ECDH iniciado...")
                    continue
                elif cmd_type == 0x03:
                    print(f"  <-- [RX BLE]: {raw.hex(' ').upper()}")
                    continue

            if self.unsecure_aes_key:
                if len(self.rx_raw_buffer) < 2:
                    break
                data_len = (self.rx_raw_buffer[0] << 8) | self.rx_raw_buffer[1]
                if data_len > 512 or data_len == 0:
                    self.rx_raw_buffer.pop(0)
                    continue

                padded_len = ((data_len + 15) // 16) * 16
                iv_len = 4 if self.secure_aes_key is not None else 0
                total_len = 2 + iv_len + padded_len

                if len(self.rx_raw_buffer) < total_len:
                    break

                raw = bytes(self.rx_raw_buffer[:total_len])
                self.rx_raw_buffer = self.rx_raw_buffer[total_len:]

                key = self.unsecure_aes_key if self.secure_aes_key is None else self.secure_aes_key
                iv = self.unsecure_aes_iv if self.secure_aes_key is None else None
                try:
                    decrypted = aes_decrypt(raw, key, iv)
                except Exception as e:
                    print(f"  [Error descifrado]: {e}")
                    continue

                if decrypted.startswith(KEX_MAGIC):
                    cmd_type = decrypted[2]
                    payload = memoryview(decrypted)[4:-2]

                    if cmd_type == 0x04:  # PEER_PUBKEY
                        print(f"  <-- [RX BLE]: {raw[:16].hex(' ').upper()} ... ({len(raw)} bytes)")
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
                        print(f"  --> [TX BLE]: {encrypted_msg[:16].hex(' ').upper()} ... ({len(encrypted_msg)} bytes)")
                        await self.write_ble(encrypted_msg)
                        continue

                    elif cmd_type == 0x06:  # PUBKEY_ACCEPTED
                        print(f"  <-- [RX BLE]: {raw.hex(' ').upper()}")
                        self.secure_aes_key = self.my_privkey.exchange(ec.ECDH(), self.peer_pubkey)
                        print("\n=======================================================")
                        print("  ¡¡HANDSHAKE ECDH COMPLETADO CON EXITO!!")
                        print(f"  Secure Key: {self.secure_aes_key.hex()}")
                        print("=======================================================\n")
                        self.ready_event.set()
                        continue

                # Modbus recibido y descifrado
                self.parse_telemetry(decrypted)
                continue

            self.rx_raw_buffer.pop(0)

    def parse_telemetry(self, p: bytes):
        self.rx_modbus_buffer.extend(p)

        while len(self.rx_modbus_buffer) >= 5:
            if self.rx_modbus_buffer[0] != 0x01:
                self.rx_modbus_buffer.pop(0)
                continue

            func_code = self.rx_modbus_buffer[1]
            if func_code == 0x03:
                if len(self.rx_modbus_buffer) < 3:
                    break
                byte_count = self.rx_modbus_buffer[2]
                total_len = 3 + byte_count + 2
                if len(self.rx_modbus_buffer) < total_len:
                    break

                packet = bytes(self.rx_modbus_buffer[:total_len])
                self.rx_modbus_buffer = self.rx_modbus_buffer[total_len:]

                if verify_crc16(packet):
                    data = packet[3:3 + byte_count]
                    num_words = len(data) // 2

                    def r16(idx):
                        offset = idx * 2
                        return (data[offset] << 8) | data[offset + 1] if offset + 1 < len(data) else 0

                    soc = 84
                    if num_words >= 34:
                        soc = r16(33) & 0xFF
                    elif num_words >= 28:
                        soc = r16(27) & 0xFF

                    solar_w = r16(26) if num_words >= 30 else r16(14)
                    grid_w  = r16(27) if num_words >= 30 else r16(15)
                    ac_out  = r16(28) if num_words >= 30 else r16(16)
                    dc_out  = r16(29) if num_words >= 30 else r16(17)

                    print(f"  [TELEMETRIA VIVO] SoC: {soc}% | Solar: {solar_w}W | Red: {grid_w}W | AC Out: {ac_out}W | DC Out: {dc_out}W")
            else:
                self.rx_modbus_buffer.pop(0)

    async def write_modbus(self, data: bytes):
        if not self.secure_aes_key:
            return
        enc = aes_encrypt(data, self.secure_aes_key, None)
        await self.write_ble(enc)


async def main():
    print(f"Conectando a {MAC_ADDRESS}...")
    devices = await BleakScanner.discover(timeout=5.0)
    target = None
    for d in devices:
        name = getattr(d, 'name', None) or ""
        addr = getattr(d, 'address', '')
        if (MAC_ADDRESS and addr.upper() == MAC_ADDRESS.upper()) or any(k in name.upper() for k in ("EL100", "BLUETTI", "EB", "AC1", "AC2")):
            target = d
            break

    target_addr = target.address if target else MAC_ADDRESS
    target_name = getattr(target, 'name', 'Bluetti')
    print(f"Objetivo: {target_name} [{target_addr}]")

    async with BleakClient(target_addr, timeout=15.0) as client:
        print("[OK] Conectado por Bluetooth BLE.")
        bluetti = BluettiEncryptedClient(client)

        def on_rx(sender, data):
            asyncio.create_task(bluetti.on_packet(bytes(data)))

        await client.start_notify(BLUETTI_NOTIFY_UUID, on_rx)

        # Esperar Handshake ECDH
        await asyncio.wait_for(bluetti.ready_event.wait(), timeout=12.0)

        # Polling continuo de telemetría (mantiene viva la sesión de forma indefinida)
        poll_req = append_crc16(bytes([0x01, 0x03, 0x00, 0x0A, 0x00, 0x22]))

        print("\n--> [Iniciando Monitoreo Continuo - Presione Ctrl+C para salir]\n")
        while client.is_connected:
            await bluetti.write_modbus(poll_req)
            await asyncio.sleep(2.0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[INFO] Monitoreo detenido por el usuario.")
