@echo off
title Compilador Bluetti Elite 100 V2 - HUD
echo ========================================================
echo   Iniciando compilador desde la carpeta windows_hud...
echo ========================================================
cd /d "%~dp0windows_hud"
call build_exe.bat
