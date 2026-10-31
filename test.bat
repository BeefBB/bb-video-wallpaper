@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv" (
    echo .venv not found. Creating virtual environment...

    python -m venv .venv
    .venv\Scripts\pip.exe install -r requirements.txt
)

echo Running tests...
echo.

.venv\Scripts\python.exe -m unittest discover -s tests -v
set "TEST_EXIT_CODE=%errorlevel%"

echo.
if "%TEST_EXIT_CODE%"=="0" (
    echo Tests completed successfully. Check above for skipped tests.
) else (
    echo Tests failed.
)

pause
exit /b %TEST_EXIT_CODE%
