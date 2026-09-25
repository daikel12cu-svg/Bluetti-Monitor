import React, { useState } from 'react';
import {
  Activity,
  Battery,
  ChevronDown,
  ChevronUp,
  Clock,
  ExternalLink,
  Eye,
  GripHorizontal,
  Maximize2,
  Minimize2,
  Move,
  Plug,
  Power,
  RotateCw,
  Smartphone,
  Sparkles,
  Sun,
  Timer,
  Tv,
  UtilityPole,
  Zap,
  ZoomIn
} from 'lucide-react';
import { BluettiTelemetry, ConnectionState } from '../types/bluetti';

interface GamerOverlayWidgetProps {
  telemetry: BluettiTelemetry;
  connectionState: ConnectionState;
  isPaused: boolean;
  onToggleAc: () => void;
  onToggleDc: () => void;
  onPauseBle: () => void;
  onResumeBle: () => void;
}

type PositionPreset = 'top-left' | 'top-center' | 'top-right' | 'bottom-left' | 'bottom-center' | 'bottom-right';
type WidgetStyle = 'minimal' | 'full';

export const GamerOverlayWidget: React.FC<GamerOverlayWidgetProps> = ({
  telemetry,
  connectionState,
  isPaused,
  onToggleAc,
  onToggleDc,
  onPauseBle,
  onResumeBle
}) => {
  const [alpha, setAlpha] = useState(0.85); // Translúcido por defecto para el fondo
  const [zoom, setZoom] = useState(1.0); // Factor de escala (100% por defecto)
  const [widgetStyle, setWidgetStyle] = useState<WidgetStyle>('minimal'); // Minimalista por defecto
  const [isMinimized, setIsMinimized] = useState(false);
  const [positionPreset, setPositionPreset] = useState<PositionPreset>('top-right');
  const [showLaunchModal, setShowLaunchModal] = useState(false);

  // Asegurar que si el switch DC está apagado, la potencia DC es estrictamente 0W
  const actualDcOutputWatts = telemetry.dcOutputOn ? telemetry.dcOutputWatts : 0;
  const totalInputWatts = telemetry.dcInputWatts + telemetry.acInputWatts;
  const totalOutputWatts = telemetry.acOutputWatts + actualDcOutputWatts;
  const netWatts = totalInputWatts - totalOutputWatts;

  const getPositionClasses = () => {
    switch (positionPreset) {
      case 'top-left': return 'top-6 left-6';
      case 'top-center': return 'top-6 left-1/2 -translate-x-1/2';
      case 'top-right': return 'top-6 right-6';
      case 'bottom-left': return 'bottom-16 left-6';
      case 'bottom-center': return 'bottom-16 left-1/2 -translate-x-1/2';
      case 'bottom-right': return 'bottom-16 right-6';
      default: return 'top-6 right-6';
    }
  };

  const getTransformOrigin = () => {
    switch (positionPreset) {
      case 'top-left': return 'top left';
      case 'top-center': return 'top center';
      case 'top-right': return 'top right';
      case 'bottom-left': return 'bottom left';
      case 'bottom-center': return 'bottom center';
      case 'bottom-right': return 'bottom right';
      default: return 'top right';
    }
  };

  const handleLaunchHud = () => {
    setShowLaunchModal(true);
  };

  return (
    <div className="space-y-6">
      {/* Banner / Instructions Bar */}
      <div className="bg-gradient-to-r from-blue-900/40 via-indigo-900/30 to-cyan-900/40 border border-blue-500/30 rounded-2xl p-4 flex flex-col md:flex-row items-center justify-between gap-4">
        <div className="flex items-center space-x-3">
          <div className="p-2.5 bg-blue-500/20 text-cyan-400 rounded-xl">
            <Tv className="w-5 h-5" />
          </div>
          <div>
            <h4 className="text-sm font-bold text-white flex items-center space-x-2">
              <span>¿Deseas una vista compacta mientras juegas o trabajas?</span>
              <span className="px-2 py-0.5 rounded bg-blue-500/20 text-blue-300 text-[10px] font-mono font-bold">Modo Widget</span>
            </h4>
            <p className="text-xs text-slate-300 mt-0.5">
              Widget minimalista flotante siempre encima (Always-On-Top) con solo Batería y Potencia Neta en Vatios.
            </p>
          </div>
        </div>
        <div className="flex items-center space-x-2 shrink-0">
          <button
            onClick={handleLaunchHud}
            className="inline-flex items-center space-x-2 px-4 py-2.5 bg-gradient-to-r from-blue-600 via-indigo-600 to-cyan-600 hover:from-blue-500 hover:to-cyan-500 text-white rounded-xl font-bold text-xs shadow-md shadow-blue-500/25 transition-all cursor-pointer"
          >
            <Tv className="w-4 h-4" />
            <span>📺 Lanzar Widget / HUD</span>
          </button>
        </div>
      </div>
      
      {/* Configuration Header for HUD */}
      <div className="bg-slate-900/80 border border-slate-800 rounded-3xl p-5 backdrop-blur-md">
        <div className="flex flex-col lg:flex-row lg:items-center justify-between gap-4">
          <div>
            <div className="flex items-center space-x-2 text-cyan-400 font-mono text-xs uppercase tracking-wider mb-1">
              <Tv className="w-4 h-4" />
              <span>Widget Flotante Always-On-Top</span>
            </div>
            <h2 className="text-xl font-bold text-white">HUD & Widget Flotante</h2>
            <p className="text-xs text-slate-400 mt-1 max-w-xl">
              Vista previa del widget con fondo translúcido y texto 100% nítido. Diseñado para no interferir con juegos ni aplicaciones a pantalla completa.
            </p>
          </div>

          {/* Controls: Widget Style, 6 Positions, Opacity & Zoom */}
          <div className="flex flex-wrap items-center gap-3">
            
            {/* Style Selector */}
            <div className="flex items-center space-x-1 bg-slate-950 p-1 rounded-xl border border-slate-800 text-xs">
              <button
                onClick={() => setWidgetStyle('minimal')}
                className={`flex items-center space-x-1.5 px-3 py-1.5 rounded-lg text-xs font-bold transition-all ${
                  widgetStyle === 'minimal'
                    ? 'bg-cyan-600 text-white shadow-md shadow-cyan-900/40'
                    : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                <Sparkles className="w-3.5 h-3.5" />
                <span>Minimalista</span>
              </button>
              <button
                onClick={() => setWidgetStyle('full')}
                className={`flex items-center space-x-1.5 px-3 py-1.5 rounded-lg text-xs font-bold transition-all ${
                  widgetStyle === 'full'
                    ? 'bg-blue-600 text-white shadow-md shadow-blue-900/40'
                    : 'text-slate-400 hover:text-slate-200'
                }`}
              >
                <Tv className="w-3.5 h-3.5" />
                <span>Completo (HUD)</span>
              </button>
            </div>

            {/* 6 Positions Selector */}
            <div className="flex flex-col bg-slate-950 p-1.5 rounded-xl border border-slate-800 text-xs">
              <span className="text-slate-400 text-[10px] font-semibold uppercase px-1 mb-1 text-center">
                Posición en Pantalla (6)
              </span>
              <div className="grid grid-cols-3 gap-1">
                <button
                  onClick={() => setPositionPreset('top-left')}
                  className={`px-2 py-1 rounded text-[11px] font-medium transition-all ${
                    positionPreset === 'top-left' ? 'bg-cyan-600 text-white shadow-sm' : 'bg-slate-900/90 text-slate-400 hover:text-slate-200'
                  }`}
                  title="Arriba a la Izquierda"
                >
                  Arriba-Izq
                </button>
                <button
                  onClick={() => setPositionPreset('top-center')}
                  className={`px-2 py-1 rounded text-[11px] font-medium transition-all ${
                    positionPreset === 'top-center' ? 'bg-cyan-600 text-white shadow-sm' : 'bg-slate-900/90 text-slate-400 hover:text-slate-200'
                  }`}
                  title="Arriba al Centro"
                >
                  Arriba-Centro
                </button>
                <button
                  onClick={() => setPositionPreset('top-right')}
                  className={`px-2 py-1 rounded text-[11px] font-medium transition-all ${
                    positionPreset === 'top-right' ? 'bg-cyan-600 text-white shadow-sm' : 'bg-slate-900/90 text-slate-400 hover:text-slate-200'
                  }`}
                  title="Arriba a la Derecha"
                >
                  Arriba-Der
                </button>

                <button
                  onClick={() => setPositionPreset('bottom-left')}
                  className={`px-2 py-1 rounded text-[11px] font-medium transition-all ${
                    positionPreset === 'bottom-left' ? 'bg-cyan-600 text-white shadow-sm' : 'bg-slate-900/90 text-slate-400 hover:text-slate-200'
                  }`}
                  title="Abajo a la Izquierda"
                >
                  Abajo-Izq
                </button>
                <button
                  onClick={() => setPositionPreset('bottom-center')}
                  className={`px-2 py-1 rounded text-[11px] font-medium transition-all ${
                    positionPreset === 'bottom-center' ? 'bg-cyan-600 text-white shadow-sm' : 'bg-slate-900/90 text-slate-400 hover:text-slate-200'
                  }`}
                  title="Abajo al Centro"
                >
                  Abajo-Centro
                </button>
                <button
                  onClick={() => setPositionPreset('bottom-right')}
                  className={`px-2 py-1 rounded text-[11px] font-medium transition-all ${
                    positionPreset === 'bottom-right' ? 'bg-cyan-600 text-white shadow-sm' : 'bg-slate-900/90 text-slate-400 hover:text-slate-200'
                  }`}
                  title="Abajo a la Derecha"
                >
                  Abajo-Der
                </button>
              </div>
            </div>

            {/* Opacity Slider (Solo fondo) */}
            <div className="flex flex-col bg-slate-950 p-2 rounded-xl border border-slate-800 text-xs justify-center min-w-[125px]">
              <div className="flex items-center justify-between text-[10px] mb-1 font-semibold uppercase">
                <span className="flex items-center gap-1 text-slate-400">
                  <Eye className="w-3 h-3" /> Fondo Opacidad
                </span>
                <span className="font-mono text-cyan-400 ml-1.5">{Math.round(alpha * 100)}%</span>
              </div>
              <input
                type="range"
                min="0.25"
                max="1.0"
                step="0.05"
                value={alpha}
                onChange={(e) => setAlpha(parseFloat(e.target.value))}
                className="w-full accent-cyan-500 cursor-pointer"
              />
            </div>

            {/* Zoom / Escala Slider */}
            <div className="flex flex-col bg-slate-950 p-2 rounded-xl border border-slate-800 text-xs justify-center min-w-[125px]">
              <div className="flex items-center justify-between text-[10px] mb-1 font-semibold uppercase">
                <span className="flex items-center gap-1 text-slate-400">
                  <ZoomIn className="w-3 h-3" /> Zoom
                </span>
                <span className="font-mono text-cyan-400 ml-1.5">{Math.round(zoom * 100)}%</span>
              </div>
              <input
                type="range"
                min="0.75"
                max="1.50"
                step="0.05"
                value={zoom}
                onChange={(e) => setZoom(parseFloat(e.target.value))}
                className="w-full accent-cyan-500 cursor-pointer"
              />
            </div>

          </div>
        </div>
      </div>

      {/* Simulated Desktop Preview Canvas */}
      <div className="relative w-full h-[600px] rounded-3xl border-2 border-slate-800 overflow-hidden bg-slate-950 shadow-2xl flex items-center justify-center select-none">
        
        {/* Simulated Game / Wallpaper Background */}
        <div className="absolute inset-0 bg-gradient-to-br from-slate-950 via-slate-900 to-indigo-950 opacity-90" />
        
        {/* Subtle grid pattern */}
        <div 
          className="absolute inset-0 opacity-15"
          style={{
            backgroundImage: `radial-gradient(#38bdf8 1px, transparent 1px)`,
            backgroundSize: '24px 24px'
          }}
        />

        {/* Mock Windows Desktop Taskbar */}
        <div className="absolute bottom-0 inset-x-0 h-10 bg-slate-900/90 border-t border-slate-800/90 flex items-center justify-between px-4 z-10 backdrop-blur-md">
          <div className="flex items-center space-x-3">
            <div className="w-5 h-5 bg-blue-500/20 border border-blue-400/50 rounded flex items-center justify-center text-[10px] font-bold text-blue-400">
              ⊞
            </div>
            <span className="text-[11px] text-slate-400 font-mono">Windows 11 Desktop (Juego / Pantalla Completa)</span>
          </div>
          <div className="flex items-center space-x-4 text-xs font-mono text-slate-400">
            <span className="flex items-center gap-1.5">
              <span className={`w-2 h-2 rounded-full ${isPaused ? 'bg-purple-500' : 'bg-emerald-400 animate-pulse'}`} />
              BLE Modbus: {isPaused ? 'Liberado' : '1.5s Polling'}
            </span>
            <span>{new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}</span>
          </div>
        </div>

        {/* Central Watermark / Helper */}
        <div className="text-center pointer-events-none z-0 max-w-md px-6">
          <p className="text-xs uppercase font-mono tracking-widest text-slate-500 mb-2">
            Vista previa del HUD flotante
          </p>
          <p className="text-sm text-slate-400">
            {widgetStyle === 'minimal'
              ? 'Modo Ultra-Minimalista activo: fondo translúcido con texto e iconos 100% nítidos.'
              : 'Modo Completo activo: visión detallada con interruptores tácticos e información de carga.'}
          </p>
        </div>

        {/* ============================================================== */}
        {/* THE FLOATING HUD WIDGET                                        */}
        {/* ============================================================== */}
        
        {widgetStyle === 'minimal' ? (
          /* VERSION ULTRA-MINIMALISTA: Fondo translúcido (solo fondo) + Texto 100% opaco */
          <div
            className={`absolute ${getPositionClasses()} z-20 transition-all duration-200`}
            style={{
              transform: `scale(${zoom})`,
              transformOrigin: getTransformOrigin()
            }}
          >
            {/* Contenedor con fondo translúcido dinámico (rgba) para mantener el texto 100% nítido y opaco */}
            <div 
              className="flex items-center space-x-2 border border-slate-700/80 shadow-2xl backdrop-blur-xl rounded-full px-3 py-1 select-none hover:border-cyan-500/60 transition-all"
              style={{
                backgroundColor: `rgba(2, 6, 23, ${alpha})`
              }}
            >
              {/* Valor 1: Batería */}
              <div className="flex items-center space-x-1.5 font-mono text-xs opacity-100">
                <Battery className={`w-3.5 h-3.5 shrink-0 ${
                  telemetry.soc > 50 ? 'text-emerald-400' : telemetry.soc > 20 ? 'text-amber-400' : 'text-rose-400'
                }`} />
                <span className="text-[11px] font-semibold text-slate-200">Batería</span>
                <span className={`text-xs font-black ${
                  telemetry.soc > 50 ? 'text-emerald-400' : telemetry.soc > 20 ? 'text-amber-400' : 'text-rose-400'
                }`}>
                  {telemetry.soc}%
                </span>
              </div>

              {/* Separador vertical sutil */}
              <div className="h-3 w-px bg-slate-700/80 opacity-100" />

              {/* Valor 2: Estado cargando o descargando vatios totales */}
              <div className="flex items-center space-x-1 font-mono text-xs whitespace-nowrap opacity-100">
                {netWatts > 0 ? (
                  <div className="flex items-center space-x-1 text-emerald-400 font-bold text-xs">
                    <span className="w-1.5 h-1.5 rounded-full bg-emerald-400 animate-ping shrink-0 inline-block" />
                    <span>Estado: Cargando +{netWatts}W</span>
                  </div>
                ) : netWatts < 0 ? (
                  <div className="flex items-center space-x-1 text-amber-400 font-bold text-xs">
                    <span className="w-1.5 h-1.5 rounded-full bg-amber-400 shrink-0 inline-block" />
                    <span>Estado: Descargando {Math.abs(netWatts)}W</span>
                  </div>
                ) : (
                  <div className="flex items-center space-x-1 text-slate-300 font-bold text-xs">
                    <span className="w-1.5 h-1.5 rounded-full bg-slate-500 shrink-0 inline-block" />
                    <span>Estado: Neutro 0W</span>
                  </div>
                )}
              </div>

              {/* Botón discreto para expandir a vista completa */}
              <button
                onClick={() => setWidgetStyle('full')}
                className="p-0.5 rounded text-slate-400 hover:text-cyan-300 hover:bg-slate-800 transition-colors ml-0.5 opacity-100"
                title="Cambiar a vista completa (HUD)"
              >
                <Maximize2 className="w-3 h-3" />
              </button>

            </div>
          </div>
        ) : (
          /* VERSION COMPLETA (HUD Gamer Expandido) */
          <div
            className={`absolute ${getPositionClasses()} z-20 transition-all duration-200`}
            style={{
              transform: `scale(${zoom})`,
              transformOrigin: getTransformOrigin()
            }}
          >
            <div 
              className="border border-cyan-500/40 shadow-2xl backdrop-blur-xl rounded-2xl p-3.5 select-none w-72 hover:border-cyan-400/80 transition-all"
              style={{
                backgroundColor: `rgba(2, 6, 23, ${alpha})`
              }}
            >
              {/* Header */}
              <div className="flex items-center justify-between border-b border-slate-800 pb-2 mb-2.5">
                <div className="flex items-center space-x-2">
                  <span className={`w-2 h-2 rounded-full ${telemetry.soc > 20 ? 'bg-emerald-400' : 'bg-rose-500'}`} />
                  <span className="text-xs font-bold text-white tracking-wide">BLUETTI ELITE 100 V2</span>
                </div>
                <div className="flex items-center space-x-1">
                  <button
                    onClick={() => setWidgetStyle('minimal')}
                    className="p-1 rounded text-slate-400 hover:text-white hover:bg-slate-800 transition-colors"
                    title="Modo Minimalista"
                  >
                    <Minimize2 className="w-3.5 h-3.5" />
                  </button>
                  <button
                    onClick={() => setIsMinimized(!isMinimized)}
                    className="p-1 rounded text-slate-400 hover:text-white hover:bg-slate-800 transition-colors"
                  >
                    {isMinimized ? <ChevronDown className="w-3.5 h-3.5" /> : <ChevronUp className="w-3.5 h-3.5" />}
                  </button>
                </div>
              </div>

              {/* Main Battery Metric */}
              <div className="flex items-center justify-between mb-2">
                <div className="flex items-center space-x-2">
                  <Battery className={`w-6 h-6 ${telemetry.soc > 50 ? 'text-emerald-400' : telemetry.soc > 20 ? 'text-amber-400' : 'text-rose-400'}`} />
                  <div>
                    <div className="text-[10px] uppercase font-mono text-slate-400">Batería</div>
                    <div className="text-lg font-black text-white font-mono leading-none">{telemetry.soc}%</div>
                  </div>
                </div>
                <div className="text-right font-mono">
                  <div className="text-[10px] text-slate-400">Flujo Neto</div>
                  <div className={`text-xs font-bold ${netWatts > 0 ? 'text-emerald-400' : netWatts < 0 ? 'text-amber-400' : 'text-slate-300'}`}>
                    {netWatts > 0 ? `+${netWatts}W` : `${netWatts}W`}
                  </div>
                </div>
              </div>

              {!isMinimized && (
                <>
                  {/* Energy Grid */}
                  <div className="grid grid-cols-2 gap-2 my-2.5 pt-2 border-t border-slate-800/80 text-xs font-mono">
                    <div className="bg-slate-900/60 p-2 rounded-lg border border-slate-800">
                      <div className="text-[9px] text-slate-400 flex items-center space-x-1">
                        <Sun className="w-2.5 h-2.5 text-amber-400" />
                        <span>Entrada Solar</span>
                      </div>
                      <div className="text-sm font-bold text-amber-300 mt-0.5">{telemetry.dcInputWatts}W</div>
                    </div>

                    <div className="bg-slate-900/60 p-2 rounded-lg border border-slate-800">
                      <div className="text-[9px] text-slate-400 flex items-center space-x-1">
                        <UtilityPole className="w-2.5 h-2.5 text-blue-400" />
                        <span>Entrada Red AC</span>
                      </div>
                      <div className="text-sm font-bold text-blue-300 mt-0.5">{telemetry.acInputWatts}W</div>
                    </div>

                    <div className="bg-slate-900/60 p-2 rounded-lg border border-slate-800">
                      <div className="text-[9px] text-slate-400 flex items-center space-x-1">
                        <Plug className="w-2.5 h-2.5 text-cyan-400" />
                        <span>Salida AC (1800W)</span>
                      </div>
                      <div className="text-sm font-bold text-cyan-300 mt-0.5">{telemetry.acOutputWatts}W</div>
                    </div>

                    <div className="bg-slate-900/60 p-2 rounded-lg border border-slate-800">
                      <div className="text-[9px] text-slate-400 flex items-center space-x-1">
                        <Zap className="w-2.5 h-2.5 text-purple-400" />
                        <span>Salidas DC (12V)</span>
                      </div>
                      <div className="text-sm font-bold text-purple-300 mt-0.5">{actualDcOutputWatts}W</div>
                    </div>
                  </div>

                  {/* Hardware Quick Toggles */}
                  <div className="grid grid-cols-2 gap-2 pt-1">
                    <button
                      onClick={onToggleAc}
                      className={`py-1.5 px-2 rounded-lg font-bold text-xs flex items-center justify-center space-x-1.5 transition-all ${
                        telemetry.acOutputOn
                          ? 'bg-cyan-500 text-slate-950 font-black shadow-md shadow-cyan-500/30'
                          : 'bg-slate-800/80 text-slate-400 hover:text-white'
                      }`}
                    >
                      <Power className="w-3 h-3" />
                      <span>AC {telemetry.acOutputOn ? 'ON' : 'OFF'}</span>
                    </button>

                    <button
                      onClick={onToggleDc}
                      className={`py-1.5 px-2 rounded-lg font-bold text-xs flex items-center justify-center space-x-1.5 transition-all ${
                        telemetry.dcOutputOn
                          ? 'bg-purple-500 text-white font-black shadow-md shadow-purple-500/30'
                          : 'bg-slate-800/80 text-slate-400 hover:text-white'
                      }`}
                    >
                      <Power className="w-3 h-3" />
                      <span>DC {telemetry.dcOutputOn ? 'ON' : 'OFF'}</span>
                    </button>
                  </div>
                </>
              )}
            </div>
          </div>
        )}

      </div>

      {/* Modal / Popup de lanzamiento */}
      {showLaunchModal && (
        <div className="fixed inset-0 z-50 bg-slate-950/80 backdrop-blur-md flex items-center justify-center p-4">
          <div className="bg-slate-900 border border-slate-700 rounded-3xl p-6 max-w-lg w-full shadow-2xl">
            <h3 className="text-lg font-bold text-white mb-2 flex items-center space-x-2">
              <Tv className="w-5 h-5 text-cyan-400" />
              <span>Lanzar Widget en Windows</span>
            </h3>
            <p className="text-xs text-slate-300 mb-4">
              Para ejecutar este widget de forma independiente sobre tus juegos o escritorio de Windows:
            </p>

            <div className="bg-slate-950 p-3 rounded-xl border border-slate-800 font-mono text-xs text-cyan-300 space-y-1 mb-4">
              <div>1. Haz doble clic en: <span className="text-white font-bold">INICIAR_WIDGET_MINIMALISTA.bat</span></div>
              <div className="text-slate-400">O para generar el .exe independiente:</div>
              <div>2. Haz doble clic en: <span className="text-emerald-400 font-bold">COMPILAR_WIDGET_EXE.bat</span></div>
            </div>

            <div className="flex justify-end">
              <button
                onClick={() => setShowLaunchModal(false)}
                className="px-4 py-2 bg-slate-800 hover:bg-slate-700 text-white rounded-xl text-xs font-bold transition-all"
              >
                Cerrar
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
};
