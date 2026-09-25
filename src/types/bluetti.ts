export interface BluettiTelemetry {
  soc: number; // State of Charge 0-100%
  batteryVoltage: number; // Volts (e.g., 51.2V)
  batteryCurrent: number; // Amps
  batteryTemp: number; // Celsius
  
  // Power Inputs
  dcInputWatts: number; // Solar / Car DC input (W) - Max 1000W
  dcInputVoltage: number; // Volts (12V - 60V MPPT)
  dcInputCurrent: number; // Amps (calculated or measured, Max 20A)
  acInputWatts: number; // Grid AC input (W) - Max 1200W TurboBoost
  acInputVoltage: number; // Volts (120V nominal)
  acInputCurrent: number; // Amps (calculated or measured, Max ~10-12A)
  
  // Power Outputs
  acOutputWatts: number; // Inverter AC output (W)
  acOutputVoltage: number; // 120V / 230V
  acOutputFreq: number; // 50 / 60 Hz
  dcOutputWatts: number; // 12V + USB outputs (W)
  
  // States
  acOutputOn: boolean;
  dcOutputOn: boolean;
  ecoModeOn: boolean;
  chargingMode: 'silent' | 'standard' | 'turbo';
  powerLiftingOn: boolean;
  gridEnhancementOn: boolean; // Modo Refuerzo de Red Eléctrica (Grid Enhancement / Self-Adaption)
  
  // Runtime estimates
  estimatedRuntimeMinutes: number | null; // null if charging
  estimatedTimeToFullMinutes: number | null; // null if discharging
  
  totalEnergyCapacityWh: number; // 1024 Wh for Elite 100 V2
  serialNumber?: string;
  firmwareVersion?: string;
  deviceName: string;
}

export type ConnectionState = 'disconnected' | 'connecting' | 'connected' | 'paused' | 'error';

export interface PacketLog {
  id: string;
  timestamp: string;
  direction: 'TX' | 'RX';
  commandType: string;
  rawHex: string;
  bytes: number[];
  crcValid: boolean;
  crcExpected: string;
  description: string;
}

export interface RegisterDefinition {
  addressHex: string;
  addressDec: number;
  lengthWords: number;
  name: string;
  type: 'uint16' | 'int16' | 'uint8' | 'bitfield' | 'string';
  unit?: string;
  scale?: number;
  description: string;
  category: 'status' | 'input' | 'output' | 'battery' | 'control';
}
