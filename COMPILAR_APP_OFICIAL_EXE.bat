@echo off
title Compilador Nativo Windows - Bluetti Elite 100 V2
color 0B
cls
echo ====================================================================
echo      COMPILADOR NATIVO OFICIAL APLICACION WINDOWS (.EXE)
echo      BLUETTI ELITE 100 V2 - ALTO RENDIMIENTO (0%% NAVEGADOR)
echo ====================================================================
echo.

echo [1/2] Verificando e instalando dependencias de interfaz y Bluetooth...
python -m pip install --quiet --upgrade pip
python -m pip install --quiet --upgrade pyinstaller bleak cryptography pyasn1 customtkinter

echo.
echo [2/2] Generando ejecutable nativo puro (Bluetti_Elite100_Control.exe)...
python -m PyInstaller --noconfirm --clean --onefile --noconsole --name "Bluetti_Elite100_Control" --add-data "bluetti_device_licence.csv;." --collect-all customtkinter --collect-all cryptography "hud_app.py"

if exist "dist\Bluetti_Elite100_Control.exe" (
    copy /y "dist\Bluetti_Elite100_Control.exe" "Bluetti_Elite100_Control.exe" >nul
)

if exist "Bluetti_Elite100_Control.exe" (
    cls
    color 0A
    echo ====================================================================
    echo             COMPILACION NATIVA COMPLETADA CON EXITO!
    echo ====================================================================
    echo.
    echo Tu aplicacion nativa de escritorio (con Panel Completo + HUD Gamer
    echo flotante, 0%% dependencias de navegadores y control instantaneo):
    echo.
    echo       ==^> Bluetti_Elite100_Control.exe
    echo.
    echo Iniciando aplicacion...
    start "" "Bluetti_Elite100_Control.exe"
) else (
    echo.
    echo [ERROR] No se pudo generar el ejecutable.
)
echo.
pause
