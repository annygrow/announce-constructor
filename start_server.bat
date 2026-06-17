@echo off
cd /d "%~dp0"
start "Announce Server" python app.py
timeout /t 3 /nobreak > nul
start "" http://localhost:5000
