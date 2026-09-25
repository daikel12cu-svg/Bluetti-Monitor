import { appendCrc16, bytesToHex, verifyCrc16 } from './crc16';
import { BluettiTelemetry, RegisterDefinition } from '../types/bluetti';

// Bluetooth GATT UUIDs for Bluetti
export const BLUETTI_SERVICE_UUID = '0000ff00-0000-1000-8000-00805f9b34fb';
export const BLUETTI_NOTIFY_UUID = '0000ff01-0000-1000-8000-00805f9b34fb';
export const BLUETTI_WRITE_UUID = '0000ff02-0000-1000-8000-00805f9b34fb';

// Common Modbus addresses for Bluetti Elite 100 V2 / AC180 architecture
export const REGISTERS = {
  // Read Holding Registers (Function 0x03) - Rango oficial Bluetti EB3A / AC180 / Elite 100 V2
  READ_BASE_STATUS: 0x000A, // Reads from offset 10 up to 0x002C (SoC, voltages, power)
  READ_BASE_STATUS_LEN: 34,  // 34 words
  
  // Specific Register offsets within status response or individual reads:
  SOC: 0x002B,               // State of Charge (%)
  DC_INPUT_WATTS: 0x0024,    // Solar / Car DC input power (W)
  AC_INPUT_WATTS: 0x0025,    // AC Grid charging power (W)
  AC_OUTPUT_WATTS: 0x0026,   // AC Inverter output power (W)
  DC_OUTPUT_WATTS: 0x0027,   // DC 12V / USB output power (W)
  
  BATTERY_VOLTAGE: 0x0014,   // Battery voltage (0.1 V)
  BATTERY_CURRENT: 0x0015,   // Battery current (0.1 A)
  BATTERY_TEMP: 0x0016,      // Battery temperature (°C)
  
  AC_OUTPUT_VOLTAGE: 0x001A, // AC output voltage (0.1 V)
  AC_OUTPUT_FREQ: 0x001B,    // AC output frequency (0.1 Hz)
  
  // Write Single Register (Function 0x06) - Control Registers
  // Config Page 0x0B offsets: 0xBF (3007) for AC Output, 0xC0 (3008) for DC Output
  CONTROL_AC_OUTPUT: 0x0BBF, // 0x0001 = ON, 0x0000 = OFF (Page 0x0B, Offset 0xBF - 3007 dec)
  CONTROL_AC_OUTPUT_ALT: 0x0B00, // Legacy fallback
  CONTROL_DC_OUTPUT: 0x0BC0, // 0x0001 = ON, 0x0000 = OFF (Page 0x0B, Offset 0xC0 - 3008 dec)
  CONTROL_DC_OUTPUT_ALT: 0x0B01, // Legacy fallback
  CONTROL_ECO_MODE: 0x0B02,  // 0x0001 = ON, 0x0000 = OFF
  CONTROL_POWER_LIFT: 0x0B03,// 0x0001 = ON, 0x0000 = OFF
  CONTROL_CHARGE_MODE: 0x0B04,// 0 = Silent, 1 = Standard, 2 = Turbo
  CONTROL_GRID_ENHANCEMENT: 0x0B05 // 0x0001 = Refuerzo de Red ON, 0x0000 = OFF
};

