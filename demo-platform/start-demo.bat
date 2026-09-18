@echo off
setlocal
REM MergePilot demo - start the local demo server (frontend is prebuilt).
REM Requires Node.js 18+. No npm install, no network, no external CDN.
REM Optional: rebuild the frontend first with "npm run build".
REM Keep this directory next to ../evidence/ (read-only replay evidence).
REM NOTE: plain ASCII + CRLF on purpose; no "chcp" (cmd.exe mis-parses otherwise).
cd /d "%~dp0"
where node >nul 2>nul
if errorlevel 1 (
  echo [ERROR] Node.js not found. Install Node.js 18+ from https://nodejs.org
  pause
  exit /b 1
)
echo Starting MergePilot demo at http://127.0.0.1:4173 ...
echo Health check: http://127.0.0.1:4173/api/health
echo Press Ctrl+C to stop.
echo.
node backend\server.mjs
echo.
echo [Server stopped]
pause
