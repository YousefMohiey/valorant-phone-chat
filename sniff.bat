@echo off
title Valorant Chat Sniffer
cd /d "%~dp0"
echo.
echo   VALORANT CHAT SNIFFER
echo   =====================
echo.
echo   Watching Valorant's WebSocket for chat events...
echo   Now send some chat messages in-game (team / party / dm)
echo.
echo   Press Ctrl+C when done, then send sniff.log to the dev.
echo.
python sniff.py
pause
