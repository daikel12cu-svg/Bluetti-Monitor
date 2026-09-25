@echo off
title Bluetti Elite 100 V2 - Widget Minimalista Flotante
color 0B
cls
echo ====================================================================
echo   INICIANDO WIDGET MINIMALISTA FLOTANTE (ALWAYS-ON-TOP)
echo ====================================================================
echo.
python widget_minimalista.py
if %errorlevel% neq 0 (
    py widget_minimalista.py
)
