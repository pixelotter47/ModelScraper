@echo off
setlocal
cd /d "%~dp0"

set "PYTHON_EXE=%CD%\.venv\Scripts\python.exe"

if not exist "%PYTHON_EXE%" (
  echo ModelScraper environment is missing.
  echo Run: powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
  pause
  exit /b 1
)

if not exist "%PYTHON_EXE%" (
  echo Could not find or create .venv\Scripts\python.exe
  pause
  exit /b 1
)

"%PYTHON_EXE%" -c "import fastapi, uvicorn, httpx, pydantic" >nul 2>&1
if errorlevel 1 (
  echo Required packages are missing.
  echo Run: powershell -ExecutionPolicy Bypass -File scripts\bootstrap.ps1
  pause
  exit /b 1
)

if "%MODEL_SCRAPER_WEB_PORT%"=="" (
  for /f %%P in ('powershell -NoProfile -Command "$p=8788; while($p -lt 8800){$c=New-Object Net.Sockets.TcpClient; try{$c.Connect('127.0.0.1',$p); $c.Close(); $p++} catch {try{$c.Close()}catch{}; Write-Output $p; break}}"') do set "MODEL_SCRAPER_WEB_PORT=%%P"
)

echo Starting ModelScraper Web on http://127.0.0.1:%MODEL_SCRAPER_WEB_PORT%/
start "" powershell -NoProfile -ExecutionPolicy Bypass -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:%MODEL_SCRAPER_WEB_PORT%/'"
"%PYTHON_EXE%" web_app.py

pause
