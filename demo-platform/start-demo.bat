@echo off
chcp 65001 >nul
REM MergePilot demo — start the local demo server (frontend is prebuilt).
REM Requires Node.js 18+. No npm install, no network, no external CDN.
REM Optional: rebuild the frontend first with "npm run build".
REM Keep this directory next to ../evidence/ (read-only replay evidence).
cd /d "%~dp0"
echo Starting MergePilot demo at http://127.0.0.1:4173 ...
echo.
echo Health check: curl http://127.0.0.1:4173/api/health
echo Press Ctrl+C to stop.
echo.
node backend/server.mjs
pause
