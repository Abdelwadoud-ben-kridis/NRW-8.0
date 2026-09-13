#!/usr/bin/env bash
# One command to start everything. Ctrl-C stops it.
set -e
cd "$(dirname "$0")"
python3 -m pip install -q -r requirements.txt 2>/dev/null || true
echo "---------------------------------------------"
echo " Smart Core Warehouse"
echo " dashboard : http://localhost:${SCW_PORT:-8000}"
echo " session   : ${SCW_SESSION:-nrw8-scw-k7q2}   (must match firmware/sketch.ino)"
echo "---------------------------------------------"
exec python3 -m uvicorn backend.main:app --host 0.0.0.0 --port "${SCW_PORT:-8000}"
