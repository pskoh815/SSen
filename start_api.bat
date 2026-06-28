@echo off
REM SSen Dashboard - FastAPI server launcher
REM Double-click this file, or run "start_api.bat" from cmd.

cd /d "%~dp0"

echo === Checking FastAPI server status... ===
curl -s -o nul -w "%%{http_code}" http://127.0.0.1:8000/docs > "%TEMP%\ssen_api_status.txt" 2>nul
set /p STATUS=<"%TEMP%\ssen_api_status.txt"
del "%TEMP%\ssen_api_status.txt" >nul 2>nul

if "%STATUS%"=="200" (
    echo Already running at http://localhost:8000/docs
    goto :end
)

echo === Starting FastAPI server in background... ===
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" api_bg

echo.
echo Swagger UI : http://localhost:8000/docs
echo Dashboard  : http://localhost:8000/dashboard

:end
echo.
pause
