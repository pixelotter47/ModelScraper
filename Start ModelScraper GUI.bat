@echo off
setlocal
cd /d "%~dp0"

set "PYTHONW_EXE=%CD%\.venv\Scripts\pythonw.exe"

if not exist "%PYTHONW_EXE%" (
  echo ModelScraper GUI could not start because the local Python environment is missing.
  echo.
  echo Expected file:
  echo   %PYTHONW_EXE%
  echo.
  echo Run: powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
  pause
  exit /b 1
)

"%CD%\.venv\Scripts\python.exe" -c "import PySide6, selenium, undetected_chromedriver" >nul 2>&1
if errorlevel 1 (
  echo Required packages are missing.
  echo Run: powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
  pause
  exit /b 1
)

start "ModelScraper GUI" /D "%CD%" "%PYTHONW_EXE%" "%CD%\gui_app.py"
exit /b 0
