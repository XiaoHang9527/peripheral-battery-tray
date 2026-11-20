@echo off
setlocal
set "PBT_PYTHONW=%~dp0.venv\Scripts\pythonw.exe"
if exist "%~dp0.data\portable-cpython-312\python\pythonw.exe" set "PBT_PYTHONW=%~dp0.data\portable-cpython-312\python\pythonw.exe"
if not exist "%PBT_PYTHONW%" (
    echo Python environment not found. Create .venv and install requirements.txt as shown in README.md.
    pause
    exit /b 1
)
if not exist "%~dp0peripheral_battery_tray.py" (
    echo Application files not found. Keep this launcher in the project folder.
    pause
    exit /b 1
)
start "" /D "%~dp0." "%PBT_PYTHONW%" -B "%~dp0peripheral_battery_tray.py"
