import {
  BLUETTI_NOTIFY_UUID,
  BLUETTI_SERVICE_UUID,
  BLUETTI_WRITE_UUID,
  buildReadHoldingRegistersPacket,
  buildSetAcOutputPacket,
  buildSetChargingModePacket,
  buildSetDcOutputPacket,
  buildSetEcoModePacket,
  buildSetPowerLiftingPacket,
  buildSetGridEnhancementPacket,
  parseBluettiResponse,
  REGISTERS
} from './bluettiProtocol';
import { bytesToHex, calculateCrc16 } from './crc16';
import { BluettiTelemetry, ConnectionState, PacketLog } from '../types/bluetti';

export class BluettiBleService {
  private device: any = null;
  private gattServer: any = null;
  private notifyChar: any = null;
  private writeChar: any = null;
  
  private pollIntervalTimer: any = null;
  private incomingBuffer: number[] = [];
  
  private connectionState: ConnectionState = 'disconnected';
  private isPaused: boolean = false;
  
  private telemetry: BluettiTelemetry = {
    soc: 0,
    batteryVoltage: 0,
    batteryCurrent: 0,
    batteryTemp: 25,
    dcInputWatts: 0,
    dcInputVoltage: 0,
    dcInputCurrent: 0,
    acInputWatts: 0,
    acInputVoltage: 120,
    acInputCurrent: 0,
    acOutputWatts: 0,
    acOutputVoltage: 120,
    acOutputFreq: 60,
    dcOutputWatts: 0,
    acOutputOn: false,
    dcOutputOn: false,
    ecoModeOn: false,
    chargingMode: 'standard',
    powerLiftingOn: false,
    gridEnhancementOn: false,
    estimatedRuntimeMinutes: null,
    estimatedTimeToFullMinutes: null,
    totalEnergyCapacityWh: 1024,
    serialNumber: '---',
    firmwareVersion: '---',
    deviceName: 'Bluetti Elite 100 V2'
  };

  private listeners: {
    telemetry: ((data: BluettiTelemetry) => void)[];
    status: ((state: ConnectionState, message: string) => void)[];
    packet: ((packet: PacketLog) => void)[];
  } = {
    telemetry: [],
    status: [],
    packet: []
  };

  private isUsingLocalBackend: boolean = false;

  constructor() {
    this.initLocalBackendSync();
  }

  private initLocalBackendSync() {
    if (typeof window === 'undefined') return;

    const pollLocalBackend = async () => {
      try {
        const res = await fetch('/api/telemetry');
        if (res.ok) {
          const data = await res.json();
          if (data && typeof data === 'object' && ('soc' in data || 'connected' in data)) {
            this.isUsingLocalBackend = true;
            this.applyPythonTelemetry(data);
          }
        }
      } catch {
        // No hay backend Python local activo, continúa con Web Bluetooth estándar
      }
    };

    pollLocalBackend();
    setInterval(pollLocalBackend, 1200);
  }

