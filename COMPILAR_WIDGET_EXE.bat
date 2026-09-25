@echo off
title Compilar Bluetti Widget Minimalista a EXE
color 0A
cls
echo ====================================================================
echo   COMPILANDO WIDGET MINIMALISTA A EJECUTABLE .EXE INDEPENDIENTE
echo ====================================================================
echo.

python -m pip install --upgrade pip
python -m pip install pyinstaller cryptography bleak pyasn1

echo.
echo Compilando ejecutable standalone...
python -m PyInstaller --clean --noconfirm --onefile --windowed --name "Bluetti_Widget" widget_minimalista.py

if %errorlevel% neq 0 (
    echo.
    echo Reintentando compilacion en modo carpeta...
    python -m PyInstaller --clean --noconfirm --onedir --windowed --name "Bluetti_Widget" widget_minimalista.py
)

echo.
echo ====================================================================
echo   COMPILACION COMPLETADA
echo   El archivo EXE se encuentra en la carpeta 'dist'
echo ====================================================================
pause