export const REGISTER_DOCS: RegisterDefinition[] = [
  {
    addressHex: '0x002B',
    addressDec: 43,
    lengthWords: 1,
    name: 'State of Charge (SoC)',
    type: 'uint8',
    unit: '%',
    description: 'Nivel porcentual de carga de la batería LiFePO4 (0 a 100%)',
    category: 'battery'
  },
  {
    addressHex: '0x0024',
    addressDec: 36,
    lengthWords: 1,
    name: 'DC Solar / Car Input',
    type: 'uint16',
    unit: 'W',
    description: 'Potencia de entrada solar MPPT (1000W Max, 12V-60V, 20A Max) o mechero de coche',
    category: 'input'
  },
  {
    addressHex: '0x0025',
    addressDec: 37,
    lengthWords: 1,
    name: 'AC Grid Input',
    type: 'uint16',
    unit: 'W',
    description: 'Potencia de entrada de recarga de red eléctrica (1200W Max TurboBoost, 120V)',
    category: 'input'
  },
  {
    addressHex: '0x0026',
    addressDec: 38,
    lengthWords: 1,
    name: 'AC Inverter Output',
    type: 'uint16',
    unit: 'W',
    description: 'Potencia consumida por las tomas de corriente alterna 120V 60Hz',
    category: 'output'
  },
  {
    addressHex: '0x0027',
    addressDec: 39,
    lengthWords: 1,
    name: 'DC Output (USB/12V)',
    type: 'uint16',
    unit: 'W',
    description: 'Potencia consumida por puertos USB-A, USB-C PD y toma de 12V 10A',
    category: 'output'
  },
  {
    addressHex: '0x0014',
    addressDec: 20,
    lengthWords: 1,
    name: 'Battery Voltage',
    type: 'uint16',
    scale: 0.1,
    unit: 'V',
    description: 'Tensión total del pack de baterías (0.1V)',
    category: 'battery'
  },
  {
    addressHex: '0x0015',
    addressDec: 21,
    lengthWords: 1,
    name: 'Battery Current',
    type: 'int16',
    scale: 0.1,
    unit: 'A',
    description: 'Corriente de batería (positivo = carga, negativo = descarga)',
    category: 'battery'
  },
  {
    addressHex: '0x0016',
    addressDec: 22,
    lengthWords: 1,
    name: 'Battery Temperature',
    type: 'int16',
    unit: '°C',
    description: 'Temperatura interna de las celdas',
    category: 'battery'
  },
  {
    addressHex: '0x0BBF',
    addressDec: 3007,
    lengthWords: 1,
    name: 'Control Inversor AC',
    type: 'uint16',
    description: '0x0001 = Encender tomas AC 1800W, 0x0000 = Apagar (Página 0x0B, offset 0xBF)',
    category: 'control'
  },
  {
    addressHex: '0x0BC0',
    addressDec: 3008,
    lengthWords: 1,
    name: 'Control Salida DC',
    type: 'uint16',
    description: '0x0001 = Encender puertos USB/12V, 0x0000 = Apagar (Página 0x0B, offset 0xC0)',
    category: 'control'
  },
  {
    addressHex: '0x0B04',
    addressDec: 2820,
    lengthWords: 1,
    name: 'Modo de Carga AC',
    type: 'uint16',
    description: '0 = Silencioso (~400W), 1 = Estándar (~800W), 2 = TurboBoost (1200W Max)',
    category: 'control'
  }
];

/**
 * Builds a Modbus RTU Read Holding Registers (0x03) frame
 * Format: [SlaveID=0x01, Function=0x03, RegHigh, RegLow, QtyHigh, QtyLow, CRCLow, CRCHigh]
 */
export function buildReadHoldingRegistersPacket(startRegister: number, count: number): number[] {
  const payload = [
    0x01, // Slave ID (Bluetti default is 0x01)
    0x03, // Modbus Function: Read Holding Registers
    (startRegister >> 8) & 0xFF,
    startRegister & 0xFF,
    (count >> 8) & 0xFF,
    count & 0xFF
  ];
  return appendCrc16(payload);
}

/**
 * Builds a Modbus RTU Write Single Register (0x06) frame
 * Format: [SlaveID=0x01, Function=0x06, RegHigh, RegLow, ValHigh, ValLow, CRCLow, CRCHigh]
 */
export function buildWriteRegisterPacket(register: number, value: number): number[] {
  const payload = [
    0x01, // Slave ID
    0x06, // Modbus Function: Write Single Register
    (register >> 8) & 0xFF,
    register & 0xFF,
    (value >> 8) & 0xFF,
    value & 0xFF
  ];
  return appendCrc16(payload);
}

/**
 * Builds AC output toggle command
 */
export function buildSetAcOutputPacket(turnOn: boolean): number[] {
  return buildWriteRegisterPacket(REGISTERS.CONTROL_AC_OUTPUT, turnOn ? 0x0001 : 0x0000);
}

/**
 * Builds DC output toggle command
 */
export function buildSetDcOutputPacket(turnOn: boolean): number[] {
  return buildWriteRegisterPacket(REGISTERS.CONTROL_DC_OUTPUT, turnOn ? 0x0001 : 0x0000);
}