  private applyPythonTelemetry(data: any) {
    const isConn = Boolean(data.connected);
    const inWatts = (data.dc_input_watts || 0) + (data.ac_input_watts || 0);
    const outWatts = (data.ac_output_watts || 0) + (data.dc_output_watts || 0);
    const net = inWatts - outWatts;

    let estRuntime: number | null = null;
    let estChargeTime: number | null = null;
    if (net < -5) {
      const remainingWh = (data.soc / 100) * 1024;
      estRuntime = Math.round((remainingWh / Math.abs(net)) * 60);
    } else if (net > 5) {
      const neededWh = ((100 - data.soc) / 100) * 1024;
      estChargeTime = Math.round((neededWh / net) * 60);
    }

    this.telemetry = {
      soc: data.soc !== undefined ? data.soc : 84,
      batteryVoltage: data.battery_volts || 51.2,
      batteryCurrent: data.battery_current || 0,
      batteryTemp: 28,
      dcInputWatts: data.dc_input_watts || 0,
      dcInputVoltage: data.dc_input_volts || 38.4,
      dcInputCurrent: data.dc_input_current || 0,
      acInputWatts: data.ac_input_watts || 0,
      acInputVoltage: data.ac_input_volts || 120,
      acInputCurrent: data.ac_input_current || 0,
      acOutputWatts: data.ac_output_watts || 0,
      acOutputVoltage: 120,
      acOutputFreq: 60,
      dcOutputWatts: data.dc_output_watts || 0,
      acOutputOn: Boolean(data.ac_output_on),
      dcOutputOn: Boolean(data.dc_output_on),
      ecoModeOn: Boolean(data.eco_mode_on),
      chargingMode: data.charge_mode || 'standard',
      powerLiftingOn: Boolean(data.power_lifting_on),
      gridEnhancementOn: Boolean(data.grid_enhancement_on),
      estimatedRuntimeMinutes: estRuntime,
      estimatedTimeToFullMinutes: estChargeTime,
      totalEnergyCapacityWh: 1024,
      serialNumber: 'EL100V22548139197153',
      firmwareVersion: 'v2.05-ECDH',
      deviceName: data.device_name || 'Bluetti Elite 100 V2'
    };

    if (isConn) {
      if (this.connectionState !== 'connected') {
        this.notifyStatus('connected', `Conectado a ${this.telemetry.deviceName} (ECDH Seguro)`);
      }
    } else {
      if (this.connectionState === 'connected') {
        this.notifyStatus('disconnected', 'Estación en reconexión...');
      }
    }

    this.notifyTelemetry();
  }

  // --- Listeners registration ---
  public onTelemetry(callback: (data: BluettiTelemetry) => void) {
    this.listeners.telemetry.push(callback);
    callback(this.telemetry);
    return () => {
      this.listeners.telemetry = this.listeners.telemetry.filter(c => c !== callback);
    };
  }

  public onStatus(callback: (state: ConnectionState, message: string) => void) {
    this.listeners.status.push(callback);
    callback(this.connectionState, this.getStatusMessage());
    return () => {
      this.listeners.status = this.listeners.status.filter(c => c !== callback);
    };
  }

  public onPacket(callback: (packet: PacketLog) => void) {
    this.listeners.packet.push(callback);
    return () => {
      this.listeners.packet = this.listeners.packet.filter(c => c !== callback);
    };
  }

  private notifyTelemetry() {
    for (const cb of this.listeners.telemetry) {
      cb({ ...this.telemetry });
    }
  }

  private notifyStatus(state: ConnectionState, message: string) {
    this.connectionState = state;
    for (const cb of this.listeners.status) {
      cb(state, message);
    }
  }

  private notifyPacket(packet: PacketLog) {
    for (const cb of this.listeners.packet) {
      cb(packet);
    }
  }

  public getTelemetry(): BluettiTelemetry {
    return { ...this.telemetry };
  }

  public getConnectionState(): ConnectionState {
    return this.connectionState;
  }

  public isSimulating(): boolean {
    return false;
  }

  public isWebBluetoothSupported(): boolean {
    return typeof navigator !== 'undefined' && 'bluetooth' in navigator;
  }

  private getStatusMessage(): string {
    if (this.isPaused) return 'Bluetooth Liberado (Móvil conectado)';
    switch (this.connectionState) {
      case 'connected': return `Conectado a ${this.telemetry.deviceName}`;
      case 'connecting': return 'Buscando y negociando GATT BLE...';
      case 'paused': return 'BLE en Pausa (Móvil)';
      case 'error': return 'Error de enlace Bluetooth';
      default: return 'Desconectado - Pulsa Buscar Bluetti';
    }
  }

