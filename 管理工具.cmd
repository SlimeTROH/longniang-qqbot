@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist "%~dp0scripts\manage.ps1" (
    echo Missing launcher: scripts\manage.ps1
    pause
    exit /b 1
)
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\manage.ps1" %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" if "%~1"=="" pause
exit /b %RC%
