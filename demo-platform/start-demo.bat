@echo off
REM MergePilot demo — start the local demo server (frontend is prebuilt).
REM Requires Node.js 18+. Optional: rebuild the frontend first with "npm run build".
cd /d "%~dp0"
echo Starting MergePilot demo at http://127.0.0.1:4173 ...
node backend/server.mjs
