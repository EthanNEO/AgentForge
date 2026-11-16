@echo off
chcp 65001 >nul
setlocal
title AgentForge
cd /d "%~dp0backend"

set "PY=python"
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"

echo Starting AgentForge...
"%PY%" -m app.launcher --port 8000
if errorlevel 1 (
    echo.
    echo AgentForge failed to start. See the message above.
    pause
    exit /b 1
)
echo.
echo AgentForge is running in the background. This window can be closed.
exit /b 0