/**
 * Builds Charging Mode command (0=silent, 1=standard, 2=turbo)
 */
export function buildSetChargingModePacket(mode: 'silent' | 'standard' | 'turbo'): number[] {
  const modeVal = mode === 'silent' ? 0x0000 : mode === 'standard' ? 0x0001 : 0x0002;
  return buildWriteRegisterPacket(REGISTERS.CONTROL_CHARGE_MODE, modeVal);
}

/**
 * Builds ECO Mode toggle command
 */
export function buildSetEcoModePacket(turnOn: boolean): number[] {
  return buildWriteRegisterPacket(REGISTERS.CONTROL_ECO_MODE, turnOn ? 0x0001 : 0x0000);
}

/**
 * Builds Power Lifting toggle command (2700W for resistive loads)
 */
export function buildSetPowerLiftingPacket(turnOn: boolean): number[] {
  return buildWriteRegisterPacket(REGISTERS.CONTROL_POWER_LIFT, turnOn ? 0x0001 : 0x0000);
}

/**
 * Builds Grid Enhancement / Refuerzo de Red command
 */
export function buildSetGridEnhancementPacket(turnOn: boolean): number[] {
  return buildWriteRegisterPacket(REGISTERS.CONTROL_GRID_ENHANCEMENT, turnOn ? 0x0001 : 0x0000);
}

/**
 * Parses a response buffer received from Bluetti NOTIFY characteristic
 */