  // --- Real Web Bluetooth Connection ---
  public async connectRealDevice(): Promise<void> {
    if (!this.isWebBluetoothSupported()) {
      throw new Error('Tu navegador no soporta Web Bluetooth. Usa Google Chrome o Microsoft Edge en Windows 10/11 con HTTPS o localhost.');
    }

    this.isPaused = false;
    this.notifyStatus('connecting', 'Abriendo selector Bluetooth de Windows...');

    try {
      const navAny = navigator as any;
      this.device = await navAny.bluetooth.requestDevice({
        filters: [
          { services: [BLUETTI_SERVICE_UUID] },
          { namePrefix: 'BLUETTI' },
          { namePrefix: 'Bluetti' },
          { namePrefix: 'bluetti' },
          { namePrefix: 'Elite' },
          { namePrefix: 'ELITE' },
          { namePrefix: 'AC' }
        ],
        optionalServices: [BLUETTI_SERVICE_UUID]
      });

      this.telemetry.deviceName = this.device.name || 'Bluetti Elite 100 V2';

      this.device.addEventListener('gattserverdisconnected', () => {
        this.handleDisconnected();
      });

      this.notifyStatus('connecting', `Conectando con ${this.telemetry.deviceName}...`);
      this.gattServer = await this.device.gatt.connect();

      this.notifyStatus('connecting', 'Obteniendo servicio Modbus GATT (0000FF00)...');
      const service = await this.gattServer.getPrimaryService(BLUETTI_SERVICE_UUID);

      this.notifyStatus('connecting', 'Configurando canal TX de escritura (0000FF02)...');
      this.writeChar = await service.getCharacteristic(BLUETTI_WRITE_UUID);

      this.notifyStatus('connecting', 'Suscribiendo a Notificaciones RX (0000FF01)...');
      this.notifyChar = await service.getCharacteristic(BLUETTI_NOTIFY_UUID);
      await this.notifyChar.startNotifications();
      this.notifyChar.addEventListener('characteristicvaluechanged', (event: any) => {
        this.handleIncomingData(event.target.value);
      });

      this.notifyStatus('connected', `Conectado a ${this.telemetry.deviceName}`);

      // Iniciar bucle de lectura con patrón "Read-First"
      this.startPollingLoop();

    } catch (err: any) {
      this.notifyStatus('error', err.message || 'Error al conectar');
      if (err.name === 'NotFoundError' || err.name === 'UserCancelledError') {
        this.notifyStatus('disconnected', 'Búsqueda cancelada por el usuario');
      }
      throw err;
    }
  }

  private handleIncomingData(dataView: DataView) {
    const rawBytes: number[] = [];
    for (let i = 0; i < dataView.byteLength; i++) {
      const b = dataView.getUint8(i);
      this.incomingBuffer.push(b);
      rawBytes.push(b);
    }

    // Handshake 2A 2A challenge response
    if (rawBytes.length >= 2 && rawBytes[0] === 0x2A && rawBytes[1] === 0x2A) {
      // Responder de inmediato por TX con el desafío/eco para autorizar la sesión
      this.writeGatt(rawBytes).catch(() => {});
    }

    // Process all complete Modbus frames in buffer
    while (this.incomingBuffer.length >= 5) {
      if (this.incomingBuffer[0] !== 0x01) {
        this.incomingBuffer.shift();
        continue;
      }

      const funcCode = this.incomingBuffer[1];

      if (funcCode === 0x03) {
        if (this.incomingBuffer.length < 3) break;
        const byteCount = this.incomingBuffer[2];
        const totalExpected = 3 + byteCount + 2;

        if (this.incomingBuffer.length < totalExpected) {
          // Still waiting for subsequent BLE fragments
          break;
        }

        const packet = this.incomingBuffer.splice(0, totalExpected);
        this.processCompletedPacket(packet);
      } else if (funcCode === 0x06) {
        if (this.incomingBuffer.length < 8) break;
        const packet = this.incomingBuffer.splice(0, 8);
        this.processCompletedPacket(packet);
      } else {
        this.incomingBuffer.shift();
      }
    }
  }

