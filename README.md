# Bluetti Elite 100 V2 - HUD Gamer & Controller (Windows 10/11)

Este proyecto incluye tanto la aplicación Web (Vite + React + Web Bluetooth) como la **aplicación nativa de escritorio para Windows** (Python + CustomTkinter + Bleak).

---

## 🚀 ¿Cómo usar el HUD Gamer Flotante en Windows?

Si descargaste este proyecto como archivo ZIP desde Google AI Studio:

### Opción 1: Compilar a Ejecutable `.EXE` (Recomendado)
1. Instala Python desde [python.org](https://www.python.org/downloads/) (marca la casilla *"Add python.exe to PATH"*).
2. Haz doble clic en el archivo:
   👉 **`COMPILAR_HUD_WINDOWS.bat`**
3. El instalador descargará automáticamente las dependencias y creará tu archivo ejecutable en:
   `windows_hud\dist\BluettiEliteHUD\BluettiEliteHUD.exe`

### Opción 2: Probar directamente sin compilar
1. Haz doble clic en el archivo:
   👉 **`EJECUTAR_HUD_WINDOWS.bat`**

---

## 📁 Archivos del HUD de Windows
Todos los archivos fuente de la versión de escritorio se encuentran en la carpeta **`windows_hud/`**:
- `windows_hud/ble_manager.py`: Comunicación Bluetooth Low Energy con la estación y protocolo Modbus RTU.
- `windows_hud/hud_app.py`: Ventana flotante gamer estilo Afterburner a 1 píxel del borde de pantalla.
- `windows_hud/requirements.txt`: Dependencias (`bleak`, `customtkinter`, `pyinstaller`).
- `windows_hud/build_exe.bat`: Script de compilación.
- `windows_hud/run.bat`: Script lanzador rápido.