export function parseBluettiResponse(
  buffer: Uint8Array | number[],
  currentTelemetry: BluettiTelemetry
): { telemetry: BluettiTelemetry; valid: boolean; error?: string } {
  const bytes = Array.from(buffer);
  
  if (bytes.length < 5) {
    return { telemetry: currentTelemetry, valid: false, error: 'Trama demasiado corta (< 5 bytes)' };
  }

  const isValidCrc = verifyCrc16(bytes);
  if (!isValidCrc) {
    return { telemetry: currentTelemetry, valid: false, error: 'Error de comprobación CRC16 Modbus' };
  }

  const slaveId = bytes[0];
  const functionCode = bytes[1];

  // Modbus Read Response: [SlaveID, 0x03, ByteCount, D0_H, D0_L, ...]
  if (functionCode === 0x03) {
    const byteCount = bytes[2];
    const dataBytes = bytes.slice(3, 3 + byteCount);

    if (dataBytes.length < byteCount) {
      return { telemetry: currentTelemetry, valid: false, error: 'Longitud de datos incompleta' };
    }

    // Helper to read 16-bit uint
    const readUint16 = (wordIndex: number) => {
      const idx = wordIndex * 2;
      if (idx + 1 < dataBytes.length) {
        return (dataBytes[idx] << 8) | dataBytes[idx + 1];
      }
      return 0;
    };

    // Helper to read 16-bit signed int
    const readInt16 = (wordIndex: number) => {
      const val = readUint16(wordIndex);
      return val > 32767 ? val - 65536 : val;
    };

    // Depending on start register requested (usually 0x0010 with 28 words)
    // Offset 0x002B - 0x0010 = 27 (0-indexed word 27) -> SoC
    // In typical Bluetti layout:
    // Word 4: Battery Voltage (0.1V)
    // Word 5: Battery Current (0.1A)
    // Word 6: Battery Temp
    // Word 10: AC Output Voltage
    // Word 14: DC Input Power
    // Word 15: AC Input Power
    // Word 16: AC Output Power
    // Word 17: DC Output Power
    // Word 27: Battery SoC %
    
    // We safely parse whatever words are provided
    const numWords = Math.floor(dataBytes.length / 2);
    const updated = { ...currentTelemetry };

    // Caso A: Rango Oficial 0x000A (30 a 35 palabras devueltas desde offset 10)
    // Offset 10 (0x000A):
    // Word 10 = Reg 0x0014: Battery Voltage (0.1V)
    // Word 11 = Reg 0x0015: Battery Current (0.1A)
    // Word 12 = Reg 0x0016: Battery Temp
    // Word 16 = Reg 0x001A: AC Output Voltage
    // Word 26 = Reg 0x0024: DC Input Power
    // Word 27 = Reg 0x0025: AC Input Power
    // Word 28 = Reg 0x0026: AC Output Power
    // Word 29 = Reg 0x0027: DC Output Power
    // Word 33 = Reg 0x002B: Battery SoC % (si se leen 34+ palabras)
    if (numWords >= 30) {
      if (numWords >= 34) {
        const socRaw = readUint16(33) & 0xFF;
        if (socRaw > 0 && socRaw <= 100) updated.soc = socRaw;
      }

      updated.dcInputWatts = readUint16(26);
      updated.acInputWatts = readUint16(27);
      updated.acOutputWatts = readUint16(28);
      updated.dcOutputWatts = readUint16(29);

      const voltRaw = readUint16(10);
      if (voltRaw > 0) updated.batteryVoltage = Math.round(voltRaw * 0.1 * 10) / 10;

      const currRaw = readInt16(11);
      updated.batteryCurrent = Math.round(currRaw * 0.1 * 10) / 10;

      const tempRaw = readInt16(12);
      if (tempRaw !== 0) updated.batteryTemp = tempRaw;

      const acVolt = readUint16(16);
      if (acVolt > 0) updated.acOutputVoltage = Math.round(acVolt * 0.1);

      if (updated.acOutputWatts > 0) updated.acOutputOn = true;
      if (updated.dcOutputWatts > 0) updated.dcOutputOn = true;
    }
    // Caso B: Rango 0x0010 (28 palabras devueltas desde offset 16)
    else if (numWords >= 28) {
      const socRaw = readUint16(27) & 0xFF;
      if (socRaw > 0 && socRaw <= 100) {
        updated.soc = socRaw;
      }

      updated.dcInputWatts = readUint16(14);
      updated.acInputWatts = readUint16(15);
      updated.acOutputWatts = readUint16(16);
      updated.dcOutputWatts = readUint16(17);

      const voltRaw = readUint16(4);
      if (voltRaw > 0) updated.batteryVoltage = Math.round(voltRaw * 0.1 * 10) / 10;

      const currRaw = readInt16(5);
      updated.batteryCurrent = Math.round(currRaw * 0.1 * 10) / 10;

      const tempRaw = readInt16(6);
      if (tempRaw !== 0) updated.batteryTemp = tempRaw;

      const acVolt = readUint16(10);
      if (acVolt > 0) updated.acOutputVoltage = Math.round(acVolt * 0.1);

      // Infer switches if output power is present or from status bits
      if (updated.acOutputWatts > 0) updated.acOutputOn = true;
      if (updated.dcOutputWatts > 0) updated.dcOutputOn = true;
    } else if (numWords === 6) {
      // Configuration & Control Registers (0x0B00 block read)
      // Word 0: AC Output (1=ON, 0=OFF)
      // Word 1: DC Output (1=ON, 0=OFF)
      // Word 2: ECO Mode (1=ON, 0=OFF)
      // Word 3: Power Lifting (1=ON, 0=OFF)
      // Word 4: Charging Mode (0=Silent, 1=Standard, 2=Turbo)
      // Word 5: Grid Enhancement (1=ON, 0=OFF)
      const acSwitch = readUint16(0);
      const dcSwitch = readUint16(1);
      const ecoSwitch = readUint16(2);
      const pLiftSwitch = readUint16(3);
      const chargeMode = readUint16(4);
      const gridEnhance = readUint16(5);

      updated.acOutputOn = acSwitch === 1;
      updated.dcOutputOn = dcSwitch === 1;
      updated.ecoModeOn = ecoSwitch === 1;
      updated.powerLiftingOn = pLiftSwitch === 1;
      if (chargeMode === 0) updated.chargingMode = 'silent';
      else if (chargeMode === 1) updated.chargingMode = 'standard';
      else if (chargeMode === 2) updated.chargingMode = 'turbo';
      updated.gridEnhancementOn = gridEnhance === 1;
    } else if (numWords === 8) {
      // Offset 0x0024 (8 words: DC in, AC in, AC out, DC out, ..., SoC at offset 7)
      updated.dcInputWatts = readUint16(0);
      updated.acInputWatts = readUint16(1);
      updated.acOutputWatts = readUint16(2);
      updated.dcOutputWatts = readUint16(3);
      const socRaw = readUint16(7) & 0xFF;
      if (socRaw > 0 && socRaw <= 100) updated.soc = socRaw;
      if (updated.acOutputWatts > 0) updated.acOutputOn = true;
      if (updated.dcOutputWatts > 0) updated.dcOutputOn = true;
    } else if (numWords >= 5) {
      // Shorter frame mapping
      const word0 = readUint16(0);
      if (word0 >= 0 && word0 <= 100) updated.soc = word0;
      if (numWords > 1) updated.dcInputWatts = readUint16(1);
      if (numWords > 2) updated.acInputWatts = readUint16(2);
      if (numWords > 3) updated.acOutputWatts = readUint16(3);
      if (numWords > 4) updated.dcOutputWatts = readUint16(4);
    }

    // Calculate Amperage (I = P / V) for inputs just like the official mobile app
    if (updated.dcInputVoltage > 0 && updated.dcInputWatts > 0) {
      updated.dcInputCurrent = Math.round((updated.dcInputWatts / updated.dcInputVoltage) * 10) / 10;
    } else {
      updated.dcInputCurrent = 0;
    }

    if (updated.acInputVoltage > 0 && updated.acInputWatts > 0) {
      updated.acInputCurrent = Math.round((updated.acInputWatts / updated.acInputVoltage) * 10) / 10;
    } else {
      updated.acInputCurrent = 0;
    }

    // Recalculate runtime estimates based on capacity and net power
    const netInputWatts = updated.dcInputWatts + updated.acInputWatts;
    const netOutputWatts = updated.acOutputWatts + updated.dcOutputWatts;
    const remainingWh = (updated.soc / 100) * updated.totalEnergyCapacityWh;

    if (netOutputWatts > netInputWatts + 5) {
      // Discharging
      const netDrainWatts = netOutputWatts - netInputWatts;
      const hours = remainingWh / netDrainWatts;
      updated.estimatedRuntimeMinutes = Math.max(1, Math.round(hours * 60));
      updated.estimatedTimeToFullMinutes = null;
    } else if (netInputWatts > netOutputWatts + 5) {
      // Charging
      const netChargeWatts = (netInputWatts - netOutputWatts) * 0.9; // 90% efficiency
      const neededWh = updated.totalEnergyCapacityWh - remainingWh;
      const hours = neededWh / netChargeWatts;
      updated.estimatedTimeToFullMinutes = Math.max(1, Math.round(hours * 60));
      updated.estimatedRuntimeMinutes = null;
    } else {
      updated.estimatedRuntimeMinutes = null;
      updated.estimatedTimeToFullMinutes = null;
    }

    return { telemetry: updated, valid: true };
  }

  // Modbus Write Response: Echoes the write request [0x01, 0x06, RegH, RegL, ValH, ValL]
  if (functionCode === 0x06 && bytes.length >= 8) {
    const reg = (bytes[2] << 8) | bytes[3];
    const val = (bytes[4] << 8) | bytes[5];
    const updated = { ...currentTelemetry };

    if (reg === REGISTERS.CONTROL_AC_OUTPUT || reg === REGISTERS.CONTROL_AC_OUTPUT_ALT || reg === 0x0BBF || reg === 0x0B00) {
      updated.acOutputOn = val === 0x0001;
    } else if (reg === REGISTERS.CONTROL_DC_OUTPUT || reg === REGISTERS.CONTROL_DC_OUTPUT_ALT || reg === 0x0BC0 || reg === 0x0B01) {
      updated.dcOutputOn = val === 0x0001;
    } else if (reg === REGISTERS.CONTROL_ECO_MODE || reg === 0x0B02) {
      updated.ecoModeOn = val === 0x0001;
    } else if (reg === REGISTERS.CONTROL_CHARGE_MODE || reg === 0x0B04) {
      updated.chargingMode = val === 0 ? 'silent' : val === 1 ? 'standard' : 'turbo';
    } else if (reg === REGISTERS.CONTROL_GRID_ENHANCEMENT) {
      updated.gridEnhancementOn = val === 0x0001;
    }

    return { telemetry: updated, valid: true };
  }

  return { telemetry: currentTelemetry, valid: true };
}
