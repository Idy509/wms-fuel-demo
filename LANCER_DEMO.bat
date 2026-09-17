@echo off
title WMS Demo Fuel
echo ========================================
echo   WMS Demo - Fuel Management
echo ========================================
echo.

cd /d "%~dp0"

:: Serveur : base demo, mode demo, port 8001
set WMS_DB_PATH=data/demo_fuel/demo_fuel.db
set WMS_DEMO_MODE=1
set WMS_PORT=8001
set WMS_HOST=127.0.0.1

:: Client : config isolee dans le dossier demo (pas dans AppData)
set WMS_CLIENT_DIR=%~dp0config_client

echo Demarrage du serveur demo (port 8001)...
start /B "" venv\Scripts\python.exe server\lancer_serveur.py

echo Attente du serveur...
timeout /t 5 /nobreak > nul

echo Lancement du client...
cd client
..\venv\Scripts\python.exe main.py

echo.
echo Demo terminee.
pause
