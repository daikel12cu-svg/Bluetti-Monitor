import React from 'react';
import {
  Activity,
  AlertTriangle,
  ArrowDown,
  ArrowRight,
  ArrowUp,
  Battery,
  BatteryCharging,
  Clock,
  Cpu,
  Flame,
  Gauge,
  Info,
  Leaf,
  Plug,
  Power,
  RotateCw,
  ShieldCheck,
  Smartphone,
  Sun,
  Thermometer,
  Timer,
  UtilityPole,
  Wind,
  Zap
} from 'lucide-react';
import { BluettiTelemetry, ConnectionState } from '../types/bluetti';

interface MainDashboardProps {
  telemetry: BluettiTelemetry;
  connectionState: ConnectionState;
  isPaused: boolean;
  onToggleAc: () => void;
  onToggleDc: () => void;
  onToggleEco: () => void;
  onTogglePowerLifting: () => void;
  onToggleGridEnhancement: () => void;
  onSetChargeMode: (mode: 'silent' | 'standard' | 'turbo') => void;
  onPauseBle: () => void;
  onResumeBle: () => void;
}

interface ModernToggleProps {
  checked: boolean;
  onChange: () => void;
  title?: string;
  disabled?: boolean;
}

const ModernToggle: React.FC<ModernToggleProps> = ({ checked, onChange, title, disabled }) => {
  return (
    <button
      type="button"
      onClick={onChange}
      disabled={disabled}
      title={title}
      className={`relative inline-flex h-[24px] w-[46px] shrink-0 cursor-pointer items-center rounded-full border transition-all duration-200 ease-in-out focus:outline-none ${
        checked
          ? 'bg-emerald-950/90 border-emerald-500/80 shadow-md shadow-emerald-500/20'
          : 'bg-slate-800/90 border-slate-600/80 hover:border-slate-500'
      } ${disabled ? 'opacity-50 cursor-not-allowed' : ''}`}
    >
      <span
        className={`pointer-events-none inline-block h-[18px] w-[18px] rounded-full transition-transform duration-200 ease-in-out ${
          checked
            ? 'translate-x-[24px] bg-emerald-400 shadow-md shadow-emerald-400/60 ring-2 ring-emerald-500/40'
            : 'translate-x-[3px] bg-slate-400'
        }`}
      />
    </button>
  );
};

