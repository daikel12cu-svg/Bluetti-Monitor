import React, { useState, useEffect, useRef } from 'react';
import {
  Activity,
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  Clock,
  Copy,
  ExternalLink,
  Info,
  Play,
  RefreshCw,
  Sliders,
  Terminal,
  VolumeX,
  Wifi,
  WifiOff,
  X,
  Zap
} from 'lucide-react';
import {
  BLUETTI_NOTIFY_UUID,
  BLUETTI_SERVICE_UUID,
  BLUETTI_WRITE_UUID,
  buildReadHoldingRegistersPacket,
  parseBluettiResponse
} from '../services/bluettiProtocol';
import { bytesToHex, calculateCrc16, verifyCrc16 } from '../services/crc16';
import { BluettiTelemetry } from '../types/bluetti';

interface BleDiagnosticModalProps {
  isOpen: boolean;
  onClose: () => void;
}

interface TestLog {
  time: string;
  type: 'info' | 'success' | 'warning' | 'error';
  text: string;
}

export const BleDiagnosticModal: React.FC<BleDiagnosticModalProps> = ({ isOpen, onClose }) => {
  const [isRunningTest, setIsRunningTest] = useState(false);
  const [activeTest, setActiveTest] = useState<string | null>(null);
  const [logs, setLogs] = useState<TestLog[]>([]);
  const [device, setDevice] = useState<any>(null);
  const [gattServer, setGattServer] = useState<any>(null);
  const [notifyChar, setNotifyChar] = useState<any>(null);
  const [writeChar, setWriteChar] = useState<any>(null);
  const charTxRef = useRef<any>(null);
  
  // Timer & Results
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const [silenceResult, setSilenceResult] = useState<'pending' | 'success' | 'failed' | null>(null);
  const [modbusResponse, setModbusResponse] = useState<string | null>(null);
  const [decodedPreview, setDecodedPreview] = useState<string | null>(null);
  const [writeMode, setWriteMode] = useState<'withoutResponse' | 'withResponse'>('withoutResponse');
  const rxBufferRef = useRef<number[]>([]);

  const logEndRef = useRef<HTMLDivElement>(null);
  const timerRef = useRef<any>(null);

  const addLog = (type: 'info' | 'success' | 'warning' | 'error', text: string) => {
    const time = new Date().toLocaleTimeString() + '.' + String(new Date().getMilliseconds()).padStart(3, '0');
    setLogs(prev => [...prev.slice(-150), { time, type, text }]);
  };

  useEffect(() => {
    if (logEndRef.current) {
      logEndRef.current.scrollIntoView({ behavior: 'smooth' });
    }
  }, [logs]);

  // Limpieza solo cuando el componente se desmonte o el usuario desconecte explícitamente
  useEffect(() => {
    return () => {
      // Solo en desmontaje total si se desea limpiar
    };
  }, []);

  const cleanup = () => {
    if (timerRef.current) clearInterval(timerRef.current);
    if (gattServer && gattServer.connected) {
      try {
        gattServer.disconnect();
      } catch {}
    }
    setDevice(null);
    setGattServer(null);
    setNotifyChar(null);
    setWriteChar(null);
    setIsRunningTest(false);
    setActiveTest(null);
    addLog('info', 'Conexión de prueba cerrada manualmente.');
  };

  // -------------------------------------------------------------
  // TEST 1: KEEPALIVE EN SILENCIO (15 SEGUNDOS)
  // Conecta y NO envía ningún comando Modbus.
  // Si se cae a los 2-3s: CULPABLE ES WINDOWS (Ahorro de energía o pairing).
  // Si aguanta 15s: Windows es 100% estable.
  // -------------------------------------------------------------
  const runSilenceTest = async () => {
    cleanup();
    setIsRunningTest(true);
    setActiveTest('silence');
    setSilenceResult('pending');
    setElapsedSeconds(0);
    setLogs([]);

    addLog('info', 'Iniciando Prueba 1: Conexión Básica en Silencio Total (15s)...');
    addLog('info', 'Abriendo selector Bluetooth de Windows...');

    try {
      const navAny = navigator as any;
      if (!navAny.bluetooth) {
        addLog('error', 'Tu navegador no tiene activado Web Bluetooth. Usa Chrome o Edge.');
        setIsRunningTest(false);
        return;
      }

      const dev = await navAny.bluetooth.requestDevice({
        filters: [
          { services: [BLUETTI_SERVICE_UUID] },
          { namePrefix: 'BLUETTI' },
          { namePrefix: 'Bluetti' },
          { namePrefix: 'Elite' },
          { namePrefix: 'ELITE' },
          { namePrefix: 'AC' }
        ],
        optionalServices: [BLUETTI_SERVICE_UUID]
      });

      setDevice(dev);
      addLog('success', `Dispositivo seleccionado: ${dev.name || dev.id}`);

      let disconnectedEarly = false;
      let durationSeconds = 0;

      dev.addEventListener('gattserverdisconnected', () => {
        disconnectedEarly = true;
        if (timerRef.current) clearInterval(timerRef.current);
        addLog('error', `[EVENTO GATT DISCONNECTED] Windows cerró la conexión tras ${durationSeconds} segundos.`);
        setSilenceResult('failed');
        setIsRunningTest(false);
      });

      addLog('info', 'Estableciendo enlace GATT con el controlador de Windows...');
      const server = await dev.gatt.connect();
      setGattServer(server);
      addLog('success', 'Enlace GATT conectado exitosamente.');
      addLog('warning', 'GUARDANDO SILENCIO TOTAL: No se enviará ningún comando Modbus durante 15 segundos...');

      timerRef.current = setInterval(() => {
        durationSeconds++;
        setElapsedSeconds(durationSeconds);

        if (durationSeconds >= 15) {
          clearInterval(timerRef.current);
          if (!disconnectedEarly && server.connected) {
            addLog('success', '¡RESULTADO: Conexión 100% estable durante 15 segundos en silencio!');
            addLog('info', 'DIAGNÓSTICO: Tu adaptador Bluetooth y Windows mantienen el enlace sin caídas espontáneas.');
            setSilenceResult('success');
          }
          setIsRunningTest(false);
        }
      }, 1000);

    } catch (err: any) {
      addLog('error', `Fallo al iniciar prueba: ${err.message}`);
      setIsRunningTest(false);
      setSilenceResult('failed');
    }
  };

  // -------------------------------------------------------------
  // TEST 2: SUSCRIPCIÓN A NOTIFICACIONES RX (0000FF01)
  // -------------------------------------------------------------
  const runNotifySubscriptionTest = async () => {
    setIsRunningTest(true);
    setActiveTest('notify');
    addLog('info', 'Iniciando Prueba 2: Suscripción a canal RX (0000FF01)...');

    try {
      let dev = device;
      if (!dev) {
        addLog('info', 'Solicitando dispositivo Bluetti en Windows...');
        const navAny = navigator as any;
        dev = await navAny.bluetooth.requestDevice({
          filters: [
            { services: [BLUETTI_SERVICE_UUID] },
            { namePrefix: 'BLUETTI' },
            { namePrefix: 'Bluetti' },
            { namePrefix: 'Elite' },
            { namePrefix: 'ELITE' },
            { namePrefix: 'AC' }
          ],
          optionalServices: [BLUETTI_SERVICE_UUID]
        });
        setDevice(dev);
        dev.addEventListener('gattserverdisconnected', () => {
          addLog('error', '[EVENTO GATT DISCONNECTED] Windows cerró la conexión BLE.');
          setIsRunningTest(false);
        });
      }

      let server = gattServer;
      if (!server || !server.connected) {
        addLog('info', 'Conectando GATT...');
        server = await dev.gatt.connect();
        setGattServer(server);
      }

      addLog('info', 'Obteniendo Servicio Primario (0000FF00)...');
      const service = await server.getPrimaryService(BLUETTI_SERVICE_UUID);

      addLog('info', 'Obteniendo Canal TX de Escritura (0000FF02)...');
      const charTx = await service.getCharacteristic(BLUETTI_WRITE_UUID);
      setWriteChar(charTx);
      charTxRef.current = charTx;
      addLog('success', 'Canal TX (0000FF02) listo.');

      addLog('info', 'Obteniendo Característica de Notificación (0000FF01)...');
      const charRx = await service.getCharacteristic(BLUETTI_NOTIFY_UUID);
      setNotifyChar(charRx);

      addLog('info', 'Llamando a startNotifications() en Windows...');
      await charRx.startNotifications();

      charRx.addEventListener('characteristicvaluechanged', (e: any) => {
        const val = e.target.value;
        const chunk: number[] = [];
        for (let i = 0; i < val.byteLength; i++) chunk.push(val.getUint8(i));
        const chunkHex = bytesToHex(chunk);
        addLog('info', `[RX FRAGMENTO BLE] (${chunk.length} bytes): ${chunkHex}`);

        // Detección inmediata de desafío de seguridad / Handshake 2A 2A
        if (chunk.length >= 2 && chunk[0] === 0x2A && chunk[1] === 0x2A) {
          addLog('warning', `[HANDSHAKE 2A 2A DETECTADO]: La estación envió desafío de enlace BLE (0x2A 0x2A).`);
          
          // Responder de inmediato por TX para evitar timeout de desconexión de 5 segundos
          if (charTxRef.current) {
            // Eco del handshake o ACK de conexión
            const ackPacket = new Uint8Array(chunk);
            charTxRef.current.writeValueWithoutResponse(ackPacket).then(() => {
              addLog('success', `[TX HANDSHAKE ACK ENVIADO]: Se envió respuesta de eco (${chunkHex}) a la estación.`);
            }).catch((err: any) => {
              charTxRef.current.writeValue(ackPacket).catch(() => {});
            });
          }
        }

        rxBufferRef.current.push(...chunk);

        // Procesar paquetes Modbus completos
        while (rxBufferRef.current.length >= 5) {
          if (rxBufferRef.current[0] !== 0x01) {
            rxBufferRef.current.shift();
            continue;
          }

          const funcCode = rxBufferRef.current[1];
          if (funcCode === 0x03) {
            if (rxBufferRef.current.length < 3) break;
            const byteCount = rxBufferRef.current[2];
            const totalLen = 3 + byteCount + 2;
            if (rxBufferRef.current.length < totalLen) break;

            const packet = rxBufferRef.current.splice(0, totalLen);
            const hex = bytesToHex(packet);
            setModbusResponse(hex);
            const crcOk = verifyCrc16(packet);
            addLog(crcOk ? 'success' : 'error', `[RX PAQUETE MODBUS COMPLETO] (${packet.length} bytes): ${hex} | CRC16: ${crcOk ? 'VÁLIDO' : 'FALLIDO'}`);

            if (crcOk) {
              const defaultTelemetry: BluettiTelemetry = {
                batteryVoltage: 0,
                batteryCurrent: 0,
                batteryTemp: 25,
                soc: 0,
                dcInputWatts: 0,
                dcInputVoltage: 38.4,
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
                ecoModeOn: true,
                powerLiftingOn: false,
                gridEnhancementOn: false,
                chargingMode: 'standard' as const,
                totalEnergyCapacityWh: 1024,
                estimatedRuntimeMinutes: null,
                estimatedTimeToFullMinutes: null,
                deviceName: 'Bluetti Elite 100 V2'
              };
              const { telemetry } = parseBluettiResponse(packet, defaultTelemetry);
              const preview = `Batería: ${telemetry.soc}% | Solar: ${telemetry.dcInputWatts}W | Red: ${telemetry.acInputWatts}W | AC Out: ${telemetry.acOutputWatts}W | DC Out: ${telemetry.dcOutputWatts}W`;
              setDecodedPreview(preview);
              addLog('success', `===> ¡DATOS DECODIFICADOS!: ${preview}`);
            }
          } else {
            rxBufferRef.current.shift();
          }
        }
      });

      addLog('success', '¡Suscripción a notificaciones RX habilitada exitosamente en Windows!');
    } catch (err: any) {
      addLog('error', `Error en suscripción de notificaciones: ${err.message}`);
      addLog('warning', 'Si el error es de seguridad o acceso denegado, Windows exige emparejar el dispositivo primero en Configuración de Windows.');
    } finally {
      setIsRunningTest(false);
    }
  };

  // -------------------------------------------------------------
  // TEST 3: ENVÍO DE CONSULTA MODBUS CONTROLADA DIRECTA
  // -------------------------------------------------------------
  const sendSingleQuery = async (label: string, startReg: number, lengthWords: number) => {
    setIsRunningTest(true);
    setActiveTest(`query_${startReg}`);
    setModbusResponse(null);

    try {
      let targetWriteChar = writeChar;

      // Si no está conectado o el canal TX no existe, conectar y configurar todo de forma atómica
      if (!targetWriteChar || !gattServer?.connected) {
        addLog('info', `Preparando enlace directo para ${label}...`);
        
        const navAny = navigator as any;
        let dev = device;
        if (!dev) {
          addLog('info', 'Abriendo selector Bluetooth...');
          dev = await navAny.bluetooth.requestDevice({
            filters: [
              { services: [BLUETTI_SERVICE_UUID] },
              { namePrefix: 'BLUETTI' },
              { namePrefix: 'Bluetti' },
              { namePrefix: 'Elite' },
              { namePrefix: 'ELITE' },
              { namePrefix: 'AC' }
            ],
            optionalServices: [BLUETTI_SERVICE_UUID]
          });
          setDevice(dev);
        }

        let server = gattServer;
        if (!server || !server.connected) {
          server = await dev.gatt.connect();
          setGattServer(server);
        }

        const service = await server.getPrimaryService(BLUETTI_SERVICE_UUID);
        targetWriteChar = await service.getCharacteristic(BLUETTI_WRITE_UUID);
        setWriteChar(targetWriteChar);
        charTxRef.current = targetWriteChar;

        const charRx = await service.getCharacteristic(BLUETTI_NOTIFY_UUID);
        setNotifyChar(charRx);
        await charRx.startNotifications();

        charRx.addEventListener('characteristicvaluechanged', (e: any) => {
          const val = e.target.value;
          const chunk: number[] = [];
          for (let i = 0; i < val.byteLength; i++) chunk.push(val.getUint8(i));
          const chunkHex = bytesToHex(chunk);
          addLog('info', `[RX RESPUESTA BLE] (${chunk.length} bytes): ${chunkHex}`);

          rxBufferRef.current.push(...chunk);

          // Procesar tramas Modbus
          while (rxBufferRef.current.length >= 5) {
            if (rxBufferRef.current[0] !== 0x01) {
              rxBufferRef.current.shift();
              continue;
            }
            const funcCode = rxBufferRef.current[1];
            if (funcCode === 0x03) {
              if (rxBufferRef.current.length < 3) break;
              const byteCount = rxBufferRef.current[2];
              const totalLen = 3 + byteCount + 2;
              if (rxBufferRef.current.length < totalLen) break;

              const packet = rxBufferRef.current.splice(0, totalLen);
              const hex = bytesToHex(packet);
              setModbusResponse(hex);
              const crcOk = verifyCrc16(packet);
              addLog(crcOk ? 'success' : 'error', `[RX PAQUETE COMPLETO] (${packet.length} bytes): ${hex} | CRC16: ${crcOk ? 'VÁLIDO' : 'FALLIDO'}`);

              if (crcOk) {
                const defaultTelemetry: BluettiTelemetry = {
                  batteryVoltage: 0,
                  batteryCurrent: 0,
                  batteryTemp: 25,
                  soc: 0,
                  dcInputWatts: 0,
                  dcInputVoltage: 38.4,
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
                  ecoModeOn: true,
                  powerLiftingOn: false,
                  gridEnhancementOn: false,
                  chargingMode: 'standard' as const,
                  totalEnergyCapacityWh: 1024,
                  estimatedRuntimeMinutes: null,
                  estimatedTimeToFullMinutes: null,
                  deviceName: 'Bluetti Elite 100 V2'
                };
                const { telemetry } = parseBluettiResponse(packet, defaultTelemetry);
                const preview = `Batería: ${telemetry.soc}% | Solar: ${telemetry.dcInputWatts}W | Red: ${telemetry.acInputWatts}W | AC Out: ${telemetry.acOutputWatts}W | DC Out: ${telemetry.dcOutputWatts}W`;
                setDecodedPreview(preview);
                addLog('success', `===> ¡DATOS DECODIFICADOS!: ${preview}`);
              }
            } else {
              rxBufferRef.current.shift();
            }
          }
        });
      }

      const packet = buildReadHoldingRegistersPacket(startReg, lengthWords);
      const hex = bytesToHex(packet);
      addLog('info', `[TX ENVIANDO ${label}]: Reg 0x${startReg.toString(16).toUpperCase()} | Cantidad: ${lengthWords} words`);
      addLog('info', `Trama binaria TX: ${hex}`);

      const t0 = performance.now();
      const uint8 = new Uint8Array(packet);
      if (writeMode === 'withoutResponse' && typeof targetWriteChar.writeValueWithoutResponse === 'function') {
        await targetWriteChar.writeValueWithoutResponse(uint8);
        addLog('info', 'Enviado con writeValueWithoutResponse.');
      } else {
        await targetWriteChar.writeValue(uint8);
        addLog('info', 'Enviado con writeValue.');
      }

      const elapsed = Math.round(performance.now() - t0);
      addLog('success', `Comando emitido en ${elapsed} ms. Esperando respuesta de la estación...`);

    } catch (err: any) {
      addLog('error', `Error en consulta: ${err.message}`);
    } finally {
      setIsRunningTest(false);
    }
  };

  const copyLogs = () => {
    const text = logs.map(l => `[${l.time}] [${l.type.toUpperCase()}] ${l.text}`).join('\n');
    navigator.clipboard.writeText(text);
    alert('Logs copiados al portapapeles');
  };

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 bg-black/80 backdrop-blur-md flex items-center justify-center p-4">
      <div className="bg-slate-900 border border-slate-800 rounded-3xl w-full max-w-4xl max-h-[92vh] flex flex-col shadow-2xl overflow-hidden animate-in fade-in zoom-in-95 duration-200">
        
        {/* Modal Header */}
        <div className="p-5 border-b border-slate-800 flex items-center justify-between bg-slate-950/60">
          <div className="flex items-center space-x-3">
            <div className="w-10 h-10 rounded-2xl bg-cyan-500/20 border border-cyan-400/40 flex items-center justify-center text-cyan-400">
              <Activity className="w-5 h-5" />
            </div>
            <div>
              <h3 className="text-base font-bold text-white flex items-center space-x-2">
                <span>Probador y Diagnóstico Técnico de Conexión BLE</span>
                <span className="px-2 py-0.5 rounded bg-cyan-500/20 text-cyan-300 text-[10px] font-mono font-bold">Paso a Paso</span>
              </h3>
              <p className="text-xs text-slate-400 mt-0.5">
                Aísla el problema para comprobar si la causa está en Windows o en el paquete Modbus de la estación.
              </p>
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-2 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 hover:text-white transition-all cursor-pointer"
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Modal Body */}
        <div className="flex-1 overflow-y-auto p-6 space-y-6">
          
          {/* AVISO IMPORTANTE: NO CERRAR LA VENTANA PARA COMPLETAR LAS PRUEBAS */}
          <div className="p-4 rounded-2xl bg-cyan-950/40 border border-cyan-500/30 flex items-start gap-3">
            <Info className="w-5 h-5 text-cyan-400 shrink-0 mt-0.5" />
            <div className="text-xs text-slate-200 leading-relaxed">
              <strong className="text-cyan-300 font-bold block mb-1">
                IMPORTANTE: Todas las pruebas (1, 2 y 3) se ejecutan dentro de esta misma ventana sin cerrarla
              </strong>
              <span>
                Para ejecutar la <strong>Prueba 2</strong> y la <strong>Prueba 3</strong>, no cierres esta ventana. Haz clic directamente en los botones de abajo. Al hacer clic en <em>&quot;Ejecutar Prueba 2&quot;</em> se activará el canal RX, y luego en la <em>&quot;Prueba 3&quot;</em> pulsa los botones de rangos (por ejemplo, <strong>Rango A</strong> o <strong>Rango B</strong>) para ver los datos decodificados en vivo.
              </span>
            </div>
          </div>

          {/* Banner si hay datos decodificados en tiempo real */}
          {decodedPreview && (
            <div className="p-4 rounded-2xl bg-emerald-950/40 border border-emerald-500/40 flex items-center justify-between gap-4 animate-in fade-in">
              <div className="flex items-center gap-3">
                <CheckCircle2 className="w-6 h-6 text-emerald-400 shrink-0" />
                <div>
                  <span className="text-xs font-bold text-emerald-300 uppercase tracking-wider block font-mono">
                    ¡Telemetría Oficial Recibida y Decodificada con Éxito!
                  </span>
                  <span className="text-sm font-bold text-white font-mono">{decodedPreview}</span>
                </div>
              </div>
              <span className="px-3 py-1 rounded-full bg-emerald-500/20 text-emerald-300 font-mono text-xs font-bold">
                CRC16 OK
              </span>
            </div>
          )}

          {/* Card: Explicación y Veredicto */}
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            
            {/* PRUEBA 1: KEEPALIVE EN SILENCIO */}
            <div className="bg-slate-950/70 border border-slate-800 rounded-2xl p-4 flex flex-col justify-between">
              <div>
                <div className="flex items-center justify-between mb-2">
                  <span className="text-xs font-mono font-bold text-cyan-400 uppercase tracking-wider flex items-center gap-1.5">
                    <VolumeX className="w-4 h-4" /> Prueba 1: Resistencia en Silencio
                  </span>
                  {silenceResult === 'success' && (
                    <span className="px-2 py-0.5 rounded-full bg-emerald-500/20 text-emerald-400 text-[10px] font-bold">15s Estable</span>
                  )}
                  {silenceResult === 'failed' && (
                    <span className="px-2 py-0.5 rounded-full bg-rose-500/20 text-rose-400 text-[10px] font-bold">Se Desconectó</span>
                  )}
                </div>
                <p className="text-xs text-slate-300 leading-relaxed">
                  Conecta al GATT y <strong>no envía ningún comando</strong>. Si se desconecta a los 2-3s sin haber enviado nada, se demuestra que <strong>el origen es Windows</strong> (ahorro de energía del adaptador o falta de emparejamiento).
                </p>
              </div>

              <div className="mt-4 pt-3 border-t border-slate-800/80 flex items-center justify-between">
                <span className="text-xs font-mono text-slate-400">
                  Tiempo: <strong className="text-cyan-400">{elapsedSeconds}s</strong> / 15s
                </span>
                <button
                  onClick={runSilenceTest}
                  disabled={isRunningTest}
                  className="px-3 py-1.5 bg-cyan-600 hover:bg-cyan-500 disabled:opacity-50 text-white rounded-xl text-xs font-bold transition-all flex items-center gap-1.5 cursor-pointer shadow-md shadow-cyan-600/20"
                >
                  <Play className="w-3.5 h-3.5" />
                  <span>Ejecutar Prueba 1</span>
                </button>
              </div>
            </div>

            {/* PRUEBA 2: SUSCRIPCIÓN RX */}
            <div className="bg-slate-950/70 border border-slate-800 rounded-2xl p-4 flex flex-col justify-between">
              <div>
                <div className="flex items-center justify-between mb-2">
                  <span className="text-xs font-mono font-bold text-indigo-400 uppercase tracking-wider flex items-center gap-1.5">
                    <Wifi className="w-4 h-4" /> Prueba 2: Suscribir a RX (0000FF01)
                  </span>
                  {notifyChar && (
                    <span className="px-2 py-0.5 rounded-full bg-emerald-500/20 text-emerald-400 text-[10px] font-bold">Suscrito OK</span>
                  )}
                </div>
                <p className="text-xs text-slate-300 leading-relaxed">
                  Activa la escucha de notificaciones con <code>startNotifications()</code>. Comprueba si el controlador Bluetooth de Windows o el descriptor CCCD aceptan la suscripción.
                </p>
              </div>

              <div className="mt-4 pt-3 border-t border-slate-800/80 flex items-center justify-between">
                <span className="text-xs font-mono text-slate-400">
                  Canal TX: <strong className={writeChar ? 'text-emerald-400' : 'text-slate-500'}>{writeChar ? 'Listo' : 'Pendiente'}</strong>
                </span>
                <button
                  onClick={runNotifySubscriptionTest}
                  disabled={isRunningTest}
                  className="px-3 py-1.5 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white rounded-xl text-xs font-bold transition-all flex items-center gap-1.5 cursor-pointer shadow-md shadow-indigo-600/20"
                >
                  <Play className="w-3.5 h-3.5" />
                  <span>Ejecutar Prueba 2</span>
                </button>
              </div>
            </div>

          </div>

          {/* PRUEBA 3: MATRIZ DE RANGOS MODBUS */}
          <div className="bg-slate-950/70 border border-slate-800 rounded-2xl p-4">
            <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3 mb-3">
              <div>
                <h4 className="text-xs font-bold text-white uppercase tracking-wider font-mono flex items-center gap-1.5">
                  <Sliders className="w-4 h-4 text-amber-400" /> Prueba 3: Matriz de Rangos Modbus (Enviar 1 a 1)
                </h4>
                <p className="text-xs text-slate-400 mt-0.5">
                  Prueba qué rango de registros acepta la estación sin que su microcontrolador cierre el enlace:
                </p>
              </div>

              {/* Selector de Modo de Escritura */}
              <div className="flex items-center space-x-2 bg-slate-900 p-1 rounded-xl border border-slate-800 text-xs">
                <span className="text-[10px] text-slate-400 uppercase font-mono px-1">Modo:</span>
                <button
                  onClick={() => setWriteMode('withoutResponse')}
                  className={`px-2 py-1 rounded text-[11px] font-semibold transition-all ${
                    writeMode === 'withoutResponse' ? 'bg-cyan-600 text-white' : 'text-slate-400 hover:text-white'
                  }`}
                >
                  WithoutResponse
                </button>
                <button
                  onClick={() => setWriteMode('withResponse')}
                  className={`px-2 py-1 rounded text-[11px] font-semibold transition-all ${
                    writeMode === 'withResponse' ? 'bg-indigo-600 text-white' : 'text-slate-400 hover:text-white'
                  }`}
                >
                  WithResponse (Confirmado)
                </button>
              </div>
            </div>

            <div className="grid grid-cols-1 sm:grid-cols-3 gap-2.5">
              <button
                onClick={() => sendSingleQuery('Rango A (Oficial 0x000A)', 0x000A, 30)}
                disabled={isRunningTest}
                className="p-3 bg-slate-900 hover:bg-slate-800/90 disabled:opacity-50 border border-slate-800 hover:border-cyan-500/40 rounded-xl text-left transition-all cursor-pointer group"
              >
                <div className="flex items-center justify-between">
                  <span className="text-xs font-bold text-white group-hover:text-cyan-400">Rango A (Oficial)</span>
                  <span className="text-[10px] font-mono text-cyan-400 bg-cyan-500/10 px-1.5 py-0.5 rounded">0x000A</span>
                </div>
                <p className="text-[11px] text-slate-400 mt-1">Offset 10, 30 palabras (EB3A / AC180 / Elite V2)</p>
              </button>

              <button
                onClick={() => sendSingleQuery('Rango B (Directo Potencias)', 0x0024, 8)}
                disabled={isRunningTest}
                className="p-3 bg-slate-900 hover:bg-slate-800/90 disabled:opacity-50 border border-slate-800 hover:border-amber-500/40 rounded-xl text-left transition-all cursor-pointer group"
              >
                <div className="flex items-center justify-between">
                  <span className="text-xs font-bold text-white group-hover:text-amber-400">Rango B (Directo)</span>
                  <span className="text-[10px] font-mono text-amber-400 bg-amber-500/10 px-1.5 py-0.5 rounded">0x0024</span>
                </div>
                <p className="text-[11px] text-slate-400 mt-1">Offset 36, 8 palabras (Potencias In/Out y SoC)</p>
              </button>

              <button
                onClick={() => sendSingleQuery('Rango C (Clásico 0x0010)', 0x0010, 28)}
                disabled={isRunningTest}
                className="p-3 bg-slate-900 hover:bg-slate-800/90 disabled:opacity-50 border border-slate-800 hover:border-purple-500/40 rounded-xl text-left transition-all cursor-pointer group"
              >
                <div className="flex items-center justify-between">
                  <span className="text-xs font-bold text-white group-hover:text-purple-400">Rango C (Clásico)</span>
                  <span className="text-[10px] font-mono text-purple-400 bg-purple-500/10 px-1.5 py-0.5 rounded">0x0010</span>
                </div>
                <p className="text-[11px] text-slate-400 mt-1">Offset 16, 28 palabras (Rango previo)</p>
              </button>
            </div>
          </div>

          {/* TERMINAL DE LOGS EN VIVO */}
          <div className="bg-slate-950 border border-slate-800 rounded-2xl p-4 flex flex-col">
            <div className="flex items-center justify-between mb-2">
              <span className="text-xs font-mono font-bold text-slate-300 uppercase tracking-wider flex items-center gap-1.5">
                <Terminal className="w-4 h-4 text-cyan-400" /> Registro de Eventos en Tiempo Real
              </span>
              <button
                onClick={copyLogs}
                className="text-xs text-slate-400 hover:text-white flex items-center gap-1 px-2 py-1 rounded bg-slate-900 border border-slate-800 hover:bg-slate-800 transition-all cursor-pointer"
              >
                <Copy className="w-3 h-3" /> Copiar Logs
              </button>
            </div>

            <div className="h-48 overflow-y-auto font-mono text-xs p-3 bg-slate-950 rounded-xl border border-slate-900 space-y-1 select-text">
              {logs.length === 0 ? (
                <div className="text-slate-600 italic">Haz clic en &quot;Ejecutar Prueba 1&quot; para iniciar el diagnóstico técnico...</div>
              ) : (
                logs.map((log, idx) => (
                  <div key={idx} className="flex items-start space-x-2">
                    <span className="text-slate-500 text-[10px] shrink-0 font-mono">[{log.time}]</span>
                    <span
                      className={`break-all ${
                        log.type === 'success'
                          ? 'text-emerald-400 font-semibold'
                          : log.type === 'error'
                          ? 'text-rose-400 font-bold'
                          : log.type === 'warning'
                          ? 'text-amber-300'
                          : 'text-slate-300'
                      }`}
                    >
                      {log.text}
                    </span>
                  </div>
                ))
              )}
              <div ref={logEndRef} />
            </div>
          </div>

          {/* GUÍA DE SOLUCIÓN DE WINDOWS */}
          <div className="bg-blue-950/30 border border-blue-500/30 rounded-2xl p-4 text-xs">
            <h4 className="font-bold text-blue-300 flex items-center gap-1.5 mb-2">
              <Info className="w-4 h-4 text-blue-400" /> Pasos Verificados para Evitar Desconexiones en Windows 10 / 11:
            </h4>
            <ol className="list-decimal list-inside space-y-1.5 text-slate-300">
              <li>
                <strong>Desactivar Ahorro de Energía de Windows:</strong> Abre el <em>Administrador de Dispositivos &gt; Bluetooth &gt; Tu Adaptador Bluetooth &gt; Propiedades &gt; Administración de energía</em> y desmarca: <span className="text-amber-300">&quot;Permitir que el equipo apague este dispositivo para ahorrar energía&quot;</span>.
              </li>
              <li>
                <strong>Emparejar en Configuración de Windows:</strong> Ve a <em>Configuración &gt; Bluetooth y dispositivos &gt; Agregar dispositivo</em> y vincula la Bluetti antes de conectar desde la app web o Python.
              </li>
              <li>
                <strong>Ejecutar el script de diagnóstico en consola:</strong> Abre una terminal y ejecuta <code>python windows_hud\diagnostico_bluetooth.py</code> para ver el informe detallado con `await client.pair()`.
              </li>
            </ol>
          </div>

        </div>

        {/* Modal Footer */}
        <div className="p-4 border-t border-slate-800 bg-slate-950/60 flex items-center justify-between">
          <span className="text-xs text-slate-400">
            {device ? `Dispositivo: ${device.name || 'Conectado'}` : 'Sin dispositivo activo'}
          </span>
          <button
            onClick={onClose}
            className="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-white rounded-xl text-xs font-bold transition-all cursor-pointer"
          >
            Cerrar Diagnóstico
          </button>
        </div>

      </div>
    </div>
  );
};
