@echo off
rem Install Python dependencies for NeedZeroXtraTrash (run once).
cd /d "%~dp0"
echo Installing dependencies...
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
echo.
echo Fetching LibreHardwareMonitor sensor library...
python fetch_deps.py
echo.
echo Done. Use run.bat to start from source, or build.bat to make the .exe.
pause
