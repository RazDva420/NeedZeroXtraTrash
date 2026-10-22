@echo off
setlocal
cd /d "%~dp0"

set "BUILD_MODE=%~1"
if not defined BUILD_MODE set "BUILD_MODE=DEV"
if /I not "%BUILD_MODE%"=="DEV" if /I not "%BUILD_MODE%"=="RELEASE" (
  echo Usage: build.bat [DEV^|RELEASE]
  exit /b 2
)

if defined NEEDZERO_PYTHON (
  if not exist "%NEEDZERO_PYTHON%" (
    echo NEEDZERO_PYTHON does not point to an existing CPython 3.12.14 x64 executable.
    exit /b 2
  )
  "%NEEDZERO_PYTHON%" build_release.py --mode %BUILD_MODE%
  exit /b %ERRORLEVEL%
)

where py.exe >nul 2>nul
if errorlevel 1 (
  echo CPython 3.12.14 x64 was not found. Set NEEDZERO_PYTHON to its absolute python.exe path.
  exit /b 2
)
py.exe -3.12 build_release.py --mode %BUILD_MODE%
exit /b %ERRORLEVEL%
