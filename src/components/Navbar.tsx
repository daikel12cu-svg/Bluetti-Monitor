import React from 'react';
import {
  Activity,
  Bluetooth,
  Play,
  Radio,
  Smartphone,
  Tv,
  Zap
} from 'lucide-react';
import { ConnectionState } from '../types/bluetti';

export type ActiveTab = 'dashboard' | 'hud' | 'protocol' | 'python' | 'guide';

interface NavbarProps {
  currentTab: ActiveTab;
  onSelectTab: (tab: ActiveTab) => void;
  connectionState: ConnectionState;
  statusMessage: string;
  isPaused: boolean;
  onConnectBle: () => void;
  onPauseBle: () => void;
  onResumeBle: () => void;
  onOpenDiagnostic: () => void;
}

export const Navbar: React.FC<NavbarProps> = ({
  currentTab,
  onSelectTab,
  connectionState,
  statusMessage,
  isPaused,
  onConnectBle,
  onPauseBle,
  onResumeBle,
  onOpenDiagnostic
}) => {
  return (
    <header className="bg-slate-900/95 backdrop-blur-md border-b border-slate-800 sticky top-0 z-40 select-none shadow-lg shadow-black/20">
      
      {/* Tier 1: Main Header (Brand + Status & Quick Actions) */}
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-3 border-b border-slate-800/60">
        <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
          
          {/* Brand & Specs */}
          <div className="flex items-center space-x-3">
            <div className="w-10 h-10 rounded-xl bg-gradient-to-tr from-cyan-600 via-blue-500 to-indigo-500 p-0.5 shadow-lg shadow-cyan-500/20 shrink-0">
              <div className="w-full h-full bg-slate-950 rounded-[10px] flex items-center justify-center">
                <Zap className="w-5 h-5 text-cyan-400" />
              </div>
            </div>
            <div>
              <div className="flex items-center space-x-2">
                <span className="font-extrabold tracking-wider text-base text-white">BLUETTI</span>
                <span className="text-xs font-semibold px-2 py-0.5 rounded-md bg-cyan-950 text-cyan-400 border border-cyan-800/80 font-mono">
                  ELITE 100 V2
                </span>
                <span className="text-[11px] font-mono px-2 py-0.5 rounded-md bg-slate-800 text-slate-300 border border-slate-700">
                  120V / 60Hz
                </span>
              </div>
              <p className="text-[11px] text-slate-400 font-mono mt-0.5">
                BLE Modbus RTU • Inversor 1800W • Pack LiFePO4 1024Wh
              </p>
            </div>
          </div>

          {/* Status Badge & Connection Action Buttons */}
          <div className="flex items-center flex-wrap gap-2.5 justify-end">
            
            {/* Connection Status Pill */}
            <div className="flex items-center space-x-2 px-3 py-1.5 rounded-xl bg-slate-950 border border-slate-800 text-xs">
              <span className={`w-2.5 h-2.5 rounded-full ${
                isPaused
                  ? 'bg-purple-500 shadow-sm shadow-purple-500'
                  : connectionState === 'connected'
                  ? 'bg-emerald-400 shadow-sm shadow-emerald-400 animate-pulse'
                  : connectionState === 'connecting'
                  ? 'bg-blue-400 shadow-sm shadow-blue-400 animate-pulse'
                  : 'bg-rose-400'
              }`} />
              <span className="text-slate-300 font-mono text-[11px]">
                {statusMessage}
              </span>
            </div>

            {/* Liberar / Reconectar BLE Button (Enfoque B) */}
            {isPaused ? (
              <button
                onClick={onResumeBle}
                className="flex items-center space-x-1.5 px-3 py-1.5 rounded-xl bg-purple-600 hover:bg-purple-500 text-white text-xs font-semibold shadow-md shadow-purple-600/30 transition-colors"
                title="Reanudar enlace exclusivo Bluetooth con este ordenador"
              >
                <Play className="w-3.5 h-3.5" />
                <span>Reconectar PC</span>
              </button>
            ) : (
              <button
                onClick={onPauseBle}
                className="flex items-center space-x-1.5 px-3 py-1.5 rounded-xl bg-slate-800/80 hover:bg-purple-950 hover:text-purple-300 hover:border-purple-700 text-slate-300 border border-slate-700 text-xs font-medium transition-all"
                title="Cierra el enlace Bluetooth en Windows para que puedas usar la app del móvil"
              >
                <Smartphone className="w-3.5 h-3.5 text-purple-400" />
                <span>Liberar BLE (Móvil)</span>
              </button>
            )}

            {/* Direct Web Bluetooth Connect Button */}
            <button
              onClick={onConnectBle}
              className={`flex items-center space-x-1.5 px-3 py-1.5 rounded-xl text-xs font-semibold transition-all ${
                connectionState === 'connected'
                  ? 'bg-emerald-600/20 text-emerald-400 border border-emerald-500/40 hover:bg-emerald-600/30'
                  : 'bg-blue-600 hover:bg-blue-500 text-white shadow-md shadow-blue-500/20'
              }`}
              title="Escanear y conectar por Bluetooth Low Energy en Google Chrome / Microsoft Edge"
            >
              <Bluetooth className="w-3.5 h-3.5" />
              <span>
                {connectionState === 'connected' ? 'BLE Enlazado' : 'Buscar Bluetti'}
              </span>
            </button>

            {/* Diagnostic Button */}
            <button
              onClick={onOpenDiagnostic}
              className="flex items-center space-x-1.5 px-3 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-cyan-300 border border-cyan-500/30 hover:border-cyan-400/50 text-xs font-semibold transition-all shadow-sm cursor-pointer"
              title="Herramienta de pruebas y diagnóstico de desconexiones BLE paso a paso"
            >
              <Activity className="w-3.5 h-3.5 text-cyan-400" />
              <span className="hidden sm:inline">Diagnóstico BLE</span>
            </button>

          </div>

        </div>
      </div>

      {/* Tier 2: Navigation Bar (Segmented, Spacious Tabs) */}
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-2">
        <nav className="flex items-center space-x-1.5 overflow-x-auto py-1 scrollbar-none">
          
          <button
            onClick={() => onSelectTab('dashboard')}
            className={`flex items-center space-x-2 px-3.5 py-1.5 rounded-xl text-xs font-medium transition-all whitespace-nowrap ${
              currentTab === 'dashboard'
                ? 'bg-blue-600 text-white shadow-md shadow-blue-500/25 font-semibold'
                : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
            }`}
          >
            <Activity className="w-3.5 h-3.5" />
            <span>Panel Principal</span>
          </button>

          <button
            onClick={() => onSelectTab('hud')}
            className={`flex items-center space-x-2 px-3.5 py-1.5 rounded-xl text-xs font-medium transition-all whitespace-nowrap ${
              currentTab === 'hud'
                ? 'bg-blue-600 text-white shadow-md shadow-blue-500/25 font-semibold'
                : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
            }`}
          >
            <Tv className="w-3.5 h-3.5" />
            <span>HUD Gamer Flotante</span>
          </button>

          <button
            onClick={() => onSelectTab('protocol')}
            className={`flex items-center space-x-2 px-3.5 py-1.5 rounded-xl text-xs font-medium transition-all whitespace-nowrap ${
              currentTab === 'protocol'
                ? 'bg-blue-600 text-white shadow-md shadow-blue-500/25 font-semibold'
                : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
            }`}
          >
            <Radio className="w-3.5 h-3.5" />
            <span>Inspector Modbus & CRC16</span>
          </button>
        </nav>
      </div>

    </header>
  );
};
