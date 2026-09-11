#!/usr/bin/env bash
# MergePilot demo — start the local demo server (frontend is prebuilt).
# Requires Node.js 18+. Optional: rebuild the frontend first with "npm run build".
cd "$(dirname "$0")"
echo "Starting MergePilot demo at http://127.0.0.1:4173 ..."
exec node backend/server.mjs
