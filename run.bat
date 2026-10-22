@echo off
rem Run NeedZeroXtraTrash from source. Run install.bat once first.
rem Tip: run this file "as administrator" so CPU/chipset sensors can be read.
cd /d "%~dp0"
set PYTHONPATH=%~dp0src
python -m needzero %*