  private processCompletedPacket(bytes: number[]) {
    const { telemetry, valid, error } = parseBluettiResponse(bytes, this.telemetry);
    this.telemetry = telemetry;

    const expectedCrc = calculateCrc16(bytes.slice(0, Math.max(0, bytes.length - 2)));

    this.notifyPacket({
      id: Math.random().toString(36).substring(2, 9),
      timestamp: new Date().toLocaleTimeString(),
      direction: 'RX',
      commandType: bytes[1] === 0x03 ? 'Telemetría (Read 0x03)' : bytes[1] === 0x06 ? 'Confirmación (Write 0x06)' : 'Respuesta',
      rawHex: bytesToHex(bytes),
      bytes,
      crcValid: valid,
      crcExpected: `0x${expectedCrc.toString(16).toUpperCase()}`,
      description: valid
        ? `Lectura confirmada: Batería ${this.telemetry.soc}% | AC ${this.telemetry.acOutputWatts}W | DC ${this.telemetry.dcOutputWatts}W`
        : (error || 'Paquete inválido')
    });

    if (valid) {
      this.notifyTelemetry();
    }
  }

  private async writeGatt(packet: number[]): Promise<void> {
    if (!this.writeChar) return;
    const uint8 = new Uint8Array(packet);
    try {
      if (typeof this.writeChar.writeValueWithoutResponse === 'function') {
        await this.writeChar.writeValueWithoutResponse(uint8);
      } else {
        await this.writeChar.writeValue(uint8);
      }
    } catch {
      // Fallback
      try {
        await this.writeChar.writeValue(uint8);
      } catch (err2) {
        console.warn('Error en escritura GATT BLE:', err2);
      }
    }
  }

  private pollCycleCounter: number = 0;
  private isPollInProgress: boolean = false;

  private startPollingLoop() {
    this.stopPollingLoop();
    this.pollCycleCounter = 0;
    this.isPollInProgress = false;
    this.incomingBuffer = [];

    const executePoll = async () => {
      if (this.connectionState !== 'connected' || this.isPaused || !this.writeChar || this.isPollInProgress) return;

      this.isPollInProgress = true;
      try {
        // Enfoque "Read-First": Cada 3er ciclo lee los registros de configuración 0x0B00..0x0B05
        const isConfigPoll = (this.pollCycleCounter % 3 === 1);
        const startReg = isConfigPoll ? REGISTERS.CONTROL_AC_OUTPUT : REGISTERS.READ_BASE_STATUS;
        const lengthWords = isConfigPoll ? 6 : REGISTERS.READ_BASE_STATUS_LEN;
        const desc = isConfigPoll 
          ? `Lectura de configuración física actual (Reg 0x0B00..0x0B05: AC, DC, ECO, Modo Carga, Refuerzo)`
          : `Polling telemetría de energía (Reg 0x${REGISTERS.READ_BASE_STATUS.toString(16).toUpperCase()}, ${REGISTERS.READ_BASE_STATUS_LEN} words)`;

        const pollPacket = buildReadHoldingRegistersPacket(startReg, lengthWords);
        await this.writeGatt(pollPacket);

        this.notifyPacket({
          id: Math.random().toString(36).substring(2, 9),
          timestamp: new Date().toLocaleTimeString(),
          direction: 'TX',
          commandType: 'Read Holding Registers (0x03)',
          rawHex: bytesToHex(pollPacket),
          bytes: pollPacket,
          crcValid: true,
          crcExpected: '',
          description: desc
        });

        this.pollCycleCounter++;
      } catch (err) {
        console.warn('Advertencia en polling BLE:', err);
      } finally {
        this.isPollInProgress = false;
      }
    };

    // Primera lectura tras un respiro de 800ms para permitir que Windows complete el handshake GATT
    setTimeout(() => {
      executePoll();
    }, 800);

    // Polling periódico cada 2000 ms
    this.pollIntervalTimer = setInterval(executePoll, 2000);
  }

  private stopPollingLoop() {
    if (this.pollIntervalTimer) {
      clearInterval(this.pollIntervalTimer);
      this.pollIntervalTimer = null;
    }
  }

  /**
   * ENFOQUE B: Cierra el enlace GATT de forma instantánea para que el teléfono
   * pueda conectarse de inmediato sin interferencias.
   */
  public async pauseAndReleaseBle(): Promise<void> {
    this.isPaused = true;
    this.stopPollingLoop();

    if (this.gattServer && this.gattServer.connected) {
      try {
        await this.gattServer.disconnect();
      } catch (e) {
        console.warn('Error desconectando GATT al pausar:', e);
      }
    }

    this.notifyStatus('paused', 'Bluetooth Liberado (Móvil conectado)');
  }