export const MainDashboard: React.FC<MainDashboardProps> = ({
  telemetry,
  connectionState,
  isPaused,
  onToggleAc,
  onToggleDc,
  onToggleEco,
  onTogglePowerLifting,
  onToggleGridEnhancement,
  onSetChargeMode,
  onPauseBle,
  onResumeBle
}) => {
  const totalInputWatts = telemetry.dcInputWatts + telemetry.acInputWatts;
  const totalOutputWatts = telemetry.acOutputWatts + telemetry.dcOutputWatts;
  const netWatts = totalInputWatts - totalOutputWatts;

  const getSocColor = (soc: number) => {
    if (soc > 50) return 'text-emerald-400 border-emerald-500/40 bg-emerald-500/10';
    if (soc > 20) return 'text-amber-400 border-amber-500/40 bg-amber-500/10';
    return 'text-rose-400 border-rose-500/40 bg-rose-500/10 animate-pulse';
  };

  const getSocBarColor = (soc: number) => {
    if (soc > 50) return 'bg-emerald-500';
    if (soc > 20) return 'bg-amber-500';
    return 'bg-rose-500';
  };

  return (
    <div className="space-y-4 min-w-[760px]">
      
      {/* Hero Energy Flow Diagram */}
      <div className="bg-slate-900/80 border border-slate-800 rounded-2xl p-4 relative overflow-hidden backdrop-blur-md">
        
        {/* Ambient background glow */}
        <div className="absolute -top-24 -left-24 w-72 h-72 bg-cyan-500/10 rounded-full blur-3xl pointer-events-none" />
        <div className="absolute -bottom-24 -right-24 w-72 h-72 bg-emerald-500/10 rounded-full blur-3xl pointer-events-none" />

        {/* Centered Energy Flow Header */}
        <div className="flex items-center justify-center space-x-2 text-center mb-3">
          <Activity className="w-4 h-4 text-cyan-400" />
          <h3 className="text-sm font-extrabold uppercase tracking-wider text-slate-200">
            Flujo de Energía en Tiempo Real
          </h3>
        </div>

        {/* 3-Column Energy Flow Layout - FIXED 3 COLUMNS, NO ACCIDENTAL VERTICAL COLLAPSE */}
        <div className="grid grid-cols-3 gap-3.5 items-stretch">
          
          {/* COL 1: Inputs (Solar + Grid) */}
          <div className="space-y-3 flex flex-col justify-between">
            {/* Destacado: Entrada Total */}
            <div className="bg-gradient-to-r from-cyan-950/70 via-slate-900/80 to-slate-900/60 border border-cyan-500/40 rounded-xl px-3 py-2 flex items-center justify-between shadow-md shadow-cyan-950/30">
              <div className="flex items-center space-x-2 shrink-0 whitespace-nowrap">
                <span className="w-2 h-2 rounded-full bg-cyan-400 animate-pulse shadow-sm shadow-cyan-400/80 shrink-0" />
                <span className="text-xs font-extrabold text-cyan-200 uppercase tracking-wider whitespace-nowrap">
                  Entrada Total
                </span>
              </div>
              <span className="font-mono text-lg font-black text-cyan-300 drop-shadow-sm whitespace-nowrap shrink-0">
                {totalInputWatts} <span className="text-xs font-normal text-cyan-400/80">W</span>
              </span>
            </div>

            {/* Solar Card */}
            <div className="bg-slate-950/70 border border-amber-500/30 rounded-xl p-3 relative overflow-hidden">
              <div className="flex items-center justify-between">
                <div className="flex items-center space-x-2.5">
                  <div className="p-1.5 rounded-lg bg-amber-500/10 text-amber-400 border border-amber-500/20">
                    <Sun className="w-4 h-4" />
                  </div>
                  <div>
                    <span className="text-[11px] font-medium text-slate-400 block whitespace-nowrap leading-tight">Solar / DC (MPPT)</span>
                    <span className="text-lg font-bold font-mono text-amber-400 whitespace-nowrap">
                      {telemetry.dcInputWatts} <span className="text-xs font-normal text-slate-400">W</span>
                    </span>
                  </div>
                </div>
                <div className="text-right font-mono text-xs">
                  <div className="text-amber-300 font-bold whitespace-nowrap">{telemetry.dcInputVoltage} V</div>
                  <div className="text-[10px] text-slate-400 whitespace-nowrap">
                    {telemetry.dcInputCurrent ?? (telemetry.dcInputVoltage > 0 ? (telemetry.dcInputWatts / telemetry.dcInputVoltage).toFixed(1) : '0.0')} A
                  </div>
                </div>
              </div>
              <div className="mt-2 text-[10px] text-slate-400 flex items-center justify-between border-t border-slate-800/80 pt-1.5 whitespace-nowrap gap-2">
                <span>Rango: 12V - 60V (Max 20A)</span>
                <span className="font-semibold text-amber-400/90 shrink-0">Max 1000W</span>
              </div>
            </div>

            {/* AC Grid Card */}
            <div className="bg-slate-950/70 border border-blue-500/30 rounded-xl p-3 relative overflow-hidden">
              <div className="flex items-center justify-between">
                <div className="flex items-center space-x-2.5">
                  <div className="p-1.5 rounded-lg bg-blue-500/10 text-blue-400 border border-blue-500/20">
                    <UtilityPole className="w-4 h-4" />
                  </div>
                  <div>
                    <span className="text-[11px] font-medium text-slate-400 block whitespace-nowrap leading-tight">Red Eléctrica (AC)</span>
                    <span className="text-lg font-bold font-mono text-blue-400 whitespace-nowrap">
                      {telemetry.acInputWatts} <span className="text-xs font-normal text-slate-400">W</span>
                    </span>
                  </div>
                </div>
                <div className="text-right font-mono text-xs">
                  <div className="text-blue-300 font-bold whitespace-nowrap">{telemetry.acInputVoltage} V</div>
                  <div className="text-[10px] text-slate-400 whitespace-nowrap">
                    {telemetry.acInputCurrent ?? (telemetry.acInputVoltage > 0 ? (telemetry.acInputWatts / telemetry.acInputVoltage).toFixed(1) : '0.0')} A
                  </div>
                </div>
              </div>
              <div className="mt-2 text-[10px] text-slate-400 flex items-center justify-between border-t border-slate-800/80 pt-1.5 whitespace-nowrap gap-1.5">
                <div className="flex items-center gap-1.5">
                  <span className={`px-1.5 py-0.5 rounded text-[9px] font-bold uppercase tracking-wider border ${
                    telemetry.chargingMode === 'silent'
                      ? 'bg-slate-700/80 text-slate-200 border-slate-500'
                      : telemetry.chargingMode === 'standard'
                      ? 'bg-blue-900/60 text-blue-300 border-blue-500/40'
                      : 'bg-rose-900/60 text-rose-300 border-rose-500/40'
                  }`}>
                    Modo {telemetry.chargingMode === 'silent' ? 'Silencioso' : telemetry.chargingMode === 'standard' ? 'Estándar' : 'Turbo'}
                  </span>
                  {telemetry.gridEnhancementOn && (
                    <span className="px-1.5 py-0.5 rounded text-[9px] font-bold uppercase tracking-wider bg-amber-950/80 text-amber-300 border border-amber-500/40">
                      Refuerzo
                    </span>
                  )}
                </div>
                <span className="font-semibold text-blue-400/90 shrink-0">Max 1200W</span>
              </div>
            </div>
          </div>

          {/* COL 2: Center Battery LiFePO4 */}
          <div className="flex flex-col">
            <div className="bg-slate-950/90 border-2 border-slate-700/80 rounded-2xl p-3 text-center shadow-lg relative overflow-hidden flex flex-col justify-between h-full">
              
              {/* Battery Header & Badge */}
              <div className="inline-flex items-center justify-center space-x-1.5 px-2.5 py-0.5 rounded-full text-[10px] font-semibold uppercase tracking-wider mb-1 border bg-slate-900 border-slate-700 text-slate-300 self-center">
                <Battery className="w-3 h-3 text-cyan-400" />
                <span>LiFePO4 1024Wh</span>
              </div>

              {/* Big SoC % Gauge */}
              <div className="my-0.5">
                <span className={`text-5xl font-black font-mono tracking-tight ${
                  telemetry.soc > 50 ? 'text-emerald-400' : telemetry.soc > 20 ? 'text-amber-400' : 'text-rose-400'
                }`}>
                  {telemetry.soc}%
                </span>
              </div>

              {/* Battery Progress Bar */}
              <div className="w-full bg-slate-900 rounded-full h-2.5 overflow-hidden p-0.5 border border-slate-800 my-1.5">
                <div
                  className={`h-full rounded-full transition-all duration-700 ${getSocBarColor(telemetry.soc)}`}
                  style={{ width: `${Math.max(5, telemetry.soc)}%` }}
                />
              </div>

              {/* Estado Operativo Dinámico Integrado (Cargando / Descargando / En Reposo) */}
              <div className="my-1 flex justify-center">
                {netWatts > 0 ? (
                  <div className="inline-flex items-center space-x-1.5 px-3 py-1 rounded-full text-xs font-bold font-mono text-emerald-400 border border-emerald-500/40 bg-emerald-950/70 shadow-sm shadow-emerald-500/20">
                    <span className="w-2 h-2 rounded-full bg-emerald-400 animate-ping inline-block" />
                    <span>Cargando (+{netWatts} W)</span>
                  </div>
                ) : netWatts < 0 ? (
                  <div className="inline-flex items-center space-x-1.5 px-3 py-1 rounded-full text-xs font-bold font-mono text-amber-400 border border-amber-500/40 bg-amber-950/70 shadow-sm shadow-amber-500/20">
                    <span className="w-2 h-2 rounded-full bg-amber-400 inline-block" />
                    <span>Descargando ({netWatts} W)</span>
                  </div>
                ) : (
                  <div className="inline-flex items-center space-x-1.5 px-3 py-1 rounded-full text-xs font-bold font-mono text-slate-300 border border-slate-700 bg-slate-900/80">
                    <span className="w-2 h-2 rounded-full bg-slate-500 inline-block" />
                    <span>En Reposo (0 W)</span>
                  </div>
                )}
              </div>

              {/* Energía Real Almacenada (Wh calculados 100% reales) */}
              <div className="grid grid-cols-2 gap-2 pt-1.5 border-t border-slate-800/80 text-[11px]">
                <div>
                  <span className="text-[9px] text-slate-500 block uppercase font-medium">Energía Real</span>
                  <span className="text-slate-200 font-mono font-bold text-xs">
                    {Math.round((telemetry.soc / 100) * 1024)} <span className="text-[10px] text-slate-400 font-normal">/ 1024 Wh</span>
                  </span>
                </div>
                <div>
                  <span className="text-[9px] text-slate-500 block uppercase font-medium">Tecnología</span>
                  <span className="text-cyan-300/90 font-mono font-semibold text-xs">3000+ Ciclos</span>
                </div>
              </div>

              {/* Estimated Time Badge */}
              <div className="mt-1.5 pt-1.5 border-t border-slate-800/80 flex items-center justify-center space-x-1.5 text-[11px]">
                <div className="p-1 rounded bg-cyan-500/10 border border-cyan-500/20 text-cyan-400 shrink-0">
                  <Timer className="w-3 h-3" />
                </div>
                {telemetry.estimatedTimeToFullMinutes !== null ? (
                  <span className="text-emerald-400 font-medium">
                    Carga: <strong>{Math.floor(telemetry.estimatedTimeToFullMinutes / 60)}h {telemetry.estimatedTimeToFullMinutes % 60}m</strong>
                  </span>
                ) : telemetry.estimatedRuntimeMinutes !== null ? (
                  <span className="text-amber-400 font-medium">
                    Autonomía: <strong>{Math.floor(telemetry.estimatedRuntimeMinutes / 60)}h {telemetry.estimatedRuntimeMinutes % 60}m</strong>
                  </span>
                ) : (
                  <span className="text-slate-500 font-medium">
                    Consumo equilibrado
                  </span>
                )}
              </div>

            </div>
          </div>

          {/* COL 3: Outputs (AC Inverter + DC Out) with DIRECT INTEGRATED SWITCHES */}
          <div className="space-y-3 flex flex-col justify-between">
            {/* Destacado: Salida Total */}
            <div className="bg-gradient-to-r from-rose-950/70 via-slate-900/80 to-slate-900/60 border border-rose-500/40 rounded-xl px-3 py-2 flex items-center justify-between shadow-md shadow-rose-950/30">
              <div className="flex items-center space-x-2 shrink-0 whitespace-nowrap">
                <span className="w-2 h-2 rounded-full bg-rose-400 animate-pulse shadow-sm shadow-rose-400/80 shrink-0" />
                <span className="text-xs font-extrabold text-rose-200 uppercase tracking-wider whitespace-nowrap">
                  Salida Total
                </span>
              </div>
              <span className="font-mono text-lg font-black text-rose-300 drop-shadow-sm whitespace-nowrap shrink-0">
                {totalOutputWatts} <span className="text-xs font-normal text-rose-400/80">W</span>
              </span>
            </div>

            {/* AC Inverter Output Card */}
            <div className={`bg-slate-950/70 border rounded-xl p-3 relative transition-all ${
              telemetry.acOutputOn ? 'border-rose-500/40 shadow-sm shadow-rose-500/10' : 'border-slate-800/70 opacity-80'
            }`}>
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center space-x-2.5">
                  <div className={`p-1.5 rounded-lg border shrink-0 ${
                    telemetry.acOutputOn
                      ? 'bg-rose-500/10 text-rose-400 border-rose-500/30'
                      : 'bg-slate-800 text-slate-500 border-slate-700'
                  }`}>
                    <Plug className="w-4 h-4" />
                  </div>
                  <div>
                    <span className="text-[11px] font-medium text-slate-400 block whitespace-nowrap leading-tight">Tomas AC (120V)</span>
                    <span className="text-lg font-bold font-mono text-rose-400 whitespace-nowrap">
                      {telemetry.acOutputWatts} <span className="text-xs font-normal text-slate-400">W</span>
                    </span>
                  </div>
                </div>

                {/* Modern Toggle Switch */}
                <ModernToggle
                  checked={telemetry.acOutputOn}
                  onChange={onToggleAc}
                  title={telemetry.acOutputOn ? 'Apagar Inversor AC' : 'Encender Inversor AC'}
                />
              </div>

              <div className="mt-2 pt-1.5 border-t border-slate-800/80 flex items-center justify-between gap-1.5 text-[10px]">
                <span className="font-medium text-slate-300 whitespace-nowrap">Onda Sinusoidal Pura</span>
                <span className="font-mono text-rose-300/90 font-semibold whitespace-nowrap text-[9px] bg-slate-900/90 px-2 py-0.5 rounded border border-slate-800">
                  1800W <span className="text-slate-400 font-normal">(Pico 2700W)</span>
                </span>
              </div>
            </div>

            {/* DC USB / 12V Output Card */}
            <div className={`bg-slate-950/70 border rounded-xl p-3 relative transition-all ${
              telemetry.dcOutputOn ? 'border-purple-500/40 shadow-sm shadow-purple-500/10' : 'border-slate-800/70 opacity-80'
            }`}>
              <div className="flex items-center justify-between gap-2">
                <div className="flex items-center space-x-2.5">
                  <div className={`p-1.5 rounded-lg border shrink-0 ${
                    telemetry.dcOutputOn
                      ? 'bg-purple-500/10 text-purple-400 border-purple-500/30'
                      : 'bg-slate-800 text-slate-500 border-slate-700'
                  }`}>
                    <Zap className="w-4 h-4" />
                  </div>
                  <div>
                    <span className="text-[11px] font-medium text-slate-400 block whitespace-nowrap leading-tight">Salidas DC (USB / 12V)</span>
                    <span className="text-lg font-bold font-mono text-purple-400 whitespace-nowrap">
                      {telemetry.dcOutputWatts} <span className="text-xs font-normal text-slate-400">W</span>
                    </span>
                  </div>
                </div>

                {/* Modern Toggle Switch */}
                <ModernToggle
                  checked={telemetry.dcOutputOn}
                  onChange={onToggleDc}
                  title={telemetry.dcOutputOn ? 'Apagar Salidas DC' : 'Encender Salidas DC'}
                />
              </div>

              {/* Compact 2-Column Port Specs Grid */}
              <div className="mt-2 pt-1.5 border-t border-slate-800/80 grid grid-cols-2 gap-1.5 text-[10px]">
                <div className="bg-slate-900/70 rounded px-2 py-1 border border-slate-800/70 flex flex-col justify-center">
                  <span className="text-[9px] text-slate-400 uppercase font-semibold">2x USB-C</span>
                  <span className="font-mono text-purple-300 font-bold text-[11px]">140W + 100W PD</span>
                </div>
                <div className="bg-slate-900/70 rounded px-2 py-1 border border-slate-800/70 flex flex-col justify-center">
                  <span className="text-[9px] text-slate-400 uppercase font-semibold">2x USB-A & DC</span>
                  <span className="font-mono text-slate-300 text-[11px] font-medium">15W+15W • 12V 10A</span>
                </div>
              </div>
            </div>
          </div>

        </div>

      </div>

      {/* Auxiliary Grid Charging Speed, ECO Mode & Grid Enhancement Settings (Compact & Streamlined) */}
      <div className="bg-slate-900/80 border border-slate-800 rounded-2xl p-2.5 backdrop-blur-md">
        <div className="flex flex-row items-center justify-between gap-3">
          
          {/* Speed Modes */}
          <div className="flex items-center gap-2.5">
            <div className="flex items-center space-x-2">
              <div className="p-1.5 rounded-lg bg-blue-500/10 text-blue-400 border border-blue-500/20">
                <Gauge className="w-4 h-4" />
              </div>
              <div>
                <h4 className="text-xs font-bold text-white uppercase tracking-wider leading-tight">Carga AC</h4>
                <p className="text-[10px] text-slate-400 leading-tight">Potencia interna</p>
              </div>
            </div>

            <div className="flex items-center p-0.5 bg-slate-950 rounded-xl border border-slate-800">
              <button
                onClick={() => onSetChargeMode('silent')}
                className={`py-1 px-2.5 rounded-lg text-xs font-semibold transition-all ${
                  telemetry.chargingMode === 'silent'
                    ? 'bg-slate-600 text-slate-100 shadow-sm border border-slate-500'
                    : 'text-slate-400 hover:text-white'
                }`}
              >
                Silencioso (≤300W)
              </button>
              <button
                onClick={() => onSetChargeMode('standard')}
                className={`py-1 px-2.5 rounded-lg text-xs font-semibold transition-all ${
                  telemetry.chargingMode === 'standard'
                    ? 'bg-blue-600 text-white shadow-sm border border-blue-500'
                    : 'text-slate-400 hover:text-white'
                }`}
              >
                Estándar (600W)
              </button>
              <button
                onClick={() => onSetChargeMode('turbo')}
                className={`py-1 px-2.5 rounded-lg text-xs font-semibold transition-all ${
                  telemetry.chargingMode === 'turbo'
                    ? 'bg-rose-600 text-white shadow-sm border border-rose-500'
                    : 'text-slate-400 hover:text-white'
                }`}
              >
                Turbo (1200W)
              </button>
            </div>
          </div>

          {/* Right Controls: ECO Mode + Power Lifting + Refuerzo de Red */}
          <div className="flex items-center gap-3">
            {/* ECO Mode Toggle */}
            <div className="flex items-center gap-2 border-l border-slate-800 pl-3">
              <div className="flex items-center space-x-1.5">
                <div className="p-1 rounded-lg bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                  <Leaf className="w-3.5 h-3.5" />
                </div>
                <div>
                  <span className="text-xs font-bold text-slate-200 block uppercase tracking-wider leading-tight">Modo ECO</span>
                  <span className="text-[10px] text-slate-400 block leading-tight">Autoapagado</span>
                </div>
              </div>
              <ModernToggle
                checked={telemetry.ecoModeOn}
                onChange={onToggleEco}
                title={telemetry.ecoModeOn ? 'Desactivar Modo ECO' : 'Activar Modo ECO'}
              />
            </div>

            {/* Power Lifting (2700W) Toggle */}
            <div className="flex items-center gap-2 border-l border-slate-800 pl-3">
              <div className="flex items-center space-x-1.5">
                <div className={`p-1 rounded-lg border transition-colors ${
                  telemetry.powerLiftingOn
                    ? 'bg-orange-500/20 text-orange-400 border-orange-500/40 shadow-sm shadow-orange-500/20'
                    : 'bg-slate-800/80 text-slate-400 border-slate-700'
                }`}>
                  <Flame className="w-3.5 h-3.5" />
                </div>
                <div>
                  <span className="text-xs font-bold text-slate-200 block uppercase tracking-wider leading-tight">Power Lifting</span>
                  <span className="text-[10px] text-orange-400/90 font-mono block leading-tight">Hasta 2700W</span>
                </div>
              </div>
              <ModernToggle
                checked={telemetry.powerLiftingOn}
                onChange={onTogglePowerLifting}
                title={telemetry.powerLiftingOn ? 'Desactivar Power Lifting' : 'Activar Power Lifting (Sobretensión para cargas resistivas puras hasta 2700W)'}
              />
            </div>

            {/* Refuerzo de Red Eléctrica Toggle */}
            <div className="flex items-center gap-2 border-l border-slate-800 pl-3">
              <div className="flex items-center space-x-1.5">
                <div className={`p-1 rounded-lg border transition-colors ${
                  telemetry.gridEnhancementOn
                    ? 'bg-amber-500/20 text-amber-400 border-amber-500/40 shadow-sm shadow-amber-500/20'
                    : 'bg-slate-800/80 text-slate-400 border-slate-700'
                }`}>
                  <Zap className="w-3.5 h-3.5" />
                </div>
                <div>
                  <span className="text-xs font-bold text-slate-200 block uppercase tracking-wider leading-tight">Refuerzo Red</span>
                  <span className="text-[10px] text-slate-400 block leading-tight">Red / Generador</span>
                </div>
              </div>
              <ModernToggle
                checked={telemetry.gridEnhancementOn}
                onChange={onToggleGridEnhancement}
                title={telemetry.gridEnhancementOn ? 'Desactivar Refuerzo de Red' : 'Activar Refuerzo de Red (Adaptación para generadores o red inestable)'}
              />
            </div>
          </div>

        </div>
      </div>

    </div>
  );
};
