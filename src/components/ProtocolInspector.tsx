import React, { useState } from 'react';
import {
  ArrowDownLeft,
  ArrowUpRight,
  BookOpen,
  Calculator,
  CheckCircle2,
  Copy,
  Filter,
  Play,
  Radio,
  Search,
  Terminal,
  Trash2,
  XCircle
} from 'lucide-react';
import { PacketLog } from '../types/bluetti';
import { appendCrc16, bytesToHex, calculateCrc16, getCrc16Bytes, hexToBytes, verifyCrc16 } from '../services/crc16';
import { REGISTER_DOCS } from '../services/bluettiProtocol';

interface ProtocolInspectorProps {
  packets: PacketLog[];
  onClearPackets: () => void;
  onSendRawPacket?: (packet: number[]) => void;
}

export const ProtocolInspector: React.FC<ProtocolInspectorProps> = ({
  packets,
  onClearPackets
}) => {
  const [selectedPacket, setSelectedPacket] = useState<PacketLog | null>(null);
  const [calculatorInput, setCalculatorInput] = useState('01 03 00 10 00 1C');
  const [calcResult, setCalcResult] = useState<{
    crc: number;
    lowByte: number;
    highByte: number;
    fullHexWithCrc: string;
    isValid: boolean;
  } | null>(null);

  // Compute live CRC for calculator input
  React.useEffect(() => {
    try {
      const bytes = hexToBytes(calculatorInput);
      if (bytes.length > 0) {
        const crc = calculateCrc16(bytes);
        const [low, high] = getCrc16Bytes(bytes);
        const fullBytes = appendCrc16(bytes);
        setCalcResult({
          crc,
          lowByte: low,
          highByte: high,
          fullHexWithCrc: bytesToHex(fullBytes),
          isValid: true
        });
      } else {
        setCalcResult(null);
      }
    } catch {
      setCalcResult(null);
    }
  }, [calculatorInput]);

  return (
    <div className="space-y-6">
      
      {/* Header Info */}
      <div className="bg-slate-900/80 border border-slate-800 rounded-3xl p-6 backdrop-blur-md">
        <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
          <div>
            <div className="flex items-center space-x-2 text-cyan-400 font-mono text-xs uppercase tracking-wider mb-1">
              <Radio className="w-4 h-4" />
              <span>GATT UUID: 0000ff01 (Notify) / 0000ff02 (Write)</span>
            </div>
            <h2 className="text-xl font-bold text-white">Inspector de Protocolo Modbus RTU</h2>
            <p className="text-xs text-slate-400 mt-1 max-w-2xl">
              Bluetti encapsula tramas industriales Modbus RTU en paquetes Bluetooth Low Energy. Aquí puedes inspeccionar el flujo de bytes, verificar la suma de comprobación CRC16 y consultar el mapa de registros de memoria.
            </p>
          </div>
          <button
            onClick={onClearPackets}
            className="flex items-center space-x-2 px-3 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 border border-slate-700 text-xs font-medium self-start md:self-auto transition-colors"
          >
            <Trash2 className="w-3.5 h-3.5" />
            <span>Limpiar Registro</span>
          </button>
        </div>
      </div>

      {/* Main 2-Column Split: Packet Stream & Detail / Calculator */}
      <div className="grid grid-cols-1 lg:grid-cols-12 gap-6">
        
        {/* Left: Live Packet Stream (7 cols) */}
        <div className="lg:col-span-7 bg-slate-900/80 border border-slate-800 rounded-3xl p-5 backdrop-blur-md flex flex-col h-[580px]">
          <div className="flex items-center justify-between pb-3 border-b border-slate-800">
            <div className="flex items-center space-x-2">
              <Terminal className="w-4 h-4 text-cyan-400" />
              <h3 className="text-sm font-bold text-slate-200">Flujo de Tramas en Vivo ({packets.length})</h3>
            </div>
            <span className="text-[11px] font-mono text-slate-500">
              TX: Write (ff02) • RX: Notify (ff01)
            </span>
          </div>

          {/* Packet List */}
          <div className="flex-1 overflow-y-auto mt-3 space-y-2 pr-1 font-mono text-xs">
            {packets.length === 0 ? (
              <div className="h-full flex items-center justify-center text-slate-500 text-center">
                <p>Esperando intercambio de paquetes Modbus...</p>
              </div>
            ) : (
              packets.map((pkt) => {
                const isSelected = selectedPacket?.id === pkt.id;
                const isTx = pkt.direction === 'TX';

                return (
                  <div
                    key={pkt.id}
                    onClick={() => setSelectedPacket(pkt)}
                    className={`p-3 rounded-2xl border cursor-pointer transition-all ${
                      isSelected
                        ? 'bg-blue-950/60 border-blue-500 shadow-md shadow-blue-500/10'
                        : 'bg-slate-950/60 border-slate-800/80 hover:border-slate-700'
                    }`}
                  >
                    <div className="flex items-center justify-between mb-1.5">
                      <div className="flex items-center space-x-2">
                        <span className={`px-2 py-0.5 rounded text-[10px] font-bold flex items-center gap-1 ${
                          isTx
                            ? 'bg-cyan-500/10 text-cyan-400 border border-cyan-500/30'
                            : 'bg-emerald-500/10 text-emerald-400 border border-emerald-500/30'
                        }`}>
                          {isTx ? <ArrowUpRight className="w-3 h-3" /> : <ArrowDownLeft className="w-3 h-3" />}
                          {pkt.direction}
                        </span>
                        <span className="font-semibold text-slate-300">{pkt.commandType}</span>
                      </div>
                      <span className="text-slate-500 text-[10px]">{pkt.timestamp}</span>
                    </div>

                    {/* Raw Hex Line */}
                    <div className="bg-slate-950 px-2.5 py-1.5 rounded-lg border border-slate-800/80 text-[11px] text-slate-300 overflow-x-auto whitespace-nowrap">
                      {pkt.rawHex}
                    </div>

                    <div className="mt-1.5 flex items-center justify-between text-[11px] text-slate-400">
                      <span className="truncate max-w-[260px]">{pkt.description}</span>
                      <span className={`flex items-center gap-1 text-[10px] font-bold ${
                        pkt.crcValid ? 'text-emerald-400' : 'text-rose-400'
                      }`}>
                        {pkt.crcValid ? <CheckCircle2 className="w-3 h-3" /> : <XCircle className="w-3 h-3" />}
                        CRC16 OK
                      </span>
                    </div>
                  </div>
                );
              })
            )}
          </div>
        </div>

        {/* Right: Inspector & CRC16 Tool (5 cols) */}
        <div className="lg:col-span-5 space-y-6">
          
          {/* Packet Byte Breakdown */}
          <div className="bg-slate-900/80 border border-slate-800 rounded-3xl p-5 backdrop-blur-md">
            <h3 className="text-sm font-bold text-slate-200 mb-3 flex items-center gap-2">
              <Search className="w-4 h-4 text-cyan-400" />
              <span>Desglose de Bytes del Paquete</span>
            </h3>

            {selectedPacket ? (
              <div className="space-y-3 text-xs">
                <div className="p-3 bg-slate-950 rounded-2xl border border-slate-800 space-y-2">
                  <div className="flex justify-between">
                    <span className="text-slate-400">Dirección:</span>
                    <span className="font-mono font-bold text-white">{selectedPacket.direction} ({selectedPacket.direction === 'TX' ? 'PC -> Bluetti' : 'Bluetti -> PC'})</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-400">Tipo:</span>
                    <span className="font-mono text-cyan-400">{selectedPacket.commandType}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-400">Longitud total:</span>
                    <span className="font-mono text-slate-300">{selectedPacket.bytes.length} bytes</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-400">Suma CRC16:</span>
                    <span className="font-mono text-emerald-400 font-bold">{selectedPacket.crcExpected || 'Válida'}</span>
                  </div>
                </div>

                {/* Byte-by-byte table */}
                <div className="overflow-x-auto">
                  <table className="w-full text-left font-mono text-[11px]">
                    <thead>
                      <tr className="text-slate-500 border-b border-slate-800">
                        <th className="pb-1.5">Índice</th>
                        <th className="pb-1.5">Hex</th>
                        <th className="pb-1.5">Significado Modbus</th>
                      </tr>
                    </thead>
                    <tbody className="divide-y divide-slate-800/60 text-slate-300">
                      <tr>
                        <td className="py-1 text-slate-500">[0]</td>
                        <td className="py-1 text-cyan-400 font-bold">0x{selectedPacket.bytes[0]?.toString(16).padStart(2, '0').toUpperCase()}</td>
                        <td className="py-1 text-slate-400">Slave Address (0x01)</td>
                      </tr>
                      <tr>
                        <td className="py-1 text-slate-500">[1]</td>
                        <td className="py-1 text-purple-400 font-bold">0x{selectedPacket.bytes[1]?.toString(16).padStart(2, '0').toUpperCase()}</td>
                        <td className="py-1 text-slate-400">
                          {selectedPacket.bytes[1] === 0x03 ? '0x03 (Read Holding Regs)' : selectedPacket.bytes[1] === 0x06 ? '0x06 (Write Single Reg)' : 'Código de Función'}
                        </td>
                      </tr>
                      {selectedPacket.bytes.length >= 4 && (
                        <tr>
                          <td className="py-1 text-slate-500">[2..{selectedPacket.bytes.length - 3}]</td>
                          <td className="py-1 text-amber-400">Datos / Carga</td>
                          <td className="py-1 text-slate-400">{selectedPacket.bytes.length - 4} bytes de payload</td>
                        </tr>
                      )}
                      {selectedPacket.bytes.length >= 2 && (
                        <tr>
                          <td className="py-1 text-slate-500">[{selectedPacket.bytes.length - 2}..{selectedPacket.bytes.length - 1}]</td>
                          <td className="py-1 text-emerald-400 font-bold">
                            {selectedPacket.bytes.slice(-2).map(b => b.toString(16).padStart(2, '0').toUpperCase()).join(' ')}
                          </td>
                          <td className="py-1 text-slate-400">CRC16 (Low, High)</td>
                        </tr>
                      )}
                    </tbody>
                  </table>
                </div>
              </div>
            ) : (
              <p className="text-xs text-slate-500 py-6 text-center">
                Haz clic en cualquier trama del listado para inspeccionar su anatomía binaria.
              </p>
            )}
          </div>

          {/* Interactive CRC16 Modbus Calculator */}
          <div className="bg-slate-900/80 border border-slate-800 rounded-3xl p-5 backdrop-blur-md">
            <div className="flex items-center space-x-2 mb-3">
              <Calculator className="w-4 h-4 text-emerald-400" />
              <h3 className="text-sm font-bold text-slate-200">Calculadora CRC16 Modbus RTU</h3>
            </div>
            <p className="text-xs text-slate-400 mb-3">
              Polinomio: <code className="text-cyan-400 font-mono">0xA001</code>. Genera los 2 bytes de control necesarios para que la Bluetti acepte tus comandos sin rechazarlos.
            </p>

            <div className="space-y-3">
              <div>
                <label className="text-[11px] font-semibold text-slate-400 block mb-1">
                  Bytes de entrada (Hex sin CRC):
                </label>
                <input
                  type="text"
                  value={calculatorInput}
                  onChange={(e) => setCalculatorInput(e.target.value)}
                  className="w-full px-3 py-2 bg-slate-950 border border-slate-800 rounded-xl font-mono text-xs text-cyan-300 focus:outline-none focus:border-cyan-500"
                  placeholder="ej. 01 03 00 10 00 1C"
                />
              </div>

              {calcResult && (
                <div className="p-3 bg-slate-950 rounded-2xl border border-emerald-500/30 text-xs font-mono space-y-2">
                  <div className="flex justify-between">
                    <span className="text-slate-400">Suma CRC16:</span>
                    <span className="text-emerald-400 font-bold">0x{calcResult.crc.toString(16).toUpperCase()}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-400">Low Byte (1º):</span>
                    <span className="text-emerald-400 font-bold">0x{calcResult.lowByte.toString(16).padStart(2, '0').toUpperCase()}</span>
                  </div>
                  <div className="flex justify-between">
                    <span className="text-slate-400">High Byte (2º):</span>
                    <span className="text-emerald-400 font-bold">0x{calcResult.highByte.toString(16).padStart(2, '0').toUpperCase()}</span>
                  </div>
                  <div className="pt-2 border-t border-slate-800">
                    <span className="text-slate-400 block mb-1">Trama Completa con CRC:</span>
                    <div className="p-2 bg-slate-900 rounded-lg text-emerald-300 text-[11px] break-all select-all">
                      {calcResult.fullHexWithCrc}
                    </div>
                  </div>
                </div>
              )}

              {/* Sample presets */}
              <div className="flex flex-wrap gap-1.5 pt-1">
                <button
                  onClick={() => setCalculatorInput('01 03 00 10 00 1C')}
                  className="px-2 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 text-[10px] font-mono"
                >
                  Leer Telemetría (28 words)
                </button>
                <button
                  onClick={() => setCalculatorInput('01 06 0B 00 00 01')}
                  className="px-2 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 text-[10px] font-mono"
                >
                  Encender AC (0x0B00)
                </button>
                <button
                  onClick={() => setCalculatorInput('01 06 0B 00 00 00')}
                  className="px-2 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 text-[10px] font-mono"
                >
                  Apagar AC (0x0B00)
                </button>
                <button
                  onClick={() => setCalculatorInput('01 06 0B 01 00 01')}
                  className="px-2 py-1 rounded bg-slate-800 hover:bg-slate-700 text-slate-300 text-[10px] font-mono"
                >
                  Encender DC (0x0B01)
                </button>
              </div>
            </div>
          </div>

        </div>

      </div>

      {/* Bluetti Register Map Table */}
      <div className="bg-slate-900/80 border border-slate-800 rounded-3xl p-6 backdrop-blur-md">
        <div className="flex items-center space-x-2 mb-4">
          <BookOpen className="w-5 h-5 text-cyan-400" />
          <h3 className="text-base font-bold text-white">Mapa de Registros Modbus Oficial (Bluetti Elite 100 V2)</h3>
        </div>

        <div className="overflow-x-auto">
          <table className="w-full text-left text-xs font-mono">
            <thead>
              <tr className="border-b border-slate-800 text-slate-400">
                <th className="py-2.5 px-3">Dirección Hex</th>
                <th className="py-2.5 px-3">Decimal</th>
                <th className="py-2.5 px-3">Nombre</th>
                <th className="py-2.5 px-3">Tipo</th>
                <th className="py-2.5 px-3">Unidad / Escala</th>
                <th className="py-2.5 px-3">Descripción</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800/60 text-slate-300">
              {REGISTER_DOCS.map((reg) => (
                <tr key={reg.addressHex} className="hover:bg-slate-800/30">
                  <td className="py-2 px-3 text-cyan-400 font-bold">{reg.addressHex}</td>
                  <td className="py-2 px-3 text-slate-400">{reg.addressDec}</td>
                  <td className="py-2 px-3 font-sans font-semibold text-white">{reg.name}</td>
                  <td className="py-2 px-3 text-purple-400">{reg.type}</td>
                  <td className="py-2 px-3 text-amber-400">{reg.unit ? `${reg.scale ? `x${reg.scale} ` : ''}${reg.unit}` : '-'}</td>
                  <td className="py-2 px-3 font-sans text-slate-400">{reg.description}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

    </div>
  );
};
