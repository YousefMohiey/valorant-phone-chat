@echo off
title Valorant Phone Chat Bridge
setlocal enabledelayedexpansion

:: ═══════════════════════════════════════════════════════════════
::  Valorant Phone Chat Bridge — One-Click Launcher
::  Double-click this file to start. No terminal commands needed.
:: ═══════════════════════════════════════════════════════════════

echo.
echo   VALORANT PHONE CHAT BRIDGE
echo   ==========================
echo.

:: ── Check Python ──────────────────────────────────────────────
python --version >nul 2>&1
if %errorlevel% neq 0 (
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

:: ── Check pip ────────────────────────────────────────────────
python -m pip --version >nul 2>&1
if %errorlevel% neq 0 (
    echo   [ERROR] pip is not installed.
    echo.
    echo   Run: python -m ensurepip --upgrade
    echo   Or reinstall Python and check "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

:: ── Install dependencies ────────────────────────────────────
echo   Installing dependencies...
python -m pip install -r requirements.txt
if %errorlevel% neq 0 (
    echo.
    echo   [ERROR] Failed to install dependencies.
    echo   Try running: python -m pip install flask requests urllib3
    echo.
    pause
    exit /b 1
)
echo   Dependencies ready.
echo.

:: ── Find local IP ─────────────────────────────────────────────
echo   Detecting your local IP address...
for /f "tokens=2 delims=:" %%a in ('ipconfig ^| findstr /c:"IPv4"') do (
    set IP=%%a
    set IP=!IP: =!
    if not "!IP!"=="127.0.0.1" (
        set LAN_IP=!IP!
    )
)

if "%LAN_IP%"=="" set LAN_IP=127.0.0.1

:: ── Firewall check ────────────────────────────────────────────
echo   Checking firewall rule for port 8080...
netsh advfirewall firewall show rule name="Valorant Chat Bridge" >nul 2>&1
if %errorlevel% neq 0 (
    echo   Adding firewall rule (admin required)...
    netsh advfirewall firewall add rule name="Valorant Chat Bridge" ^
        dir=in action=allow protocol=TCP localport=8080 >nul 2>&1
    if %errorlevel% equ 0 (
        echo   Firewall rule added.
    ) else (
        echo   [WARNING] Could not add firewall rule automatically.
        echo   You may need to allow port 8080 manually when prompted.
    )
) else (
    echo   Firewall rule already exists.
)
echo.

:: ── Start server ──────────────────────────────────────────────
echo   Starting bridge server...
echo.
echo   ╔══════════════════════════════════════════════════════╗
echo   ║                                                    ║
echo   ║   On your phone, open:                             ║
echo   ║   http://%LAN_IP%:8080
echo   ║                                                    ║
echo   ║   Phone & PC must be on the same WiFi network.     ║
echo   ║   Valorant must be running and in a match.         ║
echo   ║                                                    ║
echo   ║   Close this window to stop the bridge.            ║
echo   ╚══════════════════════════════════════════════════════╝
echo.

python server.py

:: ── Shutdown ──────────────────────────────────────────────────
echo.
echo   Bridge stopped.
pause
