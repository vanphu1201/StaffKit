@echo off
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -m engine.cli ui --config config.yaml
) else (
    python -m engine.cli ui --config config.yaml
)
if errorlevel 1 pause