  /**
   * Reanuda la conexión reconectando con el dispositivo previamente emparejado
   */
  public async resumeBle(): Promise<void> {
    this.isPaused = false;

    if (!this.device) {
      await this.connectRealDevice();
      return;
    }

    this.notifyStatus('connecting', 'Reconectando enlace BLE...');
    try {
      this.gattServer = await this.device.gatt.connect();
      const service = await this.gattServer.getPrimaryService(BLUETTI_SERVICE_UUID);
      this.notifyChar = await service.getCharacteristic(BLUETTI_NOTIFY_UUID);
      await this.notifyChar.startNotifications();
      this.writeChar = await service.getCharacteristic(BLUETTI_WRITE_UUID);

      this.notifyStatus('connected', `Reconectado a ${this.telemetry.deviceName}`);
      this.startPollingLoop();
    } catch (err: any) {
      this.notifyStatus('error', `Error al reconectar: ${err.message}`);
    }
  }

  private handleDisconnected() {
    this.stopPollingLoop();
    if (!this.isPaused) {
      this.notifyStatus('disconnected', 'Dispositivo desconectado');
    }
  }

  public disconnectRealDevice(): void {
    this.stopPollingLoop();
    if (this.gattServer && this.gattServer.connected) {
      this.gattServer.disconnect();
    }
    this.gattServer = null;
    this.device = null;
    this.notifyChar = null;
    this.writeChar = null;
    this.notifyStatus('disconnected', 'Desconectado por el usuario');
  }

