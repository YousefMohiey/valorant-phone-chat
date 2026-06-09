@echo off
title Valorant Phone Chat Bridge
setlocal enabledelayedexpansion

:: Set UTF-8 encoding to avoid Unicode issues
chcp 65001 >nul

:: Log file — captures everything for debugging
set LOGFILE=%~dp0bridge-run.log
echo VALORANT PHONE CHAT BRIDGE — Run Log > "%LOGFILE%"
echo Started: %date% %time% >> "%LOGFILE%"
echo. >> "%LOGFILE%"

echo.
echo   VALORANT PHONE CHAT BRIDGE
echo   ==========================
echo.

:: ── Check Python ──────────────────────────────────────────────
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo   [ERROR] Python is not installed or not in PATH. >> "%LOGFILE%"
    echo.
    echo   [ERROR] Python is not installed or not in PATH.
    echo.
    echo   Install Python 3.11+ from https://python.org
    echo   Make sure to check "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

for /f "tokens=2" %%V in ('python --version 2^>^&1') do set PYVER=%%V
echo   Python %PYVER% detected.
echo   Python %PYVER% detected. >> "%LOGFILE%"

:: ── Check pip ────────────────────────────────────────────────
python -m pip --version >nul 2>&1
if %errorlevel% neq 0 (
    echo   [ERROR] pip is not installed. >> "%LOGFILE%"
    echo.
    echo   [ERROR] pip is not installed.
    echo   Run: python -m ensurepip --upgrade
    echo.
    pause
    exit /b 1
)

:: ── Install dependencies ────────────────────────────────────
echo   Installing dependencies...
echo   Installing dependencies... >> "%LOGFILE%"
python -m pip install -r "%~dp0requirements.txt" >> "%LOGFILE%" 2>&1
if %errorlevel% neq 0 (
    echo.
    echo   [ERROR] Failed to install dependencies.
    echo   Check bridge-run.log for the full error output.
    echo   [ERROR] Failed to install dependencies. >> "%LOGFILE%"
    echo.
    echo   Try running manually: python -m pip install flask requests urllib3
    echo.
    pause
    exit /b 1
)
echo   Dependencies ready.
echo   Dependencies ready. >> "%LOGFILE%"
echo.

:: ── Find local IP ─────────────────────────────────────────────
echo   Detecting your local IP address...
echo   Detecting your local IP address... >> "%LOGFILE%"
set LAN_IP=127.0.0.1
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do (
    set IP=%%a
    set IP=!IP: =!
    if not "!IP!"=="127.0.0.1" (
        set LAN_IP=!IP!
    )
)
echo   Local IP: %LAN_IP%
echo   Local IP: %LAN_IP% >> "%LOGFILE%"

:: ── Firewall check ────────────────────────────────────────────
echo   Checking firewall rule for port 8080...
echo   Checking firewall rule for port 8080... >> "%LOGFILE%"
netsh advfirewall firewall show rule name="Valorant Chat Bridge" >nul 2>&1
if %errorlevel% neq 0 (
    echo   Adding firewall rule...
    echo   Adding firewall rule... >> "%LOGFILE%"
    netsh advfirewall firewall add rule name="Valorant Chat Bridge" ^
        dir=in action=allow protocol=TCP localport=8080 >> "%LOGFILE%" 2>&1
    if %errorlevel% equ 0 (
        echo   Firewall rule added.
        echo   Firewall rule added. >> "%LOGFILE%"
    ) else (
        echo   [WARNING] Could not add firewall rule automatically.
        echo   [WARNING] Run this file as Administrator to add it, or
        echo             allow port 8080 when Windows prompts you.
        echo   [WARNING] Could not add firewall rule. >> "%LOGFILE%"
    )
) else (
    echo   Firewall rule already exists.
    echo   Firewall rule already exists. >> "%LOGFILE%"
)
echo.

:: ── Start server ──────────────────────────────────────────────
echo   Starting bridge server...
echo   Starting bridge server... >> "%LOGFILE%"
echo.
echo   ========================================================
echo   ^|                                                    ^|
echo   ^|   On your phone, open:                             ^|
echo   ^|   http://%LAN_IP%:8080
echo   ^|                                                    ^|
echo   ^|   Phone ^& PC must be on the same WiFi network.     ^|
echo   ^|   Valorant must be running and in a match.         ^|
echo   ^|                                                    ^|
echo   ^|   Close this window to stop the bridge.            ^|
echo   ========================================================
echo.

echo   Server output >> "%LOGFILE%"
echo   ============= >> "%LOGFILE%"
python server.py >> "%LOGFILE%" 2>&1

echo.
echo   Bridge stopped at %time%.
echo   Bridge stopped at %time%. >> "%LOGFILE%"
echo   Full log saved to: %LOGFILE%
echo   Full log saved to: %LOGFILE% >> "%LOGFILE%"
pause
