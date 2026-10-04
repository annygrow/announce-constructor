@echo off
cd /d "%~dp0"

rem agent_guard proxy (port 8081) — Playwright's GC-push browser always routes through
rem it, even locally; without it, push-to-mail fails with ERR_PROXY_CONNECTION_FAILED.
netstat -ano | findstr ":8081" | findstr "LISTENING" >nul
if errorlevel 1 (
    start "Agent Guard Proxy" cmd /c "cd /d "%~dp0agent_guard" && mitmdump -s block_gc.py --listen-port 8081 --allow-hosts university\.zerocoder\.ru"
)

start "Announce Server" python app.py
timeout /t 3 /nobreak > nul
start "" http://localhost:5000