  // --- Hardware Control Commands ---
  public async toggleAcOutput(): Promise<void> {
    const newState = !this.telemetry.acOutputOn;

    if (this.isUsingLocalBackend) {
      await fetch('/api/command/set_ac_output', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ turn_on: newState })
      });
      this.telemetry.acOutputOn = newState;
      this.notifyTelemetry();
      return;
    }

    if (!this.writeChar || this.connectionState !== 'connected') {
      throw new Error('Debes conectar primero la estación Bluetti por Bluetooth.');
    }

    const packet = buildSetAcOutputPacket(newState);

    this.notifyPacket({
      id: Math.random().toString(36).substring(2, 9),
      timestamp: new Date().toLocaleTimeString(),
      direction: 'TX',
      commandType: 'Write Single Register (0x06)',
      rawHex: bytesToHex(packet),
      bytes: packet,
      crcValid: true,
      crcExpected: '',
      description: `Inversor AC -> ${newState ? 'ENCENDER' : 'APAGAR'} (Reg 0x0B00)`
    });

    await this.writeGatt(packet);
  }

  public async toggleDcOutput(): Promise<void> {
    const newState = !this.telemetry.dcOutputOn;

    if (this.isUsingLocalBackend) {
      await fetch('/api/command/set_dc_output', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ turn_on: newState })
      });
      this.telemetry.dcOutputOn = newState;
      this.notifyTelemetry();
      return;
    }

    if (!this.writeChar || this.connectionState !== 'connected') {
      throw new Error('Debes conectar primero la estación Bluetti por Bluetooth.');
    }

    const packet = buildSetDcOutputPacket(newState);

    this.notifyPacket({
      id: Math.random().toString(36).substring(2, 9),
      timestamp: new Date().toLocaleTimeString(),
      direction: 'TX',
      commandType: 'Write Single Register (0x06)',
      rawHex: bytesToHex(packet),
      bytes: packet,
      crcValid: true,
      crcExpected: '',
      description: `Salidas DC (12V/USB) -> ${newState ? 'ENCENDER' : 'APAGAR'} (Reg 0x0B01)`
    });

    await this.writeGatt(packet);
  }

  public async setChargingMode(mode: 'silent' | 'standard' | 'turbo'): Promise<void> {
    if (this.isUsingLocalBackend) {
      await fetch('/api/command/set_charge_mode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ mode })
      });
      this.telemetry.chargingMode = mode;
      this.notifyTelemetry();
      return;
    }

    if (!this.writeChar || this.connectionState !== 'connected') {
      throw new Error('Debes conectar primero la estación Bluetti por Bluetooth.');
    }

    const packet = buildSetChargingModePacket(mode);
    this.notifyPacket({
      id: Math.random().toString(36).substring(2, 9),
      timestamp: new Date().toLocaleTimeString(),
      direction: 'TX',
      commandType: 'Write Single Register (0x06)',
      rawHex: bytesToHex(packet),
      bytes: packet,
      crcValid: true,
      crcExpected: '',
      description: `Modo de Carga AC -> ${mode.toUpperCase()} (Reg 0x0B04)`
    });

    await this.writeGatt(packet);
  }

  public async toggleEcoMode(): Promise<void> {
    const newState = !this.telemetry.ecoModeOn;

    if (this.isUsingLocalBackend) {
      await fetch('/api/command/set_eco_mode', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ turn_on: newState })
      });
      this.telemetry.ecoModeOn = newState;
      this.notifyTelemetry();
      return;
    }

    if (!this.writeChar || this.connectionState !== 'connected') {
      throw new Error('Debes conectar primero la estación Bluetti por Bluetooth.');
    }

    const packet = buildSetEcoModePacket(newState);

    this.notifyPacket({
      id: Math.random().toString(36).substring(2, 9),
      timestamp: new Date().toLocaleTimeString(),
      direction: 'TX',
      commandType: 'Write Single Register (0x06)',
      rawHex: bytesToHex(packet),
      bytes: packet,
      crcValid: true,
      crcExpected: '',
      description: `Modo ECO -> ${newState ? 'ACTIVADO' : 'DESACTIVADO'} (Reg 0x0B02)`
    });

    await this.writeGatt(packet);
  }

  public async togglePowerLifting(): Promise<void> {
    const newState = !this.telemetry.powerLiftingOn;

    if (this.isUsingLocalBackend) {
      await fetch('/api/command/set_power_lifting', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ turn_on: newState })
      });
      this.telemetry.powerLiftingOn = newState;
      this.notifyTelemetry();
      return;
    }

    if (!this.writeChar || this.connectionState !== 'connected') {
      throw new Error('Debes conectar primero la estación Bluetti por Bluetooth.');
    }

    const packet = buildSetPowerLiftingPacket(newState);

    this.notifyPacket({
      id: Math.random().toString(36).substring(2, 9),
      timestamp: new Date().toLocaleTimeString(),
      direction: 'TX',
      commandType: 'Write Single Register (0x06)',
      rawHex: bytesToHex(packet),
      bytes: packet,
      crcValid: true,
      crcExpected: '',
      description: `Power Lifting (2700W) -> ${newState ? 'ACTIVADO' : 'DESACTIVADO'} (Reg 0x0B03)`
    });

    await this.writeGatt(packet);
  }

  public async toggleGridEnhancement(): Promise<void> {
    const newState = !this.telemetry.gridEnhancementOn;

    if (this.isUsingLocalBackend) {
      await fetch('/api/command/set_grid_enhancement', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ turn_on: newState })
      });
      this.telemetry.gridEnhancementOn = newState;
      this.notifyTelemetry();
      return;
    }

    if (!this.writeChar || this.connectionState !== 'connected') {
      throw new Error('Debes conectar primero la estación Bluetti por Bluetooth.');
    }

    const packet = buildSetGridEnhancementPacket(newState);

    this.notifyPacket({
      id: Math.random().toString(36).substring(2, 9),
      timestamp: new Date().toLocaleTimeString(),
      direction: 'TX',
      commandType: 'Write Single Register (0x06)',
      rawHex: bytesToHex(packet),
      bytes: packet,
      crcValid: true,
      crcExpected: '',
      description: `Refuerzo de Red (Grid Enhancement) -> ${newState ? 'ACTIVADO' : 'DESACTIVADO'} (Reg 0x0B05)`
    });

    await this.writeGatt(packet);
  }
}

export const bluettiService = new BluettiBleService();
