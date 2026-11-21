@echo off

cd /d "%~dp0"

echo Running "bb-video-wallpaper-preview.py"...

if not exist ".venv" (
    echo .venv not found. Creating virtual environment...

    python -m venv .venv
    .venv\Scripts\pip.exe install -r requirements.txt
)

.venv\Scripts\python.exe bb-video-wallpaper-preview.py

echo.
echo Application exited.
pause
