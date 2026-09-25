@echo off
title Bluetti Elite 100 V2 - Centro de Control Nativo Windows
echo ====================================================================
echo   BLUETTI ELITE 100 V2 - CENTRO DE CONTROL NATIVO OFICIAL
echo ====================================================================
echo.
echo [1/2] Verificando dependencias nativas de interfaz...
python -m pip install --quiet bleak cryptography pyasn1 customtkinter 2>nul
if %errorlevel% neq 0 (
    py -m pip install --quiet bleak cryptography pyasn1 customtkinter 2>nul
)

echo [2/2] Iniciando Aplicacion Nativa Oficial de Alto Rendimiento...
echo       (Panel Completo de Control + HUD Gamer Flotante)
echo.

if exist "hud_app.py" (
    python hud_app.py
    if %errorlevel% neq 0 (
        py hud_app.py
    )
    exit /b
)

if exist "windows_hud\hud_app.py" (
    cd /d "%~dp0windows_hud"
    python hud_app.py
    exit /b
)

pause
