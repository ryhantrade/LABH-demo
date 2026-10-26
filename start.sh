#!/bin/sh
# LABH demo website entrypoint (Render free web service).
# Seeds the demo database on first start only; later starts keep
# whatever state the demo is in. Render provides $PORT.
cp -n demo.db labh.db 2>/dev/null
export LABH_DEMO=1
export LABH_HOST=0.0.0.0
export LABH_PORT=${PORT:-8000}
exec python3 app.py
