import React from 'react';
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  Bluetooth,
  BookOpen,
  CheckCircle2,
  Cpu,
  Download,
  FolderCheck,
  HelpCircle,
  Laptop,
  Play,
  RotateCw,
  ShieldCheck,
  Smartphone,
  Sparkles,
  Terminal,
  Zap
} from 'lucide-react';

interface UserGuideProps {
  onGoToPythonTab: () => void;
  onGoToDashboard: () => void;
}

export const UserGuide: React.FC<UserGuideProps> = ({
  onGoToPythonTab,
  onGoToDashboard
}) => {
  return (
    <div className="space-y-6">
      
      {/* Hero Header */}
      <div className="bg-gradient-to-r from-blue-950/70 via-slate-900 to-indigo-950/70 border border-blue-500/30 rounded-3xl p-6 backdrop-blur-md">
        <div className="flex items-center space-x-2 text-cyan-400 font-mono text-xs uppercase tracking-wider mb-2">
          <BookOpen className="w-4 h-4" />
          <span>Manual de Operación • Bluetti Elite 100 V2 (120V 60Hz)</span>
        </div>
        <h2 className="text-2xl font-bold text-white">¿Cómo Funciona la App y Cómo se Empareja?</h2>
        <p className="text-sm text-slate-300 mt-2 max-w-3xl leading-relaxed">
          Respuestas directas a las dudas de conectividad, proceso de emparejamiento Bluetooth, formato <strong>Portable vs Instalador</strong> y cómo convive con la app de tu teléfono móvil.
        </p>
      </div>

      {/* 3 Main Pillars: Emparejamiento, Portable, Funcionamiento */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
        
        {/* Card 1: ¿Cómo se empareja? */}
        <div className="bg-slate-900/80 border border-slate-800 rounded-3xl p-6 backdrop-blur-md flex flex-col justify-between space-y-4">
          <div>
            <div className="w-12 h-12 rounded-2xl bg-blue-500/10 border border-blue-500/20 flex items-center justify-center text-blue-400 mb-4">
              <Bluetooth className="w-6 h-6" />
            </div>
            <h3 className="text-lg font-bold text-white mb-2">¿Cómo se Empareja?</h3>
            <p className="text-xs text-slate-300 leading-relaxed">
              <strong>NO necesitas emparejarla primero en Windows</strong> (no hay que ir al panel de configuración de Windows ni ingresar PINs).
            </p>
            <ul className="mt-3 space-y-2 text-xs text-slate-400">
              <li className="flex items-start gap-2">
                <CheckCircle2 className="w-4 h-4 text-emerald-400 shrink-0 mt-0.5" />
                <span>La Bluetti emite balizas BLE automáticas cuando está encendida.</span>
              </li>
              <li className="flex items-start gap-2">
                <CheckCircle2 className="w-4 h-4 text-emerald-400 shrink-0 mt-0.5" />
                <span>La app en Windows (o Chrome/Edge) escanea directamente el aire y se conecta en 2 segundos por GATT.</span>
              </li>
              <li className="flex items-start gap-2">
                <CheckCircle2 className="w-4 h-4 text-emerald-400 shrink-0 mt-0.5" />
                <span>Solo debes tener el Bluetooth de tu PC encendido.</span>
              </li>
            </ul>
          </div>
          <div className="p-3 bg-slate-950 rounded-2xl border border-slate-800/80 text-[11px] font-mono text-cyan-300">
            UUID: 0000ff00... (GATT Service)
          </div>
        </div>

        {/* Card 2: ¿Portable o Instalación? */}
        <div className="bg-slate-900/80 border border-slate-800 rounded-3xl p-6 backdrop-blur-md flex flex-col justify-between space-y-4">
          <div>
            <div className="w-12 h-12 rounded-2xl bg-emerald-500/10 border border-emerald-500/20 flex items-center justify-center text-emerald-400 mb-4">
              <FolderCheck className="w-6 h-6" />
            </div>
            <h3 className="text-lg font-bold text-white mb-2">¿Es Portable o Instalador?</h3>
            <p className="text-xs text-slate-300 leading-relaxed">
              <strong>¡Es 100% PORTABLE!</strong> No requiere instalación en el sistema operativo.
            </p>
            <ul className="mt-3 space-y-2 text-xs text-slate-400">
              <li className="flex items-start gap-2">
                <CheckCircle2 className="w-4 h-4 text-emerald-400 shrink-0 mt-0.5" />
                <span><strong>Cero basura en el registro</strong>: No instala servicios ni controladores invasivos.</span>
              </li>
              <li className="flex items-start gap-2">
                <CheckCircle2 className="w-4 h-4 text-emerald-400 shrink-0 mt-0.5" />
                <span>Puedes llevar la carpeta en una memoria USB o dejarla en tu escritorio.</span>
              </li>
              <li className="flex items-start gap-2">
                <CheckCircle2 className="w-4 h-4 text-emerald-400 shrink-0 mt-0.5" />
                <span>Doble clic en <code className="text-emerald-400">BluettiEliteHUD.exe</code> y listo.</span>
              </li>
            </ul>
          </div>
          <button
            onClick={onGoToPythonTab}
            className="w-full py-2.5 px-3 rounded-xl bg-emerald-600 hover:bg-emerald-500 text-white font-bold text-xs flex items-center justify-center gap-2 transition-colors shadow-lg shadow-emerald-600/20"
          >
            <Download className="w-3.5 h-3.5" />
            <span>Descargar Archivos Python & .EXE</span>
          </button>
        </div>

        {/* Card 3: El botón "Liberar BLE" para el Móvil */}
        <div className="bg-slate-900/80 border border-slate-800 rounded-3xl p-6 backdrop-blur-md flex flex-col justify-between space-y-4">
          <div>
            <div className="w-12 h-12 rounded-2xl bg-purple-500/10 border border-purple-500/20 flex items-center justify-center text-purple-400 mb-4">
              <Smartphone className="w-6 h-6" />
            </div>
            <h3 className="text-lg font-bold text-white mb-2">Uso con el Teléfono Móvil</h3>
            <p className="text-xs text-slate-300 leading-relaxed">
              La estación solo permite <strong>1 enlace Bluetooth activo</strong> a la vez en todo momento.
            </p>
            <ul className="mt-3 space-y-2 text-xs text-slate-400">
              <li className="flex items-start gap-2">
                <CheckCircle2 className="w-4 h-4 text-purple-400 shrink-0 mt-0.5" />
                <span>Si el PC está conectado, tu móvil mostrará "dispositivo ocupado".</span>
              </li>
              <li className="flex items-start gap-2">
                <CheckCircle2 className="w-4 h-4 text-purple-400 shrink-0 mt-0.5" />
                <span>Para usar el móvil, pulsa <strong>"⏸ Liberar Bluetooth"</strong> en el HUD.</span>
              </li>
              <li className="flex items-start gap-2">
                <CheckCircle2 className="w-4 h-4 text-purple-400 shrink-0 mt-0.5" />
                <span>El PC corta el enlace inmediatamente y tu teléfono conecta en 1 segundo.</span>
              </li>
            </ul>
          </div>
          <div className="p-3 bg-purple-950/40 rounded-2xl border border-purple-800/60 text-[11px] text-purple-300 font-medium">
            Pulsa "▶ Reconectar PC" al terminar en el móvil
          </div>
        </div>

      </div>

      {/* Verified Technical Specs Section */}
      <div className="bg-slate-900/80 border border-slate-800 rounded-3xl p-6 backdrop-blur-md space-y-4">
        <div className="flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <div className="p-2.5 rounded-2xl bg-cyan-500/10 border border-cyan-500/20 text-cyan-400">
              <Zap className="w-5 h-5" />
            </div>
            <div>
              <h3 className="text-base font-bold text-white">Especificaciones Técnicas Verificadas (Bluetti Elite 100 V2)</h3>
              <p className="text-xs text-slate-400">Datos oficiales comprobados con el modelo para América (120V / 60Hz)</p>
            </div>
          </div>
          <span className="hidden sm:inline-flex px-3 py-1 rounded-full bg-slate-950 border border-slate-800 text-[11px] font-mono text-cyan-400">
            LiFePO4 1024Wh
          </span>
        </div>

        <div className="grid grid-cols-1 md:grid-cols-4 gap-3 text-xs font-mono">
          <div className="p-3.5 rounded-2xl bg-slate-950 border border-amber-500/20">
            <span className="text-[10px] text-slate-400 uppercase block font-sans">Entrada Solar MPPT</span>
            <span className="text-lg font-bold text-amber-400">1,000 W <span className="text-xs font-normal">Max</span></span>
            <p className="text-[11px] text-slate-400 font-sans mt-1">12V - 60V DC, hasta 20A Max. Carga 100% en ~70 minutos.</p>
          </div>

          <div className="p-3.5 rounded-2xl bg-slate-950 border border-blue-500/20">
            <span className="text-[10px] text-slate-400 uppercase block font-sans">Entrada Red AC (Turbo)</span>
            <span className="text-lg font-bold text-blue-400">1,200 W <span className="text-xs font-normal">Max</span></span>
            <p className="text-[11px] text-slate-400 font-sans mt-1">TurboBoost: 0 a 80% en 45 min; 100% en 70 min. 120V nominal.</p>
          </div>

          <div className="p-3.5 rounded-2xl bg-slate-950 border border-rose-500/20">
            <span className="text-[10px] text-slate-400 uppercase block font-sans">Inversor AC Onda Pura</span>
            <span className="text-lg font-bold text-rose-400">1,800 W <span className="text-xs font-normal">Continuo</span></span>
            <p className="text-[11px] text-slate-400 font-sans mt-1">120V 60Hz. Lifting Power 2700W, sobretensión hasta 3600W.</p>
          </div>

          <div className="p-3.5 rounded-2xl bg-slate-950 border border-emerald-500/20">
            <span className="text-[10px] text-slate-400 uppercase block font-sans">Función SAI / UPS</span>
            <span className="text-lg font-bold text-emerald-400">&le; 10 ms</span>
            <p className="text-[11px] text-slate-400 font-sans mt-1">Conmutación ultrarrápida para evitar reinicios en tu PC gaming.</p>
          </div>
        </div>

        {/* Real-time Current Calculation Notice */}
        <div className="p-4 rounded-2xl bg-slate-950/80 border border-slate-800 text-xs flex items-start gap-3">
          <div className="p-1.5 rounded-lg bg-cyan-500/10 text-cyan-400 shrink-0">
            <Activity className="w-4 h-4" />
          </div>
          <div>
            <strong className="text-slate-200 block mb-0.5">Cálculo de Amperaje en Entradas ($I = P / V$)</strong>
            <p className="text-slate-400 leading-relaxed">
              Tanto en esta aplicación web como en el HUD de escritorio en Python, se mide el voltaje de entrada ($V$) y la potencia entregada ($W$), calculando automáticamente la corriente en Amperios ($A = W / V$) con precisión decimal, exactamente igual a como lo visualiza la app móvil oficial de Bluetti cuando consultas el estado de los paneles solares o de la red eléctrica.
            </p>
          </div>
        </div>
      </div>

      {/* Step-by-Step Practical Workflow */}
      <div className="bg-slate-900/80 border border-slate-800 rounded-3xl p-6 backdrop-blur-md space-y-6">
        <h3 className="text-lg font-bold text-white flex items-center gap-2">
          <Terminal className="w-5 h-5 text-cyan-400" />
          <span>Paso a Paso: Cómo Ponerla a Funcionar en 3 Minutos</span>
        </h3>

        <div className="grid grid-cols-1 md:grid-cols-4 gap-4 text-xs">
          
          <div className="p-4 bg-slate-950 rounded-2xl border border-slate-800 relative">
            <span className="w-6 h-6 rounded-full bg-blue-500/20 text-blue-400 font-bold flex items-center justify-center font-mono mb-3">
              1
            </span>
            <h4 className="font-bold text-white mb-1">Encender Bluetti & Bluetooth</h4>
            <p className="text-slate-400">
              Enciende tu Bluetti Elite 100 V2 (pantalla activa) y verifica que el Bluetooth del PC esté activado.
            </p>
          </div>

          <div className="p-4 bg-slate-950 rounded-2xl border border-slate-800 relative">
            <span className="w-6 h-6 rounded-full bg-blue-500/20 text-blue-400 font-bold flex items-center justify-center font-mono mb-3">
              2
            </span>
            <h4 className="font-bold text-white mb-1">Descargar el ZIP del HUD</h4>
            <p className="text-slate-400">
              Ve a la pestaña <strong>"Código Python (.exe)"</strong> y haz clic en el botón verde <strong>"Descargar Proyecto Completo (.ZIP)"</strong>.
            </p>
            <button
              onClick={onGoToPythonTab}
              className="mt-3 inline-flex items-center space-x-1.5 px-3 py-1.5 rounded-xl bg-emerald-600 hover:bg-emerald-500 text-white font-semibold text-[11px] shadow-sm shadow-emerald-600/30 transition-all"
            >
              <span>Abrir Pestaña Python</span>
              <ArrowRight className="w-3 h-3" />
            </button>
          </div>

          <div className="p-4 bg-slate-950 rounded-2xl border border-slate-800 relative">
            <span className="w-6 h-6 rounded-full bg-blue-500/20 text-blue-400 font-bold flex items-center justify-center font-mono mb-3">
              3
            </span>
            <h4 className="font-bold text-white mb-1">Doble clic en build_exe.bat</h4>
            <p className="text-slate-400">
              El script descarga las librerías y compila todo solo. Solo requiere tener Python instalado previamente en tu Windows.
            </p>
          </div>

          <div className="p-4 bg-slate-950 rounded-2xl border border-emerald-500/20 relative">
            <span className="w-6 h-6 rounded-full bg-emerald-500/20 text-emerald-400 font-bold flex items-center justify-center font-mono mb-3">
              4
            </span>
            <h4 className="font-bold text-white mb-1">¡Listo! Tu .EXE Independiente</h4>
            <p className="text-slate-400">
              Se creará la carpeta <code>dist/BluettiEliteHUD/</code> con tu ejecutable. ¡Ese .exe ya no necesita Python y puedes llevarlo donde quieras!
            </p>
          </div>

        </div>

        {/* Detailed FAQ: ¿Necesito programas extras para compilar? */}
        <div className="p-5 rounded-2xl bg-gradient-to-r from-slate-950 to-blue-950/40 border border-blue-500/30 text-xs space-y-3">
          <div className="flex items-center space-x-2 text-cyan-400 font-semibold">
            <HelpCircle className="w-4 h-4 shrink-0" />
            <span className="text-sm">¿Cómo funciona la compilación? ¿Necesito programas extras?</span>
          </div>
          <div className="space-y-2 text-slate-300 leading-relaxed">
            <p>
              <strong>1. ¿Qué es lo único que necesitas en tu PC?:</strong> Solo necesitas tener <strong>Python</strong> instalado (es gratuito desde <a href="https://www.python.org/downloads/" target="_blank" rel="noreferrer" className="text-cyan-400 underline hover:text-cyan-300">python.org</a>). Al instalarlo, solo asegúrate de marcar la casilla <em>"Add python.exe to PATH"</em>.
            </p>
            <p>
              <strong>2. No necesitas nada más:</strong> No requieres Visual Studio, ni compiladores de C++, ni herramientas complejas. El archivo <code>build_exe.bat</code> incluido en el ZIP se encarga de todo de forma 100% automatizada:
            </p>
            <ul className="list-disc list-inside space-y-1 text-slate-400 pl-2">
              <li>Instala automáticamente las dependencias (<code>bleak</code> para Bluetooth, <code>customtkinter</code> para la interfaz y <code>pyinstaller</code> para empaquetar).</li>
              <li>Empaqueta el código en una carpeta con el archivo <strong>BluettiEliteHUD.exe</strong>.</li>
            </ul>
            <p className="pt-1 text-emerald-300 font-medium">
              💡 <strong>Ventaja del .exe resultante:</strong> Una vez compilado, el ejecutable generado en la carpeta <code>dist/</code> es totalmente autónomo. Puedes copiar esa carpeta a cualquier otra PC con Windows 10 u 11 y funcionará de inmediato con doble clic, ¡sin necesidad de instalar Python en esas otras máquinas!
            </p>
          </div>
        </div>

        {/* Tip: Autostart with Windows */}
        <div className="p-4 rounded-2xl bg-cyan-950/30 border border-cyan-800/40 text-xs text-cyan-200 flex items-start gap-3">
          <Sparkles className="w-5 h-5 text-cyan-400 shrink-0 mt-0.5" />
          <div>
            <strong className="text-white block mb-0.5">Consejo: ¿Quieres que el HUD arranque automáticamente al encender tu PC?</strong>
            <span>
              Presiona <kbd className="px-1.5 py-0.5 rounded bg-slate-900 border border-slate-700 font-mono text-[10px]">Win + R</kbd>, escribe <code className="font-mono text-white">shell:startup</code> y pulsa Enter. Luego pega allí un acceso directo a tu <code>BluettiEliteHUD.exe</code>. ¡El HUD se abrirá silenciosamente cada vez que inicies Windows!
            </span>
          </div>
        </div>

      </div>

    </div>
  );
};
