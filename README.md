# LABH — demo website

A public demo of the LABH Smart Business Manager app, runnable in a
browser. Built from the same Python app as the Android app (v2.15),
plus a small demo-only patch (see below). **Demo data only** — no real
business, no Firebase account, no Google Drive access.

## Try it

Open the site, tap **Enter the demo** (no sign-in needed), and explore:
Dashboard, POS, products, customers, sales, purchases, expenses,
reports, Business Health and Labh AI — all with a seeded phone-shop
dataset (Demo Shop, October 2026).

- Google sign-in is intentionally absent on the demo (no Firebase
  project is attached, and the demo domain could never verify).
- Google Drive sync/backup is disabled on the demo.
- In-app update checks are pointed at a repo that does not exist, so
  no update pop-up ever appears here.

## Files

- `app.py` — LABH app (v2.15) + demo patch applied.
- `apply_demo_patch.py` — the patch script (documents every demo
  change; each one is gated on `LABH_DEMO=1` and is inert otherwise).
- `build_demo_db.py` — builds the seed database `demo.db`.
- `demo.db` — seed database (Demo Shop + demo user `demo@labh.demo`).
- `start.sh` — Render entrypoint: copies `demo.db` to `labh.db` on
  first start, then runs the app on `$PORT` bound to `0.0.0.0`.

## Run locally

```sh
cp demo.db labh.db
LABH_DEMO=1 LABH_PORT=8071 python3 app.py
# open http://127.0.0.1:8071/demo-enter
```

## Deploy (Render free web service)

- Build command: none (Python stdlib only).
- Start command: `sh start.sh`
- No environment variables needed (`start.sh` sets `LABH_DEMO`,
  `LABH_HOST`, `LABH_PORT` itself).

## What this is NOT

This repo is the **demo only**. It is never shipped to the Android
app. When the demo is approved, the approved app changes are ported
into the real app separately — the demo scaffolding (demo-enter,
DEMO ribbon, no-Firebase/no-Drive gates) never goes into the app.
