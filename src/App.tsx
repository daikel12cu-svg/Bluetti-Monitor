/**
 * @license
 * SPDX-License-Identifier: Apache-2.0
 */

import React, { useEffect, useState } from 'react';
import {
  Activity,
  AlertCircle,
  Bluetooth,
  CheckCircle2,
  Code2,
  ExternalLink,
  Github,
  Radio,
  Sliders,
  Smartphone,
  Tv,
  X,
  Zap
} from 'lucide-react';
import { Navbar, ActiveTab } from './components/Navbar';
import { MainDashboard } from './components/MainDashboard';
import { GamerOverlayWidget } from './components/GamerOverlayWidget';
import { ProtocolInspector } from './components/ProtocolInspector';
import { PythonProjectExport } from './components/PythonProjectExport';
import { UserGuide } from './components/UserGuide';
import { BleDiagnosticModal } from './components/BleDiagnosticModal';
import { bluettiService } from './services/webBluetooth';
import { BluettiTelemetry, ConnectionState, PacketLog } from './types/bluetti';

export default function App() {
  const [currentTab, setCurrentTab] = useState<ActiveTab>('dashboard');
  const [telemetry, setTelemetry] = useState<BluettiTelemetry>(bluettiService.getTelemetry());
  const [connectionState, setConnectionState] = useState<ConnectionState>(bluettiService.getConnectionState());
  const [statusMessage, setStatusMessage] = useState<string>('Desconectado - Pulsa Buscar Bluetti');
  const [packets, setPackets] = useState<PacketLog[]>([]);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  const [isInIframe, setIsInIframe] = useState<boolean>(false);
  const [isDiagnosticOpen, setIsDiagnosticOpen] = useState<boolean>(false);

  useEffect(() => {
    try {
      setIsInIframe(window.self !== window.top);
    } catch {
      setIsInIframe(true);
    }
  }, []);

  const isPaused = connectionState === 'paused';

  useEffect(() => {
    // Subscribe to telemetry changes
    const unsubTelemetry = bluettiService.onTelemetry((data) => {
      setTelemetry(data);
    });

    // Subscribe to status changes
    const unsubStatus = bluettiService.onStatus((state, msg) => {
      setConnectionState(state);
      setStatusMessage(msg);
    });

    // Subscribe to raw packet logs
    const unsubPacket = bluettiService.onPacket((pkt) => {
      setPackets((prev) => [pkt, ...prev.slice(0, 79)]);
    });

    return () => {
      unsubTelemetry();
      unsubStatus();
      unsubPacket();
    };
  }, []);

  const handleConnectBle = async () => {
    try {
      setErrorMessage(null);
      await bluettiService.connectRealDevice();
    } catch (err: any) {
      if (err.name !== 'NotFoundError' && err.name !== 'UserCancelledError') {
        setErrorMessage(err.message || 'No se pudo conectar por Bluetooth.');
      }
    }
  };

  const handlePauseBle = async () => {
    await bluettiService.pauseAndReleaseBle();
  };

  const handleResumeBle = async () => {
    await bluettiService.resumeBle();
  };

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 flex flex-col font-sans selection:bg-blue-600 selection:text-white min-w-[800px] min-h-[600px] overflow-x-auto">
      
      {/* Banner for Iframe mode to inform user to open in standalone tab for Bluetooth permission */}
      {isInIframe && (
        <div className="bg-gradient-to-r from-blue-950 via-slate-900 to-emerald-950 border-b border-blue-500/40 px-4 py-2 text-xs text-blue-200 flex items-center justify-between shadow-lg">
          <div className="flex items-center space-x-2">
            <Bluetooth className="w-4 h-4 text-cyan-400 shrink-0" />
            <span>
              <strong>Bluetooth en Windows:</strong> Los navegadores restringen el acceso a dispositivos Bluetooth dentro de la vista previa de AI Studio. Para conectar directamente con tu estación, abre la app en una ventana propia.
            </span>
          </div>
          <a
            href={window.location.href}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center space-x-1.5 px-3 py-1 bg-blue-600 hover:bg-blue-500 text-white rounded-lg font-bold text-[11px] shadow transition-all shrink-0 ml-4"
          >
            <span>Abrir en Ventana Completa</span>
            <ExternalLink className="w-3.5 h-3.5" />
          </a>
        </div>
      )}

      {/* Top Navigation */}
      <Navbar
        currentTab={currentTab}
        onSelectTab={setCurrentTab}
        connectionState={connectionState}
        statusMessage={statusMessage}
        isPaused={isPaused}
        onConnectBle={handleConnectBle}
        onPauseBle={handlePauseBle}
        onResumeBle={handleResumeBle}
        onOpenDiagnostic={() => setIsDiagnosticOpen(true)}
      />

      {/* Error alert toast if present */}
      {errorMessage && (
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 pt-4 w-full">
          <div className="bg-rose-950/80 border border-rose-600/50 rounded-2xl p-4 flex items-center justify-between text-rose-200 text-xs">
            <div className="flex items-center space-x-3">
              <AlertCircle className="w-4 h-4 text-rose-400 shrink-0" />
              <span>{errorMessage}</span>
            </div>
            <button
              onClick={() => setErrorMessage(null)}
              className="p-1 hover:bg-rose-900 rounded-lg text-rose-300"
            >
              <X className="w-4 h-4" />
            </button>
          </div>
        </div>
      )}

      {/* Main Content Area */}
      <main className="flex-1 max-w-7xl w-full mx-auto px-4 sm:px-6 lg:px-8 py-6">
        {currentTab === 'dashboard' && (
          <MainDashboard
            telemetry={telemetry}
            connectionState={connectionState}
            isPaused={isPaused}
            onToggleAc={() => bluettiService.toggleAcOutput()}
            onToggleDc={() => bluettiService.toggleDcOutput()}
            onToggleEco={() => bluettiService.toggleEcoMode()}
            onTogglePowerLifting={() => bluettiService.togglePowerLifting()}
            onToggleGridEnhancement={() => bluettiService.toggleGridEnhancement()}
            onSetChargeMode={(m) => bluettiService.setChargingMode(m)}
            onPauseBle={handlePauseBle}
            onResumeBle={handleResumeBle}
          />
        )}

        {currentTab === 'hud' && (
          <GamerOverlayWidget
            telemetry={telemetry}
            connectionState={connectionState}
            isPaused={isPaused}
            onToggleAc={() => bluettiService.toggleAcOutput()}
            onToggleDc={() => bluettiService.toggleDcOutput()}
            onPauseBle={handlePauseBle}
            onResumeBle={handleResumeBle}
          />
        )}

        {currentTab === 'protocol' && (
          <ProtocolInspector
            packets={packets}
            onClearPackets={() => setPackets([])}
          />
        )}

        {currentTab === 'python' && (
          <PythonProjectExport />
        )}

        {currentTab === 'guide' && (
          <UserGuide
            onGoToPythonTab={() => setCurrentTab('python')}
            onGoToDashboard={() => setCurrentTab('dashboard')}
          />
        )}
      </main>

      {/* Persistent Footer */}
      <footer className="border-t border-slate-900 bg-slate-950/80 py-4 px-4 sm:px-8 text-xs text-slate-500">
        <div className="max-w-7xl mx-auto flex items-center justify-center space-x-6">
          <button
            onClick={() => setCurrentTab('guide')}
            className="text-purple-400 hover:text-purple-300 font-medium transition-colors cursor-pointer"
          >
            ¿Cómo emparejar? (Guía)
          </button>
          <span className="text-slate-700">•</span>
          <button
            onClick={() => setCurrentTab('python')}
            className="text-emerald-400 hover:text-emerald-300 font-medium flex items-center gap-1.5 transition-colors cursor-pointer"
          >
            <Code2 className="w-3.5 h-3.5" />
            <span>Descargar .EXE / Python</span>
          </button>
        </div>
      </footer>

      {/* BLE Connection Diagnostic & Testing Modal */}
      <BleDiagnosticModal
        isOpen={isDiagnosticOpen}
        onClose={() => setIsDiagnosticOpen(false)}
      />

    </div>
  );
}
