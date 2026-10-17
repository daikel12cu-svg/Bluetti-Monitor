@echo off
title Compilador Nativo Windows - Bluetti Elite 100 V2 (version ligera)
color 0B
cls
echo ====================================================================
echo      COMPILADOR NATIVO OFICIAL (.EXE) - VERSION LIGERA
echo      Entorno virtual propio + sin --collect-all innecesarios
echo ====================================================================
echo.

REM --- 1) Entorno virtual SOLO para compilar: no arrastra librerias instaladas en el Python global
if not exist ".venv_build\Scripts\python.exe" (
    echo [1/3] Creando entorno virtual de compilacion ^(.venv_build^)...
    python -m venv .venv_build
    if errorlevel 1 (
        echo [ERROR] No se pudo crear el entorno virtual.
        pause
        exit /b 1
    )
)
set "PY=.venv_build\Scripts\python.exe"
echo Python que se va a usar:
%PY% --version
echo.
echo [2/3] Instalando SOLO las dependencias de la app en el entorno de compilacion...
%PY% -m pip install --upgrade pip
%PY% -m pip install --upgrade pyinstaller bleak cryptography pyasn1 customtkinter pystray pillow aiohttp
echo.

echo [3/3] Generando ejecutable ^(Bluetti_Elite100_Control.exe^)...
%PY% -m PyInstaller --noconfirm --clean --onefile --noconsole --name "Bluetti_Elite100_Control" --icon="bluetti.ico" --add-data "bluetti.ico;." --add-data "bluetti_device_licence.csv;." --collect-data customtkinter --collect-submodules pystray --hidden-import PIL --hidden-import telemetry_server --exclude-module numpy --exclude-module pandas --exclude-module matplotlib --exclude-module scipy --exclude-module PyQt5 --exclude-module PyQt6 --exclude-module PySide2 --exclude-module PySide6 --exclude-module IPython --exclude-module pytest --exclude-module tkinter.test "hud_app.py"

if exist "dist\Bluetti_Elite100_Control.exe" (
    copy /y "dist\Bluetti_Elite100_Control.exe" "Bluetti_Elite100_Control.exe" >nul
)

if exist "Bluetti_Elite100_Control.exe" (
    cls
    color 0A
    echo ====================================================================
    echo             COMPILACION COMPLETADA CON EXITO!
    echo ====================================================================
    echo.
    for %%A in ("Bluetti_Elite100_Control.exe") do echo Tamano del ejecutable: %%~zA bytes
    echo.
    echo Si algo falla al abrirlo, revisa: build\Bluetti_Elite100_Control\warn-Bluetti_Elite100_Control.txt
    echo ^(busca lineas "missing module" de librerias que la app use de verdad^).
    echo.
    echo Iniciando aplicacion...
    start "" "Bluetti_Elite100_Control.exe"
) else (
    echo.
    echo [ERROR] No se pudo generar el ejecutable.
)
echo.
pause
