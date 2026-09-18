#!/usr/bin/env bash
# MergePilot demo — start the local demo server (frontend is prebuilt).
# Requires Node.js 18+. No npm install, no network, no external CDN.
# Optional: rebuild the frontend first with "npm run build".
# Keep this directory next to ../evidence/ (read-only replay evidence).
cd "$(dirname "$0")"
echo "Starting MergePilot demo at http://127.0.0.1:4173 ..."
echo "Health check: curl http://127.0.0.1:4173/api/health"
echo "Press Ctrl+C to stop."
exec node backend/server.mjs
