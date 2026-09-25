import React, { useState } from 'react';
import JSZip from 'jszip';
import {
  Check,
  Code2,
  Copy,
  Download,
  FileCode,
  FileText,
  FolderArchive,
  Layers,
  Play,
  Terminal,
  Laptop
} from 'lucide-react';
import { PYTHON_FILES, PythonProjectFile } from '../services/pythonProjectFiles';

export const PythonProjectExport: React.FC = () => {
  const [selectedFileName, setSelectedFileName] = useState<string>('ble_manager.py');
  const [copied, setCopied] = useState(false);
  const [isZipping, setIsZipping] = useState(false);

  const currentFile = PYTHON_FILES.find(f => f.name === selectedFileName) || PYTHON_FILES[0];

  const handleCopyCode = async () => {
    try {
      await navigator.clipboard.writeText(currentFile.content);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch (e) {
      console.error('Error al copiar:', e);
    }
  };

  const handleDownloadSingleFile = () => {
    const blob = new Blob([currentFile.content], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = currentFile.name;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  };

  const handleDownloadAllZip = async () => {
    setIsZipping(true);
    try {
      const zip = new JSZip();
      
      // Archivos raíz y scripts de Python
      for (const file of PYTHON_FILES) {
        zip.file(file.name, file.content);
      }

      // Añadir la carpeta windows_hud con sus scripts directos
      const hudFolder = zip.folder('windows_hud');
      for (const file of PYTHON_FILES) {
        if (file.name.endsWith('.py') || file.name.endsWith('.bat')) {
          hudFolder?.file(file.name, file.content);
        }
      }

      // Añadir los archivos estáticos HTML/CSS/JS precompilados en dist
      const distFolder = zip.folder('dist');
      const distAssets = distFolder?.folder('assets');
      
      // Descargar o adjuntar index.html precompilado
      try {
        const indexHtmlResp = await fetch('/index.html');
        if (indexHtmlResp.ok) {
          const indexHtmlText = await indexHtmlResp.text();
          distFolder?.file('index.html', indexHtmlText);
        }
      } catch (e) {
        console.warn('Could not fetch index.html for dist folder', e);
      }

      const content = await zip.generateAsync({ type: 'blob' });
      const url = URL.createObjectURL(content);
      const a = document.createElement('a');
      a.href = url;
      a.download = 'Bluetti_Elite100_HUD_Windows.zip';
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error('Error creando archivo ZIP:', err);
    } finally {
      setIsZipping(false);
    }
  };

  return (
    <div className="space-y-6">
      
      {/* Top Banner with 1-Click ZIP Download */}
      <div className="bg-gradient-to-r from-emerald-950/70 via-slate-900 to-slate-900 border border-emerald-500/30 rounded-3xl p-6 backdrop-blur-md">
        <div className="flex flex-col md:flex-row md:items-center justify-between gap-4">
          <div>
            <div className="flex items-center space-x-2 text-emerald-400 font-mono text-xs uppercase tracking-wider mb-1">
              <Code2 className="w-4 h-4" />
              <span>Código Fuente Python Nativo para Windows 10/11</span>
            </div>
            <h2 className="text-xl font-bold text-white">Estructura Modular Completa</h2>
            <p className="text-xs text-slate-300 mt-1 max-w-2xl">
              Aquí tienes los archivos listos para ejecutar en tu PC: <code>ble_manager.py</code> (con Bleak, Modbus RTU, cálculo CRC16 y liberación de Bluetooth) y <code>hud_app.py</code> (interfaz CustomTkinter frameless con always-on-top y switches AC/DC).
            </p>
          </div>

          <button
            onClick={handleDownloadAllZip}
            disabled={isZipping}
            className="flex items-center space-x-2 px-5 py-3 rounded-2xl bg-emerald-600 hover:bg-emerald-500 text-white font-bold text-xs shadow-lg shadow-emerald-600/30 transition-all self-start md:self-auto shrink-0"
          >
            <FolderArchive className="w-4 h-4" />
            <span>{isZipping ? 'Generando ZIP...' : 'Descargar Proyecto Completo (.ZIP)'}</span>
          </button>
        </div>
      </div>

      {/* Beginner Didactic Guide: Why no .exe directly in ZIP */}
      <div className="bg-gradient-to-br from-slate-900 to-slate-950 border border-cyan-500/30 rounded-3xl p-6 backdrop-blur-md space-y-4">
        <div className="flex items-start gap-4">
          <div className="p-3 rounded-2xl bg-cyan-500/10 border border-cyan-500/20 text-cyan-400 shrink-0">
            <Laptop className="w-6 h-6" />
          </div>
          <div className="space-y-1">
            <h3 className="text-base font-bold text-white flex items-center gap-2">
              <span>¿Por qué no veo un archivo .EXE dentro del ZIP y cómo funciona?</span>
              <span className="text-[10px] font-mono px-2 py-0.5 rounded bg-cyan-950 border border-cyan-700/60 text-cyan-300">
                Guía para Principiantes
              </span>
            </h3>
            <p className="text-xs text-slate-300 leading-relaxed">
              En el desarrollo de software, lo que descargaste es el <strong>código fuente</strong> (los archivos <code>.py</code>, las instrucciones y los scripts de automatización). Los navegadores no pueden descargar un <code>.exe</code> precompilado porque pesaría más de 60 MB y el antivirus de Windows lo bloquearía por seguridad.
            </p>
          </div>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 pt-2">
          
          <div className="p-4 rounded-2xl bg-slate-900/90 border border-slate-800 space-y-2">
            <div className="flex items-center gap-2 text-xs font-bold text-amber-400">
              <span className="w-5 h-5 rounded-full bg-amber-500/20 flex items-center justify-center font-mono text-[11px]">
                1
              </span>
              <span>Requisito: Tener Python</span>
            </div>
            <p className="text-[11px] text-slate-400 leading-relaxed">
              Si no tienes Python instalado en Windows:
            </p>
            <ol className="text-[11px] text-slate-300 space-y-1 list-decimal list-inside">
              <li>Descárgalo gratis de <strong className="text-cyan-400">python.org</strong>.</li>
              <li>Al instalar, <strong>MARCA la casilla: "Add python.exe to PATH"</strong>.</li>
            </ol>
          </div>

          <div className="p-4 rounded-2xl bg-slate-900/90 border border-cyan-500/30 space-y-2">
            <div className="flex items-center gap-2 text-xs font-bold text-cyan-400">
              <span className="w-5 h-5 rounded-full bg-cyan-500/20 flex items-center justify-center font-mono text-[11px]">
                2A
              </span>
              <span>Compilar App 100% Idéntica (.EXE)</span>
            </div>
            <p className="text-[11px] text-slate-400 leading-relaxed">
              Haz doble clic en <strong><code>COMPILAR_APP_OFICIAL_EXE.bat</code></strong>.
            </p>
            <p className="text-[11px] text-cyan-300 font-semibold">
              ✓ Crea un archivo .EXE con la interfaz moderna de cristal, colores, gráficos, switches e integración BLE directa.
            </p>
          </div>

          <div className="p-4 rounded-2xl bg-slate-900/90 border border-emerald-500/30 space-y-2">
            <div className="flex items-center gap-2 text-xs font-bold text-emerald-400">
              <span className="w-5 h-5 rounded-full bg-emerald-500/20 flex items-center justify-center font-mono text-[11px]">
                2B
              </span>
              <span>Lanzamiento Rápido en 1 Clic</span>
            </div>
            <p className="text-[11px] text-slate-400 leading-relaxed">
              O haz doble clic en <strong><code>run.bat</code></strong> para abrir el HUD flotante gamer translúcido de inmediato.
            </p>
            <p className="text-[11px] text-emerald-300">
              ✓ Conexión BLE directa con tu SN <code>IoT2545235106072</code> sin tiempos de espera.
            </p>
          </div>

        </div>
      </div>

      {/* Code Viewer & File Switcher */}
      <div className="bg-slate-900/90 border border-slate-800 rounded-3xl overflow-hidden backdrop-blur-md">
        
        {/* File Tabs & Action Header */}
        <div className="bg-slate-950 px-4 py-2 border-b border-slate-800 flex flex-wrap items-center justify-between gap-3">
          
          {/* File Switcher Tabs */}
          <div className="flex items-center space-x-1 overflow-x-auto py-1">
            {PYTHON_FILES.map((file) => {
              const isSelected = file.name === selectedFileName;
              return (
                <button
                  key={file.name}
                  onClick={() => setSelectedFileName(file.name)}
                  className={`flex items-center space-x-2 px-3 py-1.5 rounded-xl text-xs font-mono transition-all ${
                    isSelected
                      ? 'bg-slate-800 text-emerald-400 border border-slate-700 shadow-sm'
                      : 'text-slate-400 hover:text-slate-200 hover:bg-slate-900'
                  }`}
                >
                  <FileCode className="w-3.5 h-3.5" />
                  <span>{file.name}</span>
                </button>
              );
            })}
          </div>

          {/* Actions: Copy & Download Current */}
          <div className="flex items-center space-x-2">
            <button
              onClick={handleCopyCode}
              className="flex items-center space-x-1.5 px-3 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium border border-slate-700 transition-colors"
            >
              {copied ? <Check className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5" />}
              <span>{copied ? '¡Copiado!' : 'Copiar'}</span>
            </button>
            <button
              onClick={handleDownloadSingleFile}
              className="flex items-center space-x-1.5 px-3 py-1.5 rounded-xl bg-slate-800 hover:bg-slate-700 text-slate-300 text-xs font-medium border border-slate-700 transition-colors"
            >
              <Download className="w-3.5 h-3.5" />
              <span>Descargar</span>
            </button>
          </div>
        </div>

        {/* File Description Bar */}
        <div className="px-5 py-2.5 bg-slate-900/60 border-b border-slate-800/80 text-xs text-slate-400 flex items-center justify-between">
          <span>{currentFile.description}</span>
          <span className="font-mono text-[11px] text-slate-500 uppercase">{currentFile.language}</span>
        </div>

        {/* Code Content */}
        <div className="p-4 bg-[#0d1117] max-h-[520px] overflow-y-auto">
          <pre className="font-mono text-xs text-slate-300 leading-relaxed whitespace-pre overflow-x-auto selection:bg-emerald-900 selection:text-emerald-200">
            {currentFile.content}
          </pre>
        </div>

      </div>

      {/* Step-by-Step Windows Instructions */}
      <div className="bg-slate-900/80 border border-slate-800 rounded-3xl p-6 backdrop-blur-md">
        <h3 className="text-base font-bold text-white mb-4 flex items-center gap-2">
          <Terminal className="w-5 h-5 text-emerald-400" />
          <span>Guía Rápida para Windows (3 Pasos)</span>
        </h3>

        <div className="grid grid-cols-1 md:grid-cols-3 gap-4 text-xs">
          
          <div className="p-4 rounded-2xl bg-slate-950 border border-slate-800 space-y-2">
            <span className="w-6 h-6 rounded-full bg-emerald-500/20 text-emerald-400 font-bold flex items-center justify-center font-mono">
              1
            </span>
            <h4 className="font-bold text-white">Instalar Dependencias</h4>
            <p className="text-slate-400">
              Descomprime el ZIP y abre una terminal (CMD o PowerShell) en esa carpeta:
            </p>
            <div className="p-2 bg-slate-900 rounded-lg font-mono text-cyan-300 text-[11px]">
              pip install -r requirements.txt
            </div>
          </div>

          <div className="p-4 rounded-2xl bg-slate-950 border border-slate-800 space-y-2">
            <span className="w-6 h-6 rounded-full bg-emerald-500/20 text-emerald-400 font-bold flex items-center justify-center font-mono">
              2
            </span>
            <h4 className="font-bold text-white">Probar el HUD en Vivo</h4>
            <p className="text-slate-400">
              Asegúrate de tener Bluetooth encendido en Windows y ejecuta:
            </p>
            <div className="p-2 bg-slate-900 rounded-lg font-mono text-cyan-300 text-[11px]">
              python hud_app.py
            </div>
            <p className="text-[11px] text-slate-500">
              (O haz doble clic sobre <code>run.bat</code>)
            </p>
          </div>

          <div className="p-4 rounded-2xl bg-slate-950 border border-slate-800 space-y-2">
            <span className="w-6 h-6 rounded-full bg-emerald-500/20 text-emerald-400 font-bold flex items-center justify-center font-mono">
              3
            </span>
            <h4 className="font-bold text-white">Crear el .EXE Autónomo</h4>
            <p className="text-slate-400">
              Para tener un ejecutable sin consola negra que puedas iniciar con Windows:
            </p>
            <div className="p-2 bg-slate-900 rounded-lg font-mono text-cyan-300 text-[11px]">
              build_exe.bat
            </div>
            <p className="text-[11px] text-slate-500">
              Genera <code>dist\BluettiEliteHUD\BluettiEliteHUD.exe</code>
            </p>
          </div>

        </div>
      </div>

    </div>
  );
};
