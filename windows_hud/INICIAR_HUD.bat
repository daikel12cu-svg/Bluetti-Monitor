@echo off
title Bluetti Elite 100 V2 - HUD Minimalista
color 0B
cls
echo ====================================================================
echo   INICIANDO HUD MINIMALISTA BLUETTI ELITE 100 V2
echo ====================================================================
echo.
python hud_app.py
if %errorlevel% neq 0 (
    py hud_app.py
)
pause
