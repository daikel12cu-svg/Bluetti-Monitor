/**
 * Modbus RTU CRC-16 Calculation
 * Polynomial: 0xA001 (reversed representation of 0x8005)
 * Initial Value: 0xFFFF
 * Low byte sent first, followed by High byte
 */

export function calculateCrc16(buffer: Uint8Array | number[]): number {
  let crc = 0xFFFF;

  for (let i = 0; i < buffer.length; i++) {
    crc ^= buffer[i];
    for (let j = 0; j < 8; j++) {
      if ((crc & 0x0001) !== 0) {
        crc = (crc >> 1) ^ 0xA001;
      } else {
        crc = crc >> 1;
      }
    }
  }

  return crc;
}

/**
 * Returns [lowByte, highByte] as expected by Modbus RTU frames
 */
export function getCrc16Bytes(buffer: Uint8Array | number[]): [number, number] {
  const crc = calculateCrc16(buffer);
  const lowByte = crc & 0xFF;
  const highByte = (crc >> 8) & 0xFF;
  return [lowByte, highByte];
}

/**
 * Appends the 2-byte CRC16 (low byte first) to a packet
 */
export function appendCrc16(buffer: number[]): number[] {
  const [lowByte, highByte] = getCrc16Bytes(buffer);
  return [...buffer, lowByte, highByte];
}

/**
 * Verifies if the last 2 bytes of the packet match the calculated CRC16
 */
export function verifyCrc16(buffer: Uint8Array | number[]): boolean {
  if (buffer.length < 3) return false;
  const data = Array.from(buffer.slice(0, buffer.length - 2));
  const expectedCrc = calculateCrc16(data);
  const receivedLow = buffer[buffer.length - 2];
  const receivedHigh = buffer[buffer.length - 1];
  const receivedCrc = receivedLow | (receivedHigh << 8);
  return expectedCrc === receivedCrc;
}

/**
 * Converts array of bytes to uppercase hex string formatted with spaces
 */
export function bytesToHex(bytes: Uint8Array | number[]): string {
  return Array.from(bytes)
    .map(b => b.toString(16).padStart(2, '0').toUpperCase())
    .join(' ');
}

/**
 * Parses space-separated or raw hex string into array of numbers
 */
export function hexToBytes(hex: string): number[] {
  const clean = hex.replace(/[^0-9a-fA-F]/g, '');
  const bytes: number[] = [];
  for (let i = 0; i < clean.length; i += 2) {
    bytes.push(parseInt(clean.slice(i, i + 2), 16));
  }
  return bytes;
}
