#!/usr/bin/env python3
"""LABH (Python rebuild) - Lite business manager.

Fresh implementation in Python 3 standard library only: no pip packages.
Server-rendered HTML + SQLite. Identity is Firebase Auth (see below);
the login/register pages load the Firebase JS SDK from the CDN for
Google sign-in only — every other page is server-rendered, no CDN.

Run:  python3 app.py   (serves http://127.0.0.1:8000)
Data:  labh.db in the same directory (WAL mode, indexed).
Backup: /backup page (login required) exports a dated .db copy, also kept
  in backups/; restore (owner/admin) validates the file, saves a safety
  copy of the live DB first, then replaces it. The .db file is what the
  user keeps in Google Drive — manual file flow, not automatic Drive sync.
  Auto-backup: after each burst of business writes (sale, purchase,
  payment, expense, product/customer/supplier/settings change) a dated
  copy is saved automatically in backups/ (latest 10 kept, older ones
  rotated away) — no tap needed, no hosting needed.
Updates: the Updates page (login required) shows the current version and,
  when an owner/admin sets a manifest URL in Settings, checks that JSON
  manifest for a newer version, shows its release date + changelog, and
  lets owner/admin download and install it from inside the app. Install
  backs up the current app.py first and never touches labh.db; the new
  code takes effect after the user restarts the app. No update server
  is built in — without a manifest URL the page says so plainly.
  Pop-up: on dashboard load, if the manifest reports a newer version
  the user has not dismissed, a modal shows the new version + a short
  summary, with Update now (opens the Updates page — nothing is
  downloaded/installed automatically), Later (hides it for this
  session; it returns on next login) and Don't show again for this
  version (persistent, stored in updates/dismissed.json, never in
  labh.db). Login itself never fetches a manifest, so a pop-up can
  never block login.
  APK updates (v2.4, for the standalone phone app — see /updates-apk):
  the app scans an "APK update folder" (Settings setting, else the
  Drive sync folder, else Downloads) for labh-<version>.apk files, and
  optionally checks an APK manifest URL (a separate Settings setting,
  empty by default, whose manifest uses apk_url instead of
  download_url). If a newer APK is found, the dashboard shows the same
  Later / Don't-show-again pop-up, pointing at /updates-apk, where
  Install serves the local APK file so Android's DownloadManager
  downloads it and the system installer opens it. No manifest file /
  no newer APK file = the page plainly says "no update available" —
  an APK update is never invented. Installing a new APK keeps the
  app's internal storage (HOME-based data dir on Android) intact and
  never touches labh.db; the PC Updates page (/updates) is unchanged.
  Android itself requires the user to tap Install and to allow
  "install unknown apps" for LABH once — the app cannot silently
  replace itself, and a private GitHub repo artifact URL cannot be
  used as apk_url without auth, which is why the Drive-folder source
  is primary.

Identity (Firebase Auth — the ONLY registration/login path):
  * Registration and login go through Firebase Auth: Email/Password
    (via the Firebase Auth REST API) and Google sign-in (via the
    Firebase JS SDK on the login/register pages, token verified on
    the server before any session is created). No local password is
    ever stored or checked; the local users table only links a
    verified Firebase UID to a business and a role.
  * Registration/login need internet. Business data (sales, stock,
    customers, ...) stays in local SQLite on the device and syncs
    via Google Drive exactly as before — Firebase never sees it.
Security model (fixes the original's open Firestore rules):
  * Every business row carries business_id; every query is scoped to the
    logged-in user's business. Accessing another business's row by direct
    URL/ID returns 403.
  * App sessions are random tokens stored hashed, HttpOnly cookies,
    12h expiry.
  * All POST forms carry a per-session CSRF token.
  * Login attempts are throttled (5 failures / 10 minutes per email+IP).
  * Staff users belong to one business and may sell/view only; only
    owner/admin may manage master data, purchases, expenses, settings.
"""
import csv
import hashlib
import io
import hmac
import html
import json
import os
import re
import secrets
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from datetime import date, datetime, timedelta
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

VERSION = "2.15.0"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
APP_PY_PATH = os.path.join(BASE_DIR, "app.py")
DB_PATH = os.path.join(BASE_DIR, "labh.db")
BACKUP_DIR = os.path.join(BASE_DIR, "backups")
UPDATES_DIR = os.path.join(BASE_DIR, "updates")
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024  # 100 MB cap for update downloads
MAX_RESTORE_BYTES = 50 * 1024 * 1024  # 50 MB upload cap for restore
# Tables a genuine LABH backup must contain. A file missing any of these
# is rejected by restore before live data is touched.
EXPECTED_TABLES = {"businesses", "users", "sessions", "categories", "products",
                   "customers", "suppliers", "sales", "sale_items", "purchases",
                   "purchase_items", "payments", "expenses", "stock_movements", "imeis"}
PORT = int(os.environ.get("LABH_PORT", "8000"))
HOST = os.environ.get("LABH_HOST", "127.0.0.1")
LOCK = threading.Lock()
ATTEMPTS = {}  # (ip, email) -> [timestamps]

CSS = """
/* LABH design system — copied from the AI Studio app's own ThemeContext /
   index.css / Sidebar / Header / MobileBottomNav (LABH Green, light theme).
   Primary #16A34A, hover #15803D, light #DCFCE7, border #E5EAE7,
   app bg emerald-50/30, header bg #f4fbf7, sidebar bg #F7FAF5. */
:root{--primary:#16A34A;--primary-hover:#15803D;--primary-light:#DCFCE7;--primary-text:#15803D;--border:#E5EAE7;--appbg:#f2faf5;--headerbg:#f4fbf7;--sidebarbg:#F7FAF5;--ink:#0F172A;--muted:#94A3B8;--muted2:#64748B}
*{box-sizing:border-box}
body{font-family:system-ui,-apple-system,Segoe UI,Arial,sans-serif;margin:0;background:var(--appbg);color:var(--ink);overflow-x:hidden;-webkit-font-smoothing:antialiased}
a{color:var(--primary-text)}
.layout{display:flex;min-height:100vh}
.sidebar{width:260px;background:var(--sidebarbg);color:#1E293B;border-right:1px solid rgba(188,202,192,.3);padding:0 12px 12px;position:sticky;top:0;height:100vh;overflow:auto;flex-shrink:0}
.sidebar .logo{display:flex;align-items:center;gap:12px;height:80px;padding:0 8px;border-bottom:1px solid rgba(226,232,240,.6)}
.sidebar .logo .mark{width:40px;height:40px;border-radius:12px;background:var(--primary);color:#fff;display:flex;align-items:center;justify-content:center;font-weight:900;font-size:20px;box-shadow:0 2px 6px rgba(0,0,0,.15);flex-shrink:0}
.sidebar .logo .word{font-weight:900;font-size:14px;letter-spacing:-.3px;color:var(--primary)}
.sidebar .logo .tag{font-size:10px;color:#64748B;font-weight:700}
.sidebar .grp{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#94A3B8;margin:20px 12px 6px}
.sidebar a{display:flex;align-items:center;justify-content:space-between;padding:10px 14px;border-radius:12px;font-size:12px;font-weight:500;color:#475569;text-decoration:none;min-height:44px;margin:2px 0;position:relative}
.sidebar a:hover{background:#F1F5F9;color:#0F172A}
.sidebar a.active{background:var(--primary-light);color:var(--primary-text);font-weight:700;box-shadow:0 1px 2px rgba(0,0,0,.05)}
.sidebar a.active::before{content:"";position:absolute;left:0;top:50%;transform:translateY(-50%);width:6px;height:24px;border-radius:0 6px 6px 0;background:var(--primary)}
.sidebar a .badge{font-size:10px;padding:2px 8px;border-radius:999px;font-weight:700}
.sidebar a .badge.pos{background:rgba(245,158,11,.1);color:#D97706;border:1px solid rgba(245,158,11,.2)}
.sidebar a .badge.ai{background:rgba(168,85,247,.1);color:#9333EA;border:1px solid rgba(168,85,247,.2)}
.sidebar a .badge.stock{background:var(--primary-light);color:var(--primary-text);border:1px solid rgba(22,163,74,.2)}
.sidebar a.morelink{display:none}
.maincol{flex:1;min-width:0}
.topbar{height:80px;background:var(--headerbg);border-bottom:1px solid rgba(226,232,240,.7);padding:0 16px;display:flex;gap:12px;align-items:center;position:sticky;top:0;z-index:20}
.topbar form{display:flex;gap:8px;flex:1;max-width:520px}
.topbar input{height:44px;padding:0 16px;border:1px solid rgba(226,232,240,.8);border-radius:16px;width:100%;background:#fff;box-shadow:0 1px 2px rgba(0,0,0,.05)}
.topbar .who{margin-left:auto;display:flex;gap:10px;align-items:center;font-size:12px;white-space:nowrap}
.topbar .who button,.topbar .who .btn{padding:8px 14px;min-height:40px}
main{max-width:none;margin:0;padding:8px 16px 24px}
h1{font-size:1.4em;font-weight:800;letter-spacing:-.3px;margin:10px 0 14px}
h2,h3{margin:8px 0;font-weight:800;letter-spacing:-.2px}
.card{background:#fff;border:1px solid rgba(226,232,240,.8);border-radius:16px;padding:16px;margin:12px 0;box-shadow:0 1px 2px rgba(0,0,0,.05)}
.tile{background:#fff;border:1px solid rgba(226,232,240,.8);border-radius:24px;padding:14px 16px;box-shadow:0 1px 2px rgba(0,0,0,.05);display:flex;justify-content:space-between;align-items:flex-start}
.tile .lbl{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1px;color:#94A3B8}
.tile .val{font-size:20px;font-weight:900;color:#0F172A;margin-top:2px}
.tile .chip{width:40px;height:40px;border-radius:16px;display:flex;align-items:center;justify-content:center;font-size:18px;flex-shrink:0}
table{width:100%;border-collapse:collapse}
th,td{border-bottom:1px solid #F1F5F9;padding:8px;text-align:left;vertical-align:top}
th{color:#94A3B8;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:1px}
input,select,textarea{padding:10px 12px;margin:3px 0;border:1px solid #CBD5E1;border-radius:12px;width:100%;box-sizing:border-box;background:#fff}
button,.btn{background:var(--primary);color:#fff;border:0;padding:10px 18px;border-radius:12px;cursor:pointer;text-decoration:none;display:inline-block;font-weight:600;box-shadow:0 1px 2px rgba(0,0,0,.05)}
button:hover,.btn:hover{background:var(--primary-hover)}
button.danger,.btn.danger{background:#DC2626}
button.secondary,.btn.secondary{background:#F1F5F9;color:#0F172A}
.err{background:#FEF2F2;border:1px solid #FECACA;padding:12px;border-radius:16px}
.ok{background:#DCFCE7;border:1px solid #BBF7D0;padding:12px;border-radius:16px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px}
.stat{font-size:1.7em;font-weight:900}
label{font-weight:600;font-size:.9em}
.badge{display:inline-block;background:#DCFCE7;color:#15803D;border-radius:999px;padding:2px 10px;font-size:.8em;font-weight:700}
.badge.warn{background:#FEF3C7;color:#92400E}
.badge.bad{background:#FEE2E2;color:#991B1B}
.login-wrap{max-width:440px;margin:60px auto;padding:0 16px}
.login-wrap .card{padding:24px;border-radius:24px}
.modal-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.5);display:flex;align-items:center;justify-content:center;z-index:50}
.modal{background:#fff;border-radius:16px;padding:20px;max-width:520px;width:90%;box-shadow:0 8px 30px rgba(0,0,0,.3)}
.modal form{display:inline}
.chat{border:1px solid var(--border);border-radius:12px;padding:12px;background:#F8FAFC;min-height:120px;white-space:pre-wrap}
.bottomnav{display:none}
.topbar .topmore{display:none}
@media(max-width:767px){.layout{display:block}.sidebar{display:none}.topbar{height:64px;min-height:64px;flex-wrap:nowrap;padding:8px 10px;gap:8px;overflow:hidden}.topbar .who{margin-left:auto;flex-shrink:0}.topbar .search-btn{display:none}.topbar .logout-btn{display:none}.maincol{padding-bottom:96px}.card{overflow-x:auto}button,.btn{min-height:44px;padding:12px 18px}input,select,textarea{padding:12px}main{padding:8px 8px 24px}.grid{grid-template-columns:1fr}.bottomnav{display:grid;grid-template-columns:repeat(5,1fr);position:fixed;left:0;right:0;bottom:0;z-index:9999;background:#052e22;border-top:1px solid #052e22;box-shadow:0 -4px 16px rgba(0,0,0,.08);border-radius:16px 16px 0 0;padding:12px 8px calc(14px + env(safe-area-inset-bottom));text-align:center}.bottomnav a{color:#a7f3d0;text-decoration:none;font-size:10px;font-weight:500;display:flex;flex-direction:column;align-items:center;gap:2px;min-height:44px}.bottomnav a.active{color:#ffffff;font-weight:700}.bottomnav .ico{font-size:22px;line-height:1}}
@media print{.sidebar,.topbar,.bottomnav,.noprint{display:none}}
"""

# ---------------------------------------------------------------- database
def new_conn():
    """One short-lived connection per operation (never shared across
    threads). Combined with WAL + busy_timeout this is what lets
    ThreadingHTTPServer serve many users at once on SQLite: readers do
    not block each other, and a contended writer waits up to 30s
    instead of failing with 'database is locked'."""
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn

SCHEMA = """
CREATE TABLE IF NOT EXISTS businesses(id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT NOT NULL, currency TEXT NOT NULL DEFAULT 'BDT', created_at TEXT);
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_id INTEGER NOT NULL REFERENCES businesses(id), email TEXT NOT NULL UNIQUE,
  pw_hash TEXT NOT NULL DEFAULT '', firebase_uid TEXT, role TEXT NOT NULL DEFAULT 'owner', created_at TEXT);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,
  user_id INTEGER NOT NULL REFERENCES users(id), csrf TEXT NOT NULL, expires TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS categories(id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_id INTEGER NOT NULL, name TEXT NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS products(id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_id INTEGER NOT NULL, category_id INTEGER, name TEXT NOT NULL, sku TEXT,
  purchase_price REAL NOT NULL DEFAULT 0, selling_price REAL NOT NULL DEFAULT 0,
  stock INTEGER NOT NULL DEFAULT 0, low_stock INTEGER NOT NULL DEFAULT 5, created_at TEXT);
CREATE TABLE IF NOT EXISTS customers(id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_id INTEGER NOT NULL, name TEXT NOT NULL, phone TEXT, email TEXT,
  address TEXT, opening_balance REAL NOT NULL DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS suppliers(id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_id INTEGER NOT NULL, name TEXT NOT NULL, phone TEXT, email TEXT,
  address TEXT, opening_balance REAL NOT NULL DEFAULT 0, created_at TEXT);
CREATE TABLE IF NOT EXISTS sales(id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_id INTEGER NOT NULL, customer_id INTEGER, invoice_no TEXT NOT NULL,
  total REAL NOT NULL, paid REAL NOT NULL, due REAL NOT NULL,
  sale_date TEXT NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS sale_items(id INTEGER PRIMARY KEY AUTOINCREMENT,
  sale_id INTEGER NOT NULL REFERENCES sales(id), product_id INTEGER,
  product_name TEXT, qty REAL NOT NULL, price REAL NOT NULL, amount REAL NOT NULL);
CREATE TABLE IF NOT EXISTS purchases(id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_id INTEGER NOT NULL, supplier_id INTEGER, total REAL NOT NULL,
  paid REAL NOT NULL, due REAL NOT NULL, purchase_date TEXT NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS purchase_items(id INTEGER PRIMARY KEY AUTOINCREMENT,
  purchase_id INTEGER NOT NULL REFERENCES purchases(id), product_id INTEGER,
  product_name TEXT, qty REAL NOT NULL, price REAL NOT NULL, amount REAL NOT NULL);
CREATE TABLE IF NOT EXISTS payments(id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_id INTEGER NOT NULL, entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL,
  amount REAL NOT NULL, method TEXT, note TEXT, pay_date TEXT NOT NULL, created_at TEXT);
CREATE TABLE IF NOT EXISTS expenses(id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_id INTEGER NOT NULL, title TEXT NOT NULL, category TEXT,
  amount REAL NOT NULL, method TEXT, expense_date TEXT NOT NULL, note TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS stock_movements(id INTEGER PRIMARY KEY AUTOINCREMENT,
  business_id INTEGER NOT NULL, product_id INTEGER NOT NULL, kind TEXT NOT NULL,
  qty REAL NOT NULL, note TEXT, move_date TEXT NOT NULL, created_at TEXT);
CREATE INDEX IF NOT EXISTS idx_products_biz ON products(business_id);
CREATE INDEX IF NOT EXISTS idx_customers_biz ON customers(business_id);
CREATE INDEX IF NOT EXISTS idx_suppliers_biz ON suppliers(business_id);
CREATE INDEX IF NOT EXISTS idx_sales_biz ON sales(business_id);
CREATE INDEX IF NOT EXISTS idx_purchases_biz ON purchases(business_id);
CREATE INDEX IF NOT EXISTS idx_expenses_biz ON expenses(business_id);
CREATE INDEX IF NOT EXISTS idx_movements_biz ON stock_movements(business_id);
CREATE INDEX IF NOT EXISTS idx_sale_items ON sale_items(sale_id);
CREATE INDEX IF NOT EXISTS idx_purchase_items ON purchase_items(purchase_id);
"""

def init_db():
    """Create schema + migrations once at startup (single-threaded)."""
    conn = new_conn()
    try:
        conn.executescript(SCHEMA)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(sale_items)")]
        if "cost_price" not in cols:
            conn.execute("ALTER TABLE sale_items ADD COLUMN cost_price REAL NOT NULL DEFAULT 0")
        conn.execute("""CREATE TABLE IF NOT EXISTS imeis(id INTEGER PRIMARY KEY AUTOINCREMENT,
          business_id INTEGER NOT NULL, product_id INTEGER NOT NULL, imei TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'in-stock', note TEXT, imei_date TEXT NOT NULL, created_at TEXT)""")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_imeis_biz ON imeis(business_id)")
        # App-wide settings (e.g. the update manifest URL). One row per key;
        # set only by owner/admin, read by the Updates page.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(users)")]
        if "firebase_uid" not in cols:
            conn.execute("ALTER TABLE users ADD COLUMN firebase_uid TEXT")
        conn.execute("""CREATE TABLE IF NOT EXISTS app_settings(
          key TEXT PRIMARY KEY, value TEXT NOT NULL DEFAULT '')""")
        # --- v2 migrations (idempotent; old DBs gain the new columns/tables)
        def _add_col(tbl, col, ddl):
            cols2 = [r[1] for r in conn.execute(f"PRAGMA table_info({tbl})")]
            if col not in cols2:
                conn.execute(f"ALTER TABLE {tbl} ADD COLUMN {ddl}")
        _add_col("products", "barcode", "barcode TEXT")
        _add_col("products", "dp_price", "dp_price REAL NOT NULL DEFAULT 0")
        _add_col("products", "rp_price", "rp_price REAL NOT NULL DEFAULT 0")
        _add_col("products", "mrp_price", "mrp_price REAL NOT NULL DEFAULT 0")
        _add_col("products", "wholesale_price", "wholesale_price REAL NOT NULL DEFAULT 0")
        _add_col("products", "location", "location TEXT NOT NULL DEFAULT 'Main'")
        _add_col("sales", "discount", "discount REAL NOT NULL DEFAULT 0")
        _add_col("sales", "tax_amount", "tax_amount REAL NOT NULL DEFAULT 0")
        _add_col("sales", "payment_type", "payment_type TEXT NOT NULL DEFAULT ''")
        _add_col("sale_items", "price_type", "price_type TEXT NOT NULL DEFAULT ''")
        _add_col("stock_movements", "location_from", "location_from TEXT NOT NULL DEFAULT ''")
        _add_col("stock_movements", "location_to", "location_to TEXT NOT NULL DEFAULT ''")
        conn.execute("""CREATE TABLE IF NOT EXISTS purchase_returns(id INTEGER PRIMARY KEY AUTOINCREMENT,
          business_id INTEGER NOT NULL, purchase_id INTEGER NOT NULL, total REAL NOT NULL DEFAULT 0,
          note TEXT, return_date TEXT NOT NULL, created_at TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS purchase_payments(id INTEGER PRIMARY KEY AUTOINCREMENT,
          business_id INTEGER NOT NULL, purchase_id INTEGER NOT NULL, amount REAL NOT NULL DEFAULT 0,
          method TEXT, pay_date TEXT NOT NULL, created_at TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS stock_transfers(id INTEGER PRIMARY KEY AUTOINCREMENT,
          business_id INTEGER NOT NULL, product_id INTEGER NOT NULL, from_loc TEXT NOT NULL,
          to_loc TEXT NOT NULL, qty REAL NOT NULL, note TEXT, move_date TEXT NOT NULL, created_at TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS price_history(id INTEGER PRIMARY KEY AUTOINCREMENT,
          business_id INTEGER NOT NULL, product_id INTEGER NOT NULL, old_price REAL NOT NULL DEFAULT 0,
          new_price REAL NOT NULL DEFAULT 0, changed_at TEXT NOT NULL, note TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS imei_movements(id INTEGER PRIMARY KEY AUTOINCREMENT,
          business_id INTEGER NOT NULL, imei_id INTEGER NOT NULL, kind TEXT NOT NULL,
          note TEXT, move_date TEXT NOT NULL, created_at TEXT)""")
        # --- v2.1 migrations
        _add_col("products", "brand", "brand TEXT NOT NULL DEFAULT ''")
        _add_col("sales", "salesperson", "salesperson TEXT NOT NULL DEFAULT ''")
        _add_col("sales", "payment_method", "payment_method TEXT NOT NULL DEFAULT ''")
        _add_col("sales", "note", "note TEXT NOT NULL DEFAULT ''")
        _add_col("sale_items", "discount", "discount REAL NOT NULL DEFAULT 0")
        _add_col("users", "name", "name TEXT NOT NULL DEFAULT ''")
        _add_col("purchases", "status", "status TEXT NOT NULL DEFAULT 'received'")
        conn.execute("""CREATE TABLE IF NOT EXISTS purchase_return_items(id INTEGER PRIMARY KEY AUTOINCREMENT,
          business_id INTEGER NOT NULL, return_id INTEGER NOT NULL, product_id INTEGER NOT NULL,
          qty REAL NOT NULL DEFAULT 0, reason TEXT, refund REAL NOT NULL DEFAULT 0)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS stock_audits(id INTEGER PRIMARY KEY AUTOINCREMENT,
          business_id INTEGER NOT NULL, product_id INTEGER NOT NULL, counted REAL NOT NULL DEFAULT 0,
          system_qty REAL NOT NULL DEFAULT 0, note TEXT, audit_date TEXT NOT NULL, created_at TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS notifications(id INTEGER PRIMARY KEY AUTOINCREMENT,
          business_id INTEGER NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL DEFAULT '',
          is_read INTEGER NOT NULL DEFAULT 0, created_at TEXT)""")
        conn.execute("""CREATE TABLE IF NOT EXISTS onboarding_pending(token_hash TEXT PRIMARY KEY,
          firebase_uid TEXT NOT NULL, email TEXT NOT NULL, expires TEXT NOT NULL)""")
        _add_col("onboarding_pending", "name", "name TEXT NOT NULL DEFAULT ''")
        # v2.5: raw setup token, so a later sign-in can resume the exact
        # same onboarding link instead of creating a duplicate row.
        # Device-local DB only; tokens expire in 24h and are deleted
        # when onboarding finishes. Legacy rows (no token) fall back
        # to rotation (delete + new single row).
        _add_col("onboarding_pending", "token", "token TEXT NOT NULL DEFAULT ''")
        # v2.5: one-time login-choice tokens (stuck-login page). The
        # token proves the Firebase sign-in that produced the choice
        # page, so "Create a new business" cannot be forged for an
        # arbitrary email by posting a bare uid/email.
        conn.execute("""CREATE TABLE IF NOT EXISTS login_choices(token_hash TEXT PRIMARY KEY,
          firebase_uid TEXT NOT NULL, email TEXT NOT NULL, expires TEXT NOT NULL)""")
        # v2.6: one-time Google browser-login codes (APK route). The code
        # is stored hashed, is single-use, expires in 10 minutes, and is
        # tied to the Firebase-verified uid/email — it never creates a
        # session on its own; code-login reuses the verified-user logic.
        conn.execute("""CREATE TABLE IF NOT EXISTS google_login_codes(code_hash TEXT PRIMARY KEY,
          firebase_uid TEXT NOT NULL, email TEXT NOT NULL, expires TEXT NOT NULL,
          ip TEXT NOT NULL DEFAULT '', created TEXT NOT NULL DEFAULT '',
          drive_token_hash TEXT NOT NULL DEFAULT '', is_new_user TEXT NOT NULL DEFAULT '')""")
        # v2.8: Drive-token hash + isNewUser carried by a browser-login code.
        # The Drive access token itself is NEVER stored in the DB: the
        # plaintext lives only in the process-memory DRIVE_TOKENS dict
        # (single-use, popped on code login); the DB holds only its hash.
        _add_col("google_login_codes", "drive_token_hash", "drive_token_hash TEXT NOT NULL DEFAULT ''")
        _add_col("google_login_codes", "is_new_user", "is_new_user TEXT NOT NULL DEFAULT ''")
        conn.execute("""CREATE TABLE IF NOT EXISTS income_categories(id INTEGER PRIMARY KEY AUTOINCREMENT,
          business_id INTEGER NOT NULL, name TEXT NOT NULL, note TEXT, created_at TEXT)""")
        conn.commit()
    finally:
        conn.close()

init_db()

def q(sql, params=(), one=False):
    conn = new_conn()
    try:
        cur = conn.execute(sql, params)
        return cur.fetchone() if one else cur.fetchall()
    finally:
        conn.close()

def exec_db(sql, params=(), touch=True):
    """Write + commit. touch=True (default) marks the DB dirty so the
    auto-backup thread saves a copy once writes go quiet; session
    login/logout writes pass touch=False so mere sign-ins do not
    consume auto-backup slots."""
    conn = new_conn()
    try:
        cur = conn.execute(sql, params)
        conn.commit()
        if touch:
            mark_dirty()
        return cur.lastrowid
    finally:
        conn.close()

# ---------------------------------------------------------------- helpers
def esc(v):
    return html.escape("" if v is None else str(v), quote=True)

def money(v, cur="BDT"):
    sym = "৳" if cur == "BDT" else cur
    try:
        f = float(v)
        return f"{'-' if f < 0 else ''}{sym}{abs(f):,.2f}"
    except (TypeError, ValueError):
        return f"{sym}0.00"


def fmt_dt(s):
    """'2026-10-26T10:00:00' -> '26 Oct 2026, 10:00 AM'; date-only -> '26 Oct 2026'."""
    if not s:
        return ""
    s = str(s)
    try:
        from datetime import datetime
        d = datetime.fromisoformat(s.replace("Z", ""))
        if len(s) <= 10:
            return f"{d.day} {d.strftime('%b %Y')}"
        return f"{d.day} {d.strftime('%b %Y, %I:%M %p')}"
    except Exception:
        return s

def today():
    return date.today().isoformat()

def now_iso():
    return datetime.now().isoformat(timespec="seconds")

def hash_pw(pw):
    """PBKDF2 hash — used ONLY by the automated-test Firebase stub
    below (in-memory stub accounts). Production login never calls it:
    passwords are checked by Firebase, never locally."""
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, 200_000)
    return salt.hex() + "$" + dk.hex()

def _stub_check_pw(pw, stored):
    try:
        salt_hex, dk_hex = stored.split("$")
        dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt_hex), 200_000)
        return hmac.compare_digest(dk.hex(), dk_hex)
    except Exception:
        return False

def throttled(ip, email):
    key = (ip, email.lower())
    now = time.time()
    hits = [t for t in ATTEMPTS.get(key, []) if now - t < 600]
    ATTEMPTS[key] = hits
    return len(hits) >= 5

def record_fail(ip, email):
    ATTEMPTS.setdefault((ip, email.lower()), []).append(time.time())

def clear_fails(ip, email):
    ATTEMPTS.pop((ip, email.lower()), None)

# ---------------------------------------------------------------- Firebase Auth
# Identity ONLY. Registration/login go through Firebase Auth; business
# data stays in local SQLite + Drive sync and Firebase never sees it.
# Config is read from firebase_config.json next to app.py (PC), next to
# the bundled python files (Android assets), or LABH_FIREBASE_CONFIG.
# The values are client identifiers, not secrets, but this app never
# prints or logs them.
def _load_firebase_config():
    # v2.5: candidates in order — env (path, or inline JSON), the
    # original two locations, then: cwd, every sys.path entry, HOME,
    # and the directory containing labh_server.py (Android launcher).
    # Each candidate is best-effort; a failure never breaks startup.
    # Values are never printed or logged.
    candidates = []
    env = os.environ.get("LABH_FIREBASE_CONFIG")
    if env:
        stripped = env.strip()
        if stripped.startswith("{"):
            try:
                cfg = json.loads(stripped)
                if cfg.get("apiKey") and cfg.get("projectId"):
                    return cfg
            except Exception:
                pass
        else:
            candidates.append(env)
    candidates.append(os.path.join(BASE_DIR, "firebase_config.json"))
    srcdir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "firebase_config.json")
    if srcdir not in candidates:
        candidates.append(srcdir)
    try:
        candidates.append(os.path.join(os.getcwd(), "firebase_config.json"))
    except Exception:
        pass
    for entry in list(sys.path):
        try:
            if entry:
                candidates.append(os.path.join(entry, "firebase_config.json"))
        except Exception:
            continue
    try:
        home = os.environ.get("HOME") or os.path.expanduser("~")
        if home:
            candidates.append(os.path.join(home, "firebase_config.json"))
    except Exception:
        pass
    try:
        for mod in list(sys.modules.values()):
            f = getattr(mod, "__file__", None)
            if f and os.path.basename(f) == "labh_server.py":
                candidates.append(os.path.join(
                    os.path.dirname(os.path.abspath(f)), "firebase_config.json"))
    except Exception:
        pass
    try:
        for entry in list(sys.path):
            if entry and os.path.isfile(os.path.join(entry, "labh_server.py")):
                candidates.append(os.path.join(entry, "firebase_config.json"))
    except Exception:
        pass
    seen = set()
    for path in candidates:
        if path in seen:
            continue
        seen.add(path)
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
            if cfg.get("apiKey") and cfg.get("projectId"):
                return cfg
        except Exception:
            continue
    return {}

def firebase_config_status():
    """v2.5 diagnosis line for the login/register pages. Status text
    only — config values are never shown."""
    if FIREBASE_CONFIG.get("apiKey") and FIREBASE_CONFIG.get("projectId"):
        return "Firebase: connected"
    return "Firebase: config missing on this device"

FIREBASE_CONFIG = _load_firebase_config()
_STUB_USERS = {}  # email -> (uid, pw_hash); automated-test stub ONLY
# v2.8: process-memory only. Maps a browser-code hash -> (Drive access
# token, expiry iso). NEVER written to the DB, never logged, popped
# (single-use) when the code is redeemed. A server restart simply
# drops pending tokens and the user falls back to the sync-only page.
DRIVE_TOKENS = {}

def _auth_domain_host():
    """Firebase Auth domain host for the CSP.

    Taken from the loaded config at runtime; falls back to the known
    project host when no config is present (e.g. automated tests).
    Only a bare hostname is accepted — anything else falls back too,
    so a crafted value can never inject a CSP directive."""
    host = (FIREBASE_CONFIG.get("authDomain") or "").strip()
    if host and "://" not in host and "/" not in host and " " not in host:
        return host
    return "labh-98efd.firebaseapp.com"

def _csp_header():
    """Content-Security-Policy for HTML pages.

    v2.7: signInWithPopup needs the Firebase Auth hidden iframe, so
    frame-src/child-src must allow the authDomain host (plus the
    Google account chooser and gapi hosts), and connect-src must allow
    the authDomain. Without these, the popup handshake fails with
    auth/internal-error even though the popup itself opens. No
    wildcards, no 'unsafe-eval', no weakening of the other rules."""
    host = _auth_domain_host()
    return ("default-src 'self'; style-src 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline' https://www.gstatic.com https://apis.google.com; "
            "connect-src 'self' https://www.gstatic.com https://identitytoolkit.googleapis.com "
            "https://securetoken.googleapis.com https://oauth2.googleapis.com "
            f"https://{host} https://accounts.google.com; "
            f"frame-src 'self' https://{host} https://accounts.google.com https://apis.google.com; "
            f"child-src 'self' https://{host} https://accounts.google.com https://apis.google.com")

class FirebaseAuthError(Exception):
    pass

def _firebase_http_post(url, payload, timeout=15):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode()).get("error", {})
            raise FirebaseAuthError(err.get("message", "Firebase error"))
        except FirebaseAuthError:
            raise
        except Exception:
            raise FirebaseAuthError("Firebase error")
    except Exception:
        raise FirebaseAuthError("NETWORK_ERROR")

def _firebase_http_get(url, timeout=15):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read().decode()).get("error", {})
            raise FirebaseAuthError(err.get("message", "Firebase error"))
        except FirebaseAuthError:
            raise
        except Exception:
            raise FirebaseAuthError("Firebase error")
    except Exception:
        raise FirebaseAuthError("NETWORK_ERROR")

def _stub_uid(email):
    return "stub-" + hashlib.sha256(email.encode()).hexdigest()[:32]

def firebase_sign_up(email, password):
    """Create a Firebase account. Returns (uid, id_token)."""
    if os.environ.get("LABH_FIREBASE_STUB") == "1":
        # TESTS ONLY: in-memory stub so the automated suite can run
        # without network. Never set LABH_FIREBASE_STUB in production:
        # it bypasses Firebase entirely.
        if email in _STUB_USERS:
            raise FirebaseAuthError("EMAIL_EXISTS")
        uid = _stub_uid(email)
        _STUB_USERS[email] = (uid, hash_pw(password))
        return uid, "stub-token"
    cfg = FIREBASE_CONFIG
    if not cfg:
        raise FirebaseAuthError("FIREBASE_NOT_CONFIGURED")
    data = _firebase_http_post(
        "https://identitytoolkit.googleapis.com/v1/accounts:signUp?key=" + cfg["apiKey"],
        {"email": email, "password": password, "returnSecureToken": True})
    return data["localId"], data["idToken"]

def firebase_sign_in(email, password):
    """Verify a Firebase account. Returns (uid, id_token)."""
    if os.environ.get("LABH_FIREBASE_STUB") == "1":
        entry = _STUB_USERS.get(email)
        if entry and _stub_check_pw(password, entry[1]):
            return entry[0], "stub-token"
        # Cross-device sync case (tests only): this server process never
        # saw the sign-up, but the database restored from Drive already
        # holds this email's stub UID — accept it so synced logins work.
        row = q("SELECT firebase_uid FROM users WHERE email=?", (email,), one=True)
        if row and row["firebase_uid"] and row["firebase_uid"].startswith("stub-"):
            uid = row["firebase_uid"]
            _STUB_USERS[email] = (uid, hash_pw(password))
            return uid, "stub-token"
        raise FirebaseAuthError("INVALID_LOGIN_CREDENTIALS")
    cfg = FIREBASE_CONFIG
    if not cfg:
        raise FirebaseAuthError("FIREBASE_NOT_CONFIGURED")
    data = _firebase_http_post(
        "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=" + cfg["apiKey"],
        {"email": email, "password": password, "returnSecureToken": True})
    return data["localId"], data["idToken"]

def firebase_reset_password(email):
    """Send a Firebase password-reset email. Stub returns True."""
    if os.environ.get("LABH_FIREBASE_STUB") == "1":
        return True
    cfg = FIREBASE_CONFIG
    if not cfg:
        raise FirebaseAuthError("FIREBASE_NOT_CONFIGURED")
    _firebase_http_post(
        "https://identitytoolkit.googleapis.com/v1/accounts:sendOobCode?key=" + cfg["apiKey"],
        {"requestType": "PASSWORD_RESET", "email": email})
    return True

def firebase_verify_google_token(id_token):
    """Verify a Google/Firebase idToken. NEVER trust an unverified token:
    the audience must be this Firebase project and the token must not
    be expired. Returns (uid, email). Primary check is Google's
    tokeninfo endpoint; v2.11 fallback is Firebase's own accounts:lookup
    (POST with the project's API key), because on the user's phone
    tokeninfo alone was rejecting tokens with a generic failure and
    there was no way to tell a network failure from a bad token.
    A token is accepted ONLY if one of Google's own endpoints confirms
    it for this project — the fallback never trusts the token itself."""
    if os.environ.get("LABH_FIREBASE_STUB") == "1" and id_token.startswith("stub-google:"):
        email = id_token.split(":", 1)[1].strip().lower()
        if "@" in email:
            return _stub_uid(email), email
        raise FirebaseAuthError("INVALID_TOKEN")
    cfg = FIREBASE_CONFIG
    if not cfg:
        raise FirebaseAuthError("FIREBASE_NOT_CONFIGURED")
    try:
        data = _firebase_http_get(
            "https://oauth2.googleapis.com/tokeninfo?id_token=" + quote(id_token))
        aud = data.get("aud") or data.get("audience")
        email = (data.get("email") or "").strip().lower()
        try:
            expires_in = int(data.get("expires_in", 0))
        except (TypeError, ValueError):
            expires_in = 0
        if aud != cfg["projectId"] or expires_in <= 0 or "@" not in email:
            raise FirebaseAuthError("INVALID_TOKEN")
        uid = data.get("sub") or data.get("user_id")
        if not uid:
            uid = "google-" + hashlib.sha256(email.encode()).hexdigest()[:32]
        return uid, email
    except FirebaseAuthError as first_err:
        # tokeninfo failed (network, or token rejected): try Firebase's
        # accounts:lookup, which validates the token against THIS
        # project's API key. If lookup confirms a user, accept it.
        try:
            lookup = _firebase_http_post(
                "https://identitytoolkit.googleapis.com/v1/accounts:lookup?key=" + cfg["apiKey"],
                {"idToken": id_token})
            users = lookup.get("users") or []
            if users and users[0].get("localId") and users[0].get("email"):
                return users[0]["localId"], users[0]["email"].strip().lower()
        except FirebaseAuthError as lookup_err:
            if str(first_err) == "NETWORK_ERROR" and str(lookup_err) == "NETWORK_ERROR":
                raise FirebaseAuthError("NETWORK_ERROR")
        raise FirebaseAuthError("INVALID_TOKEN")

def find_or_link_firebase_user(uid, email):
    """Local user row for a verified Firebase identity, or None.
    Links a pending row (added by an owner, or never linked yet) to
    this UID. Never creates a business here."""
    row = q("SELECT * FROM users WHERE firebase_uid=?", (uid,), one=True)
    if row:
        return row
    row = q("SELECT * FROM users WHERE email=?", (email,), one=True)
    if row and not row["firebase_uid"]:
        exec_db("UPDATE users SET firebase_uid=? WHERE id=?", (uid, row["id"]), touch=False)
        return q("SELECT * FROM users WHERE id=?", (row["id"],), one=True)
    return None

def firebase_error_page(title, err):
    """User-facing message for a Firebase failure (never leaks the
    raw Firebase error for credential failures)."""
    msg = str(err)
    if msg in ("INVALID_LOGIN_CREDENTIALS", "INVALID_PASSWORD", "EMAIL_NOT_FOUND",
               "INVALID_EMAIL", "USER_DISABLED"):
        return '<div class="card err">Invalid email or password.</div><p><a href="/login">Try again</a></p>'
    if msg == "NETWORK_ERROR":
        return ('<div class="card err">Could not reach Firebase. Check your internet '
                'connection and try again.</div><p><a href="/login">Try again</a></p>')
    if msg == "FIREBASE_NOT_CONFIGURED":
        return ('<div class="card err">Firebase is not configured on this device. '
                'Copy firebase_config.json next to app.py and restart.</div>')
    if msg == "EMAIL_EXISTS":
        return ('<div class="card err">That email is already registered. '
                'Please log in instead.</div><p><a href="/login">Go to Login</a></p>')
    return ('<div class="card err">Sign-in failed. Please try again.</div>'
            '<p><a href="/login">Try again</a></p>')

def google_signin_html(force_consent=False):
    """Google sign-in button + Firebase JS SDK (login/register pages
    only). The SDK posts the verified idToken to /auth/google via a
    plain form submit, so the session cookie is set by the browser
    exactly like a normal login. No session/CSRF exists yet at this
    point, so the idToken itself is the credential — and the server
    verifies it before creating any session.
    v2.6 NEVER-SILENT rule: the click handler is bound unconditionally
    and always shows visible status. A missing SDK, a missing config,
    or a Firebase error is shown in #google-error with its real error
    code — errors are never swallowed with an empty catch again.
    v2.6 AMENDMENT (user requirement): tapping the button opens the
    Google account chooser IN A POPUP inside the app — popup is tried
    FIRST even in the Android WebView (MainActivity onCreateWindow
    shows it in a dialog WebView). Only if the popup fails does the
    page show the real error code AND reveal the fallback link to
    /google-open, which the APK intercepts and opens in the system
    browser, where the user gets a one-time code for /auth/google-code-login.
    The browser route is NEVER auto-navigated to; the user must choose it."""
    if os.environ.get("LABH_DEMO") == "1":
        # Demo site has no Firebase project: offer demo entry
        # instead of a Google button that could never verify.
        return ('<p style="text-align:center">— or —</p>'
                '<p><a class="btn" href="/demo-enter">Enter the demo (no sign-in needed)</a></p>'
                '<p style="text-align:center;font-size:12px;color:#64748b">Demo data only — nothing here touches a real business.</p>')
    cfg = {k: FIREBASE_CONFIG.get(k, "") for k in ("apiKey", "authDomain", "projectId", "appId")}
    cfg_json = json.dumps(cfg).replace("</", "<\\/")
    js = """
<script src="https://www.gstatic.com/firebasejs/10.12.2/firebase-app-compat.js" onerror="window.LABH_SDK_FAILED=true"></script>
<script src="https://www.gstatic.com/firebasejs/10.12.2/firebase-auth-compat.js" onerror="window.LABH_SDK_FAILED=true"></script>
<script>
var LABH_FIREBASE = __CONFIG__;
function labhShowGoogleError(msg) {
  var e = document.getElementById('google-error');
  if (e) { e.textContent = msg; }
}
function labhIsNewUser(res) {
  // v2.8: additionalUserInfo.isNewUser first; fall back to comparing
  // the account's creation and last-sign-in times (equal on a first
  // sign-in). Returns true/false, or null when unknown — the server
  // treats unknown as an EXISTING user (sync/manual only, never create).
  try {
    if (res && res.additionalUserInfo && typeof res.additionalUserInfo.isNewUser === 'boolean') { return res.additionalUserInfo.isNewUser; }
    if (res && res.user && res.user.metadata && res.user.metadata.creationTime && res.user.metadata.lastSignInTime) {
      return res.user.metadata.creationTime === res.user.metadata.lastSignInTime;
    }
  } catch (e) {}
  return null;
}
function labhGoogleCredential(res) {
  try { if (res && res.credential && res.credential.accessToken) { return res.credential.accessToken; } } catch (e) {}
  return '';
}
function labhSubmitGoogle(token, driveToken, isNewUser) {
  var f = document.createElement('form');
  f.method = 'post'; f.action = '/auth/google';
  var t = document.createElement('input'); t.type = 'hidden'; t.name = 'idToken'; t.value = token; f.appendChild(t);
  if (driveToken) { var d = document.createElement('input'); d.type = 'hidden'; d.name = 'driveAccessToken'; d.value = driveToken; f.appendChild(d); }
  if (isNewUser !== null && typeof isNewUser !== 'undefined') { var n = document.createElement('input'); n.type = 'hidden'; n.name = 'isNewUser'; n.value = isNewUser ? '1' : '0'; f.appendChild(n); }
  var b = document.querySelector('[name="business"]'); if (b) { var h = document.createElement('input'); h.type = 'hidden'; h.name = 'business'; h.value = b.value; f.appendChild(h); }
  var c = document.querySelector('[name="currency"]'); if (c) { var h2 = document.createElement('input'); h2.type = 'hidden'; h2.name = 'currency'; h2.value = c.value; f.appendChild(h2); }
  document.body.appendChild(f); f.submit();
}
(function () {
  var btn = document.getElementById('google-btn');
  if (!btn) { return; }
  // Bound FIRST and unconditionally: nothing below can leave the
  // button dead, whatever the SDK or config state is.
  btn.onclick = function () {
    labhShowGoogleError('Google sign-in cholche…');
    // POPUP FIRST, everywhere including the Android WebView: the
    // user asked for the Google account chooser in a popup inside
    // the app. No auto-navigation to /google-open happens here.
    if (window.LABH_SDK_FAILED || typeof firebase === 'undefined') {
      labhShowGoogleError('Google sign-in unavailable: Firebase SDK load hoy ni. Check your internet connection and try again.');
      return;
    }
    if (!LABH_FIREBASE.apiKey) {
      labhShowGoogleError('Firebase: config missing on this device');
      return;
    }
    var auth;
    try {
      if (!firebase.apps.length) { firebase.initializeApp(LABH_FIREBASE); }
      auth = firebase.auth();
    } catch (err) {
      labhShowGoogleError('Google sign-in failed: ' + (err && err.message ? err.message : 'could not start Firebase.'));
      return;
    }
    var provider = new firebase.auth.GoogleAuthProvider();
    // v2.8: Drive read-only scope so an EXISTING user's business can
    // be auto-synced from Drive after Google connect. The access
    // token is sent with the idToken and used once, never stored.
    provider.addScope('https://www.googleapis.com/auth/drive.readonly');
    // v2.12 (sync page only): force the consent screen every time, so
    // Google actually grants the Drive scope and returns an access
    // token carrying it. Without this, a returning user can be signed
    // in on an OLD grant (no Drive scope) — the token looks fine,
    // but Drive answers 403 and auto-sync reports "could not be
    // reached". The login page keeps the default (no forced prompt).
    __FORCE_CONSENT__
    auth.getRedirectResult().then(function (res) {
      if (res && res.user) { var dt = labhGoogleCredential(res); var nu = labhIsNewUser(res); return res.user.getIdToken().then(function (token) { labhSubmitGoogle(token, dt, nu); }); }
    }).catch(function (err) {
      labhShowGoogleError('Google sign-in failed' + (err && err.code ? ' (' + err.code + ')' : '') + '. Please try again.');
    });
    auth.signInWithPopup(provider).then(function (res) {
        var dt = labhGoogleCredential(res); var nu = labhIsNewUser(res);
        return res.user.getIdToken().then(function (token) { labhSubmitGoogle(token, dt, nu); });
      })
      .catch(function (err) {
        // v2.10: ANY popup failure just shows the real error code.
        // The old browser+6-digit-code fallback is removed (user
        // 2026-10-26: no code function) — popup sign-in is the only
        // Google path, so there is nothing else to reveal here.
        labhShowGoogleError('Google sign-in failed' + (err && err.code ? ' (' + err.code + ')' : '') + '. Please try again.');
      });
  };
})();
</script>""".replace("__CONFIG__", cfg_json).replace(
        "__FORCE_CONSENT__",
        "provider.setCustomParameters({prompt: 'consent'});"
        if force_consent else "")
    return ('<p style="text-align:center">— or —</p>'
            '<p><button type="button" id="google-btn" class="google-btn">'
            '<svg width="20" height="20" viewBox="0 0 48 48"><path fill="#FFC107" d="M43.6 20.1H42V20H24v8h11.3C33.7 32.7 29.2 36 24 36c-6.6 0-12-5.4-12-12s5.4-12 12-12c3.1 0 5.8 1.2 8 3l5.7-5.7C34.5 6.1 29.3 4 24 4 13 4 4 13 4 24s9 20 20 20 20-9 20-20c0-1.3-.1-2.6-.4-3.9z"/><path fill="#FF3D00" d="M6.3 14.7l6.6 4.8C14.7 15.1 19 12 24 12c3.1 0 5.8 1.2 8 3l5.7-5.7C34.5 6.1 29.3 4 24 4 16.3 4 9.7 8.3 6.3 14.7z"/><path fill="#4CAF50" d="M24 44c5.2 0 9.9-2 13.4-5.2l-6.2-5.2C29.2 35.1 26.7 36 24 36c-5.2 0-9.6-3.3-11.3-8l-6.6 5.1C9.7 39.7 16.3 44 24 44z"/><path fill="#1976D2" d="M43.6 20.1H42V20H24v8h11.3c-.8 2.2-2.2 4.2-4.1 5.6l6.2 5.2C36.9 39.2 44 34 44 24c0-1.3-.1-2.6-.4-3.9z"/></svg>'
            'Continue with Google</button></p>'
            '<p id="google-error" class="err" style="display:block"></p>' + js)
def drive_save_html():
    """v2.13: 'Save to Google Drive' button for the logged-in Backup
    page. A Google popup (read-only + drive.file scopes, forced
    consent so the write scope is really granted) returns a fresh
    idToken + access token, which the page posts to /drive-save.
    The server verifies the idToken AND that it matches the signed-in
    user before uploading anything — so a forged post with another
    account's token cannot overwrite this business's Drive backup.
    The token is used once for this upload and never stored."""
    cfg = {k: FIREBASE_CONFIG.get(k, "") for k in ("apiKey", "authDomain", "projectId", "appId")}
    cfg_json = json.dumps(cfg).replace("</", "<\\/")
    js = """
<script src="https://www.gstatic.com/firebasejs/10.12.2/firebase-app-compat.js" onerror="window.LABH_SDK_FAILED=true"></script>
<script src="https://www.gstatic.com/firebasejs/10.12.2/firebase-auth-compat.js" onerror="window.LABH_SDK_FAILED=true"></script>
<script>
var LABH_FIREBASE = __CONFIG__;
(function () {
  var btn = document.getElementById('drive-save-btn');
  if (!btn) { return; }
  btn.addEventListener('click', function () {
    var st = document.getElementById('drive-save-status');
    function say(m) { if (st) { st.textContent = m; } }
    if (window.LABH_SDK_FAILED || !window.firebase || !firebase.auth) {
      say('Google sign-in could not start (Firebase SDK did not load). Check your connection and try again.');
      return;
    }
    say('Google sign-in running…');
    var auth;
    try {
      if (!firebase.apps.length) { firebase.initializeApp(LABH_FIREBASE); }
      auth = firebase.auth();
    } catch (err) {
      say('Google sign-in failed: could not start Firebase.');
      return;
    }
    var provider = new firebase.auth.GoogleAuthProvider();
    provider.addScope('https://www.googleapis.com/auth/drive.readonly');
    provider.addScope('https://www.googleapis.com/auth/drive.file');
    provider.setCustomParameters({prompt: 'consent'});
    auth.signInWithPopup(provider).then(function (res) {
        var dt = '';
        try { if (res && res.credential && res.credential.accessToken) { dt = res.credential.accessToken; } } catch (e) {}
        return res.user.getIdToken().then(function (token) {
          var f = document.createElement('form');
          f.method = 'POST'; f.action = '/drive-save';
          var a = document.createElement('input'); a.type = 'hidden'; a.name = 'idToken'; a.value = token; f.appendChild(a);
          var b = document.createElement('input'); b.type = 'hidden'; b.name = 'driveAccessToken'; b.value = dt; f.appendChild(b);
          document.body.appendChild(f); f.submit();
        });
      })
      .catch(function (err) {
        say('Google sign-in failed' + (err && err.code ? ' (' + err.code + ')' : '') + '. Please try again.');
      });
  });
})();
</script>""".replace("__CONFIG__", cfg_json)
    return ('<p><button type="button" id="drive-save-btn" class="google-btn">'
            'Save database to Google Drive</button></p>'
            '<p id="drive-save-status"></p>' + js)


def google_browser_html():
    """v2.6 system-browser Google page. Runs in a REAL browser (not the
    APK WebView), so Firebase popup sign-in is allowed. On success the
    idToken is POSTed to /auth/google-code, which verifies it exactly
    like /auth/google and returns a short one-time code. The page
    displays ONLY the code — never the idToken, which is never logged
    or put in the page text."""
    cfg = {k: FIREBASE_CONFIG.get(k, "") for k in ("apiKey", "authDomain", "projectId", "appId")}
    cfg_json = json.dumps(cfg).replace("</", "<\\/")
    js = """
<script src="https://www.gstatic.com/firebasejs/10.12.2/firebase-app-compat.js" onerror="window.LABH_SDK_FAILED=true"></script>
<script src="https://www.gstatic.com/firebasejs/10.12.2/firebase-auth-compat.js" onerror="window.LABH_SDK_FAILED=true"></script>
<script>
var LABH_FIREBASE = __CONFIG__;
function labhBrowserError(msg) {
  var e = document.getElementById('google-browser-error');
  if (e) { e.textContent = msg; }
}
function labhFetchCode(token, driveToken, isNewUser) {
  labhBrowserError('Code toiri hochche…');
  var body = 'idToken=' + encodeURIComponent(token);
  if (driveToken) { body += '&driveAccessToken=' + encodeURIComponent(driveToken); }
  if (isNewUser !== null && typeof isNewUser !== 'undefined') { body += '&isNewUser=' + (isNewUser ? '1' : '0'); }
  fetch('/auth/google-code', {
    method: 'POST',
    headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: body
  }).then(function (r) { return r.json(); }).then(function (data) {
    if (data && data.ok && data.code) {
      var c = document.getElementById('google-browser-code');
      c.textContent = data.code;
      c.style.display = 'block';
      var h = document.getElementById('google-browser-hint');
      if (h) { h.style.display = 'block'; }
      labhBrowserError('');
    } else {
      labhBrowserError('Could not make a code: ' + (data && data.error ? data.error : 'please try again.'));
    }
  }).catch(function () {
    labhBrowserError('Could not reach the LABH app. Make sure the app is open and try again.');
  });
}
(function () {
  var btn = document.getElementById('google-browser-btn');
  if (!btn) { return; }
  btn.onclick = function () {
    labhBrowserError('Google sign-in cholche…');
    if (window.LABH_SDK_FAILED || typeof firebase === 'undefined') {
      labhBrowserError('Google sign-in unavailable: Firebase SDK load hoy ni. Check your internet connection and try again.');
      return;
    }
    if (!LABH_FIREBASE.apiKey) {
      labhBrowserError('Firebase: config missing on this device');
      return;
    }
    var auth;
    try {
      if (!firebase.apps.length) { firebase.initializeApp(LABH_FIREBASE); }
      auth = firebase.auth();
    } catch (err) {
      labhBrowserError('Google sign-in failed: ' + (err && err.message ? err.message : 'could not start Firebase.'));
      return;
    }
    var provider = new firebase.auth.GoogleAuthProvider();
    // v2.8: same Drive read-only scope as the login page, so the
    // browser+code route can auto-sync an existing user too.
    provider.addScope('https://www.googleapis.com/auth/drive.readonly');
    auth.getRedirectResult().then(function (res) {
      if (res && res.user) { var dt = (res.credential && res.credential.accessToken) ? res.credential.accessToken : ''; var nu = null; try { if (res.additionalUserInfo && typeof res.additionalUserInfo.isNewUser === 'boolean') { nu = res.additionalUserInfo.isNewUser; } else if (res.user.metadata && res.user.metadata.creationTime && res.user.metadata.lastSignInTime) { nu = res.user.metadata.creationTime === res.user.metadata.lastSignInTime; } } catch (e) {} return res.user.getIdToken().then(function (token) { labhFetchCode(token, dt, nu); }); }
    }).catch(function (err) {
      labhBrowserError('Google sign-in failed' + (err && err.code ? ' (' + err.code + ')' : '') + '. Please try again.');
    });
    auth.signInWithPopup(provider).then(function (res) {
        var dt = (res.credential && res.credential.accessToken) ? res.credential.accessToken : ''; var nu = null; try { if (res.additionalUserInfo && typeof res.additionalUserInfo.isNewUser === 'boolean') { nu = res.additionalUserInfo.isNewUser; } else if (res.user.metadata && res.user.metadata.creationTime && res.user.metadata.lastSignInTime) { nu = res.user.metadata.creationTime === res.user.metadata.lastSignInTime; } } catch (e) {} return res.user.getIdToken().then(function (token) { labhFetchCode(token, dt, nu); });
      })
      .catch(function (err) {
        if (err && (err.code === 'auth/popup-blocked' || err.code === 'auth/popup-closed-by-user')) { auth.signInWithRedirect(provider); return; }
        labhBrowserError('Google sign-in failed' + (err && err.code ? ' (' + err.code + ')' : '') + '. Please try again.');
      });
  };
})();
</script>""".replace("__CONFIG__", cfg_json)
    body = ('<div class="auth-card"><div style="text-align:center;margin-bottom:24px">'
            f'<div class="auth-logo">{icon("building", 32)}</div>'
            '<h1 style="font-size:20px;margin:0">Google sign-in</h1>'
            '<p style="font-size:13px;color:#475569">You are in your phone browser. '
            'Tap the button, pick your Google account, and a 6-digit code will appear below. '
            'Then switch back to the LABH app and enter that code on the Sign In page.</p></div>'
            '<p><button type="button" id="google-browser-btn" class="google-btn">'
            'Continue with Google</button></p>'
            '<p id="google-browser-error" class="err" style="display:block"></p>'
            '<p id="google-browser-code" style="display:none;text-align:center;font-size:32px;letter-spacing:6px;font-weight:700"></p>'
            '<p id="google-browser-hint" style="display:none;text-align:center;font-size:13px;color:#475569">'
            'This code works once and expires in 10 minutes. Switch back to the LABH app now '
            'and enter it on the Sign In page.</p>'
            '<p style="text-align:center"><a href="/login">Back to Sign In</a></p></div>')
    return ("<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Google sign-in - LABH</title><style>{CSS}{EXTRA_CSS}</style></head><body>"
            f"<div class='auth-bg'>{body}</div>" + js + "</body></html>")


class Forbidden(Exception):
    pass

class NotFound(Exception):
    pass

def scoped_row(table, row_id, business_id):
    """Return row if it belongs to this business; 403 if it belongs to
    another business (isolation proof); 404 if it does not exist."""
    row = q(f"SELECT * FROM {table} WHERE id=?", (row_id,), one=True)
    if row is None:
        raise NotFound(f"{table} #{row_id} not found")
    if row["business_id"] != business_id:
        raise Forbidden("This record belongs to another business.")
    return row

def customer_due(biz, cust_id):
    opening = q("SELECT opening_balance FROM customers WHERE id=?", (cust_id,), one=True)
    due = q("SELECT COALESCE(SUM(due),0) FROM sales WHERE business_id=? AND customer_id=?",
            (biz, cust_id), one=True)[0]
    return (opening[0] if opening else 0) + due

def supplier_due(biz, sup_id):
    opening = q("SELECT opening_balance FROM suppliers WHERE id=?", (sup_id,), one=True)
    due = q("SELECT COALESCE(SUM(due),0) FROM purchases WHERE business_id=? AND supplier_id=?",
            (biz, sup_id), one=True)[0]
    return (opening[0] if opening else 0) + due

def apply_customer_payment(biz, cust_id, amount):
    """Reduce oldest outstanding sales first."""
    left = amount
    for s in q("SELECT id, due FROM sales WHERE business_id=? AND customer_id=? AND due>0 ORDER BY id",
               (biz, cust_id)):
        if left <= 0:
            break
        take = min(left, s["due"])
        exec_db("UPDATE sales SET due=due-?, paid=paid+? WHERE id=?", (take, take, s["id"]))
        left -= take

def apply_supplier_payment(biz, sup_id, amount):
    left = amount
    for p in q("SELECT id, due FROM purchases WHERE business_id=? AND supplier_id=? AND due>0 ORDER BY id",
               (biz, sup_id)):
        if left <= 0:
            break
        take = min(left, p["due"])
        exec_db("UPDATE purchases SET due=due-?, paid=paid+? WHERE id=?", (take, take, p["id"]))
        left -= take

def next_invoice_no(biz):
    n = q("SELECT COUNT(*) FROM sales WHERE business_id=?", (biz,), one=True)[0] + 1
    return f"INV-{n:04d}"

PRICE_TYPES = ["selling", "DP", "RP", "MRP", "Wholesale"]
def price_for(prod, ptype):
    try:
        if ptype == "DP":
            return prod["dp_price"] or prod["purchase_price"]
        if ptype == "RP":
            return prod["rp_price"] or prod["selling_price"]
        if ptype == "MRP":
            return prod["mrp_price"] or prod["selling_price"]
        if ptype == "Wholesale":
            return prod["wholesale_price"] or prod["selling_price"]
    except Exception:
        pass
    return prod["selling_price"]

def tax_config():
    try:
        enabled = get_setting("tax_enabled") == "1"
        rate = float(get_setting("tax_rate") or 0)
        mode = get_setting("tax_mode") or "exclusive"
    except Exception:
        enabled, rate, mode = False, 0.0, "exclusive"
    return enabled, rate, mode

def apply_tax(subtotal):
    enabled, rate, mode = tax_config()
    if not enabled or rate <= 0:
        return 0.0, subtotal
    if mode == "inclusive":
        return subtotal - subtotal / (1 + rate / 100.0), subtotal
    tax = subtotal * rate / 100.0
    return tax, subtotal + tax

def due_age_bucket(days):
    if days <= 30:
        return "0-30"
    if days <= 60:
        return "31-60"
    if days <= 90:
        return "61-90"
    return "90+"

def due_aging(b, kind):
    """Per-party due totals split into 0-30/31-60/61-90/90+ day buckets,
    aged from the sale/purchase date. Opening balances go in 90+."""
    out = {}
    if kind == "customer":
        parties = q("SELECT id, name, opening_balance FROM customers WHERE business_id=?", (b,))
        docs = q("SELECT customer_id, due, sale_date FROM sales WHERE business_id=? AND due>0", (b,))
        key, dfield = "customer_id", "sale_date"
    else:
        parties = q("SELECT id, name, opening_balance FROM suppliers WHERE business_id=?", (b,))
        docs = q("SELECT supplier_id, due, purchase_date FROM purchases WHERE business_id=? AND due>0", (b,))
        key, dfield = "supplier_id", "purchase_date"
    today_d = date.today()
    for pt in parties:
        out[pt["id"]] = {"name": pt["name"], "buckets": {"0-30": 0.0, "31-60": 0.0, "61-90": 0.0, "90+": 0.0}}
        if pt["opening_balance"]:
            out[pt["id"]]["buckets"]["90+"] += pt["opening_balance"]
    for d in docs:
        pid = d[key]
        if pid not in out or not d["due"]:
            continue
        try:
            dd = datetime.strptime(d[dfield], "%Y-%m-%d").date()
            days = (today_d - dd).days
        except Exception:
            days = 999
        out[pid]["buckets"][due_age_bucket(days)] += d["due"]
    return [v for v in out.values() if sum(v["buckets"].values()) > 0]

def compute_health(b):
    """Local health index 0-100 from this business's own SQLite data."""
    t = today()
    d30 = (date.today() - timedelta(days=30)).isoformat()
    d60 = (date.today() - timedelta(days=60)).isoformat()
    cur30 = q("SELECT COALESCE(SUM(total),0) FROM sales WHERE business_id=? AND sale_date>=?", (b, d30), one=True)[0]
    prev30 = q("SELECT COALESCE(SUM(total),0) FROM sales WHERE business_id=? AND sale_date>=? AND sale_date<?", (b, d60, d30), one=True)[0]
    cogs30 = 0.0
    for si in q("SELECT si.qty, COALESCE(si.cost_price, p.purchase_price, 0) AS cp FROM sale_items si JOIN sales s ON s.id=si.sale_id LEFT JOIN products p ON p.id=si.product_id WHERE s.business_id=? AND s.sale_date>=?", (b, d30)):
        cogs30 += si["qty"] * (si["cp"] or 0)
    exp30 = q("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE business_id=? AND expense_date>=?", (b, d30), one=True)[0]
    dues = q("SELECT COALESCE(SUM(due),0) FROM sales WHERE business_id=?", (b,), one=True)[0]
    total_sales = q("SELECT COALESCE(SUM(total),0) FROM sales WHERE business_id=?", (b,), one=True)[0]
    nprod = q("SELECT COUNT(*) FROM products WHERE business_id=?", (b,), one=True)[0]
    nlow = q("SELECT COUNT(*) FROM products WHERE business_id=? AND stock<=low_stock", (b,), one=True)[0]
    score = 100.0
    recs = []
    if prev30 > 0:
        trend = (cur30 - prev30) / prev30 * 100
        if trend < 0:
            score += max(trend, -25.0)
            recs.append(f"Sales fell {abs(trend):.1f}% vs the previous 30 days — follow up top customers and restock best sellers.")
        else:
            recs.append(f"Sales are up {trend:.1f}% vs the previous 30 days. Keep the momentum.")
    elif cur30 > 0:
        recs.append("Sales in the last 30 days with no prior-period baseline — keep recording to build a trend.")
    else:
        score -= 15
        recs.append("No sales in the last 30 days — check stock, pricing and customer follow-ups.")
    if total_sales > 0 and dues > 0:
        ratio = dues / total_sales
        score -= min(ratio * 30, 30)
        recs.append(f"Customer dues are {ratio*100:.1f}% of total sales ({dues:,.2f} outstanding) — collect the oldest dues first.")
    if nprod > 0 and nlow > 0:
        score -= min(nlow / nprod * 20, 20)
        recs.append(f"{nlow} of {nprod} products are at/below low-stock level — reorder soon.")
    if cur30 > 0 and exp30 > 0:
        eratio = exp30 / cur30
        score -= min(eratio * 25, 25)
        if eratio > 0.3:
            recs.append(f"Expenses are {eratio*100:.1f}% of last-30-day sales — review the biggest expense categories.")
    profit30 = cur30 - cogs30 - exp30
    score = max(0, min(100, round(score)))
    status = "Excellent" if score >= 80 else "Good" if score >= 60 else "Needs attention" if score >= 40 else "At risk"
    return {"score": score, "status": status, "sales30": cur30, "prev30": prev30,
            "profit30": profit30, "dues": dues, "low": nlow, "products": nprod,
            "expenses30": exp30, "recs": recs}


# ---------------------------------------------------------------- icons (Lucide-style inline SVG, no emoji)
def _svg(path, size=18):
    return (f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
            f'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{path}</svg>')
_ICON_PATHS = {
 "home": '<path d="M3 10.5 12 3l9 7.5"/><path d="M5 9.5V21h14V9.5"/>',
 "zap": '<path d="M13 2 3 14h7l-1 8 10-12h-7l1-8z"/>',
 "receipt": '<path d="M4 2v20l2-1 2 1 2-1 2 1 2-1 2 1 2-1 2 1V2l-2 1-2-1-2 1-2-1-2 1-2-1-2 1-2-1z"/><path d="M8 7h8M8 12h8"/>',
 "file": '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M9 13h6M9 17h6"/>',
 "users": '<path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87M16 3.13a4 4 0 0 1 0 7.75"/>',
 "package": '<path d="M21 8 12 3 3 8v8l9 5 9-5V8z"/><path d="M3 8l9 5 9-5M12 13v8"/>',
 "folder": '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>',
 "truck": '<path d="M1 3h15v13H1zM16 8h4l3 3v5h-7V8z"/><circle cx="5.5" cy="18.5" r="2.5"/><circle cx="18.5" cy="18.5" r="2.5"/>',
 "wallet": '<path d="M21 12V7H5a2 2 0 0 1 0-4h14v4"/><path d="M3 5v14a2 2 0 0 0 2 2h16v-5"/><path d="M18 12a2 2 0 0 0 0 4h4v-4z"/>',
 "chart": '<path d="M3 3v18h18"/><path d="M7 15l4-6 4 3 5-7"/>',
 "shield": '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/><path d="m9 12 2 2 4-4"/>',
 "sparkles": '<path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z"/><path d="M19 15l.9 2.1L22 18l-2.1.9L19 21l-.9-2.1L16 18l2.1-.9z"/>',
 "settings": '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09a1.65 1.65 0 0 0-1-1.51 1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09a1.65 1.65 0 0 0 1.51-1 1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33h.01a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51h.01a1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82v.01a1.65 1.65 0 0 0 1.51 1H21a2 2 0 1 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>',
 "bell": '<path d="M18 8a6 6 0 0 0-12 0c0 7-3 9-3 9h18s-3-2-3-9"/><path d="M13.7 21a2 2 0 0 1-3.4 0"/>',
 "search": '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
 "menu": '<path d="M3 6h18M3 12h18M3 18h18"/>',
 "user": '<path d="M19 21v-2a4 4 0 0 0-4-4H9a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/>',
 "scan": '<path d="M3 7V5a2 2 0 0 1 2-2h2M17 3h2a2 2 0 0 1 2 2v2M21 17v2a2 2 0 0 1-2 2h-2M7 21H5a2 2 0 0 1-2-2v-2"/><path d="M7 12h10"/>',
 "building": '<rect x="4" y="2" width="16" height="20" rx="2"/><path d="M9 22v-4h6v4M8 6h.01M16 6h.01M12 6h.01M12 10h.01M12 14h.01M16 10h.01M16 14h.01M8 10h.01M8 14h.01"/>',
 "mail": '<rect x="2" y="4" width="20" height="16" rx="2"/><path d="m22 7-10 6L2 7"/>',
 "lock": '<rect x="3" y="11" width="18" height="11" rx="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/>',
 "plus": '<path d="M12 5v14M5 12h14"/>',
 "money": '<rect x="2" y="6" width="20" height="12" rx="2"/><circle cx="12" cy="12" r="2"/><path d="M6 12h.01M18 12h.01"/>',
 "logout": '<path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4"/><path d="m16 17 5-5-5-5M21 12H9"/>',
 "check": '<path d="M20 6 9 17l-5-5"/>',
 "chevron": '<path d="m6 9 6 6 6-6"/>',
 "box": '<path d="M21 8 12 3 3 8v8l9 5 9-5V8z"/><path d="M3 8l9 5 9-5M12 13v8"/>',
}
def icon(name, size=18):
    return _svg(_ICON_PATHS.get(name, _ICON_PATHS["box"]), size)
NAV_ICONS = {"dashboard": "home", "pos": "zap", "sales": "receipt", "invoices": "file", "customers": "users",
             "products": "package", "categories": "folder", "inventory": "box", "import": "file",
             "purchases": "truck", "suppliers": "truck", "payments": "money", "expenses": "wallet",
             "reports": "chart", "health": "shield", "ai": "sparkles", "notifications": "bell",
             "backup": "file", "updates": "plus", "settings": "settings", "scan": "scan",
             "brands": "package", "profile": "user", "security": "shield", "income": "chart"}
EXTRA_CSS = """
.auth-bg{background:linear-gradient(135deg,#ecfdf5,#f0fdfa 45%,#f1f5f9);min-height:100vh;display:flex;align-items:center;justify-content:center;padding:16px}
.auth-card{background:#fff;border:1px solid #f1f5f9;border-radius:24px;box-shadow:0 25px 50px rgba(0,0,0,.12);max-width:448px;width:100%;padding:32px}
.auth-logo{width:64px;height:64px;border-radius:16px;background:linear-gradient(135deg,#059669,#14b8a6);color:#fff;display:flex;align-items:center;justify-content:center;margin:0 auto 16px;box-shadow:0 10px 20px rgba(5,150,105,.3)}
.auth-tabs{display:flex;background:#f1f5f9;padding:4px;border-radius:16px;margin-bottom:24px}
.auth-tabs a{flex:1;text-align:center;padding:10px;border-radius:12px;font-size:12px;font-weight:700;color:#64748b;text-decoration:none}
.auth-tabs a.on{background:#fff;color:#0f172a;box-shadow:0 1px 3px rgba(0,0,0,.1)}
.fieldwrap{position:relative}
.fieldwrap svg{position:absolute;left:12px;top:50%;transform:translateY(-50%);color:#94a3b8;pointer-events:none}
.fieldwrap input{padding-left:42px;height:48px;border-radius:14px}
.auth-submit{width:100%;height:48px;border-radius:14px;background:linear-gradient(135deg,#059669,#14b8a6);font-weight:700}
.google-btn{width:100%;height:48px;background:#fff;color:#1f2937;border:1px solid #d1d5db;border-radius:14px;display:flex;align-items:center;justify-content:center;gap:10px;font-weight:600;cursor:pointer}
.sidebar .navlink{display:flex;align-items:center;gap:10px;justify-content:flex-start}
.sidebar .navlink svg{flex-shrink:0}
.sidebar a .navtxt{flex:1;text-align:left}
.drawer-overlay{display:none}
@media(max-width:767px){.sidebar{position:fixed;left:-280px;top:0;z-index:100;transition:left .2s;box-shadow:0 0 30px rgba(0,0,0,.3)}
.sidebar.open{left:0}
@media(max-width:767px){.sidebar.open{display:block}}
.drawer-overlay.show{display:block;position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:90}
/* UI pass (reference: labhsbm.ai.studio) — light-grey filled inputs,
   grey-blue table header strip, plain bold row links (not green underlined). */
main input,main select,main textarea{background:#f8fafc;border:1px solid #e2e8f0;border-radius:10px}
table th{background:#f1f5f9;color:#475569;text-transform:uppercase;font-size:11px;letter-spacing:.4px;padding:10px 12px;text-align:left}
table td a{color:#0f172a;text-decoration:none;font-weight:600}
table td a:hover{text-decoration:underline}
.hamburger{display:none}
@media(max-width:767px){.hamburger{display:inline-flex;align-items:center;justify-content:center;width:44px;height:44px;border-radius:12px;background:#fff;border:1px solid #e2e8f0;cursor:pointer;flex-shrink:0}}
.bellwrap{position:relative;display:inline-flex}
.bellcount{position:absolute;top:-4px;right:-4px;background:#dc2626;color:#fff;font-size:10px;font-weight:800;min-width:18px;height:18px;border-radius:999px;display:flex;align-items:center;justify-content:center;padding:0 4px}
.avatar{width:40px;height:40px;border-radius:999px;background:#dcfce7;color:#15803d;display:inline-flex;align-items:center;justify-content:center;font-weight:800;text-decoration:none;flex-shrink:0}
.searchwrap{position:relative;flex:1;max-width:520px}
.search-dd{position:absolute;top:48px;left:0;right:0;background:#fff;border:1px solid #e2e8f0;border-radius:12px;box-shadow:0 10px 25px rgba(0,0,0,.12);z-index:50;display:none;max-height:320px;overflow:auto}
.search-dd a{display:block;padding:10px 14px;text-decoration:none;color:#0f172a;font-size:13px;border-bottom:1px solid #f1f5f9}
#labh-loader{position:fixed;inset:0;background:#f2faf5;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:14px;z-index:99999;font-weight:800;color:#15803d}
body.dark{background:#0f172a;color:#e2e8f0}
body.dark .card,body.dark .tile,body.dark .modal,body.dark .search-dd{background:#1e293b;border-color:#334155;color:#e2e8f0}
body.dark .sidebar{background:#111827;color:#e2e8f0}
body.dark .topbar{background:#111827;border-color:#334155}
body.dark input,body.dark select,body.dark textarea{background:#0f172a;color:#e2e8f0;border-color:#334155}
body.dark .bottomnav{background:#1e293b;border-color:#334155}
body.dark .bottomnav a{color:#94a3b8}
body.dark .bottomnav a.active{color:#16a34a}
body.accent-blue{--primary:#2563eb;--primary-hover:#1d4ed8;--primary-light:#dbeafe;--primary-text:#1d4ed8}
body.accent-purple{--primary:#7c3aed;--primary-hover:#6d28d9;--primary-light:#ede9fe;--primary-text:#6d28d9}
body.accent-rose{--primary:#e11d48;--primary-hover:#be123c;--primary-light:#ffe4e6;--primary-text:#be123c}
body.accent-orange{--primary:#ea580c;--primary-hover:#c2410c;--primary-light:#ffedd5;--primary-text:#c2410c}
.catalog{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:12px}
.catalog .prod{cursor:pointer}
.scanbox video{width:100%;max-width:480px;border-radius:16px;background:#000}
.notif-unread{background:#f0fdf4}
.tpl-minimal .card{border-radius:6px}
.tpl-bold .card{border:2px solid #0f172a}
.tpl-receipt .card{max-width:420px;margin-left:auto;margin-right:auto;font-family:monospace}
/* v2.2 home page: top bar stays on ONE row at every width; the search
   input shrinks instead of pushing the row into broken wrapped lines. */
.topbar{flex-wrap:nowrap}
.topbar form{min-width:0}
.topbar input{min-width:0;flex:1}
.searchwrap{min-width:0}
.bellwrap{flex-shrink:0}
.topbar .badge{flex-shrink:0;white-space:nowrap}
/* v2.2 top bar: online status is just a green dot — no text pill. */
.online-dot{width:12px;height:12px;border-radius:50%;background:#16a34a;box-shadow:0 0 0 3px #dcfce7;flex-shrink:0}
/* v2.2 home page: mobile banner + desktop KPI rows. Only the dashboard
   renders .dash-mobile / .dash-desktop; CSS picks one by width. */
.dash-mobile{display:none}
@media(max-width:767px){.dash-mobile{display:block}.dash-desktop{display:none}}
.m-banner{background:linear-gradient(135deg,#059669,#16a34a 55%,#22c55e);border-radius:20px;padding:18px;color:#fff;margin:4px 0 12px;box-shadow:0 8px 20px rgba(5,150,105,.25)}
.m-banner .m-greet{font-size:22px;font-weight:800;line-height:1.25}
.m-banner .m-sub{font-size:13px;opacity:.9;margin-top:4px}
.m-banner select{width:auto;background:rgba(255,255,255,.2);color:#fff;border:1px solid rgba(255,255,255,.4);font-weight:700}
.m-banner select option{color:#0f172a}
.m-stats{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-top:16px}
.m-stat{background:rgba(255,255,255,.16);border-radius:12px;padding:10px 6px;text-align:center;min-width:0}
.m-stat .lbl{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;opacity:.9}
.m-stat .val{font-size:14px;font-weight:800;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.m-stat .pct{font-size:10px;font-weight:700;opacity:.9}
.m-mini3{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin:12px 0}
.m-mini{background:#fff;border:1px solid rgba(226,232,240,.8);border-radius:16px;padding:12px;min-width:0}
.m-mini .lbl{font-size:9px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#94a3b8}
.m-mini .val{font-size:15px;font-weight:800;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.m-alert{background:#fff;border:1px solid rgba(226,232,240,.8);border-radius:16px;padding:14px 16px;margin:12px 0}
.m-alert .ahead{font-size:12px;font-weight:800;letter-spacing:.5px}
.kpi-row{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:12px 0}
.dash-row3{display:grid;grid-template-columns:1.5fr 1fr 1fr;gap:12px;margin:12px 0}
.dash-row3 .card,.kpi-row .tile{margin:0}
.mint-quote{background:#f0fdf4;border-radius:12px;padding:12px;margin-top:10px;font-size:13px}
.date-chip{display:inline-block;padding:4px 8px;border-radius:999px;font-size:11px;font-weight:700;background:#f1f5f9;color:#475569;margin:2px}
.date-chip.today{background:#dcfce7;color:#15803d;text-decoration:underline}
/* v2.3 home page: top bar brand + avatar initials + online dot on the
   avatar corner (no text pill anywhere); dark-green bottom nav with a
   raised centre Scan button; unified home sections (one layout, CSS
   picks scroll-row on phones / grid on desktop). Banner gradient lives
   in this class, never as an inline div style. */
.topbrand{display:flex;align-items:center;gap:8px;flex-shrink:0;min-width:0}
.topbrand .topmark{width:36px;height:36px;border-radius:10px;background:var(--primary);color:#fff;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.topbrand .topword{font-weight:900;font-size:15px;line-height:1.1;color:var(--primary)}
.topbrand .tag{display:block;font-size:9px;font-weight:700;color:#64748b;letter-spacing:.2px}
.avatarwrap{position:relative;flex-shrink:0}
.online-dot.on-avatar{position:absolute;bottom:1px;right:1px;width:11px;height:11px;box-shadow:0 0 0 2px #fff}
.avatar-arrow{display:inline-flex;color:#64748b;flex-shrink:0}
@media(max-width:480px){.topbrand .tag{display:none}.topbrand .topmark{width:32px;height:32px}}
.v23-banner{background:linear-gradient(135deg,#047857,#16a34a 55%,#22c55e);border-radius:20px;padding:18px;color:#fff;margin:4px 0 14px;box-shadow:0 8px 20px rgba(5,150,105,.25);display:flex;gap:16px;justify-content:space-between;align-items:flex-start}
.v23-banner .greet{font-size:22px;font-weight:800;line-height:1.25}
.v23-banner .sub{font-size:13px;opacity:.92;margin-top:4px}
.v23-datepill{display:inline-block;background:rgba(255,255,255,.22);border:1px solid rgba(255,255,255,.4);border-radius:999px;padding:4px 10px;font-size:11px;font-weight:700;margin-top:10px}
.v23-banner select{width:auto;background:rgba(255,255,255,.2);color:#fff;border:1px solid rgba(255,255,255,.4);font-weight:700;margin-top:10px}
.v23-banner select option{color:#0f172a}
.v23-quote{text-align:right;flex-shrink:0}
.v23-quote b{font-size:16px;line-height:1.3;display:block}
.v23-bars{display:flex;align-items:flex-end;gap:4px;height:44px;justify-content:flex-end;margin-top:8px}
.v23-bars span{width:10px;border-radius:3px 3px 0 0;background:rgba(255,255,255,.85)}
@media(max-width:767px){.v23-banner{flex-wrap:wrap}.v23-quote{flex-basis:100%;text-align:left}.v23-quote b{font-size:14px}.v23-bars{justify-content:flex-start}}
.v23-stats{display:grid;grid-template-columns:1fr 1fr;gap:12px;padding-bottom:4px;margin:12px 0}
.v23-stat{background:#fff;border:1px solid rgba(226,232,240,.8);border-radius:16px;padding:14px;box-shadow:0 1px 2px rgba(0,0,0,.05)}
.v23-stat .chip{width:40px;height:40px;border-radius:12px;display:flex;align-items:center;justify-content:center;margin-bottom:8px}
.v23-stat .lbl{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#94a3b8}
.v23-stat .val{font-size:19px;font-weight:900;margin-top:2px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.v23-stat .vs{font-size:11px;font-weight:700}
.v23-stat .vs.up{color:#16a34a}.v23-stat .vs.down{color:#dc2626}.v23-stat .vs.flat{color:#64748b}
.v23-spark{display:flex;align-items:flex-end;gap:2px;height:26px;margin-top:8px}
.v23-spark span{flex:1;border-radius:2px 2px 0 0;background:#bbf7d0;min-height:2px}
@media(min-width:768px){.v23-stats{display:grid;grid-template-columns:repeat(4,1fr);overflow:visible}.v23-stat{flex:none}}
.v23-grid2{display:grid;grid-template-columns:1fr;gap:12px;margin:12px 0}
@media(min-width:768px){.v23-grid2{grid-template-columns:1.5fr 1fr}}
.v23-grid2 .card,.v23-stats .v23-stat{margin:0}
.v23-toggle{display:flex;gap:6px;flex-wrap:wrap}
.v23-toggle a{padding:6px 12px;border-radius:999px;background:#f1f5f9;color:#475569;text-decoration:none;font-size:12px;font-weight:700}
.v23-toggle a.on{background:#16a34a;color:#fff}
.v23-ovrow{display:flex;gap:10px;align-items:center;padding:10px 0;border-bottom:1px solid #f1f5f9}
.v23-ovrow:last-child{border-bottom:0}
.v23-ovrow .chip{width:38px;height:38px;border-radius:12px;display:flex;align-items:center;justify-content:center;flex-shrink:0}
.v23-ovrow .vs{margin-left:auto;font-size:12px;font-weight:700;text-align:right}
.v23-tiles{display:grid;grid-template-columns:repeat(2,1fr);gap:10px}
@media(min-width:768px){.v23-tiles{grid-template-columns:repeat(3,1fr)}}
.v23-tile{background:#f8fafc;border:1px solid #e2e8f0;border-radius:14px;padding:12px}
.v23-tile .lbl{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.5px;color:#64748b}
.v23-tile .val{font-size:18px;font-weight:900;margin-top:2px}
.pill{display:inline-block;padding:2px 10px;border-radius:999px;font-size:11px;font-weight:800}
.pill.sale{background:#dcfce7;color:#15803d}.pill.purchase{background:#dbeafe;color:#1d4ed8}.pill.payment{background:#fef3c7;color:#92400e}
@media(max-width:767px){.bottomnav{background:#14532d;border-top:1px solid #14532d;box-shadow:0 -4px 16px rgba(0,0,0,.25)}
.bottomnav a{color:#bbf7d0}.bottomnav a.active{color:#fff}
.bottomnav .scan-raised .ico{width:56px;height:56px;border-radius:50%;background:#16a34a;color:#fff;display:flex;align-items:center;justify-content:center;transform:translateY(-16px);box-shadow:0 6px 16px rgba(22,163,74,.5);margin-bottom:-12px}}
body.dark .v23-stat,body.dark .v23-tile{background:#1e293b;border-color:#334155}
body.dark .v23-toggle a{background:#334155;color:#e2e8f0}
/* BOARD (mobile UI board) — search pills, filter pills, icon tiles, status
   pills, full-width action buttons, icon lists, hub menus, bar charts. */
.topback{display:none;background:none;border:0;color:#0f172a;cursor:pointer;padding:4px;line-height:0}
.mobtitle{display:none;font-size:17px;font-weight:800;color:#0f172a;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1;min-width:0}
.mobsearch{display:none;color:#0f172a;line-height:0;flex-shrink:0;padding:4px}
.bd-search{display:flex;align-items:center;gap:8px;background:#f1f5f9;border:1px solid #e2e8f0;border-radius:999px;padding:10px 14px;margin:0 0 12px}
.bd-search svg{color:#94a3b8;flex-shrink:0}
.bd-search input{border:0;background:transparent;padding:0;min-height:0;width:100%;font-size:14px}
.bd-search input:focus{outline:none;box-shadow:none}
.bd-pills{display:flex;gap:8px;flex-wrap:wrap;margin:0 0 14px}
.bd-pills a{padding:7px 16px;border-radius:999px;background:#fff;border:1px solid #e2e8f0;color:#475569;text-decoration:none;font-size:13px;font-weight:700}
.bd-pills a.on{background:#16a34a;border-color:#16a34a;color:#fff}
.tile-ic{width:40px;height:40px;border-radius:12px;display:inline-flex;align-items:center;justify-content:center;flex-shrink:0;background:#dcfce7;color:#15803d}
.tile-ic.blue{background:#dbeafe;color:#1d4ed8}.tile-ic.amber{background:#fef3c7;color:#92400e}
.tile-ic.red{background:#fee2e2;color:#b91c1c}.tile-ic.violet{background:#ede9fe;color:#6d28d9}
.tile-ic.grey{background:#f1f5f9;color:#475569}
.pill.paid{background:#dcfce7;color:#15803d}.pill.due{background:#fee2e2;color:#b91c1c}
.pill.partial{background:#ffedd5;color:#c2410c}.pill.low{background:#fef3c7;color:#92400e}
.pr{color:#16a34a;font-weight:800}
.stock-ok{color:#16a34a;font-weight:800}.stock-low{color:#d97706;font-weight:800}.stock-out{color:#dc2626;font-weight:800}
.btn-block{display:block;width:100%;text-align:center;padding:14px;font-size:15px;font-weight:800;border-radius:14px}
.bd-menu{display:flex;flex-direction:column;gap:2px}
.bd-menu a{display:flex;align-items:center;gap:12px;padding:12px 4px;border-bottom:1px solid #f1f5f9;text-decoration:none;color:#0f172a}
.bd-menu a:last-child{border-bottom:0}
.bd-menu .lbl{flex:1;font-weight:700;font-size:14px}
.bd-menu .chev{color:#94a3b8;font-size:18px}
.bd-hub{display:flex;gap:14px;align-items:center}
.bd-avatar{width:64px;height:64px;border-radius:50%;background:#16a34a;color:#fff;display:flex;align-items:center;justify-content:center;font-size:24px;font-weight:900;flex-shrink:0}
.bd-bars{display:flex;align-items:flex-end;gap:8px;height:120px;padding:8px 4px 0;border-bottom:1px solid #e2e8f0}
.bd-bars .bar{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end;height:100%}
.bd-bars .bar i{display:block;width:100%;max-width:26px;border-radius:6px 6px 0 0;background:#16a34a}
.bd-bars .bar span{font-size:10px;color:#64748b;margin-top:4px;white-space:nowrap}
.bd-summary{border-top:1px solid #f1f5f9;margin-top:12px;padding-top:8px}
.bd-summary .row{display:flex;justify-content:space-between;padding:6px 0;font-size:14px}
.bd-summary .row.total{font-weight:900;font-size:16px;border-top:1px solid #e2e8f0;margin-top:6px;padding-top:10px}
.bd-summary .due{color:#dc2626;font-weight:800}
.bd-cartrow{display:grid;grid-template-columns:1fr auto;gap:6px;align-items:center;padding:12px 0;border-bottom:1px solid #f1f5f9}
.bd-cartrow .meta{font-size:12px;color:#64748b}
.bd-cartrow .row-total{font-weight:800;color:#16a34a;text-align:right}
.stepper{display:inline-flex;align-items:center;gap:0;border:1px solid #e2e8f0;border-radius:999px;overflow:hidden}
.stepper button{min-height:32px!important;padding:4px 12px!important;background:#f8fafc;border:0;font-weight:900;font-size:16px;cursor:pointer}
.stepper input{border:0!important;background:#fff!important;text-align:center;width:52px!important;padding:6px 2px!important;min-height:0!important}
.line-remove{background:none;border:0;color:#dc2626;font-size:20px;cursor:pointer;padding:2px 6px}
@media(max-width:767px){
main>h1{display:none}
.topbar .topbrand{display:none}
.topbar .searchwrap{display:none}
.topbar.inner .hamburger{display:none}
.topbar.inner .topback{display:inline-flex}
.mobtitle{display:block}
.mobsearch{display:inline-flex}
}
"""


def seed_notifications(b, cur):
    """Create today's stored low-stock / overdue alerts (deduped by title+day)."""
    try:
        t = today()
        if get_setting("notif_low_stock") == "0":
            low = []
        else:
            low = q("SELECT name, stock, low_stock FROM products WHERE business_id=? AND stock<=low_stock ORDER BY stock", (b,))
        overdue = []
        if get_setting("notif_overdue") != "0":
            overdue = q("SELECT s.*, c.name AS cname FROM sales s LEFT JOIN customers c ON c.id=s.customer_id WHERE s.business_id=? AND s.due>0 AND s.sale_date<=?", (b, (date.today() - timedelta(days=30)).isoformat()))
        for r in low:
            title = f"Low stock: {r['name']}"
            if not q("SELECT id FROM notifications WHERE business_id=? AND title=? AND substr(created_at,1,10)=?", (b, title, t), one=True):
                exec_db("INSERT INTO notifications(business_id,title,body,created_at) VALUES(?,?,?,?)", (b, title, f"{r['stock']} left (threshold {r['low_stock']})", now_iso()), touch=False)
        for r in overdue:
            title = f"Overdue due: {r['invoice_no']}"
            if not q("SELECT id FROM notifications WHERE business_id=? AND title=? AND substr(created_at,1,10)=?", (b, title, t), one=True):
                exec_db("INSERT INTO notifications(business_id,title,body,created_at) VALUES(?,?,?,?)", (b, title, f"{r['cname'] or 'Walk-in'} owes {money(r['due'], cur)} since {r['sale_date']}", now_iso()), touch=False)
    except Exception:
        pass

def unread_count(b):
    try:
        return q("SELECT COUNT(*) FROM notifications WHERE business_id=? AND is_read=0", (b,), one=True)[0]
    except Exception:
        return 0

def perm_matrix():
    try:
        return json.loads(get_setting("perm_matrix") or "{}")
    except Exception:
        return {}

def can_do(user, action):
    """Staff permission matrix. Defaults: owner/admin all; staff may sell + view reports."""
    m = perm_matrix()
    if user["role"] in ("owner", "admin"):
        return True
    if action in m and "staff" in m[action]:
        return bool(m[action]["staff"])
    return action in ("create_sale", "view_reports")

def invoice_template_setting():
    t = get_setting("invoice_template") or "modern"
    return t if t in ("modern", "classic", "minimal", "bold", "receipt") else "modern"

def payment_methods_setting():
    raw = (get_setting("payment_methods") or "").strip()
    if raw:
        return [x.strip() for x in raw.split(",") if x.strip()]
    return ["Cash", "Bank", "Card", "Mobile Banking", "Other"]

# ---------------------------------------------------------------- pages
NAV = [("dashboard", "Dashboard"), ("pos", "Quick Sale POS"), ("products", "Products"),
       ("categories", "Categories"), ("inventory", "Inventory"), ("import", "Bulk Import"),
       ("customers", "Customers"), ("suppliers", "Suppliers"), ("sales", "Sales"),
       ("brands", "Brands"), ("income", "Income"),
       ("purchases", "Purchases"), ("invoices", "Invoices"), ("payments", "Payments"),
       ("expenses", "Expenses"), ("reports", "Reports"), ("health", "Business Health"),
       ("ai", "Labh AI"), ("notifications", "Notifications"),
       ("backup", "Backup"), ("updates", "Updates"), ("settings", "Settings")]
NAV_GROUPS = [("MAIN", ["dashboard", "pos", "sales", "invoices", "customers"]),
              ("INVENTORY", ["products", "categories", "inventory", "brands"]),
              ("PURCHASE", ["purchases", "suppliers"]),
              ("FINANCE", ["expenses", "income", "payments"]),
              ("REPORTS", ["reports", "health", "ai"]),
              ("SYSTEM", ["import", "notifications", "backup", "updates", "settings"])]
NAV_BADGES = {"pos": ("POS", "pos"), "inventory": ("Stock", "stock"), "ai": ("AI", "ai")}
ACTIVE_BY_TITLE = {"Dashboard": "dashboard", "Quick Sale POS": "pos", "Products": "products",
                   "Categories": "categories", "Inventory": "inventory", "Bulk Import": "import",
                   "Customers": "customers", "Suppliers": "suppliers", "Sales": "sales",
                   "Purchases": "purchases", "Invoices": "invoices", "Payments": "payments",
                   "Expenses": "expenses", "Brands": "brands", "Income Categories": "income", "Reports": "reports", "Business Health": "health",
                   "Labh AI": "ai", "Notifications": "notifications", "Backup": "backup",
                   "Updates": "updates", "APK Updates": "updates", "Settings": "settings", "Business Settings": "settings",
                   "Scan": "scan", "My Profile": "profile", "Security": "security", "Stock Reconciliation": "inventory",
                   "IMEI Transfer": "inventory", "Expenses Overview": "expenses", "Due List — Customers": "customers",
                   "Due List — Suppliers": "suppliers",
                   "Users": "settings"}
BOTTOMNAV = [("dashboard", "Home", "home"), ("sales", "Sales", "receipt"),
             ("scan", "Scan", "scan"), ("products", "Products", "package"),
             ("reports", "Reports", "chart")]
BOTTOM_ACTIVE = {"dashboard": "dashboard", "sales": "sales", "invoices": "sales",
                 "pos": "scan", "scan": "scan", "products": "products", "categories": "products",
                 "inventory": "products", "import": "products", "reports": "reports",
                 "health": "reports"}
BN = {"Dashboard": "ড্যাশবোর্ড", "Quick Sale POS": "কুইক সেল POS", "Products": "প্রোডাক্ট",
      "Categories": "ক্যাটাগরি", "Inventory": "ইনভেন্টরি", "Bulk Import": "বাল্ক ইমপোর্ট",
      "Customers": "কাস্টমার", "Suppliers": "সাপ্লায়ার", "Sales": "সেলস",
      "Purchases": "পারচেজ", "Invoices": "ইনভয়েস", "Payments": "পেমেন্ট",
      "Expenses": "খরচ", "Reports": "রিপোর্ট", "Business Health": "বিজনেস হেলথ",
      "Labh AI": "লাভ AI", "Notifications": "নোটিফিকেশন", "Backup": "ব্যাকআপ",
      "Updates": "আপডেট", "Settings": "সেটিংস", "Login": "লগইন", "Register": "রেজিস্টার"}
def tr(text):
    try:
        return BN.get(text, text) if get_setting("lang") == "bn" else text
    except Exception:
        return text

# ---------------------------------------------------------------- backup
def _copy_db(path):
    """Consistent copy of the live DB via the SQLite backup API (safe
    while the app is running)."""
    src = sqlite3.connect(DB_PATH, timeout=30)
    try:
        src.execute("PRAGMA busy_timeout=30000")
        dst = sqlite3.connect(path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()

def _unique_backup_path(stamp, infix):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    path = os.path.join(BACKUP_DIR, f"labh-backup-{stamp}{infix}.db")
    n = 2
    while os.path.exists(path):
        path = os.path.join(BACKUP_DIR, f"labh-backup-{stamp}{infix}-{n}.db")
        n += 1
    return path

def make_backup():
    """Manual/export backup. Also kept in backups/ on this device.
    Returns (path, download_filename)."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = _unique_backup_path(stamp, "")
    _copy_db(path)
    return path, os.path.basename(path)

# --- automatic backup: one copy after each burst of business writes ---
AUTO_KEEP = 10
_AUTO = {"dirty": False, "last_write": 0.0}
_AUTO_LOCK = threading.Lock()

def mark_dirty():
    with _AUTO_LOCK:
        _AUTO["dirty"] = True
        _AUTO["last_write"] = time.time()

def auto_backup():
    """Create a timestamped auto-backup and rotate: keep the latest 10
    auto-backups, delete older ones. Manual exports and before-restore
    safety copies are never rotated away (different filenames)."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = _unique_backup_path(stamp, "-auto")
    _copy_db(path)
    # Sync hub: after any finished work, the auto-backup also becomes
    # the latest sync file, so the other device picks it up on open.
    publish_sync(path)
    autos = sorted((os.path.join(BACKUP_DIR, f) for f in os.listdir(BACKUP_DIR)
                    if "-auto" in f and f.endswith(".db")),
                   key=lambda p: os.path.getmtime(p), reverse=True)
    for old in autos[AUTO_KEEP:]:
        try:
            os.remove(old)
        except OSError:
            pass
    return path

def _auto_backup_loop():
    """Daemon: when writes have been quiet for 2s, save one auto-backup.
    The quiet period is what keeps a multi-write action (a sale writes
    header, items and stock moves) from being backed up half-finished."""
    while True:
        time.sleep(2)
        with _AUTO_LOCK:
            due = _AUTO["dirty"] and time.time() - _AUTO["last_write"] >= 2
        if not due:
            continue
        if not LOCK.acquire(blocking=False):
            continue  # an export/restore holds the lock; retry next cycle
        try:
            auto_backup()
            with _AUTO_LOCK:
                _AUTO["dirty"] = False
        except Exception:
            pass  # a failed auto-backup must never break the app
        finally:
            LOCK.release()

def start_auto_backup_thread():
    threading.Thread(target=_auto_backup_loop, daemon=True).start()


# ---------------------------------------------------------------- sync via
# Google Drive (sync-on-open, no server, no Drive API)
# One shared backup file in a sync folder is the hub for PC + phone:
# the PC app and the phone APK use the same DB format, so a backup
# exported on one device restores identically on the other. This is a
# manual/shared-folder flow, NOT automatic background Drive API sync.
SYNC_FILE = "labh-sync.db"
SYNC_META = "labh-sync.json"


def sync_paths():
    """(sync_dir, sync_db_path, sync_meta_path) from Settings."""
    d = get_setting("sync_dir").strip()
    return d, os.path.join(d, SYNC_FILE), os.path.join(d, SYNC_META)


def _hub_ts(d):
    """Timestamp of the sync file already in the folder, or ''."""
    try:
        with open(os.path.join(d, SYNC_META)) as f:
            ts = json.load(f).get("timestamp", "") or ""
        if ts:
            return ts
    except Exception:
        pass
    try:
        return datetime.fromtimestamp(
            os.path.getmtime(os.path.join(d, SYNC_FILE))).strftime("%Y%m%d-%H%M%S")
    except OSError:
        return ""


def publish_sync(path):
    """Copy a backup DB into the sync folder as the latest sync file,
    with a timestamp marker. Returns the timestamp, or '' if no sync
    folder is set, publishing failed, or publishing would clobber a
    newer hub file from a device that has never synced (that device
    must sync first instead). Never raises."""
    d = get_setting("sync_dir").strip()
    if not d:
        return ""
    try:
        os.makedirs(d, exist_ok=True)
        hub = _hub_ts(d)
        last = get_setting("last_sync_ts")
        if hub and (not last or hub > last):
            set_setting("last_sync_status",
                        "hub file is newer — sync on open first", touch=False)
            return ""
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        tmp = os.path.join(d, ".labh-sync.tmp")
        shutil.copyfile(path, tmp)
        os.replace(tmp, os.path.join(d, SYNC_FILE))
        with open(os.path.join(d, SYNC_META), "w") as f:
            json.dump({"timestamp": ts, "source": "labh"}, f)
        set_setting("last_sync_ts", ts, touch=False)
        set_setting("last_sync_status", "published " + ts, touch=False)
        return ts
    except Exception:
        return ""


def _db_mtime():
    m = 0.0
    for p in (DB_PATH, DB_PATH + "-wal", DB_PATH + "-shm"):
        try:
            m = max(m, os.path.getmtime(p))
        except OSError:
            pass
    return m


def sync_check():
    """Sync-first check, run on dashboard load and on Sync-now.

    Returns (action, message) with action in:
      none     — no sync folder set, or sync file is not newer
      restored — sync file restored (latest Drive backup wins)
      conflict — restored, but local data had changed since the last
                 sync, so the pre-sync local DB was kept as a safety
                 copy first (see the message)
      error    — sync folder/file problem; local data is shown, the
                 app is never blocked by a sync failure
    Never raises. The caller must re-validate the session after a
    restore, because sessions live inside the restored database."""
    try:
        d, sync_db, sync_meta = sync_paths()
        if not d:
            return ("none", "")
        if not os.path.isfile(sync_db):
            set_setting("last_sync_error",
                        "Sync file not found in the sync folder.", touch=False)
            return ("error", "Sync: no sync file found in the sync folder — "
                             "showing local data. Export a backup to the "
                             "sync folder from the other device first.")
        ts = ""
        try:
            with open(sync_meta) as f:
                ts = json.load(f).get("timestamp", "") or ""
        except Exception:
            ts = ""
        if not ts:
            try:
                ts = datetime.fromtimestamp(
                    os.path.getmtime(sync_db)).strftime("%Y%m%d-%H%M%S")
            except OSError:
                ts = ""
        last = get_setting("last_sync_ts")
        if ts and last and ts <= last:
            return ("none", "")
        ok, problem = validate_backup(sync_db)
        if not ok:
            set_setting("last_sync_error",
                        "Sync file rejected: " + problem, touch=False)
            return ("error", "Sync: the sync file was rejected ("
                             + problem + ") — showing local data.")
        # Is there unsynced local work that this restore would replace?
        conflict = False
        if last:
            try:
                last_epoch = datetime.strptime(last, "%Y%m%d-%H%M%S").timestamp()
            except Exception:
                last_epoch = 0.0
            if _db_mtime() > last_epoch + 1:
                conflict = True
        else:
            try:
                n_biz = q("SELECT COUNT(*) FROM businesses", one=True)[0]
            except Exception:
                n_biz = 0
            conflict = bool(n_biz)
        with LOCK:
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            safety = _unique_backup_path(stamp, "-before-sync")
            _copy_db(safety)
            conn = sqlite3.connect(DB_PATH, timeout=30)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
            shutil.copyfile(sync_db, DB_PATH)
            for suffix in ("-wal", "-shm"):
                try:
                    os.remove(DB_PATH + suffix)
                except OSError:
                    pass
            init_db()
        if ts:
            set_setting("last_sync_ts", ts, touch=False)
        if conflict:
            set_setting("last_sync_status", "conflict " + (ts or ""), touch=False)
            return ("conflict",
                    "Synced from Drive, but local data had changed since the "
                    "last sync — a safety copy of your previous local data "
                    "was saved in backups/ before restoring. Please log in "
                    "again, because sessions come from the synced backup.")
        set_setting("last_sync_status", "synced " + (ts or ""), touch=False)
        return ("restored",
                "Synced from Drive. Please log in again, because sessions "
                "come from the synced backup.")
    except Exception as e:
        try:
            set_setting("last_sync_error", str(e), touch=False)
        except Exception:
            pass
        return ("error", "Sync failed (" + str(e) + ") — showing local data.")

def validate_backup(path):
    """Check a candidate restore file without touching live data.
    Returns (ok, missing_tables_or_error)."""
    try:
        with open(path, "rb") as f:
            if f.read(16) != b"SQLite format 3\x00":
                return False, "not a SQLite database file"
    except OSError as e:
        return False, str(e)
    try:
        conn = sqlite3.connect("file:" + quote(path) + "?mode=ro", uri=True)
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
    except sqlite3.Error as e:
        return False, str(e)
    missing = sorted(EXPECTED_TABLES - tables)
    if missing:
        return False, "missing LABH tables: " + ", ".join(missing)
    return True, ""

def restore_db_from_bytes(file_bytes):
    """Shared restore path (logged-in /backup/restore AND v2.5
    unauthenticated /sync-restore): validate first (SQLite header +
    all 15 LABH tables), save a safety copy of the live DB, then
    replace it. Returns (ok, problem). Live data is untouched unless
    validation passes."""
    if not file_bytes or len(file_bytes) > MAX_RESTORE_BYTES:
        return False, "No backup file uploaded, or the file is over 50 MB."
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".db", prefix="labh-restore-")
        with os.fdopen(fd, "wb") as f:
            f.write(file_bytes)
        ok, problem = validate_backup(tmp)
        if not ok:
            return False, problem
        with LOCK:
            safety_path, _ = make_backup()
            safety = os.path.join(BACKUP_DIR, os.path.basename(safety_path).replace(
                "labh-backup-", "labh-backup-before-restore-"))
            os.rename(safety_path, safety)
            conn = sqlite3.connect(DB_PATH, timeout=30)
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            finally:
                conn.close()
            shutil.copyfile(tmp, DB_PATH)
            for suffix in ("-wal", "-shm"):
                try:
                    os.remove(DB_PATH + suffix)
                except OSError:
                    pass
            init_db()
        return True, ""
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass

def drive_list_drive_backups(access_token):
    """v2.9 broad Drive search: every non-trashed *.db file whose name
    contains 'labh', newest first. Returns a list of {id, name}, [] when
    no backup exists, or None when the listing itself failed (bad/
    expired token, network error). The token is used once, in this call
    only, and is never stored, logged or put in an error message.
    TEST ONLY: with LABH_FIREBASE_STUB=1 no network is used —
    'stub-drive-backup' lists one broadly-named backup (the file named
    by LABH_STUB_DRIVE_FILE), 'stub-drive-invalid' lists one invalid
    backup, 'stub-drive-multi' lists an invalid backup FIRST and a
    valid one second (so callers must skip invalid candidates, not
    stop at the first), anything else lists nothing.
    """
    if not access_token:
        return None
    if os.environ.get("LABH_FIREBASE_STUB") == "1":
        if access_token == "stub-drive-backup":
            return [{"id": "stub-valid", "name": "labh-backup-2026-10-26.db"}]
        if access_token == "stub-drive-invalid":
            return [{"id": "stub-invalid", "name": "labh-backup.db"}]
        if access_token == "stub-drive-multi":
            return [{"id": "stub-invalid", "name": "labh-old.db"},
                    {"id": "stub-valid", "name": "labh-backup-2026-10-26.db"}]
        return []
    files, _kind = drive_list_drive_backups_detailed(access_token)
    return files


def drive_list_drive_backups_detailed(access_token):
    """v2.12: like drive_list_drive_backups, but also reports WHY a
    listing failed: (files_or_None, kind) with kind 'ok', 'auth'
    (Drive answered 401/403 — the sign-in's token carries no Drive
    scope, the classic returning-user case), 'network' (unreachable),
    or 'other'. Callers must never put the token in any message."""
    if not access_token:
        return None, "auth"
    if os.environ.get("LABH_FIREBASE_STUB") == "1":
        files = drive_list_drive_backups(access_token)
        return files, ("ok" if files is not None else "other")
    try:
        import urllib.parse
        qs = urllib.parse.urlencode({
            "spaces": "drive",
            "q": "trashed = false and name contains 'labh'",
            "orderBy": "modifiedTime desc",
            "pageSize": "10",
            "fields": "files(id,name,modifiedTime)",
        })
        req = urllib.request.Request(
            "https://www.googleapis.com/drive/v3/files?" + qs,
            headers={"Authorization": "Bearer " + access_token})
        with urllib.request.urlopen(req, timeout=15) as resp:
            listing = json.loads(resp.read().decode())
        files = listing.get("files") or []
        out = []
        for f in files:
            if not isinstance(f, dict) or not f.get("id"):
                continue
            name = str(f.get("name") or "")
            if name.lower().endswith(".db"):
                out.append({"id": f["id"], "name": name})
        return out, "ok"
    except urllib.error.HTTPError as e:
        return None, ("auth" if e.code in (401, 403) else "other")
    except Exception:
        return None, "network"


def drive_download_drive_file(access_token, file_id):
    """Download one Drive file's bytes, or None on ANY failure
    (bad token, download error, oversize). Never logs the token."""
    if not access_token or not file_id:
        return None
    if os.environ.get("LABH_FIREBASE_STUB") == "1":
        if file_id == "stub-valid":
            path = os.environ.get("LABH_STUB_DRIVE_FILE") or ""
            try:
                with open(path, "rb") as f:
                    data = f.read(MAX_RESTORE_BYTES + 1)
                return data if len(data) <= MAX_RESTORE_BYTES else None
            except OSError:
                return None
        if file_id == "stub-invalid":
            return b"not a sqlite database at all"
        return None
    try:
        req = urllib.request.Request(
            "https://www.googleapis.com/drive/v3/files/" + file_id + "?alt=media",
            headers={"Authorization": "Bearer " + access_token})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read(MAX_RESTORE_BYTES + 1)
        return data if len(data) <= MAX_RESTORE_BYTES else None
    except Exception:
        return None


def drive_upload_file(access_token, data, file_id=None):
    """v2.13: save a database to Google Drive as 'labh-sync.db'.
    With a known file_id the content is UPDATED (no duplicates);
    otherwise a new file is CREATED (multipart upload). Returns
    (file_id_or_None, kind) with kind None on success, else
    'auth' (401/403 — the token carries no Drive write scope),
    'network', 'too_big', or 'other'. The token is used once, in
    this call only, and is never stored, logged or put in a message.
    TEST ONLY: with LABH_FIREBASE_STUB=1 the token 'stub-drive-save'
    pretends a successful upload and returns a fake file id."""
    if not access_token:
        return None, "auth"
    if not data or len(data) > MAX_RESTORE_BYTES:
        return None, "too_big"
    if os.environ.get("LABH_FIREBASE_STUB") == "1":
        if access_token == "stub-drive-save":
            return "stub-file-id", None
        return None, "other"
    try:
        import urllib.parse
        if file_id:
            req = urllib.request.Request(
                "https://www.googleapis.com/upload/drive/v3/files/"
                + urllib.parse.quote(file_id) + "?uploadType=media",
                data=data, method="PATCH",
                headers={"Authorization": "Bearer " + access_token,
                         "Content-Type": "application/octet-stream"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                out = json.loads(resp.read().decode())
            return out.get("id"), None
        boundary = "LABH-DRIVE-UPLOAD"
        meta = json.dumps({"name": "labh-sync.db",
                           "mimeType": "application/octet-stream"}).encode()
        body = (b"--" + boundary.encode() + b"\r\n"
                b"Content-Type: application/json; charset=UTF-8\r\n\r\n"
                + meta + b"\r\n--" + boundary.encode() + b"\r\n"
                b"Content-Type: application/octet-stream\r\n\r\n"
                + data + b"\r\n--" + boundary.encode() + b"--\r\n")
        req = urllib.request.Request(
            "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
            data=body, method="POST",
            headers={"Authorization": "Bearer " + access_token,
                     "Content-Type": "multipart/related; boundary=" + boundary})
        with urllib.request.urlopen(req, timeout=60) as resp:
            out = json.loads(resp.read().decode())
        return out.get("id"), None
    except urllib.error.HTTPError as e:
        return None, ("auth" if e.code in (401, 403) else "other")
    except Exception:
        return None, "network"


def drive_fetch_latest_backup(access_token):
    """Back-compat wrapper: bytes of the first downloadable candidate,
    or None. New code should use try_drive_auto_sync_detailed, which
    skips invalid candidates instead of stopping at the first."""
    files = drive_list_drive_backups(access_token)
    if not files:
        return None
    return drive_download_drive_file(access_token, files[0]["id"])


def drive_backup_has_user(file_bytes, uid, email):
    """True only if the candidate backup's users table holds this
    Firebase identity (by uid or email). Guards the auto-sync: a
    backup belonging to someone else must never replace local data."""
    if not file_bytes or file_bytes[:16] != b"SQLite format 3\x00":
        return False
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(suffix=".db", prefix="labh-drive-check-")
        with os.fdopen(fd, "wb") as f:
            f.write(file_bytes)
        conn = sqlite3.connect("file:" + quote(tmp) + "?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT 1 FROM users WHERE firebase_uid=? OR email=? LIMIT 1",
                (uid, email)).fetchone()
            return row is not None
        finally:
            conn.close()
    except Exception:
        return False
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass


def try_drive_auto_sync_detailed(uid, email, drive_token):
    """v2.9 existing-user auto-sync with a reason for the sync-only
    page. Returns (user_or_None, reason): reason is 'ok' on success,
    else 'no_token' (no Drive token at all), 'drive_error' (Drive
    could not be reached/listed), 'drive_auth' (v2.12: Drive answered
    401/403 — the sign-in carries no Drive scope), 'no_backup' (no
    LABH *.db found),
    'not_user' (backups found but none holds this identity),
    'invalid' (backups found but all failed the safety check).
    Candidates are tried NEWEST FIRST and invalid ones are SKIPPED,
    never fatal: only the first candidate that both holds this
    identity AND passes restore validation is restored. Live data is
    untouched on every failure path (restore_db_from_bytes validates
    before replacing anything). Only ever called when no local user
    exists for this verified identity."""
    if not drive_token:
        return None, "no_token"
    try:
        files, list_kind = drive_list_drive_backups_detailed(drive_token)
    except Exception:
        return None, "drive_error"
    if files is None:
        # v2.12: a 401/403 means the sign-in never actually granted
        # the Drive scope — a different fix from "no connection".
        return None, ("drive_auth" if list_kind == "auth" else "drive_error")
    if not files:
        return None, "no_backup"
    saw_foreign = False
    saw_invalid = False
    for cand in files:
        try:
            data = drive_download_drive_file(drive_token, cand["id"])
        except Exception:
            continue
        if not data:
            continue
        if not drive_backup_has_user(data, uid, email):
            # A valid SQLite backup without this identity is foreign;
            # non-SQLite bytes are invalid. Both are skipped, and the
            # next (older) candidate is tried.
            if data[:16] == b"SQLite format 3\x00":
                saw_foreign = True
            else:
                saw_invalid = True
            continue
        try:
            ok, _problem = restore_db_from_bytes(data)
        except Exception:
            ok = False
        if not ok:
            saw_invalid = True
            continue
        return find_or_link_firebase_user(uid, email), "ok"
    if saw_foreign:
        return None, "not_user"
    if saw_invalid:
        return None, "invalid"
    return None, "drive_error"


def try_drive_auto_sync(uid, email, drive_token):
    """v2.8 wrapper: user or None, reason discarded. New callers
    should use try_drive_auto_sync_detailed for the sync-only page."""
    user, _reason = try_drive_auto_sync_detailed(uid, email, drive_token)
    return user


def autosync_reason_note(reason, via_google):
    """Short, honest sync-only-page note for a detailed auto-sync
    reason. Never includes a token, a stack trace, or file internals.
    via_google=False means the user arrived via email login (which
    never carries a Drive token)."""
    if reason == "no_token" and not via_google:
        return ("You signed in with email, so automatic Drive sync could "
                "not run — it needs a Google sign-in. Sign in with Google "
                "to auto-sync your business, or use manual sync below.")
    if reason == "no_token":
        return ("Drive permission was not granted, so auto-sync could not "
                "run. Sign in with Google again and allow Drive access, "
                "or use manual sync below.")
    if reason == "no_backup":
        return ("Auto-sync checked Google Drive and found no LABH backup "
                "(.db) for this account.")
    if reason == "not_user":
        return ("Auto-sync found a LABH backup in Drive, but it does not "
                "belong to this account, so it was not restored.")
    if reason == "invalid":
        return ("Auto-sync found a LABH backup in Drive, but it failed "
                "the safety check and was not restored.")
    if reason == "drive_auth":
        return ("Google Drive access was not granted for this sign-in — "
                "the Drive permission is missing from it. Sign in with "
                "Google again and allow Drive access on the consent "
                "screen; if it still says this, remove LABH at "
                "myaccount.google.com/permissions and sign in again, "
                "or use manual sync below.")
    if reason == "auto_sync_off":
        return ("Auto-sync is turned off in Settings, so no Drive "
                "download ran. Turn it on in Settings to auto-sync, "
                "or use manual sync below.")
    return ("Google Drive could not be reached. Check your connection "
            "and try again, or use manual sync below.")


def parse_is_new_user(form, fv):
    """Google isNewUser flag from a posted form. Returns True/False,
    or None when missing/unknown — callers treat None as EXISTING
    (safe side: sync/manual only, never create)."""
    v = (fv(form, "isNewUser") or "").strip().lower()
    if v in ("1", "true", "yes"):
        return True
    if v in ("0", "false", "no"):
        return False
    return None


# ---------------------------------------------------------------- updates
def get_setting(key):
    row = q("SELECT value FROM app_settings WHERE key=?", (key,), one=True)
    return row[0] if row else ""

def set_setting(key, value, touch=True):
    exec_db("INSERT INTO app_settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value),
            touch=touch)


def auto_sync_allowed():
    """v2.13: the auto-sync switch (Settings). Default ON, so existing
    behaviour is unchanged; when OFF, Google sign-in skips the Drive
    download and lands on the sync-only page instead."""
    if os.environ.get("LABH_DEMO") == "1":
        return False
    return get_setting("auto_sync") != "0"

def parse_version(v):
    """'v1.2.3-beta' -> ((1, 2, 3), 0); '1.2' -> ((1, 2), 1).
    The second element is 1 for a release, 0 for a pre-release, so
    1.0.0-beta sorts below 1.0.0."""
    s = str(v or "").strip().lstrip("vV")
    core, _, pre = s.partition("-")
    nums = []
    for part in core.split("."):
        try:
            nums.append(int(part))
        except ValueError:
            nums.append(0)
    return (tuple(nums), 0 if pre else 1)

def compare_versions(a, b):
    """-1 if a < b, 0 if equal, 1 if a > b. Pads shorter versions
    with zeros, so 1.2 == 1.2.0."""
    (na, pa), (nb, pb) = parse_version(a), parse_version(b)
    n = max(len(na), len(nb))
    na = na + (0,) * (n - len(na))
    nb = nb + (0,) * (n - len(nb))
    if na != nb:
        return -1 if na < nb else 1
    if pa != pb:
        return -1 if pa < pb else 1
    return 0

def parse_manifest(raw):
    """Validate + normalise a manifest JSON document. Raises ValueError
    on anything malformed, so a bad manifest can never be presented as
    a real update. Accepted fields: version (required), release_date,
    details (string or list) or changelog (list), download_url."""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as e:
        raise ValueError(f"manifest is not valid JSON: {e}")
    if not isinstance(data, dict):
        raise ValueError("manifest must be a JSON object")
    version = data.get("version")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("manifest is missing a 'version' string")
    details = data.get("details", data.get("changelog", []))
    if isinstance(details, str):
        details = [details]
    if not isinstance(details, list):
        raise ValueError("manifest 'details' must be a string or a list of strings")
    details = [str(d) for d in details]
    # APK manifests (v2.4) use apk_url; PC manifests use download_url.
    # They are aliases: either field fills both, so one manifest format
    # works for both update pages and neither page invents a URL.
    url = data.get("download_url", "") or data.get("apk_url", "")
    if url and (not isinstance(url, str) or not url.startswith(("http://", "https://"))):
        raise ValueError("manifest 'download_url'/'apk_url' must start with http:// or https://")
    return {"version": version.strip(),
            "release_date": str(data.get("release_date", "")),
            "details": details,
            "download_url": url if isinstance(url, str) else "",
            "apk_url": url if isinstance(url, str) else ""}

def fetch_manifest(url, timeout=8):
    """Fetch + parse a manifest. Returns (manifest_or_None, error_or_None).
    Any network/parse failure is an error string, never an exception —
    a failed check must not crash the app or look like an update."""
    if not url.startswith(("http://", "https://")):
        return None, "manifest URL must start with http:// or https://"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "LABH-Updater"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(1024 * 1024)  # 1 MB cap for a manifest
    except Exception as e:
        return None, f"could not fetch the manifest ({type(e).__name__})"
    try:
        return parse_manifest(raw), None
    except ValueError as e:
        return None, str(e)

def safe_package_filename(url, version):
    """Basename of the download URL, stripped to safe characters, so a
    hostile URL can never escape the updates/ folder."""
    base = os.path.basename(urlparse(url).path) or f"labh-update-{version}"
    base = "".join(c for c in base if c.isalnum() or c in "._-").strip(".") or f"labh-update-{version}"
    return base

def validate_package(path):
    """Check a downloaded update package. Returns (ok, kind, problem)
    where kind is 'py', 'zip' or 'apk'. A .py must be decodable text,
    a .zip must be a real zip, an .apk must be a zip starting with PK
    (APKs are zip files). Anything else is rejected before install."""
    ext = os.path.splitext(path)[1].lower()
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return False, "", str(e)
    if size == 0:
        return False, "", "downloaded file is empty"
    if ext == ".py":
        try:
            with open(path, "rb") as f:
                f.read().decode("utf-8")
        except (OSError, UnicodeDecodeError) as e:
            return False, "", f"not a valid Python file: {e}"
        return True, "py", ""
    if ext in (".zip", ".apk"):
        if not zipfile.is_zipfile(path):
            return False, "", "file is not a valid zip/APK package"
        return True, "apk" if ext == ".apk" else "zip", ""
    return False, "", f"unsupported package type '{ext}' (expected .py, .zip or .apk)"

def update_meta_path():
    return os.path.join(UPDATES_DIR, "downloaded.json")

def read_update_meta():
    try:
        with open(update_meta_path(), encoding="utf-8") as f:
            meta = json.load(f)
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}

def write_update_meta(meta):
    os.makedirs(UPDATES_DIR, exist_ok=True)
    with open(update_meta_path(), "w", encoding="utf-8") as f:
        json.dump(meta, f)

def install_python_update(downloaded_path, app_py_path, updates_dir):
    """Install a downloaded .py (or an app.py extracted from a .zip) as
    the new app.py. Backs up the current file first and returns the
    backup path. Touches NOTHING else — in particular never labh.db.
    The caller must tell the user to restart the app: the running
    process keeps serving the old code until it restarts."""
    os.makedirs(updates_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = os.path.join(updates_dir, f"app-backup-{stamp}.py")
    shutil.copyfile(app_py_path, backup)
    tmp = os.path.join(updates_dir, f".app-new-{stamp}.py")
    shutil.copyfile(downloaded_path, tmp)
    os.replace(tmp, app_py_path)
    return backup

# --- update pop-up dismissal state -------------------------------------
# "Later" is session-only: kept in memory, keyed by session token, so a
# new login (new session) shows the pop-up again. "Don't show again for
# this version" is persistent, but stored in updates/dismissed.json —
# deliberately NOT in labh.db, so dismissing a pop-up never touches the
# business database.
DISMISSED_SESSIONS = {}  # token_hash -> set of versions
_DISMISS_LOCK = threading.Lock()

def dismissed_file_path():
    return os.path.join(UPDATES_DIR, "dismissed.json")

def load_persistent_dismissals():
    """Set of (user_id, version) pairs from updates/dismissed.json."""
    try:
        with open(dismissed_file_path(), encoding="utf-8") as f:
            data = json.load(f)
        return {(int(u), str(v)) for u, v in data if isinstance(u, int) or str(u).isdigit()} \
            if isinstance(data, list) else set()
    except (OSError, ValueError, TypeError):
        return set()

def save_persistent_dismissals(pairs):
    os.makedirs(UPDATES_DIR, exist_ok=True)
    with open(dismissed_file_path(), "w", encoding="utf-8") as f:
        json.dump(sorted([u, v] for u, v in pairs), f)

def is_update_dismissed(user, version):
    """True if this user dismissed this version for this session
    ('Later') or permanently ('Don't show again for this version')."""
    with _DISMISS_LOCK:
        if version in DISMISSED_SESSIONS.get(user.get("_token_hash", ""), set()):
            return True
    return (user["id"], version) in load_persistent_dismissals()

def dismiss_update_for_session(user, version):
    with _DISMISS_LOCK:
        DISMISSED_SESSIONS.setdefault(user.get("_token_hash", ""), set()).add(version)

def dismiss_update_forever(user, version):
    with _DISMISS_LOCK:
        pairs = load_persistent_dismissals()
        pairs.add((user["id"], version))
        save_persistent_dismissals(pairs)

def available_update(url, timeout=3):
    """Manifest newer than VERSION, or None. Any fetch/parse failure
    returns None — a failed check must never block a page or look
    like an update. Short timeout: the dashboard must stay fast."""
    if not url:
        return None
    manifest, err = fetch_manifest(url, timeout=timeout)
    if err or not manifest:
        return None
    if compare_versions(manifest["version"], VERSION) <= 0:
        return None
    return manifest

def update_popup_html(user, manifest):
    """Pop-up modal for a newer version: version, short summary (first
    3 changes, full list on the Updates page), Update now (link to the
    Updates page — never an auto-download/install) and two dismissals:
    Later (this session only) and Don't show again for this version."""
    details = manifest["details"]
    shown = details[:3]
    more = f"<li>…and {len(details) - 3} more — see the Updates page for the full list.</li>" \
        if len(details) > 3 else ""
    items = "".join(f"<li>{esc(d)}</li>" for d in shown) + more
    ver = esc(manifest["version"])
    return ('<div class="modal-backdrop"><div class="modal">'
            f'<h2>New update available: {ver}</h2>'
            f'<p>Released: {esc(manifest["release_date"]) or "date not given"}. '
            f'You are on version {esc(VERSION)}.</p>'
            '<h4>What changed</h4><ul>' + items + '</ul>'
            f'<p><a class="btn" href="/updates">Update now</a> '
            + form_start("/updates/dismiss", user["_csrf"])
            + f'<input type="hidden" name="version" value="{ver}">'
            + '<input type="hidden" name="mode" value="later">'
            + '<button type="submit">Later</button></form> '
            + form_start("/updates/dismiss", user["_csrf"])
            + f'<input type="hidden" name="version" value="{ver}">'
            + '<input type="hidden" name="mode" value="never">'
            + '<button type="submit" class="danger">Don\'t show again for this version</button></form></p>'
            '<p><small>Update now only opens the Updates page — nothing is '
            'downloaded or installed until you press Download and then Install there.</small></p>'
            '</div></div>')

# --- APK updates (v2.4) -------------------------------------------------
# The standalone Android app cannot be updated by replacing app.py
# (its code is bundled in the APK), so APK updates are a separate
# flow from the PC Updates page, which is left untouched:
#   Source A (primary): a folder scanned for labh-<version>.apk.
#   Source B (optional): an APK manifest URL whose apk_url points
#     at an .apk file. A private GitHub artifact URL needs auth and
#     cannot be used here — that is why the folder source is primary.
# APK files are app-wide, not business rows, so there is no
# business-owned record to leak: the access control on these pages
# is login + owner/admin role + per-session CSRF (staff and
# cross-session tokens get 403 on download/install/file, exactly
# like the PC Updates page). Nothing here ever writes labh.db:
# dismissals live in updates/apk-dismissed.json and downloaded APKs
# in updates/; installing a new APK keeps Android internal storage.
def is_android_runtime():
    """True inside the Android app: Chaquopy's HOME is the app's
    internal storage dir, and the Android copy sets BASE_DIR to it
    (the PC copy's BASE_DIR is the script folder, not HOME).
    LABH_ANDROID=1 forces it (used by tests)."""
    if os.environ.get("LABH_ANDROID") == "1":
        return True
    home = os.environ.get("HOME") or ""
    return bool(home) and os.path.abspath(BASE_DIR) == os.path.abspath(home)

def apk_update_folder():
    """(folder, source_label). Explicit APK folder setting wins;
    else the Drive sync folder; else the phone/PC Downloads folder."""
    explicit = get_setting("apk_update_folder").strip()
    if explicit:
        return explicit, "APK update folder (Settings)"
    sync = get_setting("sync_dir").strip()
    if sync:
        return sync, "Drive sync folder (Settings)"
    return os.path.join(os.path.expanduser("~"), "Downloads"), "Downloads (default)"

APK_FILENAME_RE = re.compile(r"^labh-(.+)\.apk$", re.IGNORECASE)

def parse_apk_filename(name):
    """'labh-2.5.0.apk' -> '2.5.0'; 'labh-v2.5.1.apk' -> '2.5.1'.
    Anything else (wrong name, no digits, .zip/.txt) -> None, so a
    stray file in the folder can never be presented as an update."""
    m = APK_FILENAME_RE.match(os.path.basename(name or ""))
    if not m:
        return None
    ver = m.group(1).strip().lstrip("vV")
    if not re.fullmatch(r"\d+(\.\d+)*(-[A-Za-z0-9.]+)?", ver):
        return None
    return ver

def apk_changelog(folder, version, stem=""):
    """Changelog beside an APK: labh-<version>.json (details/changelog
    list, parsed like a manifest) preferred, else labh-<version>.txt
    (one change per line). Missing/unreadable sidecar -> []."""
    for base in [stem, f"labh-{version}"]:
        if not base:
            continue
        jpath = os.path.join(folder, base + ".json")
        try:
            with open(jpath, encoding="utf-8") as f:
                data = json.load(f)
            details = data.get("details", data.get("changelog", [])) if isinstance(data, dict) else []
            if isinstance(details, str):
                details = [details]
            if isinstance(details, list) and details:
                return [str(d) for d in details]
        except (OSError, ValueError):
            pass
        tpath = os.path.join(folder, base + ".txt")
        try:
            with open(tpath, encoding="utf-8") as f:
                lines = [ln.strip() for ln in f if ln.strip()]
            if lines:
                return lines
        except OSError:
            pass
    return []

def scan_apk_folder(folder):
    """Valid APK candidates in a folder, newest version first.
    A labh-*.apk that is not a real zip is skipped, never offered:
    a corrupt download must not pop up as an update."""
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    cands = []
    for name in sorted(names):
        ver = parse_apk_filename(name)
        if not ver:
            continue
        path = os.path.join(folder, name)
        if not os.path.isfile(path) or not zipfile.is_zipfile(path):
            continue
        try:
            size = os.path.getsize(path)
            date_s = datetime.fromtimestamp(os.path.getmtime(path)).strftime("%Y-%m-%d")
        except OSError:
            continue
        stem = os.path.splitext(name)[0]
        cands.append({"version": ver, "filename": name, "path": path,
                      "size": size, "date": date_s,
                      "changelog": apk_changelog(folder, ver, stem)})
    cands.sort(key=lambda c: (parse_version(c["version"]), c["filename"]), reverse=True)
    # parse_version's tuple comparison is not compare_versions (it
    # ignores pre-release ordering), so re-sort strictly newest-first.
    for i in range(len(cands)):
        for j in range(i + 1, len(cands)):
            if compare_versions(cands[j]["version"], cands[i]["version"]) > 0:
                cands[i], cands[j] = cands[j], cands[i]
    return cands

def find_apk_update(folder=None):
    """Newest folder APK newer than VERSION, or None."""
    if folder is None:
        folder, _ = apk_update_folder()
    for cand in scan_apk_folder(folder):
        if compare_versions(cand["version"], VERSION) > 0:
            cand = dict(cand)
            cand["source"] = "folder"
            cand["release_date"] = cand["date"]
            return cand
    return None

def apk_manifest_update(url, timeout=8):
    """(manifest_or_None, error_or_None) for an APK manifest.
    A newer version with no .apk link is an error, not an update:
    a .py/.zip link must never be offered as a phone install."""
    manifest, err = fetch_manifest(url, timeout=timeout)
    if err or not manifest:
        return None, err or "could not fetch the manifest"
    if compare_versions(manifest["version"], VERSION) <= 0:
        return None, None
    apk_url = manifest.get("apk_url") or manifest.get("download_url") or ""
    if not apk_url.lower().endswith(".apk"):
        return None, "manifest has no .apk download link (apk_url)"
    manifest = dict(manifest)
    manifest["apk_url"] = apk_url
    return manifest, None

def github_release_repo():
    """GitHub repo checked for APK releases (Settings-overridable).
    Default is the public LABH releases repo: the app fetches its
    Releases API without auth, so the repo must stay public (APK
    only — no source, no business data — business data stays on
    the device)."""
    return get_setting("github_release_repo").strip() or "ryhantrade/LABH-releases"


def parse_github_release(data):
    """Validate + normalise a GitHub Releases API document. Returns
    {version, release_date, changelog, apk_url} or None on anything
    malformed, on a version that does not parse, or when no asset
    ends in .apk — a bad release can never be offered as an update.
    tag_name accepts 'v2.9.0' or '2.9.0'."""
    if not isinstance(data, dict):
        return None
    tag = str(data.get("tag_name") or "").strip().lstrip("vV")
    if not re.fullmatch(r"\d+(\.\d+)*(-[A-Za-z0-9.]+)?", tag):
        return None
    apk_url = ""
    assets = data.get("assets") or []
    if isinstance(assets, list):
        for a in assets:
            if not isinstance(a, dict):
                continue
            name = str(a.get("name") or "")
            url = str(a.get("browser_download_url") or "")
            if name.lower().endswith(".apk") and url.startswith("https://"):
                apk_url = url
                break
    if not apk_url:
        return None
    body = data.get("body") or ""
    changelog = [ln.strip() for ln in str(body).splitlines() if ln.strip()][:10]
    published = str(data.get("published_at") or "")
    return {"version": tag, "release_date": published[:10],
            "changelog": changelog, "apk_url": apk_url}


def fetch_github_release(repo=None, timeout=5):
    """Latest GitHub release for a repo, or None on ANY failure
    (offline, 404, malformed JSON, no .apk asset, bad repo name).
    No auth is used, so only public repos work — by design. A failed
    check is silent: it must never block the dashboard or look like
    an update. TEST ONLY: with LABH_GITHUB_RELEASE_STUB=1 the JSON
    is read from LABH_STUB_RELEASE_JSON instead of the network."""
    repo = (repo or github_release_repo()).strip()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        return None
    try:
        if os.environ.get("LABH_GITHUB_RELEASE_STUB") == "1":
            path = os.environ.get("LABH_STUB_RELEASE_JSON") or ""
            with open(path, encoding="utf-8") as f:
                return parse_github_release(json.load(f))
        req = urllib.request.Request(
            "https://api.github.com/repos/" + repo + "/releases/latest",
            headers={"User-Agent": "LABH-Updater",
                     "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return parse_github_release(json.loads(resp.read().decode()))
    except Exception:
        return None


def github_release_update(timeout=5):
    """Newest GitHub release newer than VERSION, or None. The
    release's own .apk asset is the download: on Android the
    WebView DownloadManager downloads it and the system installer
    takes over, exactly like the folder/manifest flows."""
    try:
        rel = fetch_github_release(timeout=timeout)
    except Exception:
        return None
    if not rel or compare_versions(rel["version"], VERSION) <= 0:
        return None
    return {"source": "github-release", "version": rel["version"],
            "release_date": rel["release_date"],
            "changelog": rel["changelog"], "apk_url": rel["apk_url"],
            "filename": safe_package_filename(rel["apk_url"], rel["version"]),
            "path": "", "size": 0, "date": rel["release_date"]}


def github_release_check_due(user):
    """True when this user has not had a GitHub release check today.
    The dashboard uses this so the automatic check runs at most once
    per day per user; the manual button on APK Updates bypasses it."""
    try:
        today = datetime.now().date().isoformat()
    except Exception:
        return True
    return get_setting("github_release_check_%s" % user["id"]) != today


def github_release_check_record(user):
    """Record that a GitHub release check was attempted today, so the
    dashboard does not repeat it until tomorrow (success or not —
    a failed check must not hammer the API on every dashboard open)."""
    try:
        set_setting("github_release_check_%s" % user["id"],
                    datetime.now().date().isoformat())
    except Exception:
        pass


def available_apk_update(timeout=3, include_releases=True):
    """Newest APK update from any source, or None. Folder first
    (local, no network), manifest override second, GitHub Releases
    third (the DEFAULT update source — it needs no user-set folder
    or URL at all). Any failure -> None: a failed check must never
    block the dashboard or look like an update. include_releases
    lets the dashboard throttle the network leg to once per day."""
    try:
        folder, _ = apk_update_folder()
        cand = find_apk_update(folder)
        if cand:
            return cand
        url = get_setting("apk_manifest_url").strip()
        if url:
            manifest, err = apk_manifest_update(url, timeout=timeout)
            if manifest:
                return {"source": "manifest", "version": manifest["version"],
                        "release_date": manifest["release_date"],
                        "changelog": manifest["details"],
                        "apk_url": manifest["apk_url"],
                        "filename": safe_package_filename(manifest["apk_url"],
                                                          manifest["version"]),
                        "path": "", "size": 0, "date": manifest["release_date"]}
        if include_releases:
            rel = github_release_update(timeout=5)
            if rel:
                return rel
    except Exception:
        return None
    return None

# APK pop-up dismissals: same semantics as the PC pop-up, but a
# separate namespace (updates/apk-dismissed.json + "apk:" session
# keys), so dismissing an APK version never hides a PC update.
def apk_dismissed_file_path():
    return os.path.join(UPDATES_DIR, "apk-dismissed.json")

def load_apk_dismissals():
    try:
        with open(apk_dismissed_file_path(), encoding="utf-8") as f:
            data = json.load(f)
        return {(int(u), str(v)) for u, v in data if isinstance(u, int) or str(u).isdigit()} \
            if isinstance(data, list) else set()
    except (OSError, ValueError, TypeError):
        return set()

def save_apk_dismissals(pairs):
    os.makedirs(UPDATES_DIR, exist_ok=True)
    with open(apk_dismissed_file_path(), "w", encoding="utf-8") as f:
        json.dump(sorted([u, v] for u, v in pairs), f)

def is_apk_update_dismissed(user, version):
    with _DISMISS_LOCK:
        if ("apk:" + version) in DISMISSED_SESSIONS.get(user.get("_token_hash", ""), set()):
            return True
    return (user["id"], version) in load_apk_dismissals()

def dismiss_apk_update_for_session(user, version):
    with _DISMISS_LOCK:
        DISMISSED_SESSIONS.setdefault(user.get("_token_hash", ""), set()).add("apk:" + version)

def dismiss_apk_update_forever(user, version):
    with _DISMISS_LOCK:
        pairs = load_apk_dismissals()
        pairs.add((user["id"], version))
        save_apk_dismissals(pairs)

def apk_update_popup_html(user, upd):
    """Dashboard pop-up for a newer APK: version, date, source, short
    changelog, Install (opens /updates-apk — nothing is installed
    from the pop-up itself), Later, Don't show again for this version."""
    shown = (upd.get("changelog") or [])[:3]
    more = (f"<li>…and {len(upd['changelog']) - 3} more — see APK Updates for the full list.</li>"
            if len(upd.get("changelog") or []) > 3 else "")
    items = "".join(f"<li>{esc(d)}</li>" for d in shown) or "<li>No changelog listed.</li>"
    items += more
    ver = esc(upd["version"])
    if upd.get("source") == "github-release" and upd.get("apk_url"):
        install_html = (f'<a class="btn" href="{esc(upd["apk_url"])}">Download &amp; Install</a> '
                        '<a class="btn" href="/updates-apk">Details</a> ')
    else:
        install_html = '<a class="btn" href="/updates-apk">Install update</a> '
    return ('<div class="modal-backdrop"><div class="modal">'
            f'<h2>New APK update available: {ver}</h2>'
            f'<p>Released: {esc(upd.get("release_date") or upd.get("date") or "date not given")}. '
            f'You are on version {esc(VERSION)}. '
            f'Source: {esc(upd.get("source", "folder"))}.</p>'
            '<h4>What changed</h4><ul>' + items + '</ul>'
            f'<p>{install_html}'
            + form_start("/updates-apk/dismiss", user["_csrf"])
            + f'<input type="hidden" name="version" value="{ver}">'
            + '<input type="hidden" name="mode" value="later">'
            + '<button type="submit">Later</button></form> '
            + form_start("/updates-apk/dismiss", user["_csrf"])
            + f'<input type="hidden" name="version" value="{ver}">'
            + '<input type="hidden" name="mode" value="never">'
            + '<button type="submit" class="danger">Don\'t show again for this version</button></form></p>'
            '<p><small>' + ('Download &amp; Install downloads the release APK '
            '(DownloadManager) — nothing is installed until you tap Install '
            'in Android\'s installer. ' if upd.get("source") == "github-release"
            else 'Install update only opens APK Updates — nothing is '
            'downloaded or installed until you tap Install there, then tap '
            'Install in Android\'s installer. ')
            + 'Installing a new APK keeps '
            'your data: labh.db is never touched.</small></p>'
            '</div></div>')

def apk_meta_path():
    return os.path.join(UPDATES_DIR, "apk-downloaded.json")

def read_apk_meta():
    try:
        with open(apk_meta_path(), encoding="utf-8") as f:
            meta = json.load(f)
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}

def write_apk_meta(meta):
    os.makedirs(UPDATES_DIR, exist_ok=True)
    with open(apk_meta_path(), "w", encoding="utf-8") as f:
        json.dump(meta, f)

def display_name(user, biz):
    """Greeting name: the user's display name, else the business name.
    Never a bare email address (the v2.1 screenshot greeted the user
    with their raw email)."""
    try:
        name = (user["name"] or "").strip()
    except Exception:
        name = ""
    if name:
        return name
    try:
        return (biz["name"] or "").strip()
    except Exception:
        return ""


def page(user, biz, title, body, extra="", show_h1=True):
    title_t = tr(title)
    if not user:
        return ("<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                f"<title>{esc(title_t)} - LABH</title><style>{CSS}{EXTRA_CSS}</style></head><body>"
                f"<div class='auth-bg'><div class='auth-card'><h1 style='text-align:center'>{esc(title_t)}</h1>{body}</div></div>{extra}</body></html>")
    if os.environ.get("LABH_DEMO") == "1":
        body = ('<div style="background:#f59e0b;color:#1c1917;text-align:center;font-weight:800;padding:8px;border-radius:12px;margin-bottom:12px">'
                'DEMO WEBSITE — demo data only. Nothing here is a real business, and nothing syncs to Google Drive.</div>') + body
    try:
        seed_notifications(user["business_id"], biz["currency"])
        bell_n = unread_count(user["business_id"])
    except Exception:
        bell_n = 0
    try:
        dark = " dark" if get_setting("theme_mode") == "dark" else ""
        accent = get_setting("accent") or "labh-green"
        accent_cls = {"ocean-blue": " accent-blue", "royal-purple": " accent-purple",
                      "rose": " accent-rose", "orange": " accent-orange"}.get(accent, "")
    except Exception:
        dark, accent_cls = "", ""
    navmap = dict(NAV)
    active = ACTIVE_BY_TITLE.get(title, "")
    sidebar = ("<div class='logo'><span class='mark'>" + icon("building", 22) + "</span><span><span class='word'>LABH</span>"
               "<br><span class='tag'>Smart Business Manager</span></span></div>")
    for grp, slugs in NAV_GROUPS:
        sidebar += f"<div class='grp'>{esc(grp)}</div>"
        for slug in slugs:
            cls = "active navlink" if slug == active else "navlink"
            badge = ""
            if slug in NAV_BADGES:
                txt, bcls = NAV_BADGES[slug]
                badge = f'<span class="badge {bcls}">{txt}</span>'
            sidebar += (f'<a class="{cls}" href="/{slug}"><span class="navtxt" style="display:flex;gap:10px;align-items:center">'
                        f'{icon(NAV_ICONS.get(slug, "box"), 18)}<span>{esc(tr(navmap[slug]))}</span></span>{badge}</a>')
    # Logout lives at the bottom of the sidebar so phone users (whose
    # top-bar Logout button is hidden to keep the row on one line) can
    # still sign out from the drawer.
    sidebar += (f'<form method="post" action="/logout" style="margin-top:20px">'
                f'<input type="hidden" name="csrf" value="{esc(user["_csrf"])}">'
                f'<button type="submit" class="secondary" style="width:100%">Logout</button></form>')
    bottom_active = BOTTOM_ACTIVE.get(active, "")
    bottomnav = "<nav class='bottomnav'>"
    for slug, label, ico in BOTTOMNAV:
        cls = "active" if slug == bottom_active else ""
        if slug == "scan":
            cls = (cls + " scan-raised").strip()
        bottomnav += f'<a class="{cls}" href="/{slug}"><span class="ico" style="display:flex">{icon(ico, 22)}</span>{esc(tr(label))}</a>'
    bottomnav += '</nav>'
    disp = display_name(user, biz)
    _parts = (disp or "").split()
    if len(_parts) >= 2:
        _initials = (_parts[0][0] + _parts[1][0]).upper()
    elif disp:
        _initials = disp[:2].upper()
    else:
        _initials = (user["email"] or "?")[0].upper()
    who = (f'<span class="avatarwrap"><a class="avatar" href="/profile">{esc(_initials)}</a>'
           f'<span class="online-dot on-avatar" aria-label="Online"></span></span>'
           f'<span class="avatar-arrow">{icon("chevron", 16)}</span>'
           f'<form method="post" action="/logout" style="display:inline">'
           f'<input type="hidden" name="csrf" value="{esc(user["_csrf"])}">'
           f'<button type="submit" class="logout-btn">Logout</button></form>')
    search = ('<div class="searchwrap"><form method="get" action="/search"><input id="search-input" type="search" name="q" autocomplete="off" '
              'placeholder="Search anything...">'
              '<button type="submit" class="search-btn">Search</button></form><div class="search-dd" id="search-dd"></div></div>')
    bell = f'<a class="bellwrap" href="/notifications" aria-label="Notifications">{icon("bell", 22)}' + (f'<span class="bellcount">{bell_n}</span>' if bell_n else '') + '</a>'
    drawer_js = ("<script>function toggleDrawer(o){document.getElementById('sidebar').classList.toggle('open',o);"
                 "document.getElementById('drawer-overlay').classList.toggle('show',o);}"
                 "document.addEventListener('keydown',function(e){if((e.ctrlKey||e.metaKey)&&e.key.toLowerCase()==='k'){e.preventDefault();var i=document.getElementById('search-input');if(i)i.focus();}});"
                 "var si=document.getElementById('search-input');var dd=document.getElementById('search-dd');var tm;"
                 "if(si){si.addEventListener('input',function(){clearTimeout(tm);var q=si.value.trim();if(q.length<2){dd.style.display='none';return;}tm=setTimeout(function(){fetch('/search?q='+encodeURIComponent(q)+'&live=1').then(function(r){return r.json();}).then(function(items){if(!items.length){dd.style.display='none';return;}dd.innerHTML=items.map(function(it){return '<a href="'+it.url+'">'+it.label+'</a>';}).join('');dd.style.display='block';}).catch(function(){});},250);});"
                 "si.addEventListener('blur',function(){setTimeout(function(){dd.style.display='none';},200);});}"
                 "window.addEventListener('load',function(){var l=document.getElementById('labh-loader');if(l)l.style.display='none';});"
                 "setTimeout(function(){var l=document.getElementById('labh-loader');if(l)l.style.display='none';},3000);</script>")
    loader = ('<div id="labh-loader"><div class="auth-logo">' + icon("building", 32) + '</div><div>LABH</div><div style="font-weight:400;font-size:13px;color:#64748b">Loading your business...</div></div>')
    return ("<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{esc(title_t)} - LABH</title><style>{CSS}{EXTRA_CSS}</style></head>"
            f"<body class='tpl-{invoice_template_setting()}{dark}{accent_cls}'>" + loader +
            f"<div class='layout'><div class='drawer-overlay' id='drawer-overlay' onclick='toggleDrawer(false)'></div>"
            f"<aside class='sidebar' id='sidebar'>{sidebar}</aside>"
            f"<div class='maincol'><div class='topbar{' inner' if active != 'dashboard' else ''}'><button type='button' class='hamburger' onclick='toggleDrawer(true)'>{icon('menu', 22)}</button>"
            f"<button type='button' class='topback' onclick='history.back()' aria-label='Back'>&#8592;</button>"
            f"<span class='mobtitle'>{esc(title_t)}</span>"
            f"<span class='topbrand'><span class='topmark'>{icon('building', 20)}</span><span class='topword'>LABH<span class='tag'>Smart Business Manager</span></span></span>"
            f"{search}"
            f"<a class='mobsearch' href='/search' aria-label='Search'>{icon('search', 22)}</a>"
            f"{bell}"
            f"<span class='who'>{who}</span></div>"
            f"<main>{'' if not show_h1 else f'<h1>{esc(title_t)}</h1>'}{body}</main></div>{bottomnav}</div>{drawer_js}{extra}</body></html>")

def form_start(action, csrf):
    return (f'<form method="post" action="{action}">'
            f'<input type="hidden" name="csrf" value="{esc(csrf)}">')

def field(label, name, value="", typ="text", req=False):
    return (f'<p><label>{esc(label)}</label><br>'
            f'<input type="{typ}" name="{esc(name)}" value="{esc(value)}"'
            f'{" required" if req else ""}></p>')

def table(headers, rows_html):
    th = "".join(f"<th>{esc(h)}</th>" for h in headers)
    return f"<table><tr>{th}</tr>{rows_html}</table>"

# ---------------------------------------------------------------- handler
def auth_html(mode, err=None, msg=None):
    """Zip-faithful AuthScreen: gradient page, centered white rounded-3xl
    card, gradient logo box, Sign In / Create Account tabs, icon fields,
    gradient submit with loading state, Google button, Firebase footer."""
    tabs = ('<div class="auth-tabs">'
            f'<a href="/login" class="{"on" if mode == "login" else ""}">Sign In</a>'
            f'<a href="/register" class="{"on" if mode == "register" else ""}">Create Account</a></div>')
    err_html = f'<div class="card err" style="margin-bottom:16px">{esc(err)}</div>' if err else ""
    msg_html = f'<div class="card ok" style="margin-bottom:16px">{esc(msg)}</div>' if msg else ""
    # v2.5 diagnosis line (login + register only; status text, never values).
    status_html = (f'<p style="text-align:center;font-size:12px;color:#64748b">{firebase_config_status()}</p>'
                   if mode in ("login", "register") else "")
    if mode == "forgot":
        fields = ('<div class="fieldwrap">' + icon("mail", 18) + '<input type="email" name="email" placeholder="Email address" required></div>')
        title, btn, action = "Forgot password", "Send reset link", "/forgot"
        extra = '<p style="text-align:center"><a href="/login">Back to Sign In</a></p>'
    elif mode == "register":
        fields = ('<div class="fieldwrap">' + icon("user", 18) + '<input type="text" name="name" placeholder="Full name" required></div>'
                  '<div class="fieldwrap">' + icon("mail", 18) + '<input type="email" name="email" placeholder="Email address" required></div>'
                  '<div class="fieldwrap">' + icon("lock", 18) + '<input type="password" name="password" placeholder="Password (min 8 characters)" required></div>'
                  '<div class="fieldwrap">' + icon("lock", 18) + '<input type="password" name="confirm" placeholder="Confirm password" required></div>')
        title, btn, action = "Create Account", "Create account", "/register"
        extra = ""
    else:
        fields = ('<div class="fieldwrap">' + icon("mail", 18) + '<input type="email" name="email" placeholder="Email address" required></div>'
                  '<div class="fieldwrap">' + icon("lock", 18) + '<input type="password" name="password" placeholder="Password" required></div>')
        title, btn, action = "Sign In", "Sign In", "/login"
        extra = '<p style="text-align:right"><a href="/forgot">Forgot password?</a></p>'
    body = (f'<div class="auth-card"><div style="text-align:center;margin-bottom:24px">'
            f'<div class="auth-logo">{icon("building", 32)}</div>'
            '<h1 style="font-size:20px;margin:0">LABH Smart Business Manager</h1>'
            '<p style="font-size:12px;color:#64748b;font-weight:500;margin:6px 0 0">Smart Billing, Inventory &amp; CRM for Modern Enterprises</p></div>'
            + status_html + err_html + msg_html + tabs +
            f'<form id="auth-form" method="post" action="{action}">'
            + fields + extra +
            f'<p><button type="submit" class="auth-submit">{btn}</button></p></form>'
            "<script>var af=document.getElementById('auth-form');if(af){af.addEventListener('submit',function(){var b=this.querySelector('button');if(b){b.disabled=true;b.textContent='Please wait...';}});}</script>"
            + google_signin_html() +
            '<p style="text-align:center;font-size:12px;color:#64748b">Secured by Firebase Cloud Authentication</p></div>')
    return ("<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>{title} - LABH</title><style>{CSS}{EXTRA_CSS}</style></head><body>"
            f"<div class='auth-bg'>{body}</div></body></html>")

def _pending_onboarding(uid, email, name=""):
    """Pending onboarding row; carries the display name from register so
    the greeting after setup uses the user's name, never their email.
    v2.5: also stores the raw token so a later sign-in can resume this
    exact link (see _resume_pending_token)."""
    token = secrets.token_urlsafe(32)
    th = hashlib.sha256(token.encode()).hexdigest()
    exp = (datetime.now() + timedelta(hours=24)).isoformat(timespec="seconds")
    try:
        exec_db("INSERT INTO onboarding_pending(token_hash,firebase_uid,email,expires,name,token) VALUES(?,?,?,?,?,?)",
                (th, uid, email, exp, name or "", token), touch=False)
    except Exception:
        # Legacy schema without the token column (should not happen
        # after init_db, but never fail onboarding over it).
        exec_db("INSERT INTO onboarding_pending(token_hash,firebase_uid,email,expires,name) VALUES(?,?,?,?,?)",
                (th, uid, email, exp, name or ""), touch=False)
    return token

def _find_pending(uid, email):
    """Unexpired onboarding_pending row for this uid or email, or None."""
    for row in q("SELECT * FROM onboarding_pending WHERE firebase_uid=? OR email=?",
                 (uid, email)):
        if row["expires"] >= now_iso():
            return row
    return None

def _resume_pending_token(uid, email, name=""):
    """v2.5 stuck-login resume: if an unexpired pending row exists,
    return its existing token (no duplicate row). Legacy rows without
    a stored token are rotated: deleted and replaced by a single new
    row, so there is still never more than one pending row. Returns
    None when no pending row exists."""
    row = _find_pending(uid, email)
    if not row:
        return None
    try:
        stored = row["token"]
    except Exception:
        stored = ""
    if stored:
        return stored
    exec_db("DELETE FROM onboarding_pending WHERE token_hash=?", (row["token_hash"],), touch=False)
    return _pending_onboarding(uid, email, name)

def _create_login_choice(uid, email):
    """One-time (1h) token backing the stuck-login choice page."""
    token = secrets.token_urlsafe(32)
    th = hashlib.sha256(token.encode()).hexdigest()
    exp = (datetime.now() + timedelta(hours=1)).isoformat(timespec="seconds")
    exec_db("INSERT INTO login_choices(token_hash,firebase_uid,email,expires) VALUES(?,?,?,?)",
            (th, uid, email, exp), touch=False)
    return token

def login_choice_html(email, choice_token):
    """v2.5 choice page (auth-styled, no session needed): shown when a
    verified Firebase sign-in has no local user and no pending
    onboarding. Never creates a business silently — the user picks."""
    body = (f'<div class="auth-card"><div style="text-align:center;margin-bottom:24px">'
            f'<div class="auth-logo">{icon("building", 32)}</div>'
            '<h1 style="font-size:20px;margin:0">Welcome back</h1>'
            f'<p style="font-size:12px;color:#64748b">Signed in as {esc(email)}</p>'
            '<p style="font-size:13px;color:#475569">This device has no business '
            'linked to this account yet. Choose how to continue:</p></div>'
            f'<form method="post" action="/login-choice">'
            f'<input type="hidden" name="choice_token" value="{esc(choice_token)}">'
            '<p><button type="submit" class="auth-submit">Create a new business</button></p></form>'
            '<p style="text-align:center">— or —</p>'
            '<p><a class="btn secondary" style="width:100%;text-align:center" href="/sync-restore">'
            'Sync my business from Google Drive</a></p>'
            '<p style="font-size:12px;color:#64748b">Sync: download your .db backup '
            'from Google Drive, then upload it here. The file is checked before '
            'anything is replaced, a safety copy of the current database is saved '
            'first, and you sign in again afterwards.</p>'
            '<p style="text-align:center"><a href="/login">Back to Sign In</a></p></div>')
    return ("<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Choose how to continue - LABH</title><style>{CSS}{EXTRA_CSS}</style></head><body>"
            f"<div class='auth-bg'>{body}</div></body></html>")

def sync_only_html(email, note=None, show_google_button=False):
    """v2.8 existing-user page (HARD RULE, user 2026-10-26: the
    new-business page appears ONLY for genuinely new users). Shown
    when a verified sign-in has no local user AND the user is an
    existing user (Google isNewUser false/unknown, or any email
    login) and Drive auto-sync found nothing. Offers ONLY manual
    Drive sync upload + Back to Sign In — deliberately NO create
    button and NO onboarding link. The only create paths are the
    Register flow and Google isNewUser=true. v2.9: show_google_button
    adds a Google sign-in button above the manual option when the
    user arrived without a Drive token, so auto-sync can run.
    v2.10: that button is a real popup sign-in on this page
    (google_signin_html), NOT a link to /login — tapping it
    used to land the user back on the login page."""
    note_html = (f'<p style="font-size:13px;color:#475569">{esc(note)}</p>'
                 if note else "")
    google_html = ""
    if show_google_button:
        # v2.10: the button signs in RIGHT HERE (same Firebase popup
        # as the login page, posting to /auth/google with the Drive
        # token, so auto-sync runs immediately). It used to be a plain
        # link to /login — the user tapped "Google connect" and only
        # got the login page back, which looked like sync was broken.
        # v2.12: force_consent so the Drive scope is really granted.
        google_html = (google_signin_html(force_consent=True)
                       + '<p style="font-size:12px;color:#64748b">Google sign-in gives '
                       'the app read-only Drive access so it can find your backup '
                       'automatically — no file upload needed.</p>'
                       '<p style="text-align:center">— or —</p>')
    body = (f'<div class="auth-card"><div style="text-align:center;margin-bottom:24px\">'
            f'<div class="auth-logo">{icon("building", 32)}</div>'
            '<h1 style="font-size:20px;margin:0">Welcome back</h1>'
            f'<p style="font-size:12px;color:#64748b">Signed in as {esc(email)}</p>'
            '<p style="font-size:13px;color:#475569\">This device has no business '
            'linked to this account yet, and no backup was found here '
            'automatically. Sync your business from Google Drive to '
            'continue on this device.</p></div>'
            + note_html + google_html +
            '<p><a class="btn secondary" style="width:100%;text-align:center" href="/sync-restore">'
            'Sync my business from Google Drive</a></p>'
            '<p style="font-size:12px;color:#64748b">Sync: download your .db backup '
            'from Google Drive, then upload it here. The file is checked before '
            'anything is replaced, a safety copy of the current database is saved '
            'first, and you sign in again afterwards.</p>'
            '<p style="text-align:center"><a href="/login">Back to Sign In</a></p></div>')
    return ("<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Sync your business - LABH</title><style>{CSS}{EXTRA_CSS}</style></head><body>"
            f"<div class='auth-bg'>{body}</div></body></html>")


def sync_restore_html(err=None, msg=None):
    """v2.5 unauthenticated sync-restore page (auth-styled, no session).
    Reuses exactly the logged-in restore validation + safety-copy path
    (see restore_db_from_bytes); it only ever replaces the local DB
    file on this device, never touches Firebase."""
    err_html = f'<div class="card err" style="margin-bottom:16px">{esc(err)}</div>' if err else ""
    msg_html = f'<div class="card ok" style="margin-bottom:16px">{esc(msg)}</div>' if msg else ""
    body = (f'<div class="auth-card"><div style="text-align:center;margin-bottom:24px">'
            f'<div class="auth-logo">{icon("building", 32)}</div>'
            '<h1 style="font-size:20px;margin:0">Sync from Google Drive</h1>'
            '<p style="font-size:12px;color:#64748b">Restore a LABH backup (.db) '
            'you saved in Google Drive onto this device.</p></div>'
            + err_html + msg_html +
            '<p style="font-size:13px;color:#475569">The file is checked first — '
            'if it is not a valid LABH database it is rejected and your current '
            'data is <b>not</b> changed. A valid restore <b>replaces all data on '
            'this device</b> with the backup, and a safety copy of the current '
            'database is saved in backups/ first. You will need to sign in again '
            'afterwards.</p>'
            '<form method="post" action="/sync-restore" enctype="multipart/form-data">'
            '<p><label>Backup file (.db)</label><br><input type="file" name="backup_file" required></p>'
            '<p><button type="submit" class="auth-submit">Restore backup</button></p></form>'
            '<p style="text-align:center"><a href="/login">Back to Sign In</a></p></div>')
    return ("<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width,initial-scale=1'>"
            f"<title>Sync from Google Drive - LABH</title><style>{CSS}{EXTRA_CSS}</style></head><body>"
            f"<div class='auth-bg'>{body}</div></body></html>")

def _finish_onboarding(token, bname, cur):
    th = hashlib.sha256(token.encode()).hexdigest()
    row = q("SELECT * FROM onboarding_pending WHERE token_hash=?", (th,), one=True)
    if not row or row["expires"] < now_iso():
        return None
    biz_id = exec_db("INSERT INTO businesses(name,currency,created_at) VALUES(?,?,?)",
                     (bname, cur, now_iso()))
    nm = row["name"] if "name" in row.keys() else ""
    user_id = exec_db("INSERT INTO users(business_id,email,pw_hash,firebase_uid,role,name,created_at) VALUES(?,?,?,?,?,?,?)",
                      (biz_id, row["email"], "", row["firebase_uid"], "owner", nm, now_iso()))
    exec_db("DELETE FROM onboarding_pending WHERE token_hash=?", (th,), touch=False)
    return q("SELECT * FROM users WHERE id=?", (user_id,), one=True)


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    # -- response helpers
    def send_html(self, status, body, cookie=None):
        data = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", _csp_header())
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, loc, cookie=None):
        self.send_response(303)
        self.send_header("Location", loc)
        self.send_header("Content-Length", "0")
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()

    def send_json(self, status, obj):
        """v2.6: small JSON reply (used by /auth/google-code)."""
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def send_err(self, status, msg):
        self.send_html(status, page(None, None, "Error",
                                   f'<div class="card err">{esc(msg)}</div>'
                                   '<p><a href="/dashboard">Back to Dashboard</a></p>'))

    def send_file(self, data, filename, ctype):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Content-Security-Policy", "default-src 'self'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.end_headers()
        self.wfile.write(data)

    def multipart(self):
        """Minimal multipart/form-data parser (restore upload only).
        Returns (fields, filename, file_bytes)."""
        ctype = self.headers.get("Content-Type") or ""
        if "multipart/form-data" not in ctype or "boundary=" not in ctype:
            return {}, "", b""
        boundary = ctype.split("boundary=", 1)[1].strip().strip('"').encode()
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        fields, filename, file_bytes = {}, "", b""
        for part in raw.split(b"--" + boundary):
            if b"\r\n\r\n" not in part:
                continue
            head, body = part.split(b"\r\n\r\n", 1)
            if body.endswith(b"\r\n"):
                body = body[:-2]
            head_s = head.decode(errors="replace")
            if 'name="csv_file"' in head_s:
                fn = ""
                for chunk in head_s.split(";"):
                    chunk = chunk.strip()
                    if chunk.startswith("filename="):
                        fn = chunk[len("filename="):].strip('"')
                filename, file_bytes = os.path.basename(fn), body
            elif 'name="kind"' in head_s:
                fields["kind"] = body.decode(errors="replace")
            elif 'name="backup_file"' in head_s:
                fn = ""
                for chunk in head_s.split(";"):
                    chunk = chunk.strip()
                    if chunk.startswith("filename="):
                        fn = chunk[len("filename="):].strip('"')
                filename, file_bytes = os.path.basename(fn), body
            elif 'name="csrf"' in head_s:
                fields["csrf"] = body.decode(errors="replace")
        return fields, filename, file_bytes

    # -- request helpers
    def form(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length).decode(errors="replace") if length else ""
        return parse_qs(raw)

    def fv(self, form, key, default=""):
        vals = form.get(key)
        return vals[0] if vals else default

    def current_user(self):
        cookie = SimpleCookie(self.headers.get("Cookie") or "")
        morsel = cookie.get("labh_session")
        if not morsel:
            return None
        token = morsel.value
        th = hashlib.sha256(token.encode()).hexdigest()
        sess = q("SELECT * FROM sessions WHERE token_hash=?", (th,), one=True)
        if not sess or sess["expires"] < now_iso():
            return None
        user = q("SELECT * FROM users WHERE id=?", (sess["user_id"],), one=True)
        if not user:
            return None
        d = dict(user)
        d["_csrf"] = sess["csrf"]
        d["_token_hash"] = th
        return d

    def business(self, user):
        return q("SELECT * FROM businesses WHERE id=?", (user["business_id"],), one=True)

    def require_login(self):
        user = self.current_user()
        if not user:
            self.redirect("/login")
            return None
        return user

    def require_role(self, user, *roles):
        if user["role"] not in roles:
            raise Forbidden("Your role does not allow this action (staff: sell/view only).")

    def check_csrf(self, user, form):
        if not hmac.compare_digest(self.fv(form, "csrf"), user["_csrf"]):
            raise Forbidden("Invalid CSRF token.")

    # -- routing
    def do_GET(self):
        self.route()

    def do_POST(self):
        self.route()

    def route(self):
        try:
            parsed = urlparse(self.path)
            path = parsed.path.rstrip("/") or "/"
            parts = [p for p in path.split("/") if p]
            method = self.command
            if path == "/":
                return self.redirect("/dashboard")
            if path in ("/login", "/register"):
                return self.auth_page(path, method)
            if path == "/forgot":
                return self.view_forgot(method)
            if len(parts) == 2 and parts[0] == "onboarding":
                return self.view_onboarding(parts[1], method, parsed.query)
            if path == "/demo-enter":
                return self.demo_enter(method)
            if path == "/auth/google" and method == "POST":
                return self.auth_google()
            if path == "/logout" and method == "POST":
                return self.do_logout()
            # v2.6 unauthenticated Google-browser routes.
            if path == "/google-open" and method == "GET":
                # The APK intercepts this URL and opens the system
                # browser instead; a plain browser just follows through.
                return self.redirect("/google-browser")
            if path == "/google-browser" and method == "GET":
                return self.send_html(200, google_browser_html())
            if path == "/auth/google-code" and method == "POST":
                return self.auth_google_code()
            if path == "/auth/google-code-login" and method == "POST":
                return self.view_google_code_login()
            # v2.5 unauthenticated routes (choice + sync-restore).
            if path == "/login-choice" and method == "POST":
                return self.view_login_choice()
            if path == "/sync-restore":
                return self.view_sync_restore(method)
            user = self.require_login()
            if user is None:
                return
            biz = self.business(user)
            cur = biz["currency"]
            if not parts:
                return self.redirect("/dashboard")
            top = parts[0]
            if top == "dashboard" and method == "GET":
                return self.view_dashboard(user, biz, cur)
            if top == "more" and method == "GET":
                navmap = dict(NAV)
                links = "".join(f'<p><a class="btn" href="/{slug}">{esc(tr(navmap[slug]))}</a></p>' for slug, _ in NAV)
                return self.send_html(200, page(user, biz, "More", f'<div class="card"><h3>All sections</h3>{links}</div>'))
            if top == "scan" and method == "GET":
                return self.view_scan(user, biz, cur)
            if top == "profile":
                return self.view_profile(user, biz, cur, method)
            if top == "security":
                return self.view_security(user, biz, cur, method)
            if top == "brands" and method == "GET":
                return self.view_brands(user, biz, cur)
            if top == "income":
                return self.view_income(user, biz, cur, parts, method)
            if top == "pos":
                return self.view_pos(user, biz, cur, parts, method)
            if top == "health" and method == "GET":
                return self.view_health(user, biz, cur)
            if top == "ai":
                return self.view_ai(user, biz, cur, parts, method)
            if top == "search" and method == "GET":
                return self.view_search(user, biz, cur)
            if top == "notifications":
                return self.view_notifications(user, biz, cur, method)
            if top == "import":
                return self.view_import(user, biz, cur, parts, method)
            if top == "settings":
                return self.view_settings(user, biz, cur, parts, method)
            if top == "products":
                return self.view_products(user, biz, cur, parts, method)
            if top == "categories":
                return self.view_categories(user, biz, cur, parts, method)
            if top == "inventory" and len(parts) == 2 and parts[1] in ("reconcile", "imei-transfer"):
                return self.view_inventory_extra(user, biz, cur, parts[1], method)
            if top == "inventory":
                return self.view_inventory(user, biz, cur, parts, method)
            if top == "customers" and len(parts) == 2 and parts[1] == "dues":
                return self.view_dues(user, biz, cur, "customer")
            if top == "suppliers" and len(parts) == 2 and parts[1] == "dues":
                return self.view_dues(user, biz, cur, "supplier")
            if top == "customers":
                return self.view_customers(user, biz, cur, parts, method)
            if top == "suppliers":
                return self.view_suppliers(user, biz, cur, parts, method)
            if top == "sales":
                return self.view_sales(user, biz, cur, parts, method)
            if top == "purchases":
                return self.view_purchases(user, biz, cur, parts, method)
            if top == "invoices":
                return self.view_invoices(user, biz, cur, parts, method)
            if top == "payments":
                return self.view_payments(user, biz, cur, parts, method)
            if top == "expenses" and len(parts) == 2 and parts[1] == "overview":
                return self.view_expenses_overview(user, biz, cur)
            if top == "expenses":
                return self.view_expenses(user, biz, cur, parts, method)
            if top == "reports":
                if len(parts) >= 2:
                    return self.view_report_detail(user, biz, cur, parts, method)
                if method == "GET":
                    return self.view_reports(user, biz, cur)
                raise NotFound("Page not found")
            if top == "drive-save" and method == "POST":
                return self.drive_save(user, biz)
            if top == "backup":
                return self.view_backup(user, biz, cur, parts, method)
            if top == "updates-apk":
                return self.view_updates_apk(user, biz, cur, parts, method)
            if top == "updates":
                return self.view_updates(user, biz, cur, parts, method)
            raise NotFound("Page not found")
        except Forbidden as e:
            self.send_err(403, str(e))
        except NotFound as e:
            self.send_err(404, str(e))
        except Exception as e:  # never leak tracebacks to the browser
            self.send_err(500, "Something went wrong. Please try again.")


    # -- auth (Firebase ONLY — no local password login remains)
    def view_login_choice(self):
        """v2.5: consume a one-time choice token and start onboarding
        for that verified identity. No token = no business creation."""
        form = self.form()
        token = self.fv(form, "choice_token")
        th = hashlib.sha256(token.encode()).hexdigest()
        row = q("SELECT * FROM login_choices WHERE token_hash=?", (th,), one=True)
        if not row or row["expires"] < now_iso():
            return self.send_html(200, auth_html("login",
                err="This choice link has expired. Please sign in again."))
        exec_db("DELETE FROM login_choices WHERE token_hash=?", (th,), touch=False)
        return self.redirect("/onboarding/" + _pending_onboarding(
            row["firebase_uid"], row["email"]))

    def view_sync_restore(self, method):
        """v2.5: unauthenticated Drive-backup restore (same validation
        + safety-copy path as logged-in restore)."""
        if method == "GET":
            return self.send_html(200, sync_restore_html())
        if method != "POST":
            raise NotFound("Page not found")
        _fields, _fn, file_bytes = self.multipart()
        ok, problem = restore_db_from_bytes(file_bytes)
        if not ok:
            return self.send_html(400, sync_restore_html(
                err=f"Restore rejected: {problem}. Live data was not changed."))
        return self.send_html(200, self._sync_restore_success())

    def _sync_restore_success(self):
        body = ('<div class="auth-card"><div class="card ok">Backup restored. '
                'A safety copy of your previous database was saved in the '
                'backups/ folder. Please sign in now, because login sessions '
                'come from the restored backup.</div>'
                '<p style="text-align:center"><a class="btn" href="/login">Go to Sign In</a></p></div>')
        return ("<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                f"<title>Backup restored - LABH</title><style>{CSS}{EXTRA_CSS}</style></head><body>"
                f"<div class='auth-bg'>{body}</div></body></html>")

    def auth_page(self, path, method):
        if method == "GET":
            return self.send_html(200, auth_html("login" if path == "/login" else "register"))
        form = self.form()
        ip = self.client_address[0]
        if path == "/login":
            email = self.fv(form, "email").strip().lower()
            pw = self.fv(form, "password")
            if throttled(ip, email):
                return self.send_html(429, auth_html("login", err="Too many failed logins. Try again in 10 minutes."))
            try:
                uid, _tok = firebase_sign_in(email, pw)
            except FirebaseAuthError as e:
                record_fail(ip, email)
                m = str(e)
                err = ("Invalid email or password." if m in ("INVALID_LOGIN_CREDENTIALS", "INVALID_PASSWORD", "EMAIL_NOT_FOUND", "INVALID_EMAIL", "USER_DISABLED")
                       else "Could not reach Firebase. Check your internet connection and try again." if m == "NETWORK_ERROR"
                       else "Firebase is not configured on this device." if m == "FIREBASE_NOT_CONFIGURED"
                       else "Sign-in failed. Please try again.")
                return self.send_html(200, auth_html("login", err=err))
            user = find_or_link_firebase_user(uid, email)
            if not user:
                # v2.8 HARD RULE: email login is by definition an
                # existing-user path (password verified against an
                # existing Firebase account), so it NEVER offers to
                # create a business. (a) Resume a Register-started
                # pending setup, if one exists for this identity.
                # (b) Otherwise the sync-only page: manual Drive sync
                # + Back to Sign In, nothing else. Firebase auth
                # succeeded, so this is not a failure (and must not
                # count toward throttling).
                resumed = _resume_pending_token(uid, email)
                if resumed:
                    return self.redirect("/onboarding/" + resumed)
                return self.send_html(200, sync_only_html(
                    email, autosync_reason_note("no_token", via_google=False),
                    show_google_button=True))
            clear_fails(ip, email)
            return self.start_session(user)
        # register
        email = self.fv(form, "email").strip().lower()
        pw = self.fv(form, "password")
        name = self.fv(form, "name").strip()
        confirm = self.fv(form, "confirm")
        bname = self.fv(form, "business").strip()
        cur = self.fv(form, "currency").strip() or "BDT"
        if confirm and confirm != pw:
            return self.send_html(200, auth_html("register", err="Passwords do not match."))
        if len(pw) < 8 or "@" not in email:
            return self.send_html(200, auth_html("register", err="A valid email and a password of 8+ characters are required."))
        pending = q("SELECT * FROM users WHERE email=?", (email,), one=True)
        if pending and pending["firebase_uid"]:
            return self.send_html(200, auth_html("register", err="That email is already registered. Please log in instead."))
        if pending:
            try:
                uid, _tok = firebase_sign_up(email, pw)
            except FirebaseAuthError as e:
                if str(e) != "EMAIL_EXISTS":
                    return self.send_html(200, auth_html("register", err="Sign-up failed. Please try again."))
                try:
                    uid, _tok = firebase_sign_in(email, pw)
                except FirebaseAuthError:
                    return self.send_html(200, auth_html("register", err="That email is already registered. Please log in instead."))
            user = find_or_link_firebase_user(uid, email)
            if not user:
                return self.send_html(200, auth_html("register", err="Could not link this account. Please try again."))
            if name:
                exec_db("UPDATE users SET name=? WHERE id=?", (name, user["id"]), touch=False)
            return self.start_session(user)
        try:
            uid, _tok = firebase_sign_up(email, pw)
        except FirebaseAuthError as e:
            if str(e) == "EMAIL_EXISTS":
                return self.send_html(200, auth_html("register", err="That email is already registered. Please log in instead."))
            return self.send_html(200, auth_html("register", err="Sign-up failed. Please try again."))
        if bname:
            # Back-compat path (automated tests / API clients that still post a business name)
            biz_id = exec_db("INSERT INTO businesses(name,currency,created_at) VALUES(?,?,?)", (bname, cur, now_iso()))
            user_id = exec_db("INSERT INTO users(business_id,email,pw_hash,firebase_uid,role,name,created_at) VALUES(?,?,?,?,?,?,?)",
                              (biz_id, email, "", uid, "owner", name, now_iso()))
            return self.start_session(q("SELECT * FROM users WHERE id=?", (user_id,), one=True))
        # Normal UI flow: account created, business is step 2 (onboarding page)
        return self.redirect("/onboarding/" + _pending_onboarding(uid, email, name))

    def view_forgot(self, method):
        if method == "GET":
            return self.send_html(200, auth_html("forgot"))
        form = self.form()
        email = self.fv(form, "email").strip().lower()
        if "@" not in email:
            return self.send_html(200, auth_html("forgot", err="Please enter a valid email address."))
        try:
            firebase_reset_password(email)
        except FirebaseAuthError:
            return self.send_html(200, auth_html("forgot", err="Could not send the reset email. Check your connection and try again."))
        return self.send_html(200, auth_html("forgot", msg="Password reset link sent! Check your email inbox."))

    def view_onboarding(self, token, method, query=""):
        th = hashlib.sha256(token.encode()).hexdigest()
        row = q("SELECT * FROM onboarding_pending WHERE token_hash=?", (th,), one=True)
        if not row or row["expires"] < now_iso():
            return self.send_html(200, auth_html("login", err="This setup link has expired. Please register again."))
        if method == "GET":
            # v2.14: reached because Drive was checked and holds NO
            # LABH backup for this account — say so, and warn that a
            # business saved under another Google account will NOT
            # appear here (that account must sign in instead).
            banner = ('<p style="font-size:13px;color:#475569">No LABH backup '
                      'was found in Google Drive for this account, so set up '
                      'a fresh business here. If your business is saved under '
                      'a different Google account, sign in with that account '
                      'instead.</p>'
                      if "from=no_backup" in (query or "") else "")
            body = ('<div class="auth-card"><div style="text-align:center;margin-bottom:24px">'
                    f'<div class="auth-logo">{icon("building", 32)}</div>'
                    '<h1 style="font-size:20px;margin:0">Set up your business</h1>'
                    f'<p style="font-size:12px;color:#64748b">Account: {esc(row["email"])}</p></div>'
                    + banner +
                    f'<form method="post" action="/onboarding/{esc(token)}">'
                    + field("Business name", "business", "", "text", True)
                    + field("Currency (e.g. BDT, USD)", "currency", "BDT")
                    + '<p><button type="submit" class="auth-submit">Finish setup</button></p></form></div>')
            return self.send_html(200, "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                f"<title>Business setup - LABH</title><style>{CSS}{EXTRA_CSS}</style></head><body>"
                f"<div class='auth-bg'>{body}</div></body></html>")
        form = self.form()
        bname = self.fv(form, "business").strip()
        cur = self.fv(form, "currency").strip() or "BDT"
        if not bname:
            return self.send_html(200, auth_html("login", err="Business name is required to finish setup."))
        user = _finish_onboarding(token, bname, cur)
        if not user:
            return self.send_html(200, auth_html("login", err="This setup link has expired. Please register again."))
        return self.start_session(user)

    def auth_google(self):
        """Google sign-in callback. The idToken is the credential here
        (no session exists yet); it is verified against this Firebase
        project before any session is created. An unverified, expired
        or wrong-project token is rejected outright."""
        form = self.form()
        ip = self.client_address[0]
        token = self.fv(form, "idToken")
        if not token:
            return self.send_html(200, page(None, None, "Login",
                '<div class="card err">Google sign-in failed. Please try again.</div>'))
        try:
            uid, email = firebase_verify_google_token(token)
        except FirebaseAuthError as e:
            # v2.11: show the SPECIFIC reason, not one generic line —
            # the generic line hid whether the phone could not reach
            # Google at all (network) or the token itself was rejected.
            # The code is a failure category only; no token or config
            # value is ever rendered.
            record_fail(ip, "google")
            code = str(e)
            if code == "FIREBASE_NOT_CONFIGURED":
                msg = ("Firebase is not configured on this device. "
                       "Copy firebase_config.json next to app.py and restart.")
            elif code == "NETWORK_ERROR":
                msg = ("Could not reach Google to verify your sign-in. "
                       "Check your internet connection and try again. (NETWORK_ERROR)")
            else:
                msg = ("Google sign-in could not be verified. Please try again. "
                       "(INVALID_TOKEN)")
            return self.send_html(200, page(None, None, "Login",
                f'<div class="card err">{esc(msg)}</div>'))
        user = find_or_link_firebase_user(uid, email)
        if user:
            return self.start_session(user)
        # v2.8 HARD RULE: the new-business page appears ONLY for
        # genuinely NEW users. isNewUser missing/unknown counts as
        # EXISTING (safe side: sync/manual only, never create).
        is_new = parse_is_new_user(form, self.fv)
        drive_token = self.fv(form, "driveAccessToken").strip()
        if is_new is True:
            # No local user: business name is step 2 (onboarding),
            # unless the caller supplied one (back-compat path).
            bname = self.fv(form, "business").strip()
            cur = self.fv(form, "currency").strip() or "BDT"
            if bname:
                biz_id = exec_db("INSERT INTO businesses(name,currency,created_at) VALUES(?,?,?)",
                                 (bname, cur, now_iso()))
                user_id = exec_db("INSERT INTO users(business_id,email,pw_hash,firebase_uid,role,created_at) VALUES(?,?,?,?,?,?)",
                                  (biz_id, email, "", uid, "owner", now_iso()))
                return self.start_session(q("SELECT * FROM users WHERE id=?", (user_id,), one=True))
            # v2.6: shared verified-user step (resume or new onboarding).
            return self.finish_verified_google(uid, email)
        # EXISTING user, no local business: auto-sync from Drive first.
        # Order matters: a Drive backup holding this identity is the
        # user's real business; a Register-started pending setup is
        # only resumed when auto-sync finds nothing.
        # v2.13: the Settings auto-sync switch gates this — when OFF,
        # no Drive download runs at all (the user asked for a switch
        # that must be enabled for auto-sync to happen).
        if not auto_sync_allowed():
            return self.send_html(200, sync_only_html(
                email, autosync_reason_note("auto_sync_off", via_google=True),
                show_google_button=(not drive_token)))
        synced, reason = try_drive_auto_sync_detailed(uid, email, drive_token)
        if synced:
            return self.start_session(synced)
        # v2.14 (user directive: "backup na thakle direct login hobe"):
        # Drive was REACHED and holds no LABH backup at all, so there
        # is nothing to sync and no reason for the sync-only dead
        # end — go straight to business setup, which finishes with a
        # session. Other reasons (unreachable, no permission, foreign
        # or invalid backup) still get the sync-only page, because a
        # real business may exist that we could not verify — creating
        # a fresh one there could duplicate or lose it.
        if reason == "no_backup":
            return self.finish_verified_google(uid, email, "?from=no_backup")
        resumed = _resume_pending_token(uid, email)
        if resumed:
            return self.redirect("/onboarding/" + resumed)
        return self.send_html(200, sync_only_html(
            email, autosync_reason_note(reason, via_google=True),
            show_google_button=(not drive_token)))

    def finish_verified_google(self, uid, email, query=""):
        """v2.6 shared post-verification step for /auth/google and for
        code login: existing user -> session; otherwise onboarding
        (resuming an unexpired pending link when one exists). Never
        creates a business silently. query is appended to the
        onboarding redirect (v2.14: '?from=no_backup' so the setup
        page can explain why a fresh business is being offered)."""
        resumed = _resume_pending_token(uid, email)
        if resumed:
            return self.redirect("/onboarding/" + resumed + query)
        return self.redirect("/onboarding/" + _pending_onboarding(uid, email) + query)

    def auth_google_code(self):
        """v2.6: verify a Firebase idToken (exactly like /auth/google)
        and issue a short one-time code for it. The code proves ONLY
        that this token was verified — it carries no session, and the
        idToken itself is never logged or returned."""
        form = self.form()
        ip = self.client_address[0]
        # Issuance rate limit: max 10 codes per IP per 10 minutes.
        window_start = (datetime.now() - timedelta(minutes=10)).isoformat(timespec="seconds")
        recent = q("SELECT COUNT(*) FROM google_login_codes WHERE ip=? AND created>=?",
                   (ip, window_start), one=True)[0]
        if recent >= 10:
            return self.send_json(429, {"ok": False, "error": "Too many codes requested. Try again later."})
        token = self.fv(form, "idToken")
        if not token:
            return self.send_json(400, {"ok": False, "error": "Missing token."})
        try:
            uid, email = firebase_verify_google_token(token)
        except FirebaseAuthError:
            record_fail(ip, "google-code")
            return self.send_json(400, {"ok": False, "error": "Google sign-in could not be verified. Please try again."})
        code = str(secrets.randbelow(900000) + 100000)
        ch = hashlib.sha256(code.encode()).hexdigest()
        exp = (datetime.now() + timedelta(minutes=10)).isoformat(timespec="seconds")
        # v2.8: carry the Drive token + isNewUser with the code. The DB
        # row holds ONLY the token's hash and the flag; the plaintext
        # token lives in process memory (DRIVE_TOKENS) until redeemed.
        drive_token = self.fv(form, "driveAccessToken").strip()
        is_new = parse_is_new_user(form, self.fv)
        exec_db("INSERT INTO google_login_codes(code_hash,firebase_uid,email,expires,ip,created,drive_token_hash,is_new_user) VALUES(?,?,?,?,?,?,?,?)",
                (ch, uid, email, exp, ip, now_iso(),
                 hashlib.sha256(drive_token.encode()).hexdigest() if drive_token else "",
                 "1" if is_new is True else ("0" if is_new is False else "")), touch=False)
        if drive_token:
            DRIVE_TOKENS[ch] = (drive_token, exp)
        return self.send_json(200, {"ok": True, "code": code})

    def view_google_code_login(self):
        """v2.6: sign in with a one-time browser code. The code is
        looked up by hash, must be unexpired and unused, is deleted
        on use (single-use), and the login then runs the SAME
        verified-user logic as /auth/google — for the uid/email the
        code was issued for, never for a posted email. Wrong codes are
        throttled (5 tries per 10 min per IP) so the 6-digit space
        cannot be brute-forced."""
        form = self.form()
        ip = self.client_address[0]
        code = self.fv(form, "code").strip()
        if throttled(ip, "google-code"):
            return self.send_html(429, auth_html("login", err="Too many code attempts. Try again in 10 minutes."))
        ch = hashlib.sha256(code.encode()).hexdigest()
        row = q("SELECT * FROM google_login_codes WHERE code_hash=?", (ch,), one=True)
        if not row or row["expires"] < now_iso():
            record_fail(ip, "google-code")
            if row:
                exec_db("DELETE FROM google_login_codes WHERE code_hash=?", (ch,), touch=False)
            return self.send_html(200, auth_html("login",
                err="Invalid or expired code. Please sign in with Google again to get a new code."))
        exec_db("DELETE FROM google_login_codes WHERE code_hash=?", (ch,), touch=False)
        clear_fails(ip, "google-code")
        # v2.8: redeem the carried Drive token (single-use, memory only).
        drive_entry = DRIVE_TOKENS.pop(ch, None)
        drive_token = drive_entry[0] if drive_entry else ""
        user = find_or_link_firebase_user(row["firebase_uid"], row["email"])
        if user:
            return self.start_session(user)
        # v2.8 HARD RULE, same as /auth/google: only isNewUser=true
        # may reach the new-business flow; anything else (false or
        # unknown) auto-syncs, else gets the sync-only page.
        is_new = True if row["is_new_user"] == "1" else False
        if is_new:
            return self.finish_verified_google(row["firebase_uid"], row["email"])
        synced, reason = try_drive_auto_sync_detailed(
            row["firebase_uid"], row["email"], drive_token)
        if synced:
            return self.start_session(synced)
        # v2.14: same no-backup rule as /auth/google — straight to
        # business setup instead of the sync-only page.
        if reason == "no_backup":
            return self.finish_verified_google(
                row["firebase_uid"], row["email"], "?from=no_backup")
        resumed = _resume_pending_token(row["firebase_uid"], row["email"])
        if resumed:
            return self.redirect("/onboarding/" + resumed)
        return self.send_html(200, sync_only_html(
            row["email"], autosync_reason_note(reason, via_google=True),
            show_google_button=(not drive_token)))

    def start_session(self, user):
        token = secrets.token_urlsafe(32)
        th = hashlib.sha256(token.encode()).hexdigest()
        csrf = secrets.token_urlsafe(24)
        exp = (datetime.now() + timedelta(hours=12)).isoformat(timespec="seconds")
        exec_db("INSERT INTO sessions(token_hash,user_id,csrf,expires) VALUES(?,?,?,?)",
                (th, user["id"], csrf, exp), touch=False)
        cookie = f"labh_session={token}; HttpOnly; Path=/; SameSite=Lax; Max-Age=43200"
        return self.redirect("/dashboard", cookie=cookie)

    def do_logout(self):
        user = self.current_user()
        form = self.form()
        if user:
            try:
                self.check_csrf(user, form)
            except Forbidden:
                pass
            exec_db("DELETE FROM sessions WHERE token_hash=?", (user["_token_hash"],), touch=False)
        return self.redirect("/login", cookie="labh_session=; HttpOnly; Path=/; Max-Age=0")

    # -- backup / restore (local-first, manual Google Drive flow)

    def demo_enter(self, method):
        """DEMO ONLY (LABH_DEMO=1): one-click entry as the seeded demo
        user. No password, no Firebase, no Drive — demo data only.
        404 unless demo mode is on."""
        if os.environ.get("LABH_DEMO") != "1":
            raise NotFound("Page not found")
        if method == "GET":
            return self.send_html(200, "<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
                "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                f"<title>LABH demo</title><style>{CSS}{EXTRA_CSS}</style></head><body>"
                "<div class='auth-bg'><div class='auth-card'><h1 style='text-align:center'>LABH demo website</h1>"
                "<p style='text-align:center'>Explore the full app with demo data. No sign-in needed, "
                "no real business data is involved, and nothing syncs anywhere.</p>"
                "<form method='post' action='/demo-enter'>"
                "<p><button type='submit' class='auth-submit'>Enter the demo</button></p></form></div></div></body></html>")
        user = q("SELECT * FROM users WHERE email=?", ("demo@labh.demo",), one=True)
        if not user:
            return self.send_html(500, "Demo user is missing. Please redeploy the demo.")
        return self.start_session(user)

    def drive_save(self, user, biz):
        """v2.13: POST /drive-save (logged-in only). Verifies the
        posted Google idToken, requires it to match the signed-in
        user, then uploads a fresh snapshot of labh.db to Drive as
        'labh-sync.db' (updating the previous upload when its file id
        is known, so Drive never fills with copies). Records the save
        time + file id in settings. The Drive token is used once and
        never stored or logged. Returns a result page, never a file."""
        if os.environ.get("LABH_DEMO") == "1":
            return self.send_html(400, page(user, biz, "Backup",
                '<div class="card err">Google Drive is disabled on the demo site.</div>'
                '<p><a class="btn" href="/backup">Back to Backup</a></p>'))
        form = self.form()
        token = self.fv(form, "idToken")
        drive_token = self.fv(form, "driveAccessToken")
        if not token or not drive_token:
            return self.send_html(400, page(user, biz, "Backup",
                '<div class="card err">Drive save failed: Google did not '
                'return a Drive token. Sign in with Google again and allow '
                'Drive access on the consent screen.</div>'
                '<p><a class="btn" href="/backup">Back to Backup</a></p>'))
        try:
            uid, email = firebase_verify_google_token(token)
        except Exception:
            return self.send_html(400, page(user, biz, "Backup",
                '<div class="card err">Drive save failed: Google sign-in '
                'could not be verified. Please try again.</div>'
                '<p><a class="btn" href="/backup">Back to Backup</a></p>'))
        if not ((user.get("firebase_uid") and user["firebase_uid"] == uid)
                or (user.get("email") and email
                    and user["email"].lower() == email.lower())):
            return self.send_html(403, page(user, biz, "Backup",
                '<div class="card err">Drive save failed: this Google '
                'account does not match the signed-in user. Sign in with '
                'the same Google account you use in LABH.</div>'
                '<p><a class="btn" href="/backup">Back to Backup</a></p>'))
        with LOCK:
            path, _fname = make_backup()
            with open(path, "rb") as f:
                data = f.read()
        file_id, kind = drive_upload_file(
            drive_token, data, get_setting("drive_sync_file_id") or None)
        if not file_id:
            if kind == "auth":
                msg = ("Drive save failed: the Drive permission was not "
                       "granted for this sign-in. Sign in with Google again "
                       "and allow Drive access on the consent screen.")
            elif kind == "too_big":
                msg = "Drive save failed: the database is too large to upload."
            elif kind == "network":
                msg = ("Drive save failed: Google Drive could not be "
                       "reached. Check your connection and try again.")
            else:
                msg = "Drive save failed: Drive returned an error. Please try again."
            return self.send_html(400, page(user, biz, "Backup",
                f'<div class="card err">{esc(msg)}</div>'
                '<p><a class="btn" href="/backup">Back to Backup</a></p>'))
        set_setting("drive_saved_at", now_iso())
        set_setting("drive_sync_file_id", file_id)
        return self.send_html(200, page(user, biz, "Backup",
            '<div class="card ok">Database saved to Google Drive as '
            '<b>labh-sync.db</b>. On another device, Google sign-in with '
            'auto-sync on will download this backup.</div>'
            '<p><a class="btn" href="/backup">Back to Backup</a></p>'))

    def view_backup(self, user, biz, cur, parts, method):
        b = user["business_id"]
        if method == "POST" and len(parts) >= 2 and parts[1] == "sync-now":
            self.require_role(user, "owner", "admin")
            form = self.form()
            self.check_csrf(user, form)
            action, msg = sync_check()
            if action in ("restored", "conflict"):
                return self.send_html(200, page(None, None, "Synced",
                    f'<div class="card ok">{esc(msg)}</div>'
                    '<p><a class="btn" href="/login">Go to Login</a></p>'))
            cls = "err" if action == "error" else "ok"
            return self.send_html(200, page(user, biz, "Backup",
                f'<div class="card {cls}">Sync: {esc(msg or "Already up to date.")}</div>'
                '<p><a class="btn" href="/backup">Back to Backup</a></p>'))
        if method == "POST" and len(parts) >= 2 and parts[1] == "export":
            form = self.form()
            self.check_csrf(user, form)
            with LOCK:
                path, fname = make_backup()
                publish_sync(path)
                with open(path, "rb") as f:
                    data = f.read()
            return self.send_file(data, fname, "application/octet-stream")
        if method == "POST" and len(parts) >= 2 and parts[1] == "export-json":
            form = self.form()
            self.check_csrf(user, form)
            out = {"app": "LABH", "exported_at": now_iso(),
                   "business": {"id": b, "name": biz["name"], "currency": cur}}
            for tbl in ("categories", "products", "customers", "suppliers", "sales",
                        "sale_items", "purchases", "purchase_items", "payments",
                        "expenses", "stock_movements", "imeis"):
                if tbl == "sale_items":
                    rows = q("SELECT si.* FROM sale_items si JOIN sales s ON s.id=si.sale_id "
                             "WHERE s.business_id=?", (b,))
                elif tbl == "purchase_items":
                    rows = q("SELECT pi.* FROM purchase_items pi JOIN purchases p ON p.id=pi.purchase_id "
                             "WHERE p.business_id=?", (b,))
                else:
                    rows = q(f"SELECT * FROM {tbl} WHERE business_id=?", (b,))
                out[tbl] = [dict(r) for r in rows]
            out["users"] = [dict(r) for r in q(
                "SELECT email, role, created_at FROM users WHERE business_id=?", (b,))]
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            data = json.dumps(out, indent=2, default=str).encode()
            return self.send_file(data, f"labh-backup-{stamp}.json",
                                  "application/json")
        if method == "POST" and len(parts) >= 2 and parts[1] == "restore":
            self.require_role(user, "owner", "admin")
            fields, filename, file_bytes = self.multipart()
            if not hmac.compare_digest(fields.get("csrf", ""), user["_csrf"]):
                raise Forbidden("Invalid CSRF token.")
            ok, problem = restore_db_from_bytes(file_bytes)
            if not ok:
                return self.send_html(400, page(user, biz, "Backup",
                    f'<div class="card err">Restore rejected: {esc(problem)}. '
                    'Live data was not changed.</div><p><a href="/backup">Back to Backup</a></p>'))
            # Sessions live inside the restored DB, so this session is no
            # longer valid: send the user back to login.
            return self.send_html(200, page(None, None, "Backup restored",
                '<div class="card ok">Backup restored. A safety copy of your previous '
                'database was saved in the backups/ folder. Please log in again, '
                'because login sessions come from the restored backup.</div>'
                '<p><a class="btn" href="/login">Go to Login</a></p>'))
        if method != "GET" or len(parts) != 1:
            raise NotFound("Page not found")
        kept = sorted((f for f in os.listdir(BACKUP_DIR) if f.startswith("labh-backup-"))
                      , reverse=True)[:10] if os.path.isdir(BACKUP_DIR) else []
        kept_html = "".join(f"<li>{esc(f)}</li>" for f in kept) or "<li>None yet.</li>"
        body = ('<div class="card"><h3>Your data stays on this device</h3>'
                '<p>All data lives in <b>labh.db</b> on this device and the app works '
                'fully offline — no hosting is needed. A backup is a file you keep '
                'yourself (for example in Google Drive) and download back when needed.</p></div>'
                '<div class="card"><h3>Sync now (Google Drive)</h3><p>Syncs first from the sync folder '
                'set in Settings, before any data is shown. Last synced: '
                + esc(get_setting("last_sync_ts") or "never")
                + ' · Status: ' + esc(get_setting("last_sync_status") or "—") + '</p>'
                + form_start("/backup/sync-now", user["_csrf"])
                + '<button type="submit">Sync now</button></form></div>'
                '<div class="card"><h3>Export backup</h3><p>Downloads a dated copy of the '
                'whole database. A copy is also kept in the <b>backups/</b> folder on this device.</p>'
                + form_start("/backup/export", user["_csrf"])
                + '<button type="submit">Download database backup (.db)</button></form>'
                + '<div style="margin-top:10px">' + form_start("/backup/export-json", user["_csrf"])
                + '<button type="submit">Download JSON export (this business only)</button></form></div></div>'
                '<div class="card"><h3>Save to Google Drive</h3><p>Uploads this '
                'database to Google Drive as <b>labh-sync.db</b> (the previous '
                'upload is updated, not duplicated). Last saved to Drive: '
                + esc(get_setting("drive_saved_at") or "never")
                + '. This is step one of auto-sync: save here first, then '
                'Google sign-in on another device downloads it when auto-sync '
                'is on in Settings.</p>' + drive_save_html() + '</div>'
                '<div class="card"><h3>Restore from backup</h3>'
                '<p><b>Owner/admin only.</b> Upload a .db backup you downloaded from Google Drive. '
                'The file is checked first — if it is not a valid LABH database, it is rejected and '
                'your live data is <b>not</b> changed. A valid restore <b>replaces all data on this '
                'device</b> with the backup, and a safety copy of the current database is saved in '
                'backups/ first. You will need to log in again afterwards.</p>'
                f'<form method="post" action="/backup/restore" enctype="multipart/form-data">'
                f'<input type="hidden" name="csrf" value="{esc(user["_csrf"])}">'
                '<p><label>Backup file (.db)</label><br><input type="file" name="backup_file" required></p>'
                '<button type="submit" class="danger">Restore backup</button></form></div>'
                f'<div class="card"><h3>Local backups kept on this device</h3><ul>{kept_html}</ul></div>'
                '<div class="card"><h3>Sync via Google Drive — PC + phone, same data</h3>'
                '<p>Google Drive is the sync hub for both versions of this app: the PC app and the '
                'standalone phone app use the <b>same database format and the same backup file</b>, '
                'so one shared Drive backup file is the sync source for both.</p>'
                '<p><b>PC → phone:</b> here, click <b>Download database backup</b> → save the file in '
                'Google Drive → on the phone, download that file from Drive → LABH app → Backup → '
                'choose the file → <b>Restore backup</b>.</p>'
                '<p><b>Phone → PC:</b> on the phone, Backup → Download backup (it lands in Downloads) '
                '→ save it to Google Drive → here, download it from Drive → Backup → choose the file '
                '→ <b>Restore backup</b>.</p>'
                '<p><b>Latest Drive backup wins.</b> A restore <b>replaces all data on that device</b> '
                'with the backup — it is <b>not</b> a merge, and there is no automatic two-way sync. '
                'Always export a fresh backup from the device you are leaving before restoring on '
                'the other one.</p>'
                '<p>On the phone this works through the phone\'s own Drive access (download/share a '
                'backup file to Drive, pick a Drive-downloaded file to restore). This is a '
                '<b>manual</b> backup-file flow — it is <b>not</b> automatic background Google Drive '
                'sync (no Drive API connection is configured).</p></div>')
        return self.send_html(200, page(user, biz, "Backup", body))

    # -- updates (in-app update system; no built-in update server)
    def view_updates(self, user, biz, cur, parts, method):
        is_admin = user["role"] in ("owner", "admin")
        # Pop-up dismissal: Later (this session) or Don't show again
        # for this version (persistent, in updates/dismissed.json).
        # Neither touches labh.db and neither installs anything.
        if method == "POST" and len(parts) == 2 and parts[1] == "dismiss":
            form = self.form()
            self.check_csrf(user, form)
            version = self.fv(form, "version").strip()
            if version:
                if self.fv(form, "mode") == "never":
                    dismiss_update_forever(user, version)
                else:
                    dismiss_update_for_session(user, version)
            return self.redirect("/dashboard")
        # Download a previously fetched package to this device (APK
        # users need this: the file is then opened on the phone).
        if method == "GET" and len(parts) == 2 and parts[1] == "file":
            if not is_admin:
                raise Forbidden("Only owner/admin may download update packages.")
            meta = read_update_meta()
            fname = os.path.basename(meta.get("filename", ""))
            path = os.path.join(UPDATES_DIR, fname) if fname else ""
            if not fname or not os.path.isfile(path):
                return self.send_html(404, page(user, biz, "Updates",
                    '<div class="card err">No downloaded update package found. '
                    'Download an update first.</div><p><a href="/updates">Back to Updates</a></p>'))
            with open(path, "rb") as f:
                return self.send_file(f.read(), fname, "application/octet-stream")
        if method == "POST" and len(parts) == 2 and parts[1] == "download":
            self.require_role(user, "owner", "admin")
            form = self.form()
            self.check_csrf(user, form)
            url = get_setting("update_manifest_url")
            if not url:
                return self.send_html(400, page(user, biz, "Updates",
                    '<div class="card err">No update source is set. Set a manifest URL in '
                    'Settings first.</div><p><a href="/updates">Back to Updates</a></p>'))
            manifest, err = fetch_manifest(url)
            if err or not manifest or not manifest["download_url"]:
                return self.send_html(400, page(user, biz, "Updates",
                    f'<div class="card err">Could not download: {esc(err or "manifest has no download_url")}.</div>'
                    '<p><a href="/updates">Back to Updates</a></p>'))
            durl = manifest["download_url"]
            fname = safe_package_filename(durl, manifest["version"])
            if os.path.splitext(fname)[1].lower() not in (".py", ".zip", ".apk"):
                return self.send_html(400, page(user, biz, "Updates",
                    f'<div class="card err">Unsupported package type for {esc(fname)} '
                    '(expected .py, .zip or .apk). Nothing was downloaded.</div>'
                    '<p><a href="/updates">Back to Updates</a></p>'))
            try:
                req = urllib.request.Request(durl, headers={"User-Agent": "LABH-Updater"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = resp.read(MAX_DOWNLOAD_BYTES + 1)
            except Exception as e:
                return self.send_html(400, page(user, biz, "Updates",
                    f'<div class="card err">Download failed ({esc(type(e).__name__)}). '
                    'Nothing was installed.</div><p><a href="/updates">Back to Updates</a></p>'))
            if len(data) > MAX_DOWNLOAD_BYTES:
                return self.send_html(400, page(user, biz, "Updates",
                    '<div class="card err">Download is over the 100 MB limit. Nothing was installed.</div>'
                    '<p><a href="/updates">Back to Updates</a></p>'))
            os.makedirs(UPDATES_DIR, exist_ok=True)
            path = os.path.join(UPDATES_DIR, fname)
            with open(path, "wb") as f:
                f.write(data)
            ok, kind, problem = validate_package(path)
            if not ok:
                try:
                    os.remove(path)
                except OSError:
                    pass
                return self.send_html(400, page(user, biz, "Updates",
                    f'<div class="card err">Downloaded file rejected: {esc(problem)}. Nothing was installed.</div>'
                    '<p><a href="/updates">Back to Updates</a></p>'))
            write_update_meta({"version": manifest["version"], "filename": fname,
                               "download_url": durl, "kind": kind,
                               "downloaded_at": now_iso()})
            return self.send_html(200, page(user, biz, "Updates",
                f'<div class="card ok">Downloaded <b>{esc(fname)}</b> (version {esc(manifest["version"])}). '
                'It is saved in the <b>updates/</b> folder and has been checked, but '
                '<b>nothing is installed yet</b> — click <b>Install update</b> below to install it.</div>'
                '<p><a class="btn" href="/updates">Back to Updates</a></p>'))
        if method == "POST" and len(parts) == 2 and parts[1] == "install":
            self.require_role(user, "owner", "admin")
            form = self.form()
            self.check_csrf(user, form)
            meta = read_update_meta()
            fname = os.path.basename(meta.get("filename", ""))
            path = os.path.join(UPDATES_DIR, fname) if fname else ""
            if not fname or not os.path.isfile(path):
                return self.send_html(400, page(user, biz, "Updates",
                    '<div class="card err">No downloaded update package found. '
                    'Download an update first.</div><p><a href="/updates">Back to Updates</a></p>'))
            ok, kind, problem = validate_package(path)
            if not ok:
                return self.send_html(400, page(user, biz, "Updates",
                    f'<div class="card err">Package rejected: {esc(problem)}. Nothing was installed.</div>'
                    '<p><a href="/updates">Back to Updates</a></p>'))
            if kind == "apk":
                return self.send_html(200, page(user, biz, "Updates",
                    '<div class="card"><h3>APK updates install on the phone, not on this PC</h3>'
                    '<p>An APK cannot be installed by this Python server. To install it: '
                    f'<a class="btn" href="/updates/file">Download the APK file</a>, copy it to your '
                    'phone, open it, and allow “install unknown apps” when Android asks. '
                    'Your data is not touched by this.</p>'
                    '<p>Note: if the phone app is the WebView wrapper pointing at this PC server, '
                    'updating this PC app is already enough for most updates — the phone sees the '
                    'new version as soon as this server restarts. A new APK is only needed when the '
                    'Android wrapper itself changes.</p></div>'
                    '<p><a href="/updates">Back to Updates</a></p>'))
            install_src = path
            if kind == "zip":
                try:
                    with zipfile.ZipFile(path) as zf:
                        member = next((n for n in zf.namelist()
                                       if n == "app.py" or n.endswith("/app.py")), None)
                        if member is None:
                            raise ValueError("zip does not contain app.py")
                        fd, tmp = tempfile.mkstemp(suffix=".py", prefix="labh-update-")
                        with os.fdopen(fd, "wb") as f:
                            f.write(zf.read(member))
                        install_src = tmp
                except Exception as e:
                    return self.send_html(400, page(user, biz, "Updates",
                        f'<div class="card err">Zip rejected: {esc(str(e))}. Nothing was installed.</div>'
                        '<p><a href="/updates">Back to Updates</a></p>'))
            # install_python_update touches only app.py + updates/ — never labh.db.
            backup = install_python_update(install_src, APP_PY_PATH, UPDATES_DIR)
            return self.send_html(200, page(user, biz, "Updates",
                '<div class="card ok"><h3>Update installed — restart required</h3>'
                f'<p>New version <b>{esc(meta.get("version", ""))}</b> is now the app file. '
                f'Your old app was backed up as <b>{esc(os.path.basename(backup))}</b> in updates/ '
                'if you ever need to roll back (stop the app, copy that file back over app.py, restart).</p>'
                '<p><b>Your data was not touched</b> — labh.db and backups/ are unchanged.</p>'
                '<p>To use the new version: stop the app (Ctrl+C) and run <b>python3 app.py</b> again. '
                'This running process keeps serving the old version until it restarts — that is normal.</p></div>'
                '<p><a href="/updates">Back to Updates</a></p>'))
        if method != "GET" or len(parts) != 1:
            raise NotFound("Page not found")
        url = get_setting("update_manifest_url")
        meta = read_update_meta()
        head = (f'<div class="card"><h3>Current version: {esc(VERSION)}</h3>'
                '<p>Updates come from a manifest file <b>you</b> point the app at — '
                'there is no built-in update server, so the app never invents an update.</p>'
                '<p>For the standalone <b>phone app</b>, APK updates live on a separate page: '
                '<a class="btn" href="/updates-apk">APK Updates</a> '
                '(folder scan + APK manifest — this PC page is unchanged).</p></div>')
        if not url:
            body = (head + '<div class="card"><h3>No update source is set yet</h3>'
                    '<p>To enable updates, an owner/admin sets <b>Update manifest URL</b> in '
                    '<a href="/settings">Settings</a> to a <b>version.json</b> file hosted anywhere '
                    '(your own site, a GitHub release asset, Google Drive direct link — any public URL), '
                    'with this format:</p>'
                    '<pre>{\n  "version": "1.1.0",\n  "release_date": "2026-10-26",\n'
                    '  "details": ["Faster sales page", "New stock report"],\n'
                    '  "download_url": "https://example.com/labh-1.1.0.zip"\n}</pre>'
                    '<p>download_url must point to a <b>.py</b> (new app.py), <b>.zip</b> (containing app.py) '
                    'or <b>.apk</b> (Android) file. Until a URL is set, this page simply reports that '
                    'no update source is configured.</p></div>')
            return self.send_html(200, page(user, biz, "Updates", body))
        manifest, err = fetch_manifest(url)
        if err or not manifest:
            body = (head + f'<div class="card"><h3>Update source: {esc(url)}</h3>'
                    f'<div class="err">Could not check for updates: {esc(err or "unknown error")}. '
                    'Your current version keeps working — nothing was downloaded or changed. '
                    'Check the URL in Settings and try again.</div></div>')
            return self.send_html(200, page(user, biz, "Updates", body))
        cmp = compare_versions(manifest["version"], VERSION)
        details_html = "".join(f"<li>{esc(d)}</li>" for d in manifest["details"]) or "<li>No details listed.</li>"
        if cmp <= 0:
            status = ('<div class="card ok"><h3>You are up to date</h3>'
                      f'<p>Latest available version is <b>{esc(manifest["version"])}</b> '
                      f'({esc(manifest["release_date"]) or "date not given"}), current version is <b>{esc(VERSION)}</b>.</p></div>')
        else:
            dl = ""
            if is_admin:
                dl = (form_start("/updates/download", user["_csrf"])
                      + '<button type="submit">Download update</button></form>'
                      '<p>Download only saves the package in <b>updates/</b> and checks it — '
                      'nothing is installed until you click Install.</p>')
            else:
                dl = '<p>Only owner/admin can download and install updates.</p>'
            status = (f'<div class="card"><h3>New version available: {esc(manifest["version"])}</h3>'
                      f'<p>Released: {esc(manifest["release_date"]) or "date not given"}. '
                      f'Current version: {esc(VERSION)}.</p>'
                      '<h4>What is new</h4><ul>' + details_html + '</ul>' + dl + '</div>')
        dl_meta = ""
        if meta.get("filename") and os.path.isfile(os.path.join(UPDATES_DIR, os.path.basename(meta["filename"]))):
            inst = ""
            if is_admin:
                inst = (form_start("/updates/install", user["_csrf"])
                        + '<button type="submit">Install update</button></form>'
                        '<p>Install backs up the current app.py first, then replaces it. '
                        '<b>Your data (labh.db) is never touched.</b> Restart the app afterwards to use the new version.</p>')
            dl_meta = (f'<div class="card"><h3>Downloaded package ready</h3>'
                       f'<p><b>{esc(meta["filename"])}</b> (version {esc(meta.get("version", ""))}, '
                       f'downloaded {esc(meta.get("downloaded_at", ""))}).</p>'
                       + (f'<p><a class="btn" href="/updates/file">Download package to this device</a></p>' if is_admin else '')
                       + inst + '</div>')
        return self.send_html(200, page(user, biz, "Updates", head + status + dl_meta))

    # -- APK updates (v2.4; the PC Updates page above is unchanged)
    def view_updates_apk(self, user, biz, cur, parts, method):
        is_admin = user["role"] in ("owner", "admin")
        # Pop-up dismissal: Later (this session) or Don't show again
        # for this version (persistent, in updates/apk-dismissed.json).
        # Neither touches labh.db and neither installs anything.
        if method == "POST" and len(parts) == 2 and parts[1] == "dismiss":
            form = self.form()
            self.check_csrf(user, form)
            version = self.fv(form, "version").strip()
            if version:
                if self.fv(form, "mode") == "never":
                    dismiss_apk_update_forever(user, version)
                else:
                    dismiss_apk_update_for_session(user, version)
            return self.redirect("/dashboard")
        # Serve a local APK file (from the update folder, or one
        # downloaded from the manifest) so the Android WebView's
        # DownloadManager downloads it; tapping the completed
        # download opens the system installer. Owner/admin only,
        # basename-only, and the file must live inside one of the
        # two known folders — a path here can never escape them.
        if method == "GET" and len(parts) == 3 and parts[1] == "file":
            self.require_role(user, "owner", "admin")
            fname = os.path.basename(parts[2])
            if parse_apk_filename(fname) is None:
                raise NotFound("APK file not found")
            folder, _ = apk_update_folder()
            path = ""
            for d in (folder, UPDATES_DIR):
                cand = os.path.join(d, fname)
                if os.path.isfile(cand) and os.path.abspath(cand).startswith(
                        os.path.abspath(d) + os.sep):
                    path = cand
                    break
            if not path:
                return self.send_html(404, page(user, biz, "APK Updates",
                    '<div class="card err">APK file not found. Scan the folder again '
                    'or download it from the manifest first.</div>'
                    '<p><a href="/updates-apk">Back to APK Updates</a></p>'))
            try:
                size = os.path.getsize(path)
            except OSError:
                size = 0
            if size == 0 or size > MAX_DOWNLOAD_BYTES:
                return self.send_html(400, page(user, biz, "APK Updates",
                    '<div class="card err">APK file is empty or over the 100 MB limit. '
                    'Nothing was installed.</div><p><a href="/updates-apk">Back to APK Updates</a></p>'))
            with open(path, "rb") as f:
                data = f.read()
            ok, kind, problem = validate_package(path)
            if not ok or kind != "apk":
                return self.send_html(400, page(user, biz, "APK Updates",
                    f'<div class="card err">APK rejected: {esc(problem)}. Nothing was installed.</div>'
                    '<p><a href="/updates-apk">Back to APK Updates</a></p>'))
            return self.send_file(data, fname, "application/vnd.android.package-archive")
        # Download the manifest's APK to updates/ and check it.
        # Nothing is installed at this step: the next page's Install
        # button serves the file for Android's installer.
        if method == "POST" and len(parts) == 2 and parts[1] == "download":
            self.require_role(user, "owner", "admin")
            form = self.form()
            self.check_csrf(user, form)
            url = get_setting("apk_manifest_url").strip()
            if not url:
                return self.send_html(400, page(user, biz, "APK Updates",
                    '<div class="card err">No APK manifest URL is set. Set one in '
                    'Settings first, or put the APK file in the update folder.</div>'
                    '<p><a href="/updates-apk">Back to APK Updates</a></p>'))
            manifest, err = apk_manifest_update(url)
            if err or not manifest:
                return self.send_html(400, page(user, biz, "APK Updates",
                    f'<div class="card err">Could not download: {esc(err or "manifest has no .apk link")}. '
                    'Nothing was downloaded.</div><p><a href="/updates-apk">Back to APK Updates</a></p>'))
            durl = manifest["apk_url"]
            fname = safe_package_filename(durl, manifest["version"])
            if not fname.lower().endswith(".apk"):
                return self.send_html(400, page(user, biz, "APK Updates",
                    '<div class="card err">Manifest link is not an .apk file. Nothing was downloaded.</div>'
                    '<p><a href="/updates-apk">Back to APK Updates</a></p>'))
            try:
                req = urllib.request.Request(durl, headers={"User-Agent": "LABH-Updater"})
                with urllib.request.urlopen(req, timeout=60) as resp:
                    data = resp.read(MAX_DOWNLOAD_BYTES + 1)
            except Exception as e:
                return self.send_html(400, page(user, biz, "APK Updates",
                    f'<div class="card err">Download failed ({esc(type(e).__name__)}). '
                    'Nothing was installed.</div><p><a href="/updates-apk">Back to APK Updates</a></p>'))
            if len(data) > MAX_DOWNLOAD_BYTES:
                return self.send_html(400, page(user, biz, "APK Updates",
                    '<div class="card err">Download is over the 100 MB limit. Nothing was installed.</div>'
                    '<p><a href="/updates-apk">Back to APK Updates</a></p>'))
            os.makedirs(UPDATES_DIR, exist_ok=True)
            path = os.path.join(UPDATES_DIR, fname)
            with open(path, "wb") as f:
                f.write(data)
            ok, kind, problem = validate_package(path)
            if not ok or kind != "apk":
                try:
                    os.remove(path)
                except OSError:
                    pass
                return self.send_html(400, page(user, biz, "APK Updates",
                    f'<div class="card err">Downloaded file rejected: {esc(problem)}. Nothing was installed.</div>'
                    '<p><a href="/updates-apk">Back to APK Updates</a></p>'))
            write_apk_meta({"version": manifest["version"], "filename": fname,
                            "apk_url": durl, "downloaded_at": now_iso()})
            return self.send_html(200, page(user, biz, "APK Updates",
                f'<div class="card ok">Downloaded <b>{esc(fname)}</b> (version {esc(manifest["version"])}). '
                'It has been checked, but <b>nothing is installed yet</b>.</div>'
                f'<div class="card"><h3>Install on the phone</h3>'
                f'<p><a class="btn" href="/updates-apk/file/{esc(fname)}">Download / Install APK</a></p>'
                '<p>On Android, tapping this downloads the APK (DownloadManager); '
                'tap the completed download, then tap <b>Install</b> in Android\'s installer. '
                'The first time, Android asks you to allow “install unknown apps” for LABH — '
                'allow it once and the install proceeds. <b>Your data is not touched:</b> '
                'installing a new APK keeps the app\'s internal storage, and labh.db is never '
                'modified by an update.</p></div>'
                '<p><a href="/updates-apk">Back to APK Updates</a></p>'))
        # v2.9 manual GitHub release check: bypasses the dashboard's
        # once-per-day throttle, shows the result, installs nothing.
        # Any logged-in user may check; downloading/installing stays
        # owner/admin-only through the Download & Install link.
        if method == "POST" and len(parts) == 2 and parts[1] == "check":
            form = self.form()
            self.check_csrf(user, form)
            rel = github_release_update(timeout=8)
            if rel:
                cl = "".join(f"<li>{esc(d)}</li>" for d in rel["changelog"]) or "<li>No changelog listed.</li>"
                body = (f'<div class="card ok"><h3>New APK available: {esc(rel["version"])}</h3>'
                        f'<p>Released: {esc(rel["release_date"]) or "date not given"}. '
                        f'Current version: {esc(VERSION)}. '
                        f'Source: GitHub Releases ({esc(github_release_repo())}).</p>'
                        '<h4>What is new</h4><ul>' + cl + '</ul>'
                        f'<p><a class="btn" href="{esc(rel["apk_url"])}">Download &amp; Install</a></p>'
                        '<p>On Android this downloads via DownloadManager; tap the completed '
                        'download, then Install. Your data is not touched.</p></div>'
                        '<p><a href="/updates-apk">Back to APK Updates</a></p>')
            else:
                body = ('<div class="card"><h3>No newer GitHub release found</h3>'
                        f'<p>Checked GitHub Releases ({esc(github_release_repo())}). '
                        'Either you are on the latest release, or the releases page '
                        'could not be reached — your current version keeps working, '
                        'and nothing was downloaded or changed.</p></div>'
                        '<p><a href="/updates-apk">Back to APK Updates</a></p>')
            return self.send_html(200, page(user, biz, "APK Updates", body))
        if method != "GET" or len(parts) != 1:
            raise NotFound("Page not found")
        folder, folder_src = apk_update_folder()
        folder_cands = scan_apk_folder(folder)
        folder_newest = next((c for c in folder_cands
                              if compare_versions(c["version"], VERSION) > 0), None)
        manifest_url = get_setting("apk_manifest_url").strip()
        manifest, merr = (None, None)
        if manifest_url:
            manifest, merr = apk_manifest_update(manifest_url)
        head = (f'<div class="card"><h3>APK Updates — current version: {esc(VERSION)}</h3>'
                '<p>This page is for the <b>standalone phone app</b>. The PC app\'s update '
                'flow is separate, on the <a href="/updates">Updates</a> page, and is unchanged.</p>'
                '<p><b>Installing a new APK never touches your data:</b> the new APK keeps '
                'the app\'s internal storage, and labh.db is never modified by an update. '
                'Android itself requires you to tap <b>Install</b> and, the first time, to '
                'allow “install unknown apps” for LABH — the app cannot silently replace itself.</p>'
                '<p>Set the folder and the manifest URL in <a href="/settings">Settings</a> '
                '(owner/admin only).</p></div>')
        folder_html = (f'<div class="card"><h3>Source A — APK update folder</h3>'
                       f'<p>Folder: <b>{esc(folder)}</b> ({esc(folder_src)}).</p>')
        if not os.path.isdir(folder):
            folder_html += ('<div class="err">This folder does not exist on this device yet. '
                            'Create it (or set a different folder in Settings) and put the '
                            'new APK file in it.</div>')
        elif not folder_cands:
            folder_html += ('<p>No files named <b>labh-&lt;version&gt;.apk</b> were found in this folder.</p>')
        else:
            rows = "".join(
                f'<li><b>{esc(c["filename"])}</b> (version {esc(c["version"])}, {esc(c["date"])})'
                + (' — <b>newer than installed</b>' if compare_versions(c["version"], VERSION) > 0 else '')
                + '</li>' for c in folder_cands)
            folder_html += f'<ul>{rows}</ul>'
        if folder_newest:
            cl = "".join(f"<li>{esc(d)}</li>" for d in folder_newest["changelog"]) or "<li>No changelog listed.</li>"
            folder_html += (f'<h4>Newer APK found: {esc(folder_newest["version"])}</h4>'
                            f'<p>File: <b>{esc(folder_newest["filename"])}</b>, dated {esc(folder_newest["date"])}.</p>'
                            '<h4>What changed</h4><ul>' + cl + '</ul>'
                            f'<p><a class="btn" href="/updates-apk/file/{esc(folder_newest["filename"])}">Download / Install APK</a></p>'
                            '<p>On Android this downloads via DownloadManager; tap the completed '
                            'download, then Install. Your data is not touched.</p>')
        folder_html += '</div>'
        if not manifest_url:
            manifest_html = ('<div class="card"><h3>Source B — APK manifest URL (optional)</h3>'
                             '<p>No APK manifest URL is set. This source is optional: leave it blank '
                             'and use the folder source above. To use it, set <b>APK manifest URL</b> in '
                             'Settings to a JSON file with <b>version</b>, <b>release_date</b>, '
                             '<b>changelog</b> and <b>apk_url</b> — see '
                             '<b>updates/apk-manifest-sample.json</b> in the app folder. '
                             'Note: a private GitHub repo artifact URL needs authentication and '
                             'cannot be used as apk_url — use a public link or the folder source.</p></div>')
        elif merr:
            manifest_html = (f'<div class="card"><h3>Source B — APK manifest URL</h3>'
                             f'<p>URL: <b>{esc(manifest_url)}</b></p>'
                             f'<div class="err">Could not check for updates: {esc(merr)}. '
                             'Your current version keeps working — nothing was downloaded or changed.</div></div>')
        elif manifest:
            dl = ""
            if is_admin:
                dl = (form_start("/updates-apk/download", user["_csrf"])
                      + '<button type="submit">Download APK</button></form>'
                      '<p>Download only saves the APK in <b>updates/</b> and checks it — '
                      'nothing is installed until you tap Install on the next page.</p>')
            else:
                dl = '<p>Only owner/admin can download APK updates.</p>'
            cl = "".join(f"<li>{esc(d)}</li>" for d in manifest["details"]) or "<li>No details listed.</li>"
            manifest_html = (f'<div class="card"><h3>Source B — new APK available: {esc(manifest["version"])}</h3>'
                             f'<p>Released: {esc(manifest["release_date"]) or "date not given"}. '
                             f'Current version: {esc(VERSION)}.</p>'
                             '<h4>What is new</h4><ul>' + cl + '</ul>' + dl + '</div>')
        else:
            manifest_html = (f'<div class="card"><h3>Source B — APK manifest URL</h3>'
                             f'<p>URL: <b>{esc(manifest_url)}</b></p>'
                             '<p>The manifest version is not newer than the installed version.</p></div>')
        meta = read_apk_meta()
        dl_meta = ""
        if meta.get("filename") and os.path.isfile(os.path.join(
                UPDATES_DIR, os.path.basename(meta["filename"]))):
            dl_meta = (f'<div class="card"><h3>Downloaded APK ready</h3>'
                       f'<p><b>{esc(meta["filename"])}</b> (version {esc(meta.get("version", ""))}, '
                       f'downloaded {esc(meta.get("downloaded_at", ""))}).</p>'
                       + (f'<p><a class="btn" href="/updates-apk/file/{esc(meta["filename"])}">Download / Install APK</a></p>' if is_admin else '')
                       + '<p>Installing a new APK keeps your data — labh.db is never touched.</p></div>')
        release_html = (f'<div class="card"><h3>Source C — GitHub Releases (default)</h3>'
                        f'<p>Repo: <b>{esc(github_release_repo())}</b> '
                        '(change it in <a href="/settings">Settings</a>). New APKs are '
                        'published as GitHub Releases there; the dashboard checks once '
                        'a day, or check right now:</p>'
                        + form_start("/updates-apk/check", user["_csrf"])
                        + '<button type="submit">Check for updates</button></form>'
                        '<p>Checking never installs anything — if a newer release is '
                        'found you get a Download &amp; Install link, and Android\'s '
                        'installer still asks you to confirm.</p></div>')
        if folder_newest is None and manifest is None:
            status = ('<div class="card ok"><h3>No update available</h3>'
                      '<p>No newer APK was found in the update folder or from the manifest URL. '
                      'GitHub Releases are checked on the dashboard (once a day) and with '
                      'the Check button below.</p></div>')
        else:
            status = ('<div class="card ok"><h3>Update available</h3>'
                      '<p>A newer APK is listed above. Nothing is installed until you tap '
                      'Download / Install and confirm in Android\'s installer.</p></div>')
        return self.send_html(200, page(user, biz, "APK Updates", head + status + folder_html + manifest_html + release_html + dl_meta))

    # -- dashboard / settings
    def view_dashboard(self, user, biz, cur):
        sync_action, sync_msg = sync_check()
        if sync_action in ("restored", "conflict"):
            row = q("SELECT user_id FROM sessions WHERE token_hash=?", (user["_token_hash"],), one=True)
            if not row:
                return self.redirect("/login")
        sync_card = ""
        if sync_msg:
            cls = "err" if sync_action == "error" else "ok"
            sync_card = f'<div class="card {cls}">Sync: {esc(sync_msg)}</div>'
        b = user["business_id"]
        t = today()
        qs = parse_qs(urlparse(self.path).query)
        period = qs.get("period", ["month"])[0]
        tab = qs.get("tab", ["overview"])[0]
        frm = qs.get("from", [""])[0]; to = qs.get("to", [""])[0]
        td = date.today()
        if period == "today":
            start, end = td, td; pstart, pend = td - timedelta(days=1), td - timedelta(days=1)
        elif period == "yesterday":
            start, end = td - timedelta(days=1), td - timedelta(days=1); pstart, pend = td - timedelta(days=2), td - timedelta(days=2)
        elif period == "7d":
            start, end = td - timedelta(days=6), td; pstart, pend = td - timedelta(days=13), td - timedelta(days=7)
        elif period == "custom" and frm and to:
            start, end = date.fromisoformat(frm), date.fromisoformat(to); span = (end - start).days + 1
            pstart, pend = start - timedelta(days=span), start - timedelta(days=1)
        elif period == "all":
            start, end = date(2000, 1, 1), td; pstart, pend = date(2000, 1, 1), date(2000, 1, 1)
        else:
            period = "month"; start, end = td.replace(day=1), td; pstart, pend = (td.replace(day=1) - timedelta(days=1)).replace(day=1), td.replace(day=1) - timedelta(days=1)
        si, ei = start.isoformat(), end.isoformat(); psi, pei = pstart.isoformat(), pend.isoformat()
        def _sum(tbl, dcol, val, a, z):
            return q(f"SELECT COALESCE(SUM({val}),0), COUNT(*) FROM {tbl} WHERE business_id=? AND {dcol} BETWEEN ? AND ?", (b, a, z), one=True)
        sales, sales_n = _sum("sales", "sale_date", "total", si, ei)
        psales, psales_n = _sum("sales", "sale_date", "total", psi, pei)
        exps, _ = _sum("expenses", "expense_date", "amount", si, ei)
        pexps, _ = _sum("expenses", "expense_date", "amount", psi, pei)
        cogs = 0.0
        for si_row in q("SELECT si.qty, COALESCE(si.cost_price, p.purchase_price, 0) AS cp FROM sale_items si JOIN sales s ON s.id=si.sale_id LEFT JOIN products p ON p.id=si.product_id WHERE s.business_id=? AND s.sale_date BETWEEN ? AND ?", (b, si, ei)):
            cogs += si_row["qty"] * (si_row["cp"] or 0)
        profit = sales - cogs - exps
        pprofit = psales - pexps
        def _pct(cur_v, prev_v):
            if prev_v == 0:
                return "new" if cur_v > 0 else "0.0%"
            d = (cur_v - prev_v) / abs(prev_v) * 100
            return f"{'+' if d >= 0 else ''}{d:.1f}%"
        tot_sales, tot_orders = q("SELECT COALESCE(SUM(total),0), COUNT(*) FROM sales WHERE business_id=?", (b,), one=True)
        tot_exp = q("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE business_id=?", (b,), one=True)[0]
        tot_cust = q("SELECT COUNT(*) FROM customers WHERE business_id=?", (b,), one=True)[0]
        total_dues = q("SELECT COALESCE(SUM(due),0) FROM sales WHERE business_id=?", (b,), one=True)[0]
        low = q("SELECT name, stock, low_stock FROM products WHERE business_id=? AND stock<=low_stock ORDER BY stock", (b,))
        low_html = "".join(f"<tr><td>{esc(r['name'])}</td><td>{r['stock']}</td><td>{r['low_stock']}</td></tr>" for r in low)
        today_sales, today_n = _sum("sales", "sale_date", "total", t, t)
        today_exp, _ = _sum("expenses", "expense_date", "amount", t, t)
        # daily series for the chart (sales green / expenses pink / profit blue)
        ndays = max(1, (end - start).days + 1)
        days = [(start + timedelta(days=i)).isoformat() for i in range(min(ndays, 31))]
        s_by, e_by = {}, {}
        for r in q("SELECT sale_date, COALESCE(SUM(total),0) AS s FROM sales WHERE business_id=? AND sale_date BETWEEN ? AND ? GROUP BY sale_date", (b, days[0], days[-1])):
            s_by[r["sale_date"]] = r["s"]
        for r in q("SELECT expense_date, COALESCE(SUM(amount),0) AS s FROM expenses WHERE business_id=? AND expense_date BETWEEN ? AND ? GROUP BY expense_date", (b, days[0], days[-1])):
            e_by[r["expense_date"]] = r["s"]
        pts_s, pts_e, pts_p, labels = [], [], [], ""
        mx = 1.0
        for d in days:
            sv, ev = s_by.get(d, 0), e_by.get(d, 0)
            mx = max(mx, sv, ev, sv - ev)
        W, H, PAD = 560, 220, 30
        def _xy(i, v):
            x = PAD + (i * (W - 2 * PAD) / max(1, len(days) - 1)) if len(days) > 1 else W / 2
            return x, H - PAD - (v / mx * (H - 2 * PAD))
        for i, d in enumerate(days):
            sv, ev, pv = s_by.get(d, 0), e_by.get(d, 0), s_by.get(d, 0) - e_by.get(d, 0)
            xs, ys = _xy(i, sv); xe, ye = _xy(i, ev); xp, yp = _xy(i, max(pv, 0))
            pts_s.append(f"{xs:.0f},{ys:.0f}"); pts_e.append(f"{xe:.0f},{ye:.0f}"); pts_p.append(f"{xp:.0f},{yp:.0f}")
            if i % max(1, len(days) // 7) == 0:
                labels += f'<text x="{xs:.0f}" y="{H - 8}" font-size="9" text-anchor="middle" fill="#64748b">{d[5:]}</text>'
        dots = ""
        for i, d in enumerate(days):
            for (pts, col) in ((pts_s, "#16a34a"), (pts_e, "#ec4899"), (pts_p, "#2563eb")):
                pass
            xs, ys = _xy(i, s_by.get(d, 0))
            dots += f'<circle cx="{xs:.0f}" cy="{ys:.0f}" r="3" fill="#16a34a"><title>{d}: sales {money(s_by.get(d, 0), cur)}, expenses {money(e_by.get(d, 0), cur)}, profit {money(s_by.get(d, 0) - e_by.get(d, 0), cur)}</title></circle>'
        chart = (f'<svg viewBox="0 0 {W} {H}" width="100%" role="img" aria-label="Sales overview chart">'
                 f'<line x1="{PAD}" y1="{H - PAD}" x2="{W - PAD}" y2="{H - PAD}" stroke="#dce8e0"/>'
                 f'<line x1="{PAD}" y1="{PAD}" x2="{PAD}" y2="{H - PAD}" stroke="#dce8e0"/>'
                 f'<polyline points="{" ".join(pts_s)}" fill="none" stroke="#16a34a" stroke-width="2"/>'
                 f'<polyline points="{" ".join(pts_e)}" fill="none" stroke="#ec4899" stroke-width="2"/>'
                 f'<polyline points="{" ".join(pts_p)}" fill="none" stroke="#2563eb" stroke-width="2"/>{dots}{labels}</svg>'
                 '<p style="font-size:12px;color:#64748b"><span style="color:#16a34a">\u25cf Sales</span> &nbsp; <span style="color:#ec4899">\u25cf Expenses</span> &nbsp; <span style="color:#2563eb">\u25cf Profit</span> — hover a green dot for the day\u2019s figures.</p>')
        health = compute_health(b)
        # health sub-scores (0-100 each) from the same ratios
        d30 = (td - timedelta(days=30)).isoformat()
        cur30 = q("SELECT COALESCE(SUM(total),0) FROM sales WHERE business_id=? AND sale_date>=?", (b, d30), one=True)[0]
        prev30 = q("SELECT COALESCE(SUM(total),0) FROM sales WHERE business_id=? AND sale_date>=? AND sale_date<?", (b, (td - timedelta(days=60)).isoformat(), d30), one=True)[0]
        trend_sc = 100 if prev30 == 0 and cur30 > 0 else (max(0, min(100, 50 + (cur30 - prev30) / prev30 * 50)) if prev30 else 50)
        dues_sc = max(0, 100 - (total_dues / tot_sales * 100 if tot_sales else 0))
        nprod = q("SELECT COUNT(*) FROM products WHERE business_id=?", (b,), one=True)[0]
        stock_sc = max(0, 100 - (len(low) / nprod * 100 if nprod else 0))
        exp_sc = max(0, 100 - (tot_exp / tot_sales * 100 if tot_sales else 0)) if tot_sales else 100
        ring = health["score"] / 100 * 220
        health_html = (f'<div style="display:flex;gap:16px;align-items:center"><svg width="110" height="110" viewBox="0 0 110 110"><circle cx="55" cy="55" r="35" fill="none" stroke="#e2e8f0" stroke-width="10"/><circle cx="55" cy="55" r="35" fill="none" stroke="#16a34a" stroke-width="10" stroke-dasharray="{ring:.0f} 220" stroke-linecap="round" transform="rotate(-90 55 55)"/><text x="55" y="52" text-anchor="middle" font-size="18" font-weight="800">{health["score"]}</text><text x="55" y="68" text-anchor="middle" font-size="9" fill="#64748b">/ 100</text></svg>'
                       f'<div><p><b>{esc(health["status"])}</b></p>'
                       f'<p style="font-size:12px">Sales trend: {trend_sc:.0f} &nbsp; Dues: {dues_sc:.0f} &nbsp; Stock: {stock_sc:.0f} &nbsp; Expenses: {exp_sc:.0f}</p></div></div>'
                       f'<div style="background:#f0fdf4;border-radius:12px;padding:12px;margin-top:10px;font-size:13px">{esc(health["recs"][0]) if health["recs"] else "All clear."}</div>')
        recent = q("""SELECT s.*, c.name AS cname, COALESCE(GROUP_CONCAT(si.product_name, ', '), '') AS prods
                      FROM sales s LEFT JOIN customers c ON c.id=s.customer_id
                      LEFT JOIN sale_items si ON si.sale_id=s.id
                      WHERE s.business_id=? GROUP BY s.id ORDER BY s.id DESC LIMIT 8""", (b,))
        def _pay_status(r):
            if (r["due"] or 0) <= 0:
                return "Paid"
            return "Partial" if (r["paid"] or 0) > 0 else "Due"
        rh = "".join(f"<tr><td>{i + 1}</td><td><a href='/sales/{r['id']}'>{esc(r['invoice_no'])}</a></td><td>{esc(r['cname'] or 'Walk-in')}</td><td>{esc(r['prods'] or '—')}</td><td>{money(r['total'], cur)}</td><td>{_pay_status(r)}</td><td>{esc(r['sale_date'])}</td><td><a href='/sales/{r['id']}'>View</a></td></tr>" for i, r in enumerate(recent))
        greet = "Good morning" if datetime.now().hour < 12 else "Good afternoon" if datetime.now().hour < 18 else "Good evening"
        period_sel = "".join(f'<option value="{v}"{" selected" if v == period else ""}>{l}</option>' for v, l in [("today", "Today"), ("yesterday", "Yesterday"), ("7d", "Last 7 days"), ("month", "Month"), ("all", "All Time"), ("custom", "Custom")])
        tabs_html = "".join(f'<a class="btn{" secondary" if tab != t2 else ""}" href="/dashboard?period={period}&tab={t2}">{l}</a> ' for t2, l in [("overview", "Overview"), ("due", "Due"), ("analytics", "Analytics")])
        def _tile(lbl, val, sub, ic, bg):
            return (f'<div class="tile"><div><div class="lbl">{lbl}</div><div class="val">{val}</div><div style="font-size:11px;font-weight:700;color:#16A34A">{sub}</div></div>'
                    f'<div><div class="chip" style="background:{bg}">{icon(ic, 20)}</div><div style="text-align:right;color:#94a3b8">&#8942;</div></div></div>')
        # --- v2.3 home page: unified layout rebuilt from the user's
        # reference phone screenshot (banner, 5 stat cards, trend +
        # overview grid, top selling, top due, stock summary, expense
        # donut, recent transactions, dark bottom nav in the shell).
        # Every figure is this business's own SQLite data; empty states
        # say so honestly and never invent rows. Period/tab query
        # params from earlier versions still work (period drives the
        # Period Summary line; tab=due / tab=analytics add their card).
        disp = display_name(user, biz)
        _gh = datetime.now().hour
        greet2 = "Good Morning" if _gh < 12 else "Good Afternoon" if _gh < 18 else "Good Evening"
        greet_name = f", {esc(disp)}" if disp else ""
        period_label = {"today": "Today", "yesterday": "Yesterday", "7d": "Last 7 days",
                        "month": "This Month", "all": "All Time", "custom": "Custom"}.get(period, "This Month")

        def _day_stats(dstr):
            s_, n_ = _sum("sales", "sale_date", "total", dstr, dstr)
            cg_ = 0.0
            for _r in q("SELECT si.qty, COALESCE(si.cost_price, p.purchase_price, 0) AS cp FROM sale_items si JOIN sales s ON s.id=si.sale_id LEFT JOIN products p ON p.id=si.product_id WHERE s.business_id=? AND s.sale_date=?", (b, dstr)):
                cg_ += _r["qty"] * (_r["cp"] or 0)
            e_, _ = _sum("expenses", "expense_date", "amount", dstr, dstr)
            dn_ = q("SELECT COALESCE(SUM(due),0) FROM sales WHERE business_id=? AND sale_date=?", (b, dstr), one=True)[0]
            return {"sales": s_, "orders": n_, "cogs": cg_, "exp": e_,
                    "profit": s_ - cg_ - e_, "cost": cg_ + e_, "due": dn_}

        def _vs23(cur_v, prev_v):
            if prev_v == 0:
                return ("new", "up") if cur_v > 0 else ("0.0%", "flat")
            _d = (cur_v - prev_v) / abs(prev_v) * 100
            return (f"{'+' if _d >= 0 else ''}{_d:.1f}%", "up" if _d >= 0 else "down")

        tdy = _day_stats(t)
        ydy = _day_stats((td - timedelta(days=1)).isoformat())
        # 7-day sparklines (grouped queries, one pass per metric).
        _sdays = [(td - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]

        def _by_date(sql):
            return {r["d"]: r["v"] for r in q(sql, (b, _sdays[0], _sdays[-1]))}

        _s_d = _by_date("SELECT sale_date AS d, COALESCE(SUM(total),0) AS v FROM sales WHERE business_id=? AND sale_date BETWEEN ? AND ? GROUP BY sale_date")
        _o_d = _by_date("SELECT sale_date AS d, COUNT(*) AS v FROM sales WHERE business_id=? AND sale_date BETWEEN ? AND ? GROUP BY sale_date")
        _c_d = _by_date("SELECT s.sale_date AS d, COALESCE(SUM(si.qty*COALESCE(si.cost_price,p.purchase_price,0)),0) AS v FROM sale_items si JOIN sales s ON s.id=si.sale_id LEFT JOIN products p ON p.id=si.product_id WHERE s.business_id=? AND s.sale_date BETWEEN ? AND ? GROUP BY s.sale_date")
        _e_d = _by_date("SELECT expense_date AS d, COALESCE(SUM(amount),0) AS v FROM expenses WHERE business_id=? AND expense_date BETWEEN ? AND ? GROUP BY expense_date")
        _du_d = _by_date("SELECT sale_date AS d, COALESCE(SUM(due),0) AS v FROM sales WHERE business_id=? AND sale_date BETWEEN ? AND ? GROUP BY sale_date")
        _sp_sales = [_s_d.get(d, 0) for d in _sdays]
        _sp_cost = [_c_d.get(d, 0) + _e_d.get(d, 0) for d in _sdays]
        _sp_profit = [_s_d.get(d, 0) - _c_d.get(d, 0) - _e_d.get(d, 0) for d in _sdays]
        _sp_due = [_du_d.get(d, 0) for d in _sdays]
        _sp_orders = [_o_d.get(d, 0) for d in _sdays]

        def _spark(vals, color):
            _mx = max(vals) if vals and max(vals) > 0 else 1
            _bars = ""
            for _v in vals:
                _h = max(2, int(_v / _mx * 100)) if _v > 0 else 2
                _bars += f'<span style="height:{_h}%;background:{color}"></span>'
            return f'<div class="v23-spark">{_bars}</div>'

        def _stat(lbl, val, cur_v, prev_v, ic, chip_bg, spark_vals, spark_color):
            _txt, _cls = _vs23(cur_v, prev_v)
            if _txt == "new":
                _disp, _cls = "↗ 100%", "up"
            elif _cls == "up":
                _disp = "↗ " + _txt.lstrip("+")
            elif _cls == "down":
                _disp = "↘ " + _txt.lstrip("-")
            else:
                _disp = _txt
            return (f'<div class="v23-stat"><div class="chip" style="background:{chip_bg}">{icon(ic, 20)}</div>'
                    f'<div class="lbl">{lbl}</div><div class="val">{val}</div>'
                    f'<div class="vs {_cls}">{_disp}</div>{_spark(spark_vals, spark_color)}</div>')

        stats_html = ('<div class="kpi-row v23-stats">'
                      + _stat("Total Sales", money(tdy["sales"], cur), tdy["sales"], ydy["sales"], "chart", "#ECFDF5", _sp_sales, "#16a34a")
                      + _stat("Total Profit", money(tdy["profit"], cur), tdy["profit"], ydy["profit"], "money", "#EFF6FF", _sp_profit, "#2563eb")
                      + _stat("Total Due", money(tdy["due"], cur), tdy["due"], ydy["due"], "file", "#FFFBEB", _sp_due, "#dc2626")
                      + _stat("Total Orders", str(tdy["orders"]), tdy["orders"], ydy["orders"], "receipt", "#F5F3FF", _sp_orders, "#9333ea")
                      + '</div>')
        banner = (f'<div class="m-banner v23-banner"><div><div class="greet">{greet2}{greet_name}! \U0001f44b</div>'
                  f'<span class="v23-datepill">{td.strftime("%a, %d %b %Y")}</span> '
                  f'<span class="v23-datepill">{esc(period_label)}</span> '
                  f'<form method="get" action="/dashboard" style="display:inline"><select name="period" onchange="this.form.submit()">{period_sel}</select></form></div></div>')
        # --- trend chart: Daily / Weekly / Monthly toggle (Sales green,
        # Profit blue, axes + date labels + hover values on both series).
        trend = qs.get("trend", ["daily"])[0]
        if trend not in ("daily", "weekly", "monthly"):
            trend = "daily"
        if trend == "daily":
            _rstart = td - timedelta(days=13)
        elif trend == "weekly":
            _rstart = td - timedelta(days=83)
        else:
            _rstart = td.replace(day=1)
            for _ in range(11):
                _rstart = (_rstart - timedelta(days=1)).replace(day=1)
        _ts = {r["d"]: r["v"] for r in q("SELECT sale_date AS d, COALESCE(SUM(total),0) AS v FROM sales WHERE business_id=? AND sale_date BETWEEN ? AND ? GROUP BY sale_date", (b, _rstart.isoformat(), t))}
        _tc = {r["d"]: r["v"] for r in q("SELECT s.sale_date AS d, COALESCE(SUM(si.qty*COALESCE(si.cost_price,p.purchase_price,0)),0) AS v FROM sale_items si JOIN sales s ON s.id=si.sale_id LEFT JOIN products p ON p.id=si.product_id WHERE s.business_id=? AND s.sale_date BETWEEN ? AND ? GROUP BY s.sale_date", (b, _rstart.isoformat(), t))}
        _te = {r["d"]: r["v"] for r in q("SELECT expense_date AS d, COALESCE(SUM(amount),0) AS v FROM expenses WHERE business_id=? AND expense_date BETWEEN ? AND ? GROUP BY expense_date", (b, _rstart.isoformat(), t))}
        _buckets, _order = {}, []
        _cur = _rstart
        while _cur <= td:
            if trend == "daily":
                _k, _lbl = _cur.isoformat(), _cur.strftime("%d %b")
            elif trend == "weekly":
                _mon = _cur - timedelta(days=_cur.weekday())
                _k, _lbl = _mon.isoformat(), _mon.strftime("%d %b")
            else:
                _k, _lbl = _cur.strftime("%Y-%m"), _cur.strftime("%b %y")
            if _k not in _buckets:
                _buckets[_k] = [0.0, 0.0, _lbl]
                _order.append(_k)
            _cur += timedelta(days=1)
        for _d, _sv in _ts.items():
            try:
                _dd = date.fromisoformat(_d)
            except ValueError:
                continue
            if trend == "daily":
                _k = _d
            elif trend == "weekly":
                _k = (_dd - timedelta(days=_dd.weekday())).isoformat()
            else:
                _k = _d[:7]
            if _k in _buckets:
                _buckets[_k][0] += _sv
        for _d, _cv in _tc.items():
            try:
                _dd = date.fromisoformat(_d)
            except ValueError:
                continue
            _k = _d if trend == "daily" else ((_dd - timedelta(days=_dd.weekday())).isoformat() if trend == "weekly" else _d[:7])
            if _k in _buckets:
                _buckets[_k][1] += _cv
        for _d, _ev in _te.items():
            try:
                _dd = date.fromisoformat(_d)
            except ValueError:
                continue
            _k = _d if trend == "daily" else ((_dd - timedelta(days=_dd.weekday())).isoformat() if trend == "weekly" else _d[:7])
            if _k in _buckets:
                _buckets[_k][1] += _ev
        _t_sales, _t_profit, _t_labels = [], [], []
        for _k in _order:
            _sv, _cg, _lbl = _buckets[_k]
            _t_sales.append(_sv)
            _t_profit.append(_sv - _cg)
            _t_labels.append(_lbl)
        _W, _H, _PAD = 560, 220, 30
        _hi = max([1.0] + _t_sales + _t_profit)
        _lo = min([0.0] + _t_profit)
        _span = (_hi - _lo) or 1.0

        def _txy(i, v):
            _x = _PAD + (i * (_W - 2 * _PAD) / max(1, len(_order) - 1)) if len(_order) > 1 else _W / 2
            return _x, _H - _PAD - ((v - _lo) / _span * (_H - 2 * _PAD))

        _pts_s, _pts_p, _tlabels, _tdots = [], [], "", ""
        for _i, _k in enumerate(_order):
            _xs, _ys = _txy(_i, _t_sales[_i])
            _xp, _yp = _txy(_i, _t_profit[_i])
            _pts_s.append(f"{_xs:.0f},{_ys:.0f}")
            _pts_p.append(f"{_xp:.0f},{_yp:.0f}")
            if _i % max(1, len(_order) // 7) == 0:
                _tlabels += f'<text x="{_xs:.0f}" y="{_H - 8}" font-size="9" text-anchor="middle" fill="#64748b">{_t_labels[_i]}</text>'
            _tdots += (f'<circle cx="{_xs:.0f}" cy="{_ys:.0f}" r="3" fill="#16a34a"><title>{_t_labels[_i]}: sales {money(_t_sales[_i], cur)}</title></circle>'
                         f'<circle cx="{_xp:.0f}" cy="{_yp:.0f}" r="3" fill="#2563eb"><title>{_t_labels[_i]}: profit {money(_t_profit[_i], cur)}</title></circle>')
        _zero_y = _H - _PAD - ((0 - _lo) / _span * (_H - 2 * _PAD))
        trend_chart = (f'<svg viewBox="0 0 {_W} {_H}" width="100%" role="img" aria-label="Sales Overview chart">'
                       f'<line x1="{_PAD}" y1="{_H - _PAD}" x2="{_W - _PAD}" y2="{_H - _PAD}" stroke="#dce8e0"/>'
                       f'<line x1="{_PAD}" y1="{_PAD}" x2="{_PAD}" y2="{_H - _PAD}" stroke="#dce8e0"/>'
                       + (f'<line x1="{_PAD}" y1="{_zero_y:.0f}" x2="{_W - _PAD}" y2="{_zero_y:.0f}" stroke="#e2e8f0" stroke-dasharray="4 4"/>' if _lo < 0 else '')
                       + f'<polyline points="{" ".join(_pts_s)}" fill="none" stroke="#16a34a" stroke-width="2"/>'
                       + f'<polyline points="{" ".join(_pts_p)}" fill="none" stroke="#2563eb" stroke-width="2"/>{_tdots}{_tlabels}</svg>'
                       + '<p style="font-size:12px;color:#64748b"><span style="color:#16a34a">\u25cf Sales</span> &nbsp; <span style="color:#2563eb">\u25cf Profit</span> — hover a dot for the value.</p>')
        _toggle = "".join(f'<a class="{"on" if trend == _tv else ""}" href="/dashboard?period={period}&trend={_tv}">{_tl}</a> ' for _tv, _tl in [("daily", "Daily"), ("weekly", "Weekly"), ("monthly", "Monthly")])
        trend_card = (f'<div class="card"><h3>Sales &amp; Profit Trend</h3><div class="v23-toggle">{_toggle}</div>'
                      + ("<p>No sales yet.</p>" if sum(_t_sales) == 0 else "") + trend_chart + '</div>')
        # --- Business Overview: scope dropdown (This Month default).
        def _scope(kind):
            if kind == "last-month":
                _last = td.replace(day=1) - timedelta(days=1)
                _first = _last.replace(day=1)
                _plast = _first - timedelta(days=1)
                _pfirst = _plast.replace(day=1)
                return _first, _last, _pfirst, _plast
            if kind == "all-time":
                return date(2000, 1, 1), td, None, None
            _first = td.replace(day=1)
            _plast = _first - timedelta(days=1)
            return _first, td, _plast.replace(day=1), _plast

        def _scope_sel(name, cur_kind):
            _opts = "".join(f'<option value="{_v}"{" selected" if _v == cur_kind else ""}>{_l}</option>' for _v, _l in [("this-month", "This Month"), ("last-month", "Last Month"), ("all-time", "All Time")])
            return f'<form method="get" action="/dashboard" style="display:inline"><select name="{name}" onchange="this.form.submit()">{_opts}</select></form>'

        ov_kind = qs.get("ov", ["this-month"])[0]
        if ov_kind not in ("this-month", "last-month", "all-time"):
            ov_kind = "this-month"
        _of, _ol, _opf, _opl = _scope(ov_kind)
        _osi, _oei = _of.isoformat(), _ol.isoformat()
        _ov_purch, _ = _sum("purchases", "purchase_date", "total", _osi, _oei)
        _ov_exp, _ = _sum("expenses", "expense_date", "amount", _osi, _oei)
        if _opf is not None:
            _ov_purch_p, _ = _sum("purchases", "purchase_date", "total", _opf.isoformat(), _opl.isoformat())
            _ov_exp_p, _ = _sum("expenses", "expense_date", "amount", _opf.isoformat(), _opl.isoformat())
            _ov_nc = q("SELECT COUNT(*) FROM customers WHERE business_id=? AND substr(created_at,1,10) BETWEEN ? AND ?", (b, _osi, _oei), one=True)[0]
            _ov_nc_p = q("SELECT COUNT(*) FROM customers WHERE business_id=? AND substr(created_at,1,10) BETWEEN ? AND ?", (b, _opf.isoformat(), _opl.isoformat()), one=True)[0]
            _ov_np = q("SELECT COUNT(*) FROM products WHERE business_id=? AND substr(created_at,1,10) BETWEEN ? AND ?", (b, _osi, _oei), one=True)[0]
            _ov_np_p = q("SELECT COUNT(*) FROM products WHERE business_id=? AND substr(created_at,1,10) BETWEEN ? AND ?", (b, _opf.isoformat(), _opl.isoformat()), one=True)[0]
            _vs_txt = lambda cv, pv: _vs23(cv, pv)[0] + " vs previous"
        else:
            _ov_purch_p, _ov_exp_p, _ov_nc, _ov_nc_p, _ov_np, _ov_np_p = 0, 0, 0, 0, 0, 0
            _vs_txt = lambda cv, pv: "all time"
        _nprod_total = q("SELECT COUNT(*) FROM products WHERE business_id=?", (b,), one=True)[0]

        def _ovrow(lbl, val, vs_txt, ic, bg):
            return (f'<div class="v23-ovrow"><div class="chip" style="background:{bg}">{icon(ic, 20)}</div>'
                    f'<div><div style="font-size:12px;color:#64748b">{lbl}</div><div style="font-weight:800">{val}</div></div>'
                    f'<div class="vs">{vs_txt}</div></div>')

        _ring = health["score"] / 100 * 150
        # v2.15: reference overview is ONLY the four stats with their
        # % changes — no Period Summary line, no Business Health ring
        # (health lives on its own screen). Both are removed here.
        overview_card = (f'<div class="card"><h3>Business Overview</h3><p>{_scope_sel("ov", ov_kind)}</p>'
                         + _ovrow("Total Customers", str(tot_cust), _vs_txt(_ov_nc, _ov_nc_p), "users", "#EFF6FF")
                         + _ovrow("Total Products", str(_nprod_total), _vs_txt(_ov_np, _ov_np_p), "package", "#FFFBEB")
                         + _ovrow("Total Purchase", money(_ov_purch, cur), _vs_txt(_ov_purch, _ov_purch_p), "truck", "#ECFDF5")
                         + _ovrow("Total Expenses", money(_ov_exp, cur), _vs_txt(_ov_exp, _ov_exp_p), "wallet", "#FFF1F2")
                         + '</div>')
        # --- Top Selling Products (This Month default, top 5).
        top_kind = qs.get("top", ["this-month"])[0]
        if top_kind not in ("this-month", "last-month", "all-time"):
            top_kind = "this-month"
        _tf, _tl, _tpf, _tpl = _scope(top_kind)
        top_prods = q("""SELECT si.product_name AS nm, COALESCE(SUM(si.qty),0) AS qq, COALESCE(SUM(si.amount),0) AS aa,
                                COALESCE(SUM(si.qty*COALESCE(si.cost_price,p.purchase_price,0)),0) AS cc
                         FROM sale_items si JOIN sales s ON s.id=si.sale_id LEFT JOIN products p ON p.id=si.product_id
                         WHERE s.business_id=? AND s.sale_date BETWEEN ? AND ?
                         GROUP BY si.product_name ORDER BY qq DESC LIMIT 5""", (b, _tf.isoformat(), _tl.isoformat()))
        _top_rows = "".join(f"<tr><td>{i + 1}</td><td>{esc(r['nm'] or 'Product')}</td><td>{r['qq']:g}</td><td>{money(r['aa'], cur)}</td><td>{money(r['aa'] - r['cc'], cur)}</td></tr>" for i, r in enumerate(top_prods))
        top_card = ('<div class="card"><h3>Top Selling Products</h3><p>' + _scope_sel("top", top_kind) + '</p>'
                    + table(["#", "Product", "Qty", "Sales", "Profit"], _top_rows)
                    + ("<p>No product sales yet.</p>" if not top_prods else "")
                    + '<p><a href="/products">View All &rarr;</a></p></div>')
        # --- Top Due List (per-customer totals, same basis as /customers/dues).
        _due_rows = []
        for _cst in q("SELECT id, name FROM customers WHERE business_id=? ORDER BY name", (b,)):
            _dd = customer_due(b, _cst["id"])
            if _dd > 0:
                _due_rows.append((_cst["name"], _dd))
        _due_rows.sort(key=lambda _x: -_x[1])
        _total_cust_due = sum(_d for _, _d in _due_rows)
        _due_tr = "".join(f"<tr><td>{i + 1}</td><td>{esc(_nm)}</td><td>{money(_d, cur)}</td></tr>" for i, (_nm, _d) in enumerate(_due_rows[:5]))
        due_card = ('<div class="card"><h3>Top Due List</h3>'
                    + table(["#", "Customer", "Due Amount"], _due_tr)
                    + ("<p>No customer dues.</p>" if not _due_rows else "")
                    + f'<p><b>Total Customer Due: {money(_total_cust_due, cur)}</b></p>'
                    + '<p><a href="/customers/dues">View All &rarr;</a></p></div>')
        # --- Stock Summary. Over Stock = stock over twice the product's
        # own low-stock threshold (labelled in the tile's title).
        _prods = q("SELECT stock, low_stock, purchase_price FROM products WHERE business_id=?", (b,))
        _st_n = len(_prods)
        _st_qty = sum(_p["stock"] or 0 for _p in _prods)
        _st_val = sum((_p["stock"] or 0) * (_p["purchase_price"] or 0) for _p in _prods)
        _st_low = sum(1 for _p in _prods if (_p["stock"] or 0) <= (_p["low_stock"] or 0))
        _st_out = sum(1 for _p in _prods if (_p["stock"] or 0) <= 0)
        _st_over = sum(1 for _p in _prods if (_p["stock"] or 0) > 2 * (_p["low_stock"] or 0))

        def _stile(lbl, val):
            return f'<div class="v23-tile"><div class="lbl">{lbl}</div><div class="val">{val}</div></div>'

        stock_card = ('<div class="card"><h3>Stock Summary</h3><div class="v23-tiles">'
                      + _stile("Total Products", str(_st_n))
                      + _stile("Total Stock Qty", f"{_st_qty:g}")
                      + _stile("Stock Value", money(_st_val, cur))
                      + _stile("Low Stock", str(_st_low))
                      + _stile("Out of Stock", str(_st_out))
                      + f'<div class="v23-tile" title="Stock over twice the low-stock threshold"><div class="lbl">Over Stock</div><div class="val">{_st_over}</div></div>'
                      + '</div><p><a href="/products">View All &rarr;</a></p></div>')
        # --- Expense Breakdown donut (This Month default).
        exp_kind = qs.get("exp", ["this-month"])[0]
        if exp_kind not in ("this-month", "last-month", "all-time"):
            exp_kind = "this-month"
        _ef, _el, _epf, _epl = _scope(exp_kind)
        _buckets_e = {"Rent": 0.0, "Salary": 0.0, "Utility": 0.0, "Transport": 0.0, "Others": 0.0}
        for _er in q("SELECT COALESCE(category,'') AS cat, COALESCE(SUM(amount),0) AS tot FROM expenses WHERE business_id=? AND expense_date BETWEEN ? AND ? GROUP BY cat", (b, _ef.isoformat(), _el.isoformat())):
            _cl = (_er["cat"] or "").lower()
            if "rent" in _cl:
                _buckets_e["Rent"] += _er["tot"]
            elif "salar" in _cl:
                _buckets_e["Salary"] += _er["tot"]
            elif "util" in _cl:
                _buckets_e["Utility"] += _er["tot"]
            elif "transport" in _cl:
                _buckets_e["Transport"] += _er["tot"]
            else:
                _buckets_e["Others"] += _er["tot"]
        _exp_total = sum(_buckets_e.values())
        _donut_colors = {"Rent": "#16a34a", "Salary": "#2563eb", "Utility": "#f59e0b", "Transport": "#ec4899", "Others": "#94a3b8"}
        if _exp_total > 0:
            _C = 219.9
            _cum, _segs, _legend = 0.0, "", ""
            for _nm, _v in _buckets_e.items():
                _pct_e = _v / _exp_total * 100
                _seg = _v / _exp_total * _C
                if _seg > 0:
                    _segs += f'<circle cx="55" cy="55" r="35" fill="none" stroke="{_donut_colors[_nm]}" stroke-width="18" stroke-dasharray="{_seg:.1f} {_C - _seg:.1f}" stroke-dashoffset="{-_cum:.1f}" transform="rotate(-90 55 55)"/>'
                _cum += _seg
                _pct_txt = f"{_pct_e:.1f}".rstrip("0").rstrip(".") + "%"
                _legend += f'<p style="font-size:13px"><span style="color:{_donut_colors[_nm]}">\u25cf</span> {_nm} {_pct_txt}</p>'
            _donut = (f'<div style="display:flex;gap:16px;align-items:center;flex-wrap:wrap"><svg width="110" height="110" viewBox="0 0 110 110" role="img" aria-label="Expense breakdown donut"><circle cx="55" cy="55" r="35" fill="none" stroke="#e2e8f0" stroke-width="18"/>{_segs}<text x="55" y="52" text-anchor="middle" font-size="11" font-weight="800">{money(_exp_total, cur)}</text><text x="55" y="66" text-anchor="middle" font-size="8" fill="#64748b">Total</text></svg><div>{_legend}</div></div>')
        else:
            _legend = "".join(f'<p style="font-size:13px"><span style="color:{_donut_colors[_nm]}">\u25cf</span> {_nm} 0%</p>' for _nm in _buckets_e)
            _donut = (f'<div style="display:flex;gap:16px;align-items:center;flex-wrap:wrap"><svg width="110" height="110" viewBox="0 0 110 110" role="img" aria-label="Expense breakdown donut"><circle cx="55" cy="55" r="35" fill="none" stroke="#e2e8f0" stroke-width="18"/><text x="55" y="52" text-anchor="middle" font-size="11" font-weight="800">{money(0, cur)}</text><text x="55" y="66" text-anchor="middle" font-size="8" fill="#64748b">Total</text></svg><div>{_legend}</div></div>'
                        '<p>No expenses yet.</p>')
        exp_card = ('<div class="card"><h3>Expense Breakdown</h3><p>' + _scope_sel("exp", exp_kind) + '</p>' + _donut + '</div>')
        # --- Recent Transactions (sales + purchases + payments, newest first).
        _txns = []
        for _r in q("""SELECT s.*, c.name AS cname, COALESCE(GROUP_CONCAT(si.product_name, ', '), '') AS prods
                      FROM sales s LEFT JOIN customers c ON c.id=s.customer_id
                      LEFT JOIN sale_items si ON si.sale_id=s.id
                      WHERE s.business_id=? GROUP BY s.id ORDER BY s.id DESC LIMIT 10""", (b,)):
            _st = "Paid" if (_r["due"] or 0) <= 0 else ("Partial" if (_r["paid"] or 0) > 0 else "Due")
            _txns.append(("Sale", f"Sale {esc(_r['invoice_no'])} · {esc(_r['cname'] or 'Walk-in')}" + (f" · {esc(_r['prods'])}" if _r["prods"] else "") + f" · {_st}", _r["total"], _r["created_at"] or _r["sale_date"]))
        for _r in q("""SELECT pu.*, sup.name AS sname FROM purchases pu LEFT JOIN suppliers sup ON sup.id=pu.supplier_id
                      WHERE pu.business_id=? ORDER BY pu.id DESC LIMIT 10""", (b,)):
            _txns.append(("Purchase", f"Purchase · {esc(_r['sname'] or 'Supplier')}", _r["total"], _r["created_at"] or _r["purchase_date"]))
        for _r in q("""SELECT pay.*, c.name AS cname, sup.name AS sname FROM payments pay
                      LEFT JOIN customers c ON c.id=pay.entity_id AND pay.entity_type='customer'
                      LEFT JOIN suppliers sup ON sup.id=pay.entity_id AND pay.entity_type='supplier'
                      WHERE pay.business_id=? ORDER BY pay.id DESC LIMIT 10""", (b,)):
            _nm = _r["cname"] or _r["sname"] or "Payment"
            _txns.append(("Payment", f"Payment · {esc(_nm)}" + (f" · {esc(_r['method'])}" if _r["method"] else ""), _r["amount"], _r["created_at"] or _r["pay_date"]))
        _txns.sort(key=lambda _x: _x[3] or "", reverse=True)
        _tx_rows = "".join(f"<tr><td>{i + 1}</td><td><span class=\"pill {_tp.lower()}\">{_tp}</span></td><td>{_det}</td><td>{money(_amt, cur)}</td><td>{esc(fmt_dt(_tm))}</td></tr>" for i, (_tp, _det, _amt, _tm) in enumerate(_txns[:10]))
        txn_card = ('<div class="card"><h3>Recent Transactions</h3>'
                    + table(["#", "Type", "Details", "Amount", "Time"], _tx_rows)
                    + ("<p>No transactions yet.</p>" if not _txns else "")
                    + '<p><a href="/sales">View All &rarr;</a></p></div>')
        # Carry-over cards for the old period/tab params (kept working).
        tab_card = ""
        if tab == "due":
            tab_card = (f'<div class="card"><h3>Due overview ({period})</h3><p>Total customer dues (all time): <b>{money(total_dues, cur)}</b></p>'
                        '<p><a class="btn" href="/customers/dues">Customer Due List</a> <a class="btn secondary" href="/suppliers/dues">Supplier Due List</a> '
                        '<a class="btn secondary" href="/reports/customer-dues">Due aging report</a></p></div>')
        elif tab == "analytics":
            tab_card = (f'<div class="card"><h3>Analytics ({period})</h3><p>Sales: {money(sales, cur)} ({sales_n} orders) &middot; Expenses: {money(exps, cur)} &middot; COGS: {money(cogs, cur)} &middot; Profit: <b>{money(profit, cur)}</b></p>'
                        f'<p>Previous period sales: {money(psales, cur)} &middot; Previous profit: {money(pprofit, cur)}</p></div>')
        # v2.15: reference home has no action-button card (those pages
        # are reached from the nav), and shows ৳ for BDT.
        home = (banner + stats_html + tab_card
                + f'<div class="v23-grid2">{trend_card}{overview_card}</div>'
                + top_card + due_card + stock_card + exp_card + txn_card)
        if cur == "BDT":
            home = re.sub(r"BDT (?=\d)", "৳", home)
        body = sync_card + home
        popup = ""
        try:
            # v2.9: the GitHub release leg of the update check runs at
            # most once per day per user (the manual button on APK
            # Updates bypasses this); folder/manifest legs are local
            # and always run.
            due = github_release_check_due(user)
            apk_upd = available_apk_update(timeout=3, include_releases=due)
            if due:
                github_release_check_record(user)
            if apk_upd and not is_apk_update_dismissed(user, apk_upd["version"]):
                popup = apk_update_popup_html(user, apk_upd)
            else:
                manifest = available_update(get_setting("update_manifest_url"), timeout=3)
                if manifest and not is_update_dismissed(user, manifest["version"]):
                    popup = update_popup_html(user, manifest)
        except Exception:
            popup = ""
        return self.send_html(200, page(user, biz, "Dashboard", body + popup, show_h1=False))
    def view_settings(self, user, biz, cur, parts, method):
        b = user["business_id"]
        if len(parts) >= 2 and parts[1] == "permissions":
            self.require_role(user, "owner")
            if method == "POST":
                form = self.form()
                self.check_csrf(user, form)
                m = {}
                for act in ("create_sale", "create_product", "create_purchase", "manage_settings", "manage_users", "view_reports"):
                    m[act] = {"staff": bool(self.fv(form, "perm_staff_" + act))}
                set_setting("perm_matrix", json.dumps(m))
                return self.redirect("/settings/permissions")
            m = perm_matrix()
            acts = [("create_sale", "Create sales / POS"), ("create_product", "Create products"),
                    ("create_purchase", "Create purchases"), ("manage_settings", "Change settings"),
                    ("manage_users", "Manage users"), ("view_reports", "View reports")]
            rows = ""
            for act, lbl in acts:
                chk = " checked" if m.get(act, {}).get("staff") else ""
                rows += f"<tr><td>{lbl}</td><td><input type='checkbox' name='perm_staff_{act}' value='1' style='width:auto'{chk}></td></tr>"
            body = ('<div class="card"><h3>Staff permission matrix</h3><p>Owner and admin always have every permission. '
                    'Tick what staff may do; unticked actions are blocked for staff.</p>'
                    + form_start("/settings/permissions", user["_csrf"])
                    + table(["Permission", "Staff allowed"], rows)
                    + '<button type="submit">Save matrix</button></form></div>'
                    '<p><a href="/settings">Back to Settings</a></p>')
            return self.send_html(200, page(user, biz, "Staff Permissions", body))
        if len(parts) >= 2 and parts[1] == "users":
            self.require_role(user, "owner", "admin")
            if method == "POST":
                form = self.form()
                self.check_csrf(user, form)
                email = self.fv(form, "email").strip().lower()
                role = self.fv(form, "role") if self.fv(form, "role") in ("admin", "staff") else "staff"
                if "@" in email and not q("SELECT id FROM users WHERE email=?", (email,), one=True):
                    # Pending user: no password is set here. They join
                    # this business by registering/logging in with
                    # Firebase using this email (Email/Password or Google).
                    exec_db("INSERT INTO users(business_id,email,pw_hash,firebase_uid,role,created_at) VALUES(?,?,?,?,?,?)",
                            (b, email, "", None, role, now_iso()))
                return self.redirect("/settings/users")
            users = q("SELECT email, role, created_at FROM users WHERE business_id=? ORDER BY id", (b,))
            rows = "".join(f"<tr><td>{esc(u['email'])}</td><td>{esc(u['role'])}</td><td>{esc(u['created_at'])}</td></tr>" for u in users)
            body = ('<div class="card"><h3>Staff / users of this business</h3>'
                    + table(["Email", "Role", "Created"], rows)
                    + '</div><div class="card"><h3>Add user</h3>' + form_start("/settings/users", user["_csrf"])
                    + field("Email", "email", "", "email", True)
                    + '<p><label>Role</label><br><select name="role"><option value="staff">staff (sell/view only)</option><option value="admin">admin</option></select></p>'
                    + '<p><i>No password is set here. The user joins this business by signing up / logging in '
                    'with Firebase (Email/Password or Google) using this email.</i></p>'
                    + '<button type="submit">Add user</button></form></div>'
                    + '<p><a href="/settings">Back to Settings</a></p>')
            return self.send_html(200, page(user, biz, "Users", body))
        if method == "POST":
            self.require_role(user, "owner", "admin")
            form = self.form()
            self.check_csrf(user, form)
            exec_db("UPDATE businesses SET name=?, currency=? WHERE id=?",
                    (self.fv(form, "name").strip() or biz["name"], self.fv(form, "currency").strip() or "BDT", b))
            set_setting("lang", "bn" if self.fv(form, "lang") == "bn" else "en")
            set_setting("tax_enabled", "1" if self.fv(form, "tax_enabled") == "1" else "0")
            try:
                set_setting("tax_rate", str(float(self.fv(form, "tax_rate") or 0)))
            except ValueError:
                set_setting("tax_rate", "0")
            set_setting("tax_mode", "inclusive" if self.fv(form, "tax_mode") == "inclusive" else "exclusive")
            set_setting("update_manifest_url", self.fv(form, "update_url").strip())
            set_setting("apk_update_folder", self.fv(form, "apk_folder").strip())
            set_setting("apk_manifest_url", self.fv(form, "apk_manifest_url").strip())
            set_setting("github_release_repo", self.fv(form, "github_release_repo").strip())
            set_setting("sync_dir", self.fv(form, "sync_dir").strip())
            set_setting("theme_mode", "dark" if self.fv(form, "theme_mode") == "dark" else "light")
            set_setting("accent", self.fv(form, "accent") if self.fv(form, "accent") in ("labh-green", "ocean-blue", "royal-purple", "rose", "orange") else "labh-green")
            set_setting("invoice_template", self.fv(form, "invoice_template") if self.fv(form, "invoice_template") in ("modern", "classic", "minimal", "bold", "receipt") else "modern")
            set_setting("payment_methods", self.fv(form, "payment_methods").strip())
            set_setting("notif_low_stock", "0" if self.fv(form, "notif_low_stock") == "0" else "1")
            set_setting("notif_overdue", "0" if self.fv(form, "notif_overdue") == "0" else "1")
            set_setting("auto_sync", "0" if self.fv(form, "auto_sync") == "0" else "1")
            return self.redirect("/settings")
        menu_items = [("Business Information", "building", "#business"), ("Currency Settings", "money", "#currency"),
                      ("Application Settings", "settings", "#app"), ("Barcode Settings", "scan", "#barcode"),
                      ("Print Settings", "receipt", "#print"), ("User Management", "users", "/settings/users"),
                      ("Backup & Restore", "file", "/backup"), ("About App", "shield", "#about")]
        menu_html = "".join(
            f'<a href="{href}"><span class="tile-ic">{icon(ic, 18)}</span><span class="lbl">{esc(n)}</span><span class="chev">&#8250;</span></a>'
            for n, ic, href in menu_items)
        body = (f'<div class="card"><h3>Settings</h3><div class="bd-menu">{menu_html}</div></div>'
                '<div class="card">' + form_start("/settings", user["_csrf"])
                + '<div id="business"></div>'
                + field("Business name", "name", biz["name"], "text", True)
                + '<div id="currency"></div>'
                + field("Currency (e.g. BDT, USD)", "currency", cur)
                + '<div id="language"></div>'
                + f'<p><label>Language / ভাষা</label><br><select name="lang"><option value="en"{" selected" if get_setting("lang") != "bn" else ""}>English</option><option value="bn"{" selected" if get_setting("lang") == "bn" else ""}>Bangla (বাংলা)</option></select></p>'
                + f'<p><label>Tax &amp; VAT</label><br><select name="tax_enabled"><option value="0"{" selected" if get_setting("tax_enabled") != "1" else ""}>Disabled</option><option value="1"{" selected" if get_setting("tax_enabled") == "1" else ""}>Enabled</option></select></p>'
                + field("Tax / VAT rate (%)", "tax_rate", get_setting("tax_rate") or "0", "number")
                + f'<p><label>Tax mode</label><br><select name="tax_mode"><option value="exclusive"{" selected" if get_setting("tax_mode") != "inclusive" else ""}>Exclusive (added on top)</option><option value="inclusive"{" selected" if get_setting("tax_mode") == "inclusive" else ""}>Inclusive (inside price)</option></select></p>' 
                + field("Update manifest URL (owner/admin only, blank = no update source)",
                        "update_url", get_setting("update_manifest_url"))
                + '<p>Point this at a <b>version.json</b> file (see the Updates page for '
                'its format) to let the app check for updates from inside the app.</p>'
                + field("APK update folder (phone APK updates, owner/admin only — blank = Drive sync folder, else Downloads)",
                        "apk_folder", get_setting("apk_update_folder"))
                + '<p>For the standalone phone app: put a new <b>labh-&lt;version&gt;.apk</b> '
                'file in this folder and the app offers it on the <a href="/updates-apk">APK Updates</a> '
                'page and as a dashboard pop-up. A <b>labh-&lt;version&gt;.txt</b> or '
                '<b>.json</b> file beside it is shown as the changelog.</p>'
                + field("APK manifest URL (optional, owner/admin only, blank = no manifest source)",
                        "apk_manifest_url", get_setting("apk_manifest_url"))
                + '<p>Optional second APK source: a JSON file with <b>version</b>, <b>release_date</b>, '
                '<b>changelog</b> and <b>apk_url</b> (see <b>updates/apk-manifest-sample.json</b>). '
                'A private GitHub artifact URL needs authentication and cannot be used here — '
                'use a public link or the folder source above.</p>'
                + field("GitHub releases repo (APK updates, owner/admin only — blank = ryhantrade/LABH-releases)",
                        "github_release_repo", get_setting("github_release_repo"))
                + '<p>Default APK update source: the app checks this public repo\'s GitHub '
                'Releases for a newer APK (dashboard, once a day, plus the Check button on '
                'APK Updates). Only the APK is published there — no source, no business data.</p>'
                + field("Sync folder (Google Drive sync location, owner/admin only)",
                        "sync_dir", get_setting("sync_dir"))
                + '<p><b>Sync via Google Drive:</b> point this at the folder that Google Drive '
                'syncs on this device (PC: your Drive for Desktop folder; phone: the local folder '
                'a folder-sync app mirrors from Drive, or leave it blank and use the manual '
                'Backup → Drive steps instead). After any finished work, auto-backup also writes '
                'the latest sync file here; on open, the app syncs from here first, before the '
                'dashboard shows data. Latest Drive backup wins — it replaces this device\'s data, '
                'never merges. Last synced: '
                + esc(get_setting("last_sync_ts") or "never")
                + ' · Status: ' + esc(get_setting("last_sync_status") or "—")
                + ' · Error: ' + esc(get_setting("last_sync_error") or "—") + '</p>'
                + f'<div class="card" id="app"><h3>Theme &amp; Appearance</h3>'
                + f'<p><label>Mode</label><br><select name="theme_mode"><option value="light"{" selected" if get_setting("theme_mode") != "dark" else ""}>Light</option><option value="dark"{" selected" if get_setting("theme_mode") == "dark" else ""}>Dark</option></select></p>'
                + f'<p><label>Accent colour</label><br><select name="accent">' + "".join(f'<option value="{v}"{" selected" if (get_setting("accent") or "labh-green") == v else ""}>{l}</option>' for v, l in [("labh-green", "LABH Green"), ("ocean-blue", "Ocean Blue"), ("royal-purple", "Royal Purple"), ("rose", "Rose"), ("orange", "Orange")]) + '</select></p></div>'
                + f'<div class="card" id="print"><h3>Invoice Templates</h3>'
                + f'<p><label>Default invoice template</label><br><select name="invoice_template">' + "".join(f'<option value="{v}"{" selected" if invoice_template_setting() == v else ""}>{l}</option>' for v, l in [("modern", "Modern (green header)"), ("classic", "Classic"), ("minimal", "Minimal"), ("bold", "Bold"), ("receipt", "Receipt")]) + '</select></p>'
                + f'<p><label>Payment methods (comma-separated, used in POS &amp; New Sale)</label><br><input type="text" name="payment_methods" value="{esc(",".join(payment_methods_setting()))}"></p></div>'
                + f'<div class="card" id="notif"><h3>Notification preferences</h3>'
                + f'<p><label>Low-stock alerts</label><br><select name="notif_low_stock"><option value="1"{" selected" if get_setting("notif_low_stock") != "0" else ""}>On</option><option value="0"{" selected" if get_setting("notif_low_stock") == "0" else ""}>Off</option></select></p>'
                + f'<p><label>Overdue-due alerts</label><br><select name="notif_overdue"><option value="1"{" selected" if get_setting("notif_overdue") != "0" else ""}>On</option><option value="0"{" selected" if get_setting("notif_overdue") == "0" else ""}>Off</option></select></p>'
                + f'<p><label>Auto-sync with Google Drive</label><br><select name="auto_sync"><option value="1"{" selected" if get_setting("auto_sync") != "0" else ""}>On — Google sign-in downloads the Drive backup automatically</option><option value="0"{" selected" if get_setting("auto_sync") == "0" else ""}>Off — Google sign-in never downloads automatically</option></select></p></div>'
                + f'<div class="card" id="about"><h3>About App</h3><p>LABH — Smart Business Manager, version {VERSION}. Theme &amp; Appearance, Business &amp; Store, Invoice Config, Payment Methods, Tax &amp; VAT, Staff Management, Notifications, Language, Backup &amp; Restore, Invoice Templates — all on this page. There is no subscription or paywall in LABH.</p></div>'
                + '<div class="card" id="barcode"><h3>Barcode settings</h3><p>Barcodes are stored on each product (see <a href="/products">Products</a>) and scanned from the <a href="/scan">Scan</a> page. There is no separate barcode setting — the scanner reads the barcode saved on the product.</p></div>'
                + '<button type="submit">Save settings</button></form></div>'
                + '<div class="card"><a class="btn" href="/settings/users">Manage users</a> '
                + '<a class="btn" href="/settings/permissions">Staff permission matrix</a> '
                + '<a class="btn" href="/profile">My Profile</a> '
                + '<a class="btn" href="/security">Security</a> '
                + '<a class="btn" href="/brands">Brands</a> '
                + '<a class="btn" href="/updates">Updates</a> '
                + '<a class="btn" href="/updates-apk">APK Updates (phone)</a></div>')
        return self.send_html(200, page(user, biz, "Business Settings", body))

    # -- generic CRUD helpers
    def crud_list(self, user, biz, cur, title, headers, rows_html, new_url, new_label):
        body = f'<div class="card">{table(headers, rows_html)}</div>'
        if user["role"] in ("owner", "admin"):
            body += f'<div class="card noprint"><a class="btn btn-block" href="{new_url}">{esc(new_label)}</a></div>'
        return self.send_html(200, page(user, biz, title, body))

    def view_categories(self, user, biz, cur, parts, method):
        b = user["business_id"]
        if len(parts) == 1 and method == "GET":
            rows = q("SELECT * FROM categories WHERE business_id=? ORDER BY name", (b,))
            rh = "".join(f"<tr><td>{esc(r['name'])}</td><td>{esc(r['created_at'])}</td></tr>" for r in rows)
            return self.crud_list(user, biz, cur, "Categories", ["Name", "Created"], rh, "/categories/new", "+ New Category")
        if len(parts) == 2 and parts[1] == "new":
            if method == "POST":
                self.require_role(user, "owner", "admin")
                form = self.form()
                self.check_csrf(user, form)
                exec_db("INSERT INTO categories(business_id,name,created_at) VALUES(?,?,?)",
                        (b, self.fv(form, "name").strip(), now_iso()))
                return self.redirect("/categories")
            self.require_role(user, "owner", "admin")
            body = ('<div class="card">' + form_start("/categories/new", user["_csrf"])
                    + field("Category name", "name", "", "text", True)
                    + '<button type="submit">Save</button></form></div>')
            return self.send_html(200, page(user, biz, "New Category", body))
        raise NotFound("Category page not found")

    def view_products(self, user, biz, cur, parts, method):
        b = user["business_id"]
        if len(parts) == 1 and method == "GET":
            qs = parse_qs(urlparse(self.path).query)
            qterm = qs.get("q", [""])[0].strip().lower()
            stock_f = qs.get("stock", ["all"])[0]
            rows = q("""SELECT p.*, c.name AS cat FROM products p LEFT JOIN categories c ON c.id=p.category_id
                        WHERE p.business_id=? ORDER BY p.name""", (b,))
            if qterm:
                rows = [r for r in rows if qterm in r["name"].lower() or qterm in (r["sku"] or "").lower()
                        or qterm in (r["barcode"] or "").lower() or qterm in (r["cat"] or "").lower()]
            if stock_f == "in":
                rows = [r for r in rows if r["stock"] > r["low_stock"]]
            elif stock_f == "low":
                rows = [r for r in rows if 0 < r["stock"] <= r["low_stock"]]
            elif stock_f == "out":
                rows = [r for r in rows if r["stock"] <= 0]
            def _stock_cls(r):
                if r["stock"] <= 0:
                    return "stock-out"
                if r["stock"] <= r["low_stock"]:
                    return "stock-low"
                return "stock-ok"
            rh = "".join(
                f"<tr><td>{i + 1}</td><td><span style='display:inline-flex;gap:8px;align-items:center'><span class='tile-ic'>{icon('package', 18)}</span><a href='/products/{r['id']}'>{esc(r['name'])}</a></span></td>"
                f"<td>{esc(r['sku'])}{('<br><small>' + esc(r['barcode']) + '</small>') if r['barcode'] and r['barcode'] != r['sku'] else ''}</td>"
                f"<td>{esc(r['cat'])}</td><td class='pr'>{money(r['selling_price'], cur)}</td><td class='{_stock_cls(r)}'>{r['stock']}</td></tr>"
                for i, r in enumerate(rows))
            pills = "".join(f'<a class="{"on" if stock_f == v else ""}" href="/products?stock={v}{("&amp;q=" + esc(qterm)) if qterm else ""}">{l}</a>'
                            for v, l in [("all", "All"), ("in", "In Stock"), ("low", "Low Stock"), ("out", "Out of Stock")])
            search = (f'<form method="get" action="/products"><input type="hidden" name="stock" value="{esc(stock_f)}"><div class="bd-search">{icon("search", 18)}<input type="text" name="q" placeholder="Search products…" value="{esc(qterm)}"><button type="submit" class="btn secondary">Search</button></div></form>'
                      f'<div class="bd-pills">{pills}</div>')
            body = f'<div class="card">{search}{table(["#", "Product", "SKU-Barcode", "Category", "Price", "Stock"], rh)}</div>'
            if user["role"] in ("owner", "admin"):
                body += '<div class="card noprint"><a class="btn btn-block" href="/products/new">+ New Product</a></div>'
            return self.send_html(200, page(user, biz, "Products", body))
        if len(parts) == 2 and parts[1] == "new":
            if method == "POST":
                self.require_role(user, "owner", "admin")
                if not can_do(user, "create_product"):
                    raise Forbidden("Staff may not create products.")
                form = self.form()
                self.check_csrf(user, form)
                cat = self.fv(form, "category_id")
                exec_db("""INSERT INTO products(business_id,category_id,name,sku,barcode,purchase_price,
                           selling_price,dp_price,rp_price,mrp_price,wholesale_price,location,stock,low_stock,created_at,brand) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (b, int(cat) if cat else None, self.fv(form, "name").strip(),
                         self.fv(form, "sku").strip(), self.fv(form, "barcode").strip(),
                         float(self.fv(form, "purchase_price") or 0),
                         float(self.fv(form, "selling_price") or 0),
                         float(self.fv(form, "dp_price") or 0), float(self.fv(form, "rp_price") or 0),
                         float(self.fv(form, "mrp_price") or 0), float(self.fv(form, "wholesale_price") or 0),
                         self.fv(form, "location").strip() or "Main",
                         int(self.fv(form, "stock") or 0),
                         int(self.fv(form, "low_stock") or 5), now_iso(), self.fv(form, "brand").strip()))
                return self.redirect("/products")
            self.require_role(user, "owner", "admin")
            cats = q("SELECT id, name FROM categories WHERE business_id=? ORDER BY name", (b,))
            opts = "".join(f'<option value="{c["id"]}">{esc(c["name"])}</option>' for c in cats)
            body = ('<div class="card">' + form_start("/products/new", user["_csrf"])
                    + field("Name", "name", "", "text", True)
                    + field("SKU", "sku") + field("Barcode", "barcode")
                    + f'<p><label>Category</label><br><select name="category_id"><option value="">-</option>{opts}</select></p>'
                    + field("Purchase price", "purchase_price", "0", "number")
                    + field("Selling price (RP)", "selling_price", "0", "number")
                    + field("DP price", "dp_price", "0", "number")
                    + field("RP price", "rp_price", "0", "number")
                    + field("MRP price", "mrp_price", "0", "number")
                    + field("Wholesale price", "wholesale_price", "0", "number")
                    + field("Location", "location", "Main")
                    + field("Brand", "brand")
                    + field("Opening stock", "stock", "0", "number")
                    + field("Low-stock threshold", "low_stock", "5", "number")
                    + '<button type="submit">Save product</button></form></div>')
            return self.send_html(200, page(user, biz, "New Product", body))
        if len(parts) == 2 and parts[1].isdigit():
            prod = scoped_row("products", int(parts[1]), b)
            if method == "POST":
                self.require_role(user, "owner", "admin")
                form = self.form()
                self.check_csrf(user, form)
                if self.fv(form, "action") == "delete":
                    exec_db("DELETE FROM products WHERE id=?", (prod["id"],))
                    return self.redirect("/products")
                old_price = prod["selling_price"]
                new_price = float(self.fv(form, "selling_price") or 0)
                exec_db("""UPDATE products SET name=?, sku=?, barcode=?, purchase_price=?, selling_price=?,
                           dp_price=?, rp_price=?, mrp_price=?, wholesale_price=?, location=?,
                           low_stock=?, brand=? WHERE id=?""",
                        (self.fv(form, "name").strip(), self.fv(form, "sku").strip(), self.fv(form, "barcode").strip(),
                         float(self.fv(form, "purchase_price") or 0), new_price,
                         float(self.fv(form, "dp_price") or 0), float(self.fv(form, "rp_price") or 0),
                         float(self.fv(form, "mrp_price") or 0), float(self.fv(form, "wholesale_price") or 0),
                         self.fv(form, "location").strip() or "Main",
                         int(self.fv(form, "low_stock") or 5), self.fv(form, "brand").strip(), prod["id"]))
                if old_price != new_price:
                    exec_db("INSERT INTO price_history(business_id,product_id,old_price,new_price,changed_at,note) VALUES(?,?,?,?,?,?)",
                            (b, prod["id"], old_price, new_price, now_iso(), "Edited on product page"))
                return self.redirect("/products")
            moves = q("SELECT * FROM stock_movements WHERE business_id=? AND product_id=? ORDER BY id DESC LIMIT 20", (b, prod["id"]))
            mh = "".join(f"<tr><td>{esc(m['move_date'])}</td><td>{esc(m['kind'])}</td><td>{m['qty']}</td><td>{esc(m['note'])}</td></tr>" for m in moves)
            body = (f'<div class="card"><p>SKU: {esc(prod["sku"])} | Barcode: {esc(prod["barcode"])} | Location: {esc(prod["location"])}<br>'
                    f'Purchase: {money(prod["purchase_price"], cur)} | Selling: {money(prod["selling_price"], cur)} | '
                    f'DP: {money(prod["dp_price"], cur)} | RP: {money(prod["rp_price"], cur)} | MRP: {money(prod["mrp_price"], cur)} | Wholesale: {money(prod["wholesale_price"], cur)} | Stock: {prod["stock"]} | Brand: {esc(prod["brand"])}</p></div>')
            ph = q("SELECT * FROM price_history WHERE business_id=? AND product_id=? ORDER BY id DESC LIMIT 20", (b, prod["id"]))
            phh = "".join(f"<tr><td>{esc(r['changed_at'])}</td><td>{money(r['old_price'], cur)}</td><td>{money(r['new_price'], cur)}</td></tr>" for r in ph)
            body += '<div class="card"><h3>Price history</h3>' + (table(["Changed", "Old price", "New price"], phh) if ph else "<p>No price changes recorded.</p>") + '</div>'
            if user["role"] in ("owner", "admin"):
                body += ('<div class="card">' + form_start(f"/products/{prod['id']}", user["_csrf"])
                         + field("Name", "name", prod["name"], "text", True) + field("SKU", "sku", prod["sku"]) + field("Barcode", "barcode", prod["barcode"])
                         + field("Purchase price", "purchase_price", prod["purchase_price"], "number")
                         + field("Selling price", "selling_price", prod["selling_price"], "number")
                         + field("DP price", "dp_price", prod["dp_price"], "number")
                         + field("RP price", "rp_price", prod["rp_price"], "number")
                         + field("MRP price", "mrp_price", prod["mrp_price"], "number")
                         + field("Wholesale price", "wholesale_price", prod["wholesale_price"], "number")
                         + field("Location", "location", prod["location"])
                         + field("Brand", "brand", prod["brand"])
                         + field("Low-stock threshold", "low_stock", prod["low_stock"], "number")
                         + '<button type="submit">Save changes</button></form>'
                         + f'<form method="post" action="/products/{prod["id"]}" style="margin-top:10px">'
                         f'<input type="hidden" name="csrf" value="{esc(user["_csrf"])}">'
                         '<input type="hidden" name="action" value="delete">'
                         '<button type="submit" class="danger">Delete product</button></form></div>')
            body += '<div class="card"><h3>Recent stock movements</h3>' + (table(["Date", "Kind", "Qty", "Note"], mh) if moves else "<p>None yet.</p>") + '</div>'
            return self.send_html(200, page(user, biz, prod["name"], body))
        raise NotFound("Product page not found")

    def view_inventory(self, user, biz, cur, parts, method):
        b = user["business_id"]
        if method == "POST" and len(parts) == 2 and parts[1] == "transfer":
            self.require_role(user, "owner", "admin")
            form = self.form()
            self.check_csrf(user, form)
            prod = scoped_row("products", int(self.fv(form, "product_id") or 0), b)
            qty = float(self.fv(form, "qty") or 0)
            floc, tloc = self.fv(form, "from_loc").strip(), self.fv(form, "to_loc").strip()
            if qty <= 0 or not floc or not tloc or floc == tloc:
                return self.send_html(200, page(user, biz, "Inventory", '<div class="card err">Transfer needs a positive quantity and two different locations.</div>'))
            exec_db("INSERT INTO stock_transfers(business_id,product_id,from_loc,to_loc,qty,note,move_date,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (b, prod["id"], floc, tloc, qty, self.fv(form, "note").strip(), today(), now_iso()))
            exec_db("INSERT INTO stock_movements(business_id,product_id,kind,qty,note,move_date,created_at,location_from,location_to) VALUES(?,?,?,?,?,?,?,?,?)",
                    (b, prod["id"], "transfer", qty, f"Transfer {floc} -> {tloc}", today(), now_iso(), floc, tloc))
            return self.redirect("/inventory")
        if method == "POST":
            self.require_role(user, "owner", "admin")
            form = self.form()
            self.check_csrf(user, form)
            prod = scoped_row("products", int(self.fv(form, "product_id") or 0), b)
            kind = self.fv(form, "kind")
            qty = float(self.fv(form, "qty") or 0)
            if kind not in ("in", "out") or qty <= 0:
                return self.send_html(200, page(user, biz, "Inventory", '<div class="card err">Invalid movement.</div>'))
            if kind == "out" and prod["stock"] < qty:
                return self.send_html(200, page(user, biz, "Inventory", '<div class="card err">Not enough stock for this out movement.</div>'))
            delta = qty if kind == "in" else -qty
            exec_db("UPDATE products SET stock=stock+? WHERE id=?", (delta, prod["id"]))
            exec_db("INSERT INTO stock_movements(business_id,product_id,kind,qty,note,move_date,created_at) VALUES(?,?,?,?,?,?,?)",
                    (b, prod["id"], kind, qty, self.fv(form, "note").strip(), today(), now_iso()))
            return self.redirect("/inventory")
        rows = q("SELECT * FROM products WHERE business_id=? ORDER BY name", (b,))
        qs = parse_qs(urlparse(self.path).query)
        qterm = qs.get("q", [""])[0].strip().lower()
        stock_f = qs.get("stock", ["all"])[0]
        # In/Out from recorded purchases, sales and manual movements; transfers
        # move stock between locations and do not change the total.
        pur_q = {r["pid"]: r["v"] for r in q("SELECT product_id AS pid, COALESCE(SUM(qty),0) AS v FROM purchase_items pi JOIN purchases p ON p.id=pi.purchase_id WHERE p.business_id=? GROUP BY product_id", (b,))}
        sal_q = {r["pid"]: r["v"] for r in q("SELECT product_id AS pid, COALESCE(SUM(qty),0) AS v FROM sale_items si JOIN sales s ON s.id=si.sale_id WHERE s.business_id=? GROUP BY product_id", (b,))}
        mov_in = {r["pid"]: r["v"] for r in q("SELECT product_id AS pid, COALESCE(SUM(qty),0) AS v FROM stock_movements WHERE business_id=? AND kind='in' GROUP BY product_id", (b,))}
        mov_out = {r["pid"]: r["v"] for r in q("SELECT product_id AS pid, COALESCE(SUM(qty),0) AS v FROM stock_movements WHERE business_id=? AND kind='out' GROUP BY product_id", (b,))}
        inv = []
        for r in rows:
            inn = pur_q.get(r["id"], 0) + mov_in.get(r["id"], 0)
            out = sal_q.get(r["id"], 0) + mov_out.get(r["id"], 0)
            inv.append((r, r["stock"] - inn + out, inn, out))
        if qterm:
            inv = [x for x in inv if qterm in x[0]["name"].lower() or qterm in (x[0]["sku"] or "").lower()]
        if stock_f == "in":
            inv = [x for x in inv if x[0]["stock"] > x[0]["low_stock"]]
        elif stock_f == "low":
            inv = [x for x in inv if 0 < x[0]["stock"] <= x[0]["low_stock"]]
        elif stock_f == "out":
            inv = [x for x in inv if x[0]["stock"] <= 0]
        def _inv_cls(r):
            if r["stock"] <= 0:
                return "stock-out"
            if r["stock"] <= r["low_stock"]:
                return "stock-low"
            return "stock-ok"
        rh = "".join(
            f"<tr><td>{i + 1}</td><td><span style='display:inline-flex;gap:8px;align-items:center'><span class='tile-ic'>{icon('package', 18)}</span><a href='/products/{r['id']}'>{esc(r['name'])}</a></span></td>"
            f"<td>{o:g}</td><td>{inn:g}</td><td>{out:g}</td><td class='{_inv_cls(r)}'>{r['stock']:g}</td><td class='pr'>{money(r['stock'] * r['purchase_price'], cur)}</td></tr>"
            for i, (r, o, inn, out) in enumerate(inv))
        total_products = len(rows)
        total_stock = sum(r["stock"] for r in rows)
        stock_value = sum(r["stock"] * r["purchase_price"] for r in rows)
        low_n = sum(1 for r in rows if 0 < r["stock"] <= r["low_stock"])
        out_n = sum(1 for r in rows if r["stock"] <= 0)
        pills = "".join(f'<a class="{"on" if stock_f == v else ""}" href="/inventory?stock={v}{("&amp;q=" + esc(qterm)) if qterm else ""}">{l}</a>'
                        for v, l in [("all", "All"), ("in", "In Stock"), ("low", "Low Stock"), ("out", "Out of Stock")])
        search = (f'<form method="get" action="/inventory"><input type="hidden" name="stock" value="{esc(stock_f)}"><div class="bd-search">{icon("search", 18)}<input type="text" name="q" placeholder="Search inventory…" value="{esc(qterm)}"><button type="submit" class="btn secondary">Search</button></div></form>'
                  f'<div class="bd-pills">{pills}</div>')
        summary = (f'<div class="card"><h3>Stock Summary</h3><div class="v23-tiles">'
                   f'<div class="v23-tile"><div class="lbl">Total Products</div><div class="val">{total_products}</div></div>'
                   f'<div class="v23-tile"><div class="lbl">Total Stock</div><div class="val">{total_stock:g}</div></div>'
                   f'<div class="v23-tile"><div class="lbl">Stock Value</div><div class="val">{money(stock_value, cur)}</div></div></div>'
                   f'<p><span class="pill low">Low Stock: {low_n}</span> <span class="pill due">Out of Stock: {out_n}</span></p></div>')
        body = (f'<div class="card"><h3>Inventory</h3>{search}' + table(["#", "Product", "Opening", "In", "Out", "Current", "Value"], rh)
                + '<p style="color:#64748b;font-size:12px">Opening = Current &minus; In + Out, derived from recorded purchases, sales and manual stock movements.</p></div>'
                + summary)
        if user["role"] in ("owner", "admin"):
            body += '<div class="card noprint"><a class="btn btn-block" href="/products/new">+ Add Product</a></div>'
        moves = q("""SELECT m.*, p.name AS pname FROM stock_movements m JOIN products p ON p.id=m.product_id
                     WHERE m.business_id=? ORDER BY m.id DESC LIMIT 30""", (b,))
        mh = "".join(f"<tr><td>{esc(m['move_date'])}</td><td>{esc(m['pname'])}</td><td>{esc(m['kind'])}</td><td>{m['qty']}</td></tr>" for m in moves)
        opts = "".join(f'<option value="{r["id"]}">{esc(r["name"])} (stock {r["stock"]})</option>' for r in rows)
        if user["role"] in ("owner", "admin"):
            body += ('<div class="card"><h3>Stock in / out</h3>' + form_start("/inventory", user["_csrf"])
                     + f'<p><label>Product</label><br><select name="product_id" required>{opts}</select></p>'
                     + '<p><label>Direction</label><br><select name="kind"><option value="in">In</option><option value="out">Out</option></select></p>'
                     + field("Quantity", "qty", "", "number", True) + field("Note", "note")
                     + '<button type="submit">Record movement</button></form></div>')
        body += '<div class="card"><h3>Recent movements</h3>' + (table(["Date", "Product", "Kind", "Qty"], mh) if moves else "<p>None yet.</p>") + '</div>'
        transfers = q("SELECT t.*, p.name AS pname FROM stock_transfers t JOIN products p ON p.id=t.product_id WHERE t.business_id=? ORDER BY t.id DESC LIMIT 20", (b,))
        th = "".join(f"<tr><td>{esc(t['move_date'])}</td><td>{esc(t['pname'])}</td><td>{esc(t['from_loc'])} &rarr; {esc(t['to_loc'])}</td><td>{t['qty']}</td></tr>" for t in transfers)
        body += '<div class="card"><h3>Stock transfers</h3>' + (table(["Date", "Product", "From &rarr; To", "Qty"], th) if transfers else "<p>No transfers yet.</p>") + '</div>'
        if user["role"] in ("owner", "admin"):
            body += ('<div class="card"><h3>Stock transfer (between locations)</h3>' + form_start("/inventory/transfer", user["_csrf"])
                     + f'<p><label>Product</label><br><select name="product_id" required>{opts}</select></p>'
                     + field("From location", "from_loc", "Main", "text", True) + field("To location", "to_loc", "", "text", True)
                     + field("Quantity", "qty", "", "number", True) + field("Note", "note")
                     + '<button type="submit">Record transfer</button></form></div>')
        return self.send_html(200, page(user, biz, "Inventory", body))

    def _party_list(self, user, biz, cur, kind, title, new_label):
        b = user["business_id"]
        table_name = "customers" if kind == "customer" else "suppliers"
        qs = parse_qs(urlparse(self.path).query)
        qterm = qs.get("q", [""])[0].strip().lower()
        rows = q(f"SELECT * FROM {table_name} WHERE business_id=? ORDER BY name", (b,))
        if qterm:
            rows = [r for r in rows if qterm in r["name"].lower() or qterm in (r["phone"] or "").lower()]
        rh = ""
        for r in rows:
            due = customer_due(b, r["id"]) if kind == "customer" else supplier_due(b, r["id"])
            initials = "".join(w[0] for w in r["name"].split()[:2]).upper() if r["name"] else "?"
            tint = " blue" if kind == "supplier" else ""
            due_cls = "pr" if due <= 0 else "stock-out"
            rh += (f"<tr><td><span style='display:inline-flex;gap:8px;align-items:center'><span class='tile-ic{tint}'>{esc(initials)}</span><a href='/{kind}s/{r['id']}'>{esc(r['name'])}</a></span></td>"
                   f"<td>{esc(r['phone'])}</td><td class='{due_cls}'>{money(due, cur)}</td></tr>")
        search = (f'<form method="get" action="/{kind}s"><div class="bd-search">{icon("search", 18)}<input type="text" name="q" placeholder="Search {kind}s…" value="{esc(qterm)}"><button type="submit" class="btn secondary">Search</button></div></form>')
        body = f'<div class="card">{search}' + table(["Name", "Phone", "Due"], rh) + '</div>'
        if user["role"] in ("owner", "admin"):
            body += f'<div class="card noprint"><a class="btn btn-block" href="/{kind}s/new">{esc(new_label)}</a></div>'
        return self.send_html(200, page(user, biz, title, body))

    def _party_detail(self, user, biz, cur, kind, row_id, method):
        b = user["business_id"]
        table_name = "customers" if kind == "customer" else "suppliers"
        row = scoped_row(table_name, row_id, b)
        if method == "POST":
            self.require_role(user, "owner", "admin")
            form = self.form()
            self.check_csrf(user, form)
            if self.fv(form, "action") == "collect":
                amt = float(self.fv(form, "amount") or 0)
                if amt <= 0:
                    return self.send_html(200, page(user, biz, "Collect Payment", '<div class="card err">Amount must be positive.</div>'))
                etype = "customer" if kind == "customer" else "supplier"
                note = f"Ref: {self.fv(form, 'reference').strip()} | {self.fv(form, 'notes').strip()}".strip(" |")
                pdate = self.fv(form, "pay_date").strip() or today()
                exec_db("INSERT INTO payments(business_id,entity_type,entity_id,amount,method,note,pay_date,created_at) VALUES(?,?,?,?,?,?,?,?)",
                        (b, etype, row["id"], amt, self.fv(form, "method").strip(), note, pdate, now_iso()))
                if kind == "customer":
                    apply_customer_payment(b, row["id"], amt)
                else:
                    apply_supplier_payment(b, row["id"], amt)
                return self.redirect(f"/{kind}s/{row['id']}")
            if self.fv(form, "action") == "delete":
                exec_db(f"DELETE FROM {table_name} WHERE id=?", (row["id"],))
                return self.redirect(f"/{kind}s")
            exec_db(f"UPDATE {table_name} SET name=?, phone=?, email=?, address=?, opening_balance=? WHERE id=?",
                    (self.fv(form, "name").strip(), self.fv(form, "phone").strip(),
                     self.fv(form, "email").strip(), self.fv(form, "address").strip(),
                     float(self.fv(form, "opening_balance") or 0), row["id"]))
            return self.redirect(f"/{kind}s")
        due = customer_due(b, row["id"]) if kind == "customer" else supplier_due(b, row["id"])
        body = f'<div class="card"><p>Phone: {esc(row["phone"])} | Email: {esc(row["email"])} | Address: {esc(row["address"])} | Due: {money(due, cur)}</p></div>'
        if user["role"] in ("owner", "admin"):
            body += ('<div class="card">' + form_start(f"/{kind}s/{row['id']}", user["_csrf"])
                     + field("Name", "name", row["name"], "text", True) + field("Phone", "phone", row["phone"])
                     + field("Email", "email", row["email"], "email") + field("Address", "address", row["address"])
                     + field("Opening balance", "opening_balance", row["opening_balance"], "number")
                     + '<button type="submit">Save changes</button></form>'
                     + f'<form method="post" action="/{kind}s/{row["id"]}" style="margin-top:10px">'
                     f'<input type="hidden" name="csrf" value="{esc(user["_csrf"])}">'
                     '<input type="hidden" name="action" value="delete">'
                     '<button type="submit" class="danger">Delete</button></form></div>')
        if kind == "customer":
            led = q("SELECT * FROM sales WHERE business_id=? AND customer_id=? ORDER BY id DESC LIMIT 20", (b, row["id"]))
            lh = "".join(f"<tr><td>{esc(r['invoice_no'])}</td><td>{esc(r['sale_date'])}</td><td>{money(r['total'], cur)}</td><td>{money(r['paid'], cur)}</td><td>{money(r['due'], cur)}</td></tr>" for r in led)
            body += '<div class="card"><h3>Customer ledger (sales)</h3>' + (table(["Invoice", "Date", "Total", "Paid", "Due"], lh) if led else "<p>No sales yet.</p>") + '</div>'
        else:
            led = q("SELECT * FROM purchases WHERE business_id=? AND supplier_id=? ORDER BY id DESC LIMIT 20", (b, row["id"]))
            lh = "".join(f"<tr><td>#{r['id']}</td><td>{esc(r['purchase_date'])}</td><td>{money(r['total'], cur)}</td><td>{money(r['paid'], cur)}</td><td>{money(r['due'], cur)}</td></tr>" for r in led)
            body += '<div class="card"><h3>Supplier ledger (purchases)</h3>' + (table(["Purchase", "Date", "Total", "Paid", "Due"], lh) if led else "<p>No purchases yet.</p>") + '</div>'
        pays = q("SELECT * FROM payments WHERE business_id=? AND entity_type=? AND entity_id=? ORDER BY id DESC LIMIT 20", (b, "customer" if kind == "customer" else "supplier", row["id"]))
        ph = "".join(f"<tr><td>{esc(r['pay_date'])}</td><td>{money(r['amount'], cur)}</td><td>{esc(r['method'])}</td><td>{esc(r['note'])}</td></tr>" for r in pays)
        body += '<div class="card"><h3>Payment history</h3>' + (table(["Date", "Amount", "Method", "Reference / notes"], ph) if pays else "<p>No payments yet.</p>") + '</div>'
        if user["role"] in ("owner", "admin"):
            body += ('<div class="card"><h3>Collect Payment</h3>' + form_start(f"/{kind}s/{row['id']}", user["_csrf"])
                     + '<input type="hidden" name="action" value="collect">'
                     + field("Amount", "amount", "", "number", True) + field("Payment method", "method")
                     + field("Reference", "reference") + field("Date", "pay_date", today(), "date") + field("Notes", "notes")
                     + '<button type="submit">Record payment</button></form></div>')
        return self.send_html(200, page(user, biz, row["name"], body))

    def _party_new(self, user, biz, cur, kind, method):
        b = user["business_id"]
        if method == "POST":
            self.require_role(user, "owner", "admin")
            form = self.form()
            self.check_csrf(user, form)
            table_name = "customers" if kind == "customer" else "suppliers"
            exec_db(f"INSERT INTO {table_name}(business_id,name,phone,email,address,opening_balance,created_at) VALUES(?,?,?,?,?,?,?)",
                    (b, self.fv(form, "name").strip(), self.fv(form, "phone").strip(),
                     self.fv(form, "email").strip(), self.fv(form, "address").strip(),
                     float(self.fv(form, "opening_balance") or 0), now_iso()))
            return self.redirect(f"/{kind}s")
        self.require_role(user, "owner", "admin")
        body = ('<div class="card">' + form_start(f"/{kind}s/new", user["_csrf"])
                + field("Name", "name", "", "text", True) + field("Phone", "phone")
                + field("Email", "email", "", "email") + field("Address", "address")
                + field("Opening balance", "opening_balance", "0", "number")
                + '<button type="submit">Save</button></form></div>')
        return self.send_html(200, page(user, biz, f"New {kind.title()}", body))

    def view_customers(self, user, biz, cur, parts, method):
        if len(parts) == 1 and method == "GET":
            return self._party_list(user, biz, cur, "customer", "Customers", "+ New Customer")
        if len(parts) == 2 and parts[1] == "new":
            return self._party_new(user, biz, cur, "customer", method)
        if len(parts) == 2 and parts[1].isdigit():
            return self._party_detail(user, biz, cur, "customer", int(parts[1]), method)
        raise NotFound("Customer page not found")

    def view_suppliers(self, user, biz, cur, parts, method):
        if len(parts) == 1 and method == "GET":
            return self._party_list(user, biz, cur, "supplier", "Suppliers", "+ New Supplier")
        if len(parts) == 2 and parts[1] == "new":
            return self._party_new(user, biz, cur, "supplier", method)
        if len(parts) == 2 and parts[1].isdigit():
            return self._party_detail(user, biz, cur, "supplier", int(parts[1]), method)
        raise NotFound("Supplier page not found")

    # -- sales / purchases / invoices
    def _lines_form(self, form, user):
        """Parse indexed sale/purchase lines; returns (items, error).
        Each item is (product, qty, price, discount); discount defaults to 0
        and is clamped to the line total, so callers that ignore it behave
        exactly as before."""
        b = user["business_id"]
        items = []
        for i in range(30):
            pid = self.fv(form, f"product_{i}")
            if not pid:
                continue
            prod = scoped_row("products", int(pid), b)
            qty = float(self.fv(form, f"qty_{i}") or 0)
            price = self.fv(form, f"price_{i}")
            price = float(price) if price else prod["selling_price"]
            if qty <= 0:
                return None, "Quantity must be positive."
            disc = float(self.fv(form, f"discount_{i}") or 0)
            disc = max(0.0, min(disc, qty * price))
            items.append((prod, qty, price, disc))
        if not items:
            return None, "Add at least one item."
        return items, None

    def _line_picker(self, products, cur, with_discount=False):
        opts = "".join(f'<option value="{p["id"]}" data-price="{p["selling_price"]}">{esc(p["name"])} - {money(p["selling_price"], cur)} (stock {p["stock"]})</option>' for p in products)
        disc_html = ' <input type="number" step="any" name="discount_0" value="0" placeholder="Discount" style="width:90px">' if with_discount else ""
        total_html = ' <span class="row-total"></span>' if with_discount else ""
        row = (f'<div class="line bd-cartrow"><div><select name="product_0" required>{opts}</select> '
               f'<div style="display:flex;gap:8px;align-items:center;margin-top:8px">'
               f'<span class="stepper"><button type="button" onclick="stepQty(this,-1)">&#8722;</button><input type="number" step="any" name="qty_0" value="1" required><button type="button" onclick="stepQty(this,1)">+</button></span>'
               f' <input type="number" step="any" name="price_0" placeholder="Price" style="width:110px">{disc_html}</div></div>'
               f'<div style="text-align:right">{total_html} <button type="button" class="line-remove" onclick="removeLine(this)" aria-label="Remove line">&times;</button></div></div>')
        js = ("<script>let n=1;"
              "function addLine(){let d=document.createElement('div');d.className='line bd-cartrow';"
              "d.innerHTML=document.querySelector('.line').innerHTML"
              ".replace(/product_0/g,'product_'+n).replace(/qty_0/g,'qty_'+n).replace(/price_0/g,'price_'+n).replace(/discount_0/g,'discount_'+n);"
              "document.getElementById('lines').appendChild(d);n++;recalc();}"
              "function removeLine(b){let ls=document.querySelectorAll('.line');"
              "if(ls.length>1){b.closest('.line').remove();}else{b.closest('.line').querySelector('select').selectedIndex=0;}recalc();}"
              "function stepQty(b,d){let inp=b.parentElement.querySelector('input');let v=parseFloat(inp.value||0)+d;if(v<1)v=1;inp.value=v;recalc();}"
              "function fmtMoney(v){return '\\u09f3'+v.toLocaleString('en-US',{minimumFractionDigits:2,maximumFractionDigits:2});}"
              "function findProduct(){let q=document.getElementById('prod-search');if(!q)return;let t=q.value.trim().toLowerCase();if(!t)return;"
              "let sels=document.querySelectorAll('#lines .line select');if(!sels.length)return;let sel=sels[sels.length-1];"
              "for(let o of sel.options){if(o.text.toLowerCase().includes(t)){sel.value=o.value;break;}}recalc();}"
              "function recalc(){let sub=0,disc=0,items=0;"
              "document.querySelectorAll('.line').forEach(function(ln){let sel=ln.querySelector('select');"
              "let q=parseFloat(ln.querySelector('[name^=qty_]').value||0);"
              "let pv=ln.querySelector('[name^=price_]').value;let pr=parseFloat(pv);"
              "if(isNaN(pr)){pr=sel.selectedOptions.length?parseFloat(sel.selectedOptions[0].dataset.price||0):0;}"
              "let dc=ln.querySelector('[name^=discount_]');let d=dc?parseFloat(dc.value||0):0;"
              "if(sel.value)items++;sub+=q*pr;disc+=d;"
              "let rt=ln.querySelector('.row-total');if(rt)rt.textContent=fmtMoney(q*pr-d);});"
              "let e=function(id){return document.getElementById(id);};"
              "if(e('sum-sub'))e('sum-sub').textContent=fmtMoney(sub);"
              "if(e('sum-disc'))e('sum-disc').textContent=fmtMoney(disc);"
              "if(e('sum-total'))e('sum-total').textContent=fmtMoney(sub-disc);"
              "if(e('sum-items'))e('sum-items').textContent=items;"
              "if(e('sum-paid')&&e('sum-due')){let paid=parseFloat(document.querySelector('[name=paid]').value||0);"
              "e('sum-paid').textContent=fmtMoney(paid);e('sum-due').textContent=fmtMoney(Math.max(0,sub-disc-paid));}}"
              "document.addEventListener('input',recalc);recalc();</script>")
        return row, js

    def view_sales(self, user, biz, cur, parts, method):
        b = user["business_id"]
        if len(parts) == 1 and method == "GET":
            qs = parse_qs(urlparse(self.path).query)
            qterm = qs.get("q", [""])[0].strip().lower()
            status = qs.get("status", ["all"])[0]
            if qs.get("export") == ["csv"]:
                rows = q("""SELECT s.*, c.name AS cname FROM sales s LEFT JOIN customers c ON c.id=s.customer_id
                            WHERE s.business_id=? ORDER BY s.id DESC""", (b,))
                out = io.StringIO()
                w = csv.writer(out)
                w.writerow(["invoice", "customer", "date", "total", "paid", "due"])
                for r in rows:
                    w.writerow([r["invoice_no"], r["cname"] or "Walk-in", r["sale_date"], r["total"], r["paid"], r["due"]])
                return self.send_file(out.getvalue().encode(), "sales.csv", "text/csv")
            rows = q("""SELECT s.*, c.name AS cname FROM sales s LEFT JOIN customers c ON c.id=s.customer_id
                        WHERE s.business_id=? ORDER BY s.id DESC""", (b,))
            if qterm:
                rows = [r for r in rows if qterm in r["invoice_no"].lower() or qterm in (r["cname"] or "").lower()]
            if status == "paid":
                rows = [r for r in rows if r["due"] <= 0]
            elif status == "due":
                rows = [r for r in rows if r["due"] > 0]
            def _status(r):
                if r["due"] <= 0:
                    return '<span class="pill paid">Paid</span>'
                if r["paid"] > 0:
                    return '<span class="pill partial">Partial</span>'
                return '<span class="pill due">Due</span>'
            rh = "".join(f"<tr><td><a href='/sales/{r['id']}'>{esc(r['invoice_no'])}</a></td><td>{esc(r['cname'] or 'Walk-in')}</td>"
                         f"<td>{esc(r['sale_date'])}</td><td class='pr'>{money(r['total'], cur)}</td><td>{money(r['due'], cur)}</td><td>{_status(r)}</td></tr>" for r in rows)
            pills = "".join(f'<a class="{"on" if status == v else ""}" href="/sales?status={v}{("&amp;q=" + esc(qterm)) if qterm else ""}">{l}</a>'
                            for v, l in [("all", "All"), ("paid", "Paid"), ("due", "Due")])
            filt = (f'<div class="card"><form method="get" action="/sales"><input type="hidden" name="status" value="{esc(status)}"><div class="bd-search">{icon("search", 18)}<input type="search" name="q" placeholder="Search invoice/customer" value="{esc(qterm)}"><button type="submit" class="btn secondary">Search</button></div></form>'
                    f'<div class="bd-pills">{pills}</div><p><a class="btn secondary" href="/sales?export=csv">Export CSV</a></p></div>')
            body = filt + '<div class="card">' + table(["Invoice", "Customer", "Date", "Total", "Due", "Status"], rh) + '</div>'
            if user["role"] in ("owner", "admin"):
                body += '<div class="card noprint"><a class="btn btn-block" href="/sales/new">+ New Sale</a></div>'
            return self.send_html(200, page(user, biz, "Sales", body))
        if len(parts) == 2 and parts[1] == "new":
            if method == "POST":
                form = self.form()
                self.check_csrf(user, form)
                items, err = self._lines_form(form, user)
                if err:
                    return self.send_html(200, page(user, biz, "New Sale", f'<div class="card err">{esc(err)}</div>'))
                for prod, qty, _, _ in items:
                    if prod["stock"] < qty:
                        return self.send_html(200, page(user, biz, "New Sale",
                            f'<div class="card err">Not enough stock for {esc(prod["name"])} (have {prod["stock"]}).</div>'))
                total = sum(qty * price - disc for _, qty, price, disc in items)
                paid = max(0.0, min(float(self.fv(form, "paid") or 0), total))
                cust = self.fv(form, "customer_id")
                cust_id = int(cust) if cust else None
                if cust_id:
                    scoped_row("customers", cust_id, b)
                sdate = self.fv(form, "sale_date").strip() or today()
                sale_id = exec_db("""INSERT INTO sales(business_id,customer_id,invoice_no,total,paid,due,
                                    sale_date,created_at,salesperson,payment_method,note) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                                  (b, cust_id, next_invoice_no(b), total, paid, total - paid, sdate, now_iso(),
                                   self.fv(form, "salesperson").strip(), self.fv(form, "payment_method").strip(),
                                   self.fv(form, "note").strip()))
                for prod, qty, price, disc in items:
                    exec_db("INSERT INTO sale_items(sale_id,product_id,product_name,qty,price,amount,cost_price,discount) VALUES(?,?,?,?,?,?,?,?)",
                            (sale_id, prod["id"], prod["name"], qty, price, qty * price - disc, prod["purchase_price"], disc))
                    exec_db("UPDATE products SET stock=stock-? WHERE id=?", (qty, prod["id"]))
                    exec_db("INSERT INTO stock_movements(business_id,product_id,kind,qty,note,move_date,created_at) VALUES(?,?,?,?,?,?,?)",
                            (b, prod["id"], "sale", qty, f"Sale #{sale_id}", today(), now_iso()))
                return self.redirect(f"/sales/{sale_id}")
            products = q("SELECT * FROM products WHERE business_id=? ORDER BY name", (b,))
            custs = q("SELECT id, name FROM customers WHERE business_id=? ORDER BY name", (b,))
            copts = '<option value="">Walk-in</option>' + "".join(f'<option value="{c["id"]}">{esc(c["name"])}</option>' for c in custs)
            row, js = self._line_picker(products, cur, with_discount=True)
            pm_opts = "".join(f'<option value="{m}">{esc(m)}</option>' for m in payment_methods_setting())
            body = ('<div class="card">' + form_start("/sales/new", user["_csrf"])
                    + f'<p><label>Customer</label><br><select name="customer_id">{copts}</select> <a class="btn secondary" href="/customers/new">+ New customer</a></p>'
                    + field("Salesperson", "salesperson") + field("Sale date", "sale_date", today(), "date")
                    + f'<p>Auto invoice number: <b>{esc(next_invoice_no(b))}</b></p>'
                    + '<div class="bd-search">' + icon("search", 18) + '<input type="text" id="prod-search" placeholder="Search products…" onkeydown="if(event.key===\'Enter\'){event.preventDefault();findProduct();}"><button type="button" class="btn secondary" onclick="findProduct()">Find</button> <a class="btn secondary" href="/scan">Scan</a></div>'
                    + '<div id="lines">' + row + '</div>'
                    + '<p><button type="button" class="secondary" onclick="addLine()">+ Add line</button></p>'
                    + '<div class="bd-summary"><div class="row"><span>Items</span><span id="sum-items">0</span></div>'
                    + '<div class="row"><span>Subtotal</span><span id="sum-sub">৳0.00</span></div>'
                    + '<div class="row"><span>Discount</span><span id="sum-disc">৳0.00</span></div>'
                    + '<div class="row total"><span>Total</span><span id="sum-total">৳0.00</span></div>'
                    + '<div class="row"><span>Paid Amount</span><span id="sum-paid">৳0.00</span></div>'
                    + '<div class="row"><span>Due Amount</span><span class="due" id="sum-due">৳0.00</span></div></div>'
                    + field("Paid amount", "paid", "0", "number")
                    + f'<p><label>Payment method</label><br><select name="payment_method">{pm_opts}</select></p>'
                    + field("Note", "note")
                    + '<p>Price-type pills: choose DP / RP / MRP / Wholesale on the POS page, or enter a manual price per line here.</p>'
                    + '<button type="submit" class="btn-block">✓ Complete Sale</button></form></div>')
            return self.send_html(200, page(user, biz, "New Sale", body, extra=js))
        if len(parts) == 2 and parts[1].isdigit():
            return self.invoice_view(user, biz, cur, int(parts[1]))
        raise NotFound("Sale page not found")

    def invoice_view(self, user, biz, cur, sale_id, template=None):
        if template is None:
            template = invoice_template_setting()
        b = user["business_id"]
        sale = scoped_row("sales", sale_id, b)
        cust = q("SELECT name FROM customers WHERE id=?", (sale["customer_id"],), one=True) if sale["customer_id"] else None
        items = q("SELECT * FROM sale_items WHERE sale_id=?", (sale_id,))
        _note = sale["note"] if "note" in sale.keys() else ""
        if any(i["discount"] for i in items):
            rh = "".join(f"<tr><td>{esc(i['product_name'])}</td><td>{i['qty']}</td><td>{money(i['price'], cur)}</td><td>{money(i['discount'], cur)}</td><td>{money(i['amount'], cur)}</td></tr>" for i in items)
            _cols = ["Item", "Qty", "Price", "Discount", "Amount"]
        else:
            rh = "".join(f"<tr><td>{esc(i['product_name'])}</td><td>{i['qty']}</td><td>{money(i['price'], cur)}</td><td>{money(i['amount'], cur)}</td></tr>" for i in items)
            _cols = ["Item", "Qty", "Price", "Amount"]
        if template == "modern":
            head = f'<div style="background:#15803d;color:#fff;padding:14px;border-radius:12px 12px 0 0"><b>LABH</b> — {esc(biz["name"])}</div>'
        elif template == "bold":
            head = f'<div style="background:#0f172a;color:#fff;padding:14px"><b>LABH</b> — {esc(biz["name"])}</div>'
        elif template == "receipt":
            head = f'<div style="text-align:center"><b>LABH</b><br>{esc(biz["name"])}</div>'
        elif template == "minimal":
            head = f'<div style="border-bottom:1px solid #e2e8f0;padding-bottom:8px"><b>{esc(biz["name"])}</b></div>'
        else:
            head = f'<div><b>{esc(biz["name"])}</b> — Invoice</div>' 
        body = (f'<div class="card">{head}<h2>Invoice {esc(sale["invoice_no"])}</h2>'
                f'<p>Business: {esc(biz["name"])}<br>Customer: {esc(cust["name"]) if cust else "Walk-in"}<br>Date: {esc(sale["sale_date"])}</p>'
                + (f'<p>Note: {esc(_note)}</p>' if _note else "")
                + table(_cols, rh)
                + f'<p>Total: {money(sale["total"], cur)}<br>Paid: {money(sale["paid"], cur)}<br>Due: {money(sale["due"], cur)}</p>'
                + f'<p class="noprint">Templates: <a class="btn" href="/invoices/{sale["id"]}?template=modern">Modern</a> <a class="btn secondary" href="/invoices/{sale["id"]}?template=classic">Classic</a> <a class="btn secondary" href="/invoices/{sale["id"]}?template=minimal">Minimal</a> <a class="btn secondary" href="/invoices/{sale["id"]}?template=bold">Bold</a> <a class="btn secondary" href="/invoices/{sale["id"]}?template=receipt">Receipt</a> — set the default in Settings &gt; Invoice Templates. Use browser Print (Ctrl+P) to print.</p></div>')
        return self.send_html(200, page(user, biz, f"Invoice {sale['invoice_no']}", body))

    def view_invoices(self, user, biz, cur, parts, method):
        b = user["business_id"]
        if len(parts) == 2 and parts[1].isdigit() and method == "GET":
            qs = parse_qs(urlparse(self.path).query)
            return self.invoice_view(user, biz, cur, int(parts[1]), template=qs.get("template", [None])[0])
        if len(parts) == 1 and method == "GET":
            qs = parse_qs(urlparse(self.path).query)
            qterm = qs.get("q", [""])[0].strip().lower()
            status = qs.get("status", ["all"])[0]
            if qs.get("export") == ["csv"]:
                rows = q("""SELECT s.*, c.name AS cname FROM sales s LEFT JOIN customers c ON c.id=s.customer_id
                            WHERE s.business_id=? ORDER BY s.id DESC""", (b,))
                out = io.StringIO()
                w = csv.writer(out)
                w.writerow(["invoice", "customer", "date", "total", "paid", "due"])
                for r in rows:
                    w.writerow([r["invoice_no"], r["cname"] or "Walk-in", r["sale_date"], r["total"], r["paid"], r["due"]])
                return self.send_file(out.getvalue().encode(), "invoices.csv", "text/csv")
            rows = q("""SELECT s.*, c.name AS cname FROM sales s LEFT JOIN customers c ON c.id=s.customer_id
                        WHERE s.business_id=? ORDER BY s.id DESC""", (b,))
            if qterm:
                rows = [r for r in rows if qterm in r["invoice_no"].lower() or qterm in (r["cname"] or "").lower()]
            if status == "paid":
                rows = [r for r in rows if r["due"] <= 0]
            elif status == "due":
                rows = [r for r in rows if r["due"] > 0]
            rh = "".join(f"<tr><td><a href='/invoices/{r['id']}'>{esc(r['invoice_no'])}</a></td><td>{esc(r['cname'] or 'Walk-in')}</td>"
                         f"<td>{esc(r['sale_date'])}</td><td>{money(r['total'], cur)}</td><td>{money(r['paid'], cur)}</td><td>{money(r['due'], cur)}</td></tr>" for r in rows)
            filt = ('<div class="card"><form method="get" action="/invoices">'
                    f'<input type="search" name="q" placeholder="Search invoice/customer" value="{esc(qterm)}"> '
                    '<select name="status"><option value="all">All</option><option value="paid">Paid</option><option value="due">Due</option></select> '
                    '<button type="submit">Filter</button> <a class="btn secondary" href="/invoices?export=csv">Export CSV</a></form></div>')
            return self.send_html(200, page(user, biz, "Invoices", filt + '<div class="card">' + table(["Invoice", "Customer", "Date", "Total", "Paid", "Due"], rh) + '</div>'))
        raise NotFound("Invoice page not found")

    def view_purchases(self, user, biz, cur, parts, method):
        b = user["business_id"]
        if len(parts) == 1 and method == "GET":
            qs = parse_qs(urlparse(self.path).query)
            qterm = qs.get("q", [""])[0].strip().lower()
            rows = q("""SELECT p.*, s.name AS sname FROM purchases p LEFT JOIN suppliers s ON s.id=p.supplier_id
                        WHERE p.business_id=? ORDER BY p.id DESC""", (b,))
            total_pur = sum(r["total"] for r in rows)
            if qterm:
                rows = [r for r in rows if qterm in (r["sname"] or "").lower() or qterm in f"pur-{r['id']:04d}" or qterm == str(r["id"])]
            def _pstatus(r):
                if r["due"] <= 0:
                    return '<span class="pill paid">Paid</span>'
                if r["paid"] > 0:
                    return '<span class="pill partial">Partial</span>'
                return '<span class="pill due">Due</span>'
            rh = "".join(f"<tr><td><a href='/purchases/{r['id']}'>PUR-{r['id']:04d}</a></td><td>{esc(r['sname'] or '-')}</td><td>{esc(r['purchase_date'])}</td>"
                         f"<td class='pr'>{money(r['total'], cur)}</td><td>{money(r['due'], cur)}</td><td>{_pstatus(r)}</td></tr>" for r in rows)
            search = (f'<form method="get" action="/purchases"><div class="bd-search">{icon("search", 18)}<input type="text" name="q" placeholder="Search purchase…" value="{esc(qterm)}"><button type="submit" class="btn secondary">Search</button></div></form>')
            body = (f'<div class="card"><h3>Total Purchase</h3><p style="font-size:20px;font-weight:900;color:#16a34a">{money(total_pur, cur)}</p>{search}'
                    + table(["Purchase", "Supplier", "Date", "Total", "Due", "Status"], rh) + '</div>')
            if user["role"] in ("owner", "admin"):
                body += '<div class="card noprint"><a class="btn btn-block" href="/purchases/new">+ New Purchase</a></div>'
            return self.send_html(200, page(user, biz, "Purchases", body))
        if len(parts) == 2 and parts[1].isdigit():
            pur = scoped_row("purchases", int(parts[1]), b)
            if method == "POST":
                self.require_role(user, "owner", "admin")
                form = self.form()
                self.check_csrf(user, form)
                action = self.fv(form, "action")
                if action == "receive":
                    if pur["status"] == "draft":
                        for it in q("SELECT * FROM purchase_items WHERE purchase_id=?", (pur["id"],)):
                            exec_db("UPDATE products SET stock=stock+? WHERE id=?", (it["qty"], it["product_id"]))
                            exec_db("INSERT INTO stock_movements(business_id,product_id,kind,qty,note,move_date,created_at) VALUES(?,?,?,?,?,?,?)",
                                    (b, it["product_id"], "purchase", it["qty"], f"Purchase #{pur['id']} received", today(), now_iso()))
                        exec_db("UPDATE purchases SET status='received' WHERE id=?", (pur["id"],))
                    return self.redirect(f"/purchases/{pur['id']}")
                if action == "cancel":
                    if pur["status"] == "received":
                        for it in q("SELECT * FROM purchase_items WHERE purchase_id=?", (pur["id"],)):
                            exec_db("UPDATE products SET stock=stock-? WHERE id=?", (it["qty"], it["product_id"]))
                    exec_db("UPDATE purchases SET status='cancelled' WHERE id=?", (pur["id"],))
                    return self.redirect(f"/purchases/{pur['id']}")
                if action == "return-item":
                    items_all = q("SELECT * FROM purchase_items WHERE purchase_id=?", (pur["id"],))
                    item = next((it for it in items_all if str(it["id"]) == self.fv(form, "item_id")), None)
                    if not item:
                        return self.send_html(200, page(user, biz, "Purchase", '<div class="card err">Item not found.</div>'))
                    rqty = float(self.fv(form, "qty") or 0)
                    if rqty <= 0 or rqty > item["qty"]:
                        return self.send_html(200, page(user, biz, "Purchase", '<div class="card err">Return quantity must be between 1 and the purchased quantity.</div>'))
                    refund = rqty * item["price"]
                    ret_id = exec_db("INSERT INTO purchase_returns(business_id,purchase_id,total,note,return_date,created_at) VALUES(?,?,?,?,?,?)",
                                     (b, pur["id"], refund, self.fv(form, "reason").strip(), today(), now_iso()))
                    exec_db("INSERT INTO purchase_return_items(business_id,return_id,product_id,qty,reason,refund) VALUES(?,?,?,?,?,?)",
                            (b, ret_id, item["product_id"], rqty, self.fv(form, "reason").strip(), refund))
                    exec_db("UPDATE products SET stock=stock-? WHERE id=?", (rqty, item["product_id"]))
                    new_due = max(0.0, pur["due"] - refund)
                    new_paid = pur["paid"] - max(0.0, refund - pur["due"])
                    exec_db("UPDATE purchases SET due=?, paid=? WHERE id=?", (new_due, max(0.0, new_paid), pur["id"]))
                    return self.redirect(f"/purchases/{pur['id']}")
                amt = float(self.fv(form, "amount") or 0)
                if amt <= 0:
                    return self.send_html(200, page(user, biz, "Purchase", '<div class="card err">Amount must be positive.</div>'))
                if action == "return":
                    exec_db("INSERT INTO purchase_returns(business_id,purchase_id,total,note,return_date,created_at) VALUES(?,?,?,?,?,?)",
                            (b, pur["id"], amt, self.fv(form, "note").strip(), today(), now_iso()))
                    new_due = max(0.0, pur["due"] - amt)
                    new_paid = pur["paid"] - max(0.0, amt - pur["due"])
                    exec_db("UPDATE purchases SET due=?, paid=? WHERE id=?", (new_due, max(0.0, new_paid), pur["id"]))
                elif action == "pay":
                    exec_db("INSERT INTO purchase_payments(business_id,purchase_id,amount,method,pay_date,created_at) VALUES(?,?,?,?,?,?)",
                            (b, pur["id"], amt, self.fv(form, "method").strip(), today(), now_iso()))
                    exec_db("UPDATE purchases SET paid=?, due=? WHERE id=?",
                            (pur["paid"] + amt, max(0.0, pur["due"] - amt), pur["id"]))
                else:
                    raise NotFound("Unknown purchase action")
                return self.redirect(f"/purchases/{pur['id']}")
            sup = q("SELECT name FROM suppliers WHERE id=?", (pur["supplier_id"],), one=True) if pur["supplier_id"] else None
            items = q("SELECT * FROM purchase_items WHERE purchase_id=?", (pur["id"],))
            ih = "".join(f"<tr><td>{esc(i['product_name'])}</td><td>{i['qty']}</td><td>{money(i['price'], cur)}</td><td>{money(i['amount'], cur)}</td></tr>" for i in items)
            rets = q("SELECT * FROM purchase_returns WHERE business_id=? AND purchase_id=? ORDER BY id DESC", (b, pur["id"]))
            rh2 = "".join(f"<tr><td>{esc(r['return_date'])}</td><td>{money(r['total'], cur)}</td><td>{esc(r['note'])}</td></tr>" for r in rets)
            pays = q("SELECT * FROM purchase_payments WHERE business_id=? AND purchase_id=? ORDER BY id DESC", (b, pur["id"]))
            ph2 = "".join(f"<tr><td>{esc(r['pay_date'])}</td><td>{money(r['amount'], cur)}</td><td>{esc(r['method'])}</td></tr>" for r in pays)
            body = (f'<div class="card"><h3>Purchase #{pur["id"]}</h3><p>Status: <b>{esc(pur["status"])}</b><br>Supplier: {esc(sup["name"]) if sup else "-"}<br>Date: {esc(pur["purchase_date"])}<br>Total: {money(pur["total"], cur)} | Paid: {money(pur["paid"], cur)} | Due: {money(pur["due"], cur)}</p>'
                    + table(["Item", "Qty", "Price", "Amount"], ih) + '</div>'
                    '<div class="card"><h3>Purchase returns</h3>' + (table(["Date", "Amount", "Note"], rh2) if rets else "<p>None yet.</p>") + '</div>'
                    '<div class="card"><h3>Purchase payments</h3>' + (table(["Date", "Amount", "Method"], ph2) if pays else "<p>None yet.</p>") + '</div>')
            if user["role"] in ("owner", "admin"):
                iopts = "".join(f'<option value="{i["id"]}">{esc(i["product_name"])} (qty {i["qty"]})</option>' for i in items)
                body += ('<div class="card"><h3>Receive / Cancel</h3>' + form_start(f"/purchases/{pur['id']}", user["_csrf"])
                         + '<input type="hidden" name="action" value="receive"><button type="submit">Mark received (adds stock, draft only)</button></form>'
                         + form_start(f"/purchases/{pur['id']}", user["_csrf"])
                         + '<input type="hidden" name="action" value="cancel"><button type="submit" class="danger">Cancel purchase (reverses stock if received)</button></form></div>'
                         '<div class="card"><h3>Return a purchased item</h3>' + form_start(f"/purchases/{pur['id']}", user["_csrf"])
                         + '<input type="hidden" name="action" value="return-item">'
                         + f'<p><label>Item</label><br><select name="item_id" required>{iopts}</select></p>'
                         + field("Return quantity", "qty", "", "number", True) + field("Reason", "reason")
                         + '<button type="submit">Record item return</button></form></div>')
                body += ('<div class="card"><h3>Record purchase return (amount only)</h3>' + form_start(f"/purchases/{pur['id']}", user["_csrf"])
                         + '<input type="hidden" name="action" value="return">'
                         + field("Return amount", "amount", "", "number", True) + field("Note", "note")
                         + '<button type="submit">Record return</button></form></div>'
                         '<div class="card"><h3>Record purchase payment</h3>' + form_start(f"/purchases/{pur['id']}", user["_csrf"])
                         + '<input type="hidden" name="action" value="pay">'
                         + field("Payment amount", "amount", "", "number", True) + field("Method", "method")
                         + '<button type="submit">Record payment</button></form></div>')
            return self.send_html(200, page(user, biz, f"Purchase #{pur['id']}", body))
        if len(parts) == 2 and parts[1] == "new":
            if method == "POST":
                self.require_role(user, "owner", "admin")
                form = self.form()
                self.check_csrf(user, form)
                items, err = self._lines_form(form, user)
                if err:
                    return self.send_html(200, page(user, biz, "New Purchase", f'<div class="card err">{esc(err)}</div>'))
                total = sum(qty * price for _, qty, price, _ in items)
                paid = max(0.0, min(float(self.fv(form, "paid") or 0), total))
                sup = self.fv(form, "supplier_id")
                sup_id = int(sup) if sup else None
                if sup_id:
                    scoped_row("suppliers", sup_id, b)
                pur_id = exec_db("""INSERT INTO purchases(business_id,supplier_id,total,paid,due,
                                    purchase_date,created_at) VALUES(?,?,?,?,?,?,?)""",
                                 (b, sup_id, total, paid, total - paid, today(), now_iso()))
                for prod, qty, price, _ in items:
                    exec_db("INSERT INTO purchase_items(purchase_id,product_id,product_name,qty,price,amount) VALUES(?,?,?,?,?,?)",
                            (pur_id, prod["id"], prod["name"], qty, price, qty * price))
                    exec_db("UPDATE products SET stock=stock+? WHERE id=?", (qty, prod["id"]))
                    exec_db("INSERT INTO stock_movements(business_id,product_id,kind,qty,note,move_date,created_at) VALUES(?,?,?,?,?,?,?)",
                            (b, prod["id"], "purchase", qty, f"Purchase #{pur_id}", today(), now_iso()))
                return self.redirect("/purchases")
            self.require_role(user, "owner", "admin")
            products = q("SELECT * FROM products WHERE business_id=? ORDER BY name", (b,))
            sups = q("SELECT id, name FROM suppliers WHERE business_id=? ORDER BY name", (b,))
            sopts = '<option value="">-</option>' + "".join(f'<option value="{s["id"]}">{esc(s["name"])}</option>' for s in sups)
            row, js = self._line_picker(products, cur)
            body = ('<div class="card">' + form_start("/purchases/new", user["_csrf"])
                    + f'<p><label>Supplier</label><br><select name="supplier_id">{sopts}</select></p>'
                    + '<div id="lines">' + row + '</div>'
                    + '<p><button type="button" onclick="addLine()">+ Add line</button></p>'
                    + field("Paid amount", "paid", "0", "number")
                    + '<button type="submit">Save purchase</button></form></div>')
            return self.send_html(200, page(user, biz, "New Purchase", body, extra=js))
        raise NotFound("Purchase page not found")

    def view_payments(self, user, biz, cur, parts, method):
        b = user["business_id"]
        if method == "POST":
            self.require_role(user, "owner", "admin")
            form = self.form()
            self.check_csrf(user, form)
            etype = self.fv(form, "entity_type")
            eid = int(self.fv(form, "customer_id") if etype == "customer" else self.fv(form, "supplier_id") or 0)
            if etype == "customer":
                scoped_row("customers", eid, b)
            else:
                scoped_row("suppliers", eid, b)
            amount = float(self.fv(form, "amount") or 0)
            if amount <= 0:
                return self.send_html(200, page(user, biz, "Payments", '<div class="card err">Amount must be positive.</div>'))
            exec_db("""INSERT INTO payments(business_id,entity_type,entity_id,amount,method,note,pay_date,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (b, etype, eid, amount, self.fv(form, "method").strip(), self.fv(form, "note").strip(), today(), now_iso()))
            if etype == "customer":
                apply_customer_payment(b, eid, amount)
            else:
                apply_supplier_payment(b, eid, amount)
            return self.redirect("/payments")
        rows = q("SELECT * FROM payments WHERE business_id=? ORDER BY id DESC", (b,))
        rh = ""
        for r in rows:
            if r["entity_type"] == "customer":
                nm = q("SELECT name FROM customers WHERE id=?", (r["entity_id"],), one=True)
            else:
                nm = q("SELECT name FROM suppliers WHERE id=?", (r["entity_id"],), one=True)
            rh += (f"<tr><td>{esc(r['pay_date'])}</td><td>{esc(r['entity_type'])}</td>"
                   f"<td>{esc(nm['name']) if nm else '-'}</td><td>{money(r['amount'], cur)}</td><td>{esc(r['method'])}</td></tr>")
        custs = q("SELECT id, name FROM customers WHERE business_id=? ORDER BY name", (b,))
        sups = q("SELECT id, name FROM suppliers WHERE business_id=? ORDER BY name", (b,))
        copts = "".join(f'<option value="{c["id"]}">{esc(c["name"])}</option>' for c in custs)
        sopts = "".join(f'<option value="{s["id"]}">{esc(s["name"])}</option>' for s in sups)
        body = ('<div class="card">' + table(["Date", "Type", "Party", "Amount", "Method"], rh) + '</div>')
        if user["role"] in ("owner", "admin"):
            body += ('<div class="card"><h3>Record payment (reduces outstanding due)</h3>'
                     + form_start("/payments", user["_csrf"])
                     + '<p><label>Type</label><br><select name="entity_type"><option value="customer">Customer (received)</option><option value="supplier">Supplier (paid)</option></select></p>'
                     + f'<p><label>Customer</label><br><select name="customer_id">{copts}</select></p>'
                     + f'<p><label>Supplier</label><br><select name="supplier_id">{sopts}</select></p>'
                     + field("Amount", "amount", "", "number", True) + field("Method (cash/bKash/bank)", "method")
                     + field("Note", "note") + '<button type="submit">Record payment</button></form></div>')
        return self.send_html(200, page(user, biz, "Payments", body))

    def view_expenses(self, user, biz, cur, parts, method):
        b = user["business_id"]
        if method == "POST":
            self.require_role(user, "owner", "admin")
            form = self.form()
            self.check_csrf(user, form)
            exec_db("""INSERT INTO expenses(business_id,title,category,amount,method,expense_date,note,created_at)
                       VALUES(?,?,?,?,?,?,?,?)""",
                    (b, self.fv(form, "title").strip(), self.fv(form, "category").strip(),
                     float(self.fv(form, "amount") or 0), self.fv(form, "method").strip(), today(),
                     self.fv(form, "note").strip(), now_iso()))
            return self.redirect("/expenses")
        qs = parse_qs(urlparse(self.path).query)
        qterm = qs.get("q", [""])[0].strip().lower()
        cat_f = qs.get("cat", ["all"])[0]
        rows = q("SELECT * FROM expenses WHERE business_id=? ORDER BY id DESC", (b,))
        cats = sorted({r["category"] for r in rows if r["category"]})
        if qterm:
            rows = [r for r in rows if qterm in r["title"].lower() or qterm in (r["category"] or "").lower()]
        if cat_f != "all":
            rows = [r for r in rows if (r["category"] or "") == cat_f]
        rh = "".join(f"<tr><td><span style='display:inline-flex;gap:8px;align-items:center'><span class='tile-ic amber'>{icon('wallet', 18)}</span>{esc(r['title'])}</span></td><td>{esc(r['category'])}</td><td>{esc(r['expense_date'])}</td><td class='pr'>{money(r['amount'], cur)}</td></tr>" for r in rows)
        pills = "".join(f'<a class="{"on" if cat_f == v else ""}" href="/expenses?cat={esc(v)}{("&amp;q=" + esc(qterm)) if qterm else ""}">{esc(l)}</a>'
                        for v, l in [("all", "All")] + [(c, c) for c in cats])
        search = (f'<form method="get" action="/expenses"><input type="hidden" name="cat" value="{esc(cat_f)}"><div class="bd-search">{icon("search", 18)}<input type="text" name="q" placeholder="Search expenses…" value="{esc(qterm)}"><button type="submit" class="btn secondary">Search</button></div></form>'
                  f'<div class="bd-pills">{pills}</div>')
        body = f'<div class="card">{search}' + table(["Expense", "Category", "Date", "Amount"], rh) + '</div>'
        if user["role"] in ("owner", "admin"):
            body += '<div class="card noprint"><a class="btn btn-block" href="#new-expense">+ Add Expense</a></div>'
            body += ('<div class="card" id="new-expense"><h3>New expense</h3>' + form_start("/expenses", user["_csrf"])
                     + field("Title", "title", "", "text", True) + field("Category (Rent/Salary/Transport/Utility/Others)", "category")
                     + field("Amount", "amount", "", "number", True) + field("Method", "method") + field("Note", "note")
                     + '<button type="submit">Save expense</button></form></div>')
        return self.send_html(200, page(user, biz, "Expenses", body))



    # -- v2.1 pages: scan, profile, security, brands, income, dues, inventory extras
    def view_scan(self, user, biz, cur):
        body = ('<div class="card scanbox"><h3>Camera scan</h3>'
                '<p>Point the camera at a barcode. If the camera is denied or unavailable, use the text box below.</p>'
                '<video id="scan-video" autoplay playsinline muted style="display:none"></video>'
                '<p><button type="button" class="btn" onclick="startScan()">Start camera</button> '
                '<button type="button" class="btn secondary" onclick="stopScan()">Stop</button></p>'
                '<p id="scan-status" style="color:var(--muted2)"></p></div>'
                '<div class="card"><h3>Or enter the code</h3>'
                '<form method="get" action="/pos"><p><input type="text" name="code" placeholder="Barcode or IMEI, then Enter">'
                '<button type="submit">Find product</button></p></form></div>'
                "<script>var scanStream=null;async function startScan(){var st=document.getElementById('scan-status');var v=document.getElementById('scan-video');"
                "try{if(!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia){st.textContent='Camera is not available on this device. Use the text box below.';return;}"
                "scanStream=await navigator.mediaDevices.getUserMedia({video:{facingMode:'environment'}});v.srcObject=scanStream;v.style.display='block';st.textContent='Camera is on. Scan a barcode, then type the code below if it is not read automatically.';"
                "if('BarcodeDetector' in window){var det=new BarcodeDetector({formats:['ean_13','ean_8','code_128','qr_code']});"
                "var tick=async function(){if(!scanStream)return;try{var codes=await det.detect(v);if(codes.length){window.location='/pos?code='+encodeURIComponent(codes[0].rawValue);return;}}catch(e){}setTimeout(tick,500);};tick();}"
                "}catch(e){st.textContent='Camera permission was denied. Use the text box below.';}}"
                "function stopScan(){if(scanStream){scanStream.getTracks().forEach(function(t){t.stop();});scanStream=null;}var v=document.getElementById('scan-video');v.style.display='none';}</script>")
        return self.send_html(200, page(user, biz, "Scan", body))

    def view_profile(self, user, biz, cur, method):
        b = user["business_id"]
        if method == "POST":
            form = self.form()
            self.check_csrf(user, form)
            exec_db("UPDATE users SET name=? WHERE id=?", (self.fv(form, "name").strip(), user["id"]), touch=False)
            return self.redirect("/profile")
        initials = user.get("_initials", "?")
        disp = user["name"] or user["email"]
        pmenu = [("Edit Profile", "user", "#edit-profile"), ("Change Password", "lock", "/forgot"),
                 ("Security", "shield", "/security"), ("Notification Settings", "bell", "/settings#notif"),
                 ("App Settings", "settings", "/settings#app"), ("Language", "sparkles", "/settings#language")]
        pmenu_html = "".join(
            f'<a href="{href}"><span class="tile-ic">{icon(ic, 18)}</span><span class="lbl">{esc(n)}</span><span class="chev">&#8250;</span></a>'
            for n, ic, href in pmenu)
        body = (f'<div class="card"><div class="bd-hub"><div class="bd-avatar">{esc(initials)}</div>'
                f'<div><div style="font-size:18px;font-weight:900">{esc(disp)}</div>'
                f'<div style="color:#64748b;font-size:13px">{esc(user["role"])} · {esc(biz["name"])} ({esc(cur)})</div>'
                f'<div style="color:#64748b;font-size:13px">{esc(user["email"])}</div></div></div></div>'
                f'<div class="card"><div class="bd-menu">{pmenu_html}</div></div>'
                '<div class="card"><h3>My Profile</h3>'
                f'<p>Email: {esc(user["email"])}<br>Role: {esc(user["role"])}<br>Business: {esc(biz["name"])} ({esc(cur)})</p></div>'
                '<div class="card" id="edit-profile"><h3>Edit display name</h3>' + form_start("/profile", user["_csrf"])
                + field("Display name", "name", user["name"] or "")
                + '<button type="submit">Save</button></form></div>'
                '<div class="card"><p><a class="btn" href="/security">Security settings</a> '
                '<a class="btn secondary" href="/settings">Business settings</a></p></div>')
        return self.send_html(200, page(user, biz, "My Profile", body))

    def view_security(self, user, biz, cur, method):
        if method == "POST":
            form = self.form()
            self.check_csrf(user, form)
            if self.fv(form, "action") == "revoke-others":
                exec_db("DELETE FROM sessions WHERE user_id=? AND token_hash!=?", (user["id"], user["_token_hash"]), touch=False)
            return self.redirect("/security")
        rows = q("SELECT token_hash, expires FROM sessions WHERE user_id=? ORDER BY expires DESC", (user["id"],))
        rh = "".join(f"<tr><td>{'This device' if r['token_hash'] == user['_token_hash'] else 'Other device'}</td><td>{esc(r['expires'])}</td></tr>" for r in rows)
        body = ('<div class="card"><h3>Security</h3><p>Your account signs in with Firebase. To change your password, '
                'use the password-reset email (it works even while you are signed in here).</p>'
                '<p><a class="btn" href="/forgot">Send password-reset email</a></p></div>'
                '<div class="card"><h3>Active sessions</h3>' + table(["Device", "Expires"], rh) + '</div>'
                '<div class="card">' + form_start("/security", user["_csrf"])
                + '<input type="hidden" name="action" value="revoke-others">'
                + '<button type="submit" class="danger">Sign out all other devices</button></form></div>')
        return self.send_html(200, page(user, biz, "Security", body))

    def view_brands(self, user, biz, cur):
        b = user["business_id"]
        rows = q("SELECT COALESCE(brand,'') AS br, COUNT(*) AS n, COALESCE(SUM(stock),0) AS st FROM products WHERE business_id=? GROUP BY br ORDER BY n DESC", (b,))
        rh = "".join(f"<tr><td>{esc(r['br']) or '(no brand)'}</td><td>{r['n']}</td><td>{r['st']}</td></tr>" for r in rows)
        body = '<div class="card"><h3>Brands</h3>' + (table(["Brand", "Products", "Total stock"], rh) if rows else "<p>No brands yet — set a brand on a product page.</p>") + '</div>'
        return self.send_html(200, page(user, biz, "Brands", body))

    def view_income(self, user, biz, cur, parts, method):
        b = user["business_id"]
        if method == "POST":
            self.require_role(user, "owner", "admin")
            form = self.form()
            self.check_csrf(user, form)
            exec_db("INSERT INTO income_categories(business_id,name,note,created_at) VALUES(?,?,?,?)",
                    (b, self.fv(form, "name").strip(), self.fv(form, "note").strip(), now_iso()))
            return self.redirect("/income")
        rows = q("SELECT * FROM income_categories WHERE business_id=? ORDER BY name", (b,))
        rh = "".join(f"<tr><td>{esc(r['name'])}</td><td>{esc(r['note'])}</td></tr>" for r in rows)
        body = ('<div class="card"><h3>Income categories</h3>' + (table(["Category", "Note"], rh) if rows else "<p>No income categories yet.</p>") + '</div>'
                '<div class="card"><p>Income itself is the Balance Sheet page in Reports — it shows sales, inventory value, receivables and cash.</p>'
                '<p><a class="btn" href="/reports/balance-sheet">Open Income (Balance Sheet)</a></p></div>')
        if user["role"] in ("owner", "admin"):
            body += ('<div class="card"><h3>New income category</h3>' + form_start("/income", user["_csrf"])
                     + field("Name", "name", "", "text", True) + field("Note", "note")
                     + '<button type="submit">Save</button></form></div>')
        return self.send_html(200, page(user, biz, "Income Categories", body))

    def view_dues(self, user, biz, cur, kind):
        b = user["business_id"]
        if kind == "customer":
            rows = q("SELECT * FROM customers WHERE business_id=? ORDER BY name", (b,))
            rh = "".join(f"<tr><td><a href='/customers/{r['id']}'>{esc(r['name'])}</a></td><td>{money(customer_due(b, r['id']), cur)}</td></tr>" for r in rows)
            title, label = "Due List — Customers", "Customer"
        else:
            rows = q("SELECT * FROM suppliers WHERE business_id=? ORDER BY name", (b,))
            rh = "".join(f"<tr><td><a href='/suppliers/{r['id']}'>{esc(r['name'])}</a></td><td>{money(supplier_due(b, r['id']), cur)}</td></tr>" for r in rows)
            title, label = "Due List — Suppliers", "Supplier"
        body = '<div class="card"><h3>' + title + '</h3>' + table([label, "Due"], rh) + '</div>'
        return self.send_html(200, page(user, biz, title, body))

    def view_inventory_extra(self, user, biz, cur, kind, method):
        b = user["business_id"]
        if kind == "reconcile":
            if method == "POST":
                self.require_role(user, "owner", "admin")
                form = self.form()
                self.check_csrf(user, form)
                prod = scoped_row("products", int(self.fv(form, "product_id") or 0), b)
                counted = float(self.fv(form, "counted") or 0)
                exec_db("INSERT INTO stock_audits(business_id,product_id,counted,system_qty,note,audit_date,created_at) VALUES(?,?,?,?,?,?,?)",
                        (b, prod["id"], counted, prod["stock"], self.fv(form, "note").strip(), today(), now_iso()))
                if self.fv(form, "apply") == "1":
                    exec_db("UPDATE products SET stock=? WHERE id=?", (counted, prod["id"]))
                return self.redirect("/inventory/reconcile")
            rows = q("SELECT * FROM products WHERE business_id=? ORDER BY name", (b,))
            opts = "".join(f'<option value="{r["id"]}">{esc(r["name"])} (system {r["stock"]})</option>' for r in rows)
            audits = q("""SELECT a.*, p.name AS pname FROM stock_audits a JOIN products p ON p.id=a.product_id
                          WHERE a.business_id=? ORDER BY a.id DESC LIMIT 30""", (b,))
            ah = "".join(f"<tr><td>{esc(a['audit_date'])}</td><td>{esc(a['pname'])}</td><td>{a['system_qty']}</td><td>{a['counted']}</td><td>{a['counted'] - a['system_qty']}</td></tr>" for a in audits)
            body = ('<div class="card"><h3>Stock reconciliation audit</h3><p>Count a product on the shelf, compare with the system quantity, and optionally apply the counted quantity.</p>'
                    + (table(["Date", "Product", "System", "Counted", "Difference"], ah) if audits else "<p>No audits yet.</p>") + '</div>')
            if user["role"] in ("owner", "admin"):
                body += ('<div class="card"><h3>New count</h3>' + form_start("/inventory/reconcile", user["_csrf"])
                         + f'<p><label>Product</label><br><select name="product_id" required>{opts}</select></p>'
                         + field("Counted quantity", "counted", "", "number", True) + field("Note", "note")
                         + '<p><label><input type="checkbox" name="apply" value="1" style="width:auto"> Apply counted quantity to stock</label></p>'
                         + '<button type="submit">Record audit</button></form></div>')
            return self.send_html(200, page(user, biz, "Stock Reconciliation", body))
        # imei-transfer
        if method == "POST":
            self.require_role(user, "owner", "admin")
            form = self.form()
            self.check_csrf(user, form)
            row = scoped_row("imeis", int(self.fv(form, "imei_id") or 0), b)
            floc, tloc = self.fv(form, "from_loc").strip(), self.fv(form, "to_loc").strip()
            if not floc or not tloc or floc == tloc:
                return self.send_html(200, page(user, biz, "IMEI Transfer", '<div class="card err">Two different locations are required.</div>'))
            exec_db("INSERT INTO imei_movements(business_id,imei_id,kind,note,move_date,created_at) VALUES(?,?,?,?,?,?)",
                    (b, row["id"], "transfer", f"{floc} -> {tloc}: {self.fv(form, 'note').strip()}", today(), now_iso()))
            return self.redirect("/inventory/imei-transfer")
        rows = q("""SELECT i.*, p.name AS pname FROM imeis i JOIN products p ON p.id=i.product_id
                    WHERE i.business_id=? AND i.status='in-stock' ORDER BY i.id DESC""", (b,))
        opts = "".join(f'<option value="{r["id"]}">{esc(r["imei"])} — {esc(r["pname"])}</option>' for r in rows)
        moves = q("""SELECT m.*, i.imei AS imei FROM imei_movements m JOIN imeis i ON i.id=m.imei_id
                     WHERE m.business_id=? AND m.kind='transfer' ORDER BY m.id DESC LIMIT 30""", (b,))
        mh = "".join(f"<tr><td>{esc(m['move_date'])}</td><td>{esc(m['imei'])}</td><td>{esc(m['note'])}</td></tr>" for m in moves)
        body = ('<div class="card"><h3>IMEI transfers</h3>' + (table(["Date", "IMEI", "Note"], mh) if moves else "<p>No IMEI transfers yet.</p>") + '</div>')
        if user["role"] in ("owner", "admin"):
            body += ('<div class="card"><h3>Transfer an IMEI</h3>' + form_start("/inventory/imei-transfer", user["_csrf"])
                     + f'<p><label>IMEI (in stock)</label><br><select name="imei_id" required>{opts}</select></p>'
                     + field("From location", "from_loc", "Main", "text", True) + field("To location", "to_loc", "", "text", True) + field("Note", "note")
                     + '<button type="submit">Record transfer</button></form></div>')
        return self.send_html(200, page(user, biz, "IMEI Transfer", body))

    def view_expenses_overview(self, user, biz, cur):
        b = user["business_id"]
        rows = q("SELECT COALESCE(category,'') AS cat, COUNT(*) AS n, COALESCE(SUM(amount),0) AS tot FROM expenses WHERE business_id=? GROUP BY cat ORDER BY tot DESC", (b,))
        tot = sum(r["tot"] for r in rows) or 1
        rh = "".join(f"<tr><td>{esc(r['cat']) or '(no category)'}</td><td>{r['n']}</td><td>{money(r['tot'], cur)}</td><td>{r['tot'] / tot * 100:.1f}%</td></tr>" for r in rows)
        bars = "".join(f"<p>{esc(r['cat']) or '(no category)'} — {money(r['tot'], cur)} ({r['tot'] / tot * 100:.0f}%)</p><div style='background:#e2e8f0;border-radius:8px;height:14px'><div style='background:#16a34a;height:14px;border-radius:8px;width:{r['tot'] / tot * 100:.0f}%'></div></div>" for r in rows)
        body = ('<div class="card"><h3>Expenses overview</h3>' + (table(["Category", "Count", "Amount", "Share"], rh) if rows else "<p>No expenses yet.</p>") + '</div>'
                '<div class="card"><h3>By category</h3>' + (bars or "<p>No expenses yet.</p>") + '</div>')
        return self.send_html(200, page(user, biz, "Expenses Overview", body))


    # -- v2 pages: POS, Health, Labh AI, Search, Notifications, Import
    def _pos_lines(self, form, user):
        b = user["business_id"]
        items = []
        for i in range(10):
            pid = self.fv(form, f"product_{i}")
            if not pid:
                continue
            prod = scoped_row("products", int(pid), b)
            qty = float(self.fv(form, f"qty_{i}") or 0)
            if qty <= 0:
                return None, "Quantity must be positive."
            ptype = self.fv(form, f"ptype_{i}") or "selling"
            price = self.fv(form, f"price_{i}")
            price = float(price) if price else price_for(prod, ptype)
            disc = float(self.fv(form, f"disc_{i}") or 0)
            items.append((prod, qty, price, ptype, disc))
        if not items:
            return None, "Add at least one item."
        return items, None

    def view_pos(self, user, biz, cur, parts, method):
        b = user["business_id"]
        if len(parts) == 2 and parts[1] == "receipt":
            raise NotFound("Receipt needs a sale id")
        if len(parts) == 3 and parts[1] == "receipt" and parts[2].isdigit():
            sale = scoped_row("sales", int(parts[2]), b)
            qs = parse_qs(urlparse(self.path).query)
            template = qs.get("template", [None])[0]
            return self.invoice_view(user, biz, cur, sale["id"], template=template)
        if method == "POST":
            form = self.form()
            self.check_csrf(user, form)
            items, err = self._pos_lines(form, user)
            if err:
                return self.send_html(200, page(user, biz, "Quick Sale POS", f'<div class="card err">{esc(err)}</div>'))
            for prod, qty, _pr, _pt, _dc in items:
                if prod["stock"] < qty:
                    return self.send_html(200, page(user, biz, "Quick Sale POS",
                        f'<div class="card err">Not enough stock for {esc(prod["name"])} (have {prod["stock"]}).</div>'))
            subtotal = sum(qty * price for _, qty, price, _, _ in items)
            line_disc = sum(disc for _, qty, price, _, disc in items)
            disc_mode = self.fv(form, "discount_mode") or "percent"
            disc_pct = float(self.fv(form, "discount") or 0)
            if disc_mode == "amount":
                discount = disc_pct + line_disc
            else:
                discount = subtotal * disc_pct / 100.0 + line_disc
            discount = min(discount, subtotal)
            tax, total = apply_tax(subtotal - discount)
            ptype_pay = self.fv(form, "payment_type") or "cash"
            if ptype_pay == "cash":
                paid = total
            elif ptype_pay == "due":
                paid = 0.0
            else:
                paid = max(0.0, min(float(self.fv(form, "paid") or 0), total))
            cust = self.fv(form, "customer_id")
            cust_id = int(cust) if cust else None
            if cust_id:
                scoped_row("customers", cust_id, b)
            sale_id = exec_db("""INSERT INTO sales(business_id,customer_id,invoice_no,total,paid,due,
                                discount,tax_amount,payment_type,sale_date,created_at,salesperson,payment_method) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                              (b, cust_id, next_invoice_no(b), total, paid, total - paid,
                               discount, tax, ptype_pay, today(), now_iso(),
                               self.fv(form, "salesperson").strip(),
                               self.fv(form, "payment_method") or ptype_pay))
            code = self.fv(form, "code").strip()
            imei_row = None
            if code:
                imei_row = q("SELECT * FROM imeis WHERE business_id=? AND imei=? AND status='in-stock'", (b, code), one=True)
            for prod, qty, price, ptype, disc in items:
                exec_db("INSERT INTO sale_items(sale_id,product_id,product_name,qty,price,amount,cost_price,price_type,discount) VALUES(?,?,?,?,?,?,?,?,?)",
                        (sale_id, prod["id"], prod["name"], qty, price, qty * price, prod["purchase_price"], ptype, disc))
                exec_db("UPDATE products SET stock=stock-? WHERE id=?", (qty, prod["id"]))
                exec_db("INSERT INTO stock_movements(business_id,product_id,kind,qty,note,move_date,created_at) VALUES(?,?,?,?,?,?,?)",
                        (b, prod["id"], "sale", qty, f"POS Sale #{sale_id}", today(), now_iso()))
            if imei_row:
                exec_db("UPDATE imeis SET status='sold' WHERE id=?", (imei_row["id"],))
                exec_db("INSERT INTO imei_movements(business_id,imei_id,kind,note,move_date,created_at) VALUES(?,?,?,?,?,?)",
                        (b, imei_row["id"], "sold", f"POS Sale #{sale_id}", today(), now_iso()))
            return self.redirect(f"/pos/receipt/{sale_id}")
        if method != "GET" or len(parts) != 1:
            raise NotFound("POS page not found")
        qs = parse_qs(urlparse(self.path).query)
        code = qs.get("code", [""])[0].strip()
        imei_mode = qs.get("imei_mode", [""])[0] == "1"
        preselect = None
        if qs.get("preselect", [""])[0].isdigit():
            preselect = int(qs["preselect"][0])
        code_note = ""
        if code:
            by_imei = q("SELECT * FROM imeis WHERE business_id=? AND imei=? AND status='in-stock'", (b, code), one=True) if imei_mode else None
            by_barcode = None if imei_mode else q("SELECT * FROM products WHERE business_id=? AND barcode=?", (b, code), one=True)
            if not by_imei and not by_barcode:
                by_barcode = q("SELECT * FROM products WHERE business_id=? AND barcode=?", (b, code), one=True)
            if not by_barcode and not by_imei:
                by_imei = q("SELECT * FROM imeis WHERE business_id=? AND imei=? AND status='in-stock'", (b, code), one=True)
            if by_barcode:
                preselect = by_barcode["id"]
                code_note = f'<div class="card ok">Barcode matched: {esc(by_barcode["name"])}</div>'
            elif by_imei:
                preselect = by_imei["product_id"]
                code_note = f'<div class="card ok">IMEI matched product #{by_imei["product_id"]} — it will be marked sold on completion.</div>'
            else:
                code_note = '<div class="card err">No product or in-stock IMEI matched that code.</div>'
        products = q("SELECT * FROM products WHERE business_id=? ORDER BY name", (b,))
        ps = qs.get("ps", [""])[0].strip().lower()
        if ps:
            products = [pr for pr in products if ps in pr["name"].lower() or ps in (pr["sku"] or "").lower() or ps in (pr["barcode"] or "").lower()]
        custs = q("SELECT id, name FROM customers WHERE business_id=? ORDER BY name", (b,))
        copts = '<option value="">Walk-in</option>' + "".join(f'<option value="{c["id"]}">{esc(c["name"])}</option>' for c in custs)
        popts = ""
        for pr in products:
            sel = " selected" if preselect and pr["id"] == preselect else ""
            popts += f'<option value="{pr["id"]}"{sel}>{esc(pr["name"])} - {money(pr["selling_price"], cur)} (stock {pr["stock"]})</option>'
        def _pills(i):
            out = ""
            for t in PRICE_TYPES:
                out += f'<label style="display:inline-block;margin-right:6px"><input type="radio" name="ptype_{i}" value="{t}" style="width:auto"{" checked" if t == "selling" else ""}> {t}</label>'
            return out
        lines = ""
        for i in range(3):
            lines += (f'<p><select name="product_{i}"><option value="">- product -</option>{popts}</select> '
                      f'Qty <input type="number" step="any" name="qty_{i}" value="1" style="width:80px"> '
                      f'Price (blank = price type) <input type="number" step="any" name="price_{i}" style="width:120px"> '
                      f'Item discount <input type="number" step="any" name="disc_{i}" value="0" style="width:90px"></p>'
                      f'<p style="font-size:12px">Price type: {_pills(i)}</p>')
        catalog = "".join(
            f'<div class="card prod"><b>{esc(pr["name"])}</b><p>{money(pr["selling_price"], cur)} &middot; stock {pr["stock"]}</p>'
            f'<p><a class="btn" href="/pos?preselect={pr["id"]}">+ Add</a></p></div>' for pr in products) or "<p>No products match.</p>"
        pm_opts = "".join(f'<option value="{m}">{esc(m)}</option>' for m in payment_methods_setting())
        disabled = "" if products else " disabled"
        body = (code_note + '<div class="card"><h3>Quick Sale Counter</h3></div>'
                '<div class="card"><h3>1 &middot; Barcode / SKU Quick Scan</h3>'
                '<form method="get" action="/pos"><p><input type="text" name="code" placeholder="Barcode or IMEI, then Enter" value="'
                + esc(code) + '">'
                + f'<label style="display:block;margin-top:8px"><input type="checkbox" name="imei_mode" value="1" style="width:auto"{" checked" if imei_mode else ""}> IMEI mode (match IMEI first)</label>'
                + '<button type="submit">Find product</button></p></form>'
                + '<p><a class="btn secondary" href="/scan">Open camera scan</a></p></div>'
                '<div class="card"><h3>2 &middot; Product Catalog</h3>'
                '<form method="get" action="/pos"><p><input type="search" name="ps" placeholder="Search catalog" value="' + esc(ps) + '"><button type="submit">Search</button></p></form>'
                + '<div class="catalog">' + catalog + '</div></div>'
                '<div class="card">' + form_start("/pos", user["_csrf"])
                + '<h3>3 &middot; Current Cart</h3>'
                + f'<p><label>Customer</label><br><select name="customer_id">{copts}</select></p>'
                + f'<p><label>Salesperson</label><br><input type="text" name="salesperson" placeholder="Salesperson name (optional)"></p>'
                + f'<input type="hidden" name="code" value="{esc(code)}">'
                + lines
                + '<h3>4 &middot; Checkout &amp; Payment</h3>'
                + '<p><label>Discount Method</label><br><select name="discount_mode"><option value="percent">% Percent</option><option value="amount">Amount</option></select></p>'
                + field("Discount (% or amount, see method)", "discount", "0", "number")
                + '<p style="font-size:12px;color:#64748b">Note: the live site caps a POS discount at 5%. This build allows more, but a discount above 5% is unusual — double-check it before completing.</p>'
                + '<p><label>Payment Type</label><br><select name="payment_type"><option value="cash">Cash (full)</option><option value="due">Due (full)</option><option value="cash_due">Cash &amp; Due</option></select></p>'
                + f'<p><label>Payment Method</label><br><select name="payment_method">{pm_opts}</select></p>'
                + field("Paid Amount (for Cash & Due)", "paid", "0", "number")
                + f'<p>Total Payable is calculated on completion (subtotal - discount + tax). Tax &amp; VAT from Settings is applied automatically when enabled.</p>'
                + f'<button type="submit"{disabled}>Complete Quick Sale</button></form></div>')
        return self.send_html(200, page(user, biz, "Quick Sale POS", body))

    def view_health(self, user, biz, cur):
        b = user["business_id"]
        h = compute_health(b)
        rh = "".join(f"<li>{esc(r)}</li>" for r in h["recs"])
        body = ('<div class="grid">'
                f'<div class="card"><div>Health Index</div><div class="stat">{h["score"]} / 100</div><div><span class="badge">{esc(h["status"])}</span></div></div>'
                f'<div class="card"><div>Sales (30 days)</div><div class="stat">{money(h["sales30"], cur)}</div><div>Previous 30 days: {money(h["prev30"], cur)}</div></div>'
                f'<div class="card"><div>Profit (30 days)</div><div class="stat">{money(h["profit30"], cur)}</div><div>Expenses: {money(h["expenses30"], cur)}</div></div>'
                f'<div class="card"><div>Customer dues</div><div class="stat">{money(h["dues"], cur)}</div><div>Low-stock products: {h["low"]} / {h["products"]}</div></div></div>'
                f'<div class="card"><h3>Recommendations</h3><ul>{rh}</ul>'
                '<p><i>Computed locally from your own sales, dues, stock and expenses — no internet, no AI service.</i></p></div>')
        return self.send_html(200, page(user, biz, "Business Health", body))

    def ai_answer(self, user, biz, cur, question):
        b = user["business_id"]
        bn = get_setting("lang") == "bn" or any("\u0980" <= ch <= "\u09ff" for ch in question)
        ql = question.lower()
        t = today()
        m30 = (date.today() - timedelta(days=30)).isoformat()
        rev = q("SELECT COALESCE(SUM(total),0), COALESCE(SUM(due),0), COUNT(*) FROM sales WHERE business_id=? AND sale_date>=?", (b, m30), one=True)
        exps = q("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE business_id=? AND expense_date>=?", (b, m30), one=True)[0]
        cogs = 0.0
        for si in q("SELECT si.qty, COALESCE(si.cost_price, p.purchase_price, 0) AS cp FROM sale_items si JOIN sales s ON s.id=si.sale_id LEFT JOIN products p ON p.id=si.product_id WHERE s.business_id=? AND s.sale_date>=?", (b, m30)):
            cogs += si["qty"] * (si["cp"] or 0)
        profit = rev[0] - cogs - exps
        top = q("""SELECT si.product_name, COALESCE(SUM(si.qty),0) AS qt, COALESCE(SUM(si.amount),0) AS amt
                   FROM sale_items si JOIN sales s ON s.id=si.sale_id WHERE s.business_id=?
                   GROUP BY si.product_name ORDER BY amt DESC LIMIT 3""", (b,))
        top_txt = ", ".join(f"{r['product_name']} ({r['qt']} pcs, {money(r['amt'], cur)})" for r in top) or "no sales yet"
        low = q("SELECT name, stock FROM products WHERE business_id=? AND stock<=low_stock ORDER BY stock LIMIT 5", (b,))
        low_txt = ", ".join(f"{r['name']} ({r['stock']})" for r in low) or "none"
        dues = due_aging(b, "customer")
        due_total = sum(sum(v["buckets"].values()) for v in dues)
        due_txt = ", ".join(f"{v['name']} ({money(sum(v['buckets'].values()), cur)})" for v in sorted(dues, key=lambda v: -sum(v["buckets"].values()))[:3]) or "none"
        if "audit" in ql or "\u0985\u09a1\u09bf\u099f" in ql:
            ans = (f"Full audit (last 30 days): Sales {money(rev[0], cur)} ({rev[2]} sales), "
                   f"COGS {money(cogs, cur)}, Expenses {money(exps, cur)}, Profit {money(profit, cur)}. "
                   f"Outstanding customer dues {money(due_total, cur)}. Top products: {top_txt}. Low stock: {low_txt}.")
            if bn:
                ans = (f"পূর্ণ অডিট (গত ৩০ দিন): বিক্রি {money(rev[0], cur)} ({rev[2]}টি), "
                       f"খরচ {money(exps, cur)}, লাভ {money(profit, cur)}। বকেয়া {money(due_total, cur)}। "
                       f"সেরা প্রোডাক্ট: {top_txt}। কম স্টক: {low_txt}।")
            return ans
        if any(k in ql for k in ("profit", "লাভ")):
            return (f"Last 30 days profit: {money(profit, cur)} (sales {money(rev[0], cur)} - COGS {money(cogs, cur)} - expenses {money(exps, cur)})." if not bn else
                    f"গত ৩০ দিনের লাভ: {money(profit, cur)} (বিক্রি {money(rev[0], cur)} - খরচ {money(exps, cur)})।")
        if any(k in ql for k in ("sale", "বিক্রি", "বিক্রয়")):
            return (f"Last 30 days: {rev[2]} sales totalling {money(rev[0], cur)}; today check the Dashboard for today's figure." if not bn else
                    f"গত ৩০ দিনে {rev[2]}টি বিক্রি, মোট {money(rev[0], cur)}।")
        if any(k in ql for k in ("top", "সেরা", "বেশি")):
            return (f"Top products by revenue: {top_txt}." if not bn else f"সবচেয়ে বেশি বিক্রি হওয়া প্রোডাক্ট: {top_txt}।")
        if any(k in ql for k in ("due", "বকেয়া", "পাওনা")):
            return (f"Total customer dues: {money(due_total, cur)}. Largest: {due_txt}. Collect the oldest (90+ days) first." if not bn else
                    f"মোট বকেয়া: {money(due_total, cur)}। সবচেয়ে বেশি: {due_txt}। সবচেয়ে পুরনো বকেয়া আগে তুলুন।")
        if any(k in ql for k in ("stock", "স্টক")):
            return (f"Low-stock products: {low_txt}." if not bn else f"কম স্টকের প্রোডাক্ট: {low_txt}।")
        if any(k in ql for k in ("expense", "খরচ")):
            return (f"Last 30 days expenses: {money(exps, cur)}." if not bn else f"গত ৩০ দিনের খরচ: {money(exps, cur)}।")
        return ("Hello! I am Labh AI — a local assistant (no internet, no API). Ask me about profit, sales, top products, dues, low stock or expenses, or click Run Full Audit." if not bn else
                "আসসালামু আলাইকুম! আমি লাভ AI — লোকাল সহকারী (ইন্টারনেট/API ছাড়া)। লাভ, বিক্রি, সেরা প্রোডাক্ট, বকেয়া, কম স্টক বা খরচ সম্পর্কে জিজ্ঞেস করুন, অথবা Full Audit চালান।")

    def view_ai(self, user, biz, cur, parts, method):
        b = user["business_id"]
        ans = ""
        if method == "POST":
            form = self.form()
            self.check_csrf(user, form)
            qtext = self.fv(form, "question").strip()
            ans = self.ai_answer(user, biz, cur, qtext) if qtext else self.ai_answer(user, biz, cur, "audit")
        body = ('<div class="card"><h3>Labh AI — Your Business Assistant</h3>'
                '<p><i>Local and free: answers come only from your own data on this device. No API key, no internet call.</i></p>'
                '<div class="chat">' + esc(ans or "Ask a question below, or run a full audit.") + '</div>'
                + form_start("/ai", user["_csrf"])
                + '<p><input type="text" name="question" placeholder="e.g. What was my profit last month? / গত মাসে লাভ কত?"></p>'
                + '<button type="submit">Ask Labh AI</button></form>'
                + form_start("/ai", user["_csrf"])
                + '<input type="hidden" name="question" value="audit">'
                + '<button type="submit" class="secondary">Run Full Audit</button></form></div>'
                '<div class="card"><h3>Try asking</h3><ul><li>What is my profit?</li><li>Top products?</li>'
                '<li>Who owes me money? / বকেয়া কত?</li><li>Low stock items?</li></ul></div>')
        return self.send_html(200, page(user, biz, "Labh AI", body))

    def view_search(self, user, biz, cur):
        b = user["business_id"]
        qs = parse_qs(urlparse(self.path).query)
        term = qs.get("q", [""])[0].strip()
        if qs.get("live") == ["1"]:
            like = f"%{term}%"
            items = []
            for r in q("SELECT id, name FROM products WHERE business_id=? AND name LIKE ? LIMIT 5", (b, like)):
                items.append({"label": "Product: " + r["name"], "url": f"/products/{r['id']}"})
            for r in q("SELECT id, name FROM customers WHERE business_id=? AND name LIKE ? LIMIT 5", (b, like)):
                items.append({"label": "Customer: " + r["name"], "url": f"/customers/{r['id']}"})
            for r in q("SELECT id, invoice_no FROM sales WHERE business_id=? AND invoice_no LIKE ? LIMIT 5", (b, like)):
                items.append({"label": "Sale: " + r["invoice_no"], "url": f"/sales/{r['id']}"})
            for r in q("SELECT id FROM purchases WHERE business_id=? AND CAST(id AS TEXT) LIKE ? LIMIT 5", (b, like)):
                items.append({"label": f"Purchase #{r['id']}", "url": f"/purchases/{r['id']}"})
            return self.send_file(json.dumps(items).encode(), "search.json", "application/json")
        like = f"%{term}%"
        prods = q("SELECT * FROM products WHERE business_id=? AND (name LIKE ? OR sku LIKE ? OR barcode LIKE ?)", (b, like, like, like)) if term else []
        custs = q("SELECT * FROM customers WHERE business_id=? AND name LIKE ?", (b, like)) if term else []
        sups = q("SELECT * FROM suppliers WHERE business_id=? AND name LIKE ?", (b, like)) if term else []
        sales = q("SELECT * FROM sales WHERE business_id=? AND invoice_no LIKE ?", (b, like)) if term else []
        imeis = q("SELECT * FROM imeis WHERE business_id=? AND imei LIKE ?", (b, like)) if term else []
        rh = ""
        for r in prods:
            rh += f"<tr><td>Product</td><td><a href='/products/{r['id']}'>{esc(r['name'])}</a></td><td>{esc(r['sku'])}</td></tr>"
        for r in custs:
            rh += f"<tr><td>Customer</td><td><a href='/customers/{r['id']}'>{esc(r['name'])}</a></td><td>{esc(r['phone'])}</td></tr>"
        for r in sups:
            rh += f"<tr><td>Supplier</td><td><a href='/suppliers/{r['id']}'>{esc(r['name'])}</a></td><td>{esc(r['phone'])}</td></tr>"
        for r in sales:
            rh += f"<tr><td>Invoice</td><td><a href='/sales/{r['id']}'>{esc(r['invoice_no'])}</a></td><td>{money(r['total'], cur)}</td></tr>"
        for r in imeis:
            rh += f"<tr><td>IMEI</td><td>{esc(r['imei'])}</td><td>{esc(r['status'])}</td></tr>"
        purch = q("SELECT * FROM purchases WHERE business_id=? AND CAST(id AS TEXT) LIKE ?", (b, like)) if term else []
        for r in purch:
            rh += f"<tr><td>Purchase</td><td><a href='/purchases/{r['id']}'>Purchase #{r['id']}</a></td><td>{money(r['total'], cur)}</td></tr>"
        body = f'<div class="card"><p>Results for: <b>{esc(term)}</b></p>' + (table(["Type", "Name", "Detail"], rh) if rh else "<p>No results.</p>") + '</div>'
        return self.send_html(200, page(user, biz, "Search", body))

    def view_notifications(self, user, biz, cur, method):
        b = user["business_id"]
        if method == "POST":
            form = self.form()
            self.check_csrf(user, form)
            if self.fv(form, "action") == "mark-all-read":
                exec_db("UPDATE notifications SET is_read=1 WHERE business_id=?", (b,), touch=False)
            elif self.fv(form, "action") == "mark-read":
                row = scoped_row("notifications", int(self.fv(form, "notif_id") or 0), b)
                exec_db("UPDATE notifications SET is_read=1 WHERE id=?", (row["id"],), touch=False)
            return self.redirect("/notifications")
        seed_notifications(b, cur)
        rows = q("SELECT * FROM notifications WHERE business_id=? ORDER BY id DESC LIMIT 100", (b,))
        items = ""
        for r in rows:
            cls = "" if r["is_read"] else "notif-unread"
            mark = "" if r["is_read"] else (f"<form method='post' action='/notifications' style='display:inline'>"
                f"<input type='hidden' name='csrf' value='{esc(user['_csrf'])}'>"
                f"<input type='hidden' name='action' value='mark-read'>"
                f"<input type='hidden' name='notif_id' value='{r['id']}'><button type='submit' class='secondary'>Mark read</button></form>")
            items += f"<li class='{cls}'><b>{esc(r['title'])}</b> — {esc(r['body'])} <small>({esc(fmt_dt(r['created_at']))})</small> {mark}</li>"
        body = ('<div class="card"><h3>Notifications</h3>'
                + (f"<ul>{items}</ul>" if rows else "<p>All clear — no alerts.</p>")
                + f"<p>{form_start('/notifications', user['_csrf'])}<input type='hidden' name='action' value='mark-all-read'>"
                + '<button type="submit">Mark all as read</button></form></p></div>')
        return self.send_html(200, page(user, biz, "Notifications", body))

    def view_import(self, user, biz, cur, parts, method):
        b = user["business_id"]
        if len(parts) == 2 and parts[1] == "template" and method == "GET":
            qs = parse_qs(urlparse(self.path).query)
            if qs.get("type") == ["imei"]:
                data = "imei,product name or id\n123456789012345,Phone X\n"
                return self.send_file(data.encode(), "imei-template.csv", "text/csv")
            data = "name,sku,barcode,purchase_price,selling_price,stock,brand\nPhone X,PX1,123456,100,150,10,BrandA\n"
            return self.send_file(data.encode(), "products-template.csv", "text/csv")
        if method == "POST":
            ctype = self.headers.get("Content-Type") or ""
            if "multipart/form-data" in ctype:
                fields, fname, fbytes = self.multipart()
                if fields.get("csrf") != user["_csrf"]:
                    raise Forbidden("Bad CSRF token.")
                if fname.lower().endswith((".xlsx", ".xls")):
                    return self.send_html(200, page(user, biz, "Bulk Import",
                        '<div class="card err">Excel (.xlsx) import is not available in this stdlib build — download the CSV template below and import that instead.</div><p><a class="btn" href="/import">Back to Import</a></p>'))
                # Reuse the paste-CSV path by feeding the file text through a synthetic form
                text = fbytes.decode(errors="replace")
                kind = fields.get("kind", "products")
                return self._import_csv(user, biz, cur, kind, text)
            # fall through to paste-CSV handling below
            self.require_role(user, "owner", "admin")
            form = self.form()
            self.check_csrf(user, form)
            kind = self.fv(form, "kind")
            text = self.fv(form, "csv")
            added, skipped = 0, 0
            try:
                reader = csv.reader(io.StringIO(text))
                rows = [r for r in reader if r and any(c.strip() for c in r)]
            except Exception:
                rows = []
            if kind == "products":
                for r in rows:
                    try:
                        name = r[0].strip()
                        if not name or name.lower() == "name":
                            continue
                        sku = r[1].strip() if len(r) > 1 else ""
                        barcode = r[2].strip() if len(r) > 2 else ""
                        pp = float(r[3]) if len(r) > 3 and r[3].strip() else 0
                        sp = float(r[4]) if len(r) > 4 and r[4].strip() else 0
                        stock = int(float(r[5])) if len(r) > 5 and r[5].strip() else 0
                        exec_db("INSERT INTO products(business_id,name,sku,barcode,purchase_price,selling_price,stock,low_stock,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                                (b, name, sku, barcode, pp, sp, stock, 5, now_iso()))
                        added += 1
                    except Exception:
                        skipped += 1
            else:
                for r in rows:
                    try:
                        imei = r[0].strip()
                        if not imei or imei.lower() == "imei":
                            continue
                        prod = None
                        if len(r) > 1 and r[1].strip():
                            prod = q("SELECT * FROM products WHERE business_id=? AND (name=? OR id=?)", (b, r[1].strip(), r[1].strip()), one=True)
                        if not prod:
                            skipped += 1
                            continue
                        if q("SELECT id FROM imeis WHERE business_id=? AND imei=?", (b, imei), one=True):
                            skipped += 1
                            continue
                        exec_db("INSERT INTO imeis(business_id,product_id,imei,status,note,imei_date,created_at) VALUES(?,?,?,?,?,?,?)",
                                (b, prod["id"], imei, "in-stock", "", today(), now_iso()))
                        added += 1
                    except Exception:
                        skipped += 1
            return self.send_html(200, page(user, biz, "Bulk Import",
                f'<div class="card ok">Imported {added} row(s), skipped {skipped}.</div><p><a class="btn" href="/import">Back to Import</a></p>'))
        body = ('<div class="card"><h3>Bulk import (upload a CSV file)</h3>'
                f'<form method="post" action="/import" enctype="multipart/form-data">'
                f'<input type="hidden" name="csrf" value="{esc(user["_csrf"])}">'
                '<p><label>Type</label><br><select name="kind"><option value="products">Products</option><option value="imei">IMEI</option></select></p>'
                '<p><label>CSV file</label><br><input type="file" name="csv_file" accept=".csv"></p>'
                '<button type="submit">Upload &amp; import</button></form>'
                '<p>Templates: <a href="/import/template?type=products">Products CSV template</a> &middot; <a href="/import/template?type=imei">IMEI CSV template</a><br>'
                'Excel (.xlsx) is not imported in this stdlib build — save the sheet as CSV first.</p></div>'
                '<div class="card"><h3>Bulk import (paste CSV)</h3>' + form_start("/import", user["_csrf"])
                + '<p><label>Type</label><br><select name="kind"><option value="products">Products (name,sku,barcode,purchase_price,selling_price,stock)</option><option value="imei">IMEI (imei,product name or id)</option></select></p>'
                + '<p><label>CSV text (one row per line)</label><br><textarea name="csv" rows="8" placeholder="Phone X,PX1,123456,100,150,10"></textarea></p>'
                + '<button type="submit">Import</button></form></div>')
        return self.send_html(200, page(user, biz, "Bulk Import", body))

    def _import_csv(self, user, biz, cur, kind, text):
        b = user["business_id"]
        added, skipped = 0, 0
        try:
            reader = csv.reader(io.StringIO(text))
            rows = [r for r in reader if r and any(c.strip() for c in r)]
        except Exception:
            rows = []
        if kind == "products":
            for r in rows:
                try:
                    name = r[0].strip()
                    if not name or name.lower() == "name":
                        continue
                    sku = r[1].strip() if len(r) > 1 else ""
                    barcode = r[2].strip() if len(r) > 2 else ""
                    pp = float(r[3]) if len(r) > 3 and r[3].strip() else 0
                    sp = float(r[4]) if len(r) > 4 and r[4].strip() else 0
                    stock = int(float(r[5])) if len(r) > 5 and r[5].strip() else 0
                    brand = r[6].strip() if len(r) > 6 else ""
                    exec_db("INSERT INTO products(business_id,name,sku,barcode,purchase_price,selling_price,stock,low_stock,created_at,brand) VALUES(?,?,?,?,?,?,?,?,?,?)",
                            (b, name, sku, barcode, pp, sp, stock, 5, now_iso(), brand))
                    added += 1
                except Exception:
                    skipped += 1
        else:
            for r in rows:
                try:
                    imei = r[0].strip()
                    if not imei or imei.lower() == "imei":
                        continue
                    prod = None
                    if len(r) > 1 and r[1].strip():
                        prod = q("SELECT * FROM products WHERE business_id=? AND (name=? OR id=?)", (b, r[1].strip(), r[1].strip()), one=True)
                    if not prod:
                        skipped += 1
                        continue
                    if q("SELECT id FROM imeis WHERE business_id=? AND imei=?", (b, imei), one=True):
                        skipped += 1
                        continue
                    exec_db("INSERT INTO imeis(business_id,product_id,imei,status,note,imei_date,created_at) VALUES(?,?,?,?,?,?,?)",
                            (b, prod["id"], imei, "in-stock", "", today(), now_iso()))
                    added += 1
                except Exception:
                    skipped += 1
        return self.send_html(200, page(user, biz, "Bulk Import",
            f'<div class="card ok">Imported {added} row(s), skipped {skipped}.</div><p><a class="btn" href="/import">Back to Import</a></p>'))

    def view_reports(self, user, biz, cur):
        b = user["business_id"]
        qs = parse_qs(urlparse(self.path).query)
        frm = qs.get("from", [""])[0] or "2000-01-01"
        to = qs.get("to", [""])[0] or "2999-12-31"
        sales = q("SELECT COUNT(*), COALESCE(SUM(total),0), COALESCE(SUM(paid),0), COALESCE(SUM(due),0) FROM sales WHERE business_id=? AND sale_date BETWEEN ? AND ?", (b, frm, to), one=True)
        purch = q("SELECT COALESCE(SUM(total),0) FROM purchases WHERE business_id=? AND purchase_date BETWEEN ? AND ?", (b, frm, to), one=True)[0]
        exps = q("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE business_id=? AND expense_date BETWEEN ? AND ?", (b, frm, to), one=True)[0]
        profit = sales[1] - purch - exps
        custs = q("SELECT id, name FROM customers WHERE business_id=? ORDER BY name", (b,))
        sups = q("SELECT id, name FROM suppliers WHERE business_id=? ORDER BY name", (b,))
        crh = "".join(f"<tr><td>{esc(c['name'])}</td><td>{money(customer_due(b, c['id']), cur)}</td></tr>" for c in custs)
        srh = "".join(f"<tr><td>{esc(s['name'])}</td><td>{money(supplier_due(b, s['id']), cur)}</td></tr>" for s in sups)
        inv = q("SELECT name, stock, purchase_price FROM products WHERE business_id=? ORDER BY name", (b,))
        irh = "".join(f"<tr><td>{esc(p['name'])}</td><td>{p['stock']}</td><td>{money(p['stock'] * p['purchase_price'], cur)}</td></tr>" for p in inv)
        # Cash flow is counted from sales/purchases paid fields only. A payment
        # recorded in /payments is applied onto a sale/purchase (its paid/due
        # fields move), so adding payments again would double-count it.
        purch_paid = q("SELECT COALESCE(SUM(paid),0) FROM purchases WHERE business_id=? AND purchase_date BETWEEN ? AND ?", (b, frm, to), one=True)[0]
        cash_in = sales[2]
        cash_out = purch_paid + exps
        groups = [
            ("Sales", [("sales", "Sales"), ("sales-daily", "Daily sales"),
                       ("sales-weekly", "Weekly sales"), ("sales-monthly", "Monthly sales"),
                       ("discount", "Discount")]),
            ("Inventory", [("inventory", "Inventory"), ("stock-valuation", "Stock valuation"),
                           ("stock-movement", "Stock movement"), ("imei", "IMEI")]),
            ("Customers & suppliers", [("customer-dues", "Customer dues"),
                                       ("supplier-dues", "Supplier dues"),
                                       ("receivables", "Receivables"), ("payables", "Payables")]),
            ("Purchases", [("returns", "Purchase returns")]),
            ("Profit", [("gross-profit", "Gross profit"), ("real-profit", "Real profit"),
                        ("profit-loss", "Profit/Loss"),
                        ("profit-by-category", "Profit by category"),
                        ("profit-by-product", "Profit by product"),
                        ("profit-by-brand", "Profit by brand"),
                        ("profit-by-customer", "Profit by customer")]),
            ("Finance", [("balance-sheet", "Balance sheet"), ("cash-flow", "Cash flow"),
                         ("expense-cost", "Expense cost")]),
            ("Other", [("user-activity", "User activity"),
                       ("business-analytics", "Business analytics")]),
        ]
        links = "".join(f'<h4 style="margin:14px 0 6px">{g}</h4><p>' +
                        "".join(f'<a class="btn" href="/reports/{s}">{n}</a> ' for s, n in items) + '</p>'
                        for g, items in groups)
        menu = [("Sales", "receipt", "sales"), ("Purchase", "truck", "/purchases"),
                ("Inventory", "package", "inventory"), ("Customer", "users", "customer-dues"),
                ("Supplier", "truck", "supplier-dues"), ("Expense", "wallet", "expense-cost"),
                ("Income", "chart", "balance-sheet"), ("Profit & Loss", "chart", "profit-loss"),
                ("Cash Flow", "money", "cash-flow"), ("Real Profit", "money", "real-profit"),
                ("Gross Profit", "chart", "gross-profit")]
        menu_html = "".join(
            f'<a href="{s if s.startswith("/") else "/reports/" + s}"><span class="tile-ic">{icon(ic, 18)}</span><span class="lbl">{esc(n)}</span><span class="chev">&#8250;</span></a>'
            for n, ic, s in menu)
        body = (f'<div class="card"><h3>Reports</h3><div class="bd-menu">{menu_html}</div></div>'
                f'<div class="card"><h3>All reports</h3>{links}</div>'
                f'<div class="card"><form method="get" action="/reports">From <input type="date" name="from" value="{esc(frm) if frm != "2000-01-01" else ""}"> '
                f'To <input type="date" name="to" value="{esc(to) if to != "2999-12-31" else ""}"> <button type="submit">Filter</button></form></div>'
                '<div class="card"><h3>Sales summary</h3>'
                f'<p>Sales: {sales[0]} | Total: {money(sales[1], cur)} | Paid: {money(sales[2], cur)} | Due: {money(sales[3], cur)}</p></div>'
                '<div class="card"><h3>Profit / Loss</h3>'
                f'<p>Sales {money(sales[1], cur)} - Purchases {money(purch, cur)} - Expenses {money(exps, cur)} = <b>{money(profit, cur)}</b></p></div>'
                '<div class="card"><h3>Cash flow</h3>'
                f'<p>Cash in (sales paid): {money(cash_in, cur)}<br>Cash out (supplier payments + expenses): {money(cash_out, cur)}<br>Net: <b>{money(cash_in - cash_out, cur)}</b></p></div>'
                '<div class="card"><h3>Inventory report</h3>' + table(["Product", "Stock", "Value (cost)"], irh) + '</div>'
                '<div class="card"><h3>Customer dues</h3>' + table(["Customer", "Due"], crh) + '</div>'
                '<div class="card"><h3>Supplier dues</h3>' + table(["Supplier", "Due"], srh) + '</div>')
        return self.send_html(200, page(user, biz, "Reports", body))

    # -- full report suite (each report is scoped to this business and
    #    computed from real SQLite sale/purchase/expense data)
    def _range(self):
        qs = parse_qs(urlparse(self.path).query)
        frm = qs.get("from", [""])[0] or "2000-01-01"
        to = qs.get("to", [""])[0] or "2999-12-31"
        return frm, to

    def _cogs(self, b, frm, to):
        return q("""SELECT COALESCE(SUM(si.qty * COALESCE(si.cost_price, p.purchase_price, 0)),0)
                    FROM sale_items si JOIN sales s ON s.id=si.sale_id
                    LEFT JOIN products p ON p.id=si.product_id
                    WHERE s.business_id=? AND s.sale_date BETWEEN ? AND ?""", (b, frm, to), one=True)[0]

    def _filter_form(self, frm, to, slug):
        return (f'<div class="card"><form method="get" action="/reports/{slug}">'
                f'From <input type="date" name="from" value="{esc(frm) if frm != "2000-01-01" else ""}"> '
                f'To <input type="date" name="to" value="{esc(to) if to != "2999-12-31" else ""}"> '
                '<button type="submit">Filter</button></form></div>')

    def view_report_detail(self, user, biz, cur, parts, method):
        b = user["business_id"]
        slug = parts[1]
        frm, to = self._range()
        filt = self._filter_form(frm, to, slug)
        if slug == "sales":
            qs = parse_qs(urlparse(self.path).query)
            rows = q("SELECT * FROM sales WHERE business_id=? AND sale_date BETWEEN ? AND ? ORDER BY id DESC", (b, frm, to))
            if qs.get("export") == ["csv"]:
                out = io.StringIO()
                w = csv.writer(out)
                w.writerow(["invoice", "date", "total", "paid", "due"])
                for r in rows:
                    w.writerow([r["invoice_no"], r["sale_date"], r["total"], r["paid"], r["due"]])
                return self.send_file(out.getvalue().encode(), "sales-report.csv", "text/csv")
            tot = q("SELECT COUNT(*), COALESCE(SUM(total),0), COALESCE(SUM(paid),0), COALESCE(SUM(due),0) FROM sales WHERE business_id=? AND sale_date BETWEEN ? AND ?", (b, frm, to), one=True)
            profit = tot[1] - self._cogs(b, frm, to)
            avg = tot[1] / tot[0] if tot[0] else 0
            date_pill = "All time" if (frm == "2000-01-01" and to == "2999-12-31") else f"{fmt_dt(frm)} - {fmt_dt(to)}"
            stats = (f'<div class="v23-stats">'
                     f'<div class="v23-stat"><div class="chip" style="background:#ECFDF5">{icon("chart", 20)}</div><div class="lbl">Total Sales</div><div class="val">{money(tot[1], cur)}</div></div>'
                     f'<div class="v23-stat"><div class="chip" style="background:#EFF6FF">{icon("money", 20)}</div><div class="lbl">Total Profit</div><div class="val">{money(profit, cur)}</div></div>'
                     f'<div class="v23-stat"><div class="chip" style="background:#F5F3FF">{icon("receipt", 20)}</div><div class="lbl">Total Invoice</div><div class="val">{tot[0]}</div></div>'
                     f'<div class="v23-stat"><div class="chip" style="background:#FFFBEB">{icon("wallet", 20)}</div><div class="lbl">Avg Order</div><div class="val">{money(avg, cur)}</div></div></div>')
            daily = {}
            for r in rows:
                daily[r["sale_date"]] = daily.get(r["sale_date"], 0) + r["total"]
            days = sorted(daily.items())[-14:]
            mx = max([v for _, v in days], default=1) or 1
            bars = "".join(f'<div class="bar"><i style="height:{max(2, int(v / mx * 100))}%"></i><span>{d[8:10]}/{d[5:7]}</span></div>' for d, v in days)
            chart = f'<div class="card"><h3>Sales by day</h3><div class="bd-bars">{bars}</div></div>' if days else ""
            rh = "".join(f"<tr><td>{esc(r['invoice_no'])}</td><td>{esc(r['sale_date'])}</td><td>{money(r['total'], cur)}</td><td>{money(r['paid'], cur)}</td><td>{money(r['due'], cur)}</td></tr>" for r in rows)
            exports = (f'<p><button type="button" class="btn" onclick="window.print()">Print</button> '
                       f'<a class="btn secondary" href="/reports/sales?from={esc(frm)}&amp;to={esc(to)}&amp;export=csv">Export CSV</a></p>')
            body = (filt + f'<div class="card"><p><span class="pill paid">{esc(date_pill)}</span></p>{stats}{exports}'
                    + table(["Invoice", "Date", "Total", "Paid", "Due"], rh) + '</div>' + chart)
            return self.send_html(200, page(user, biz, "Sales report", body))
        if slug == "inventory":
            rows = q("SELECT * FROM products WHERE business_id=? ORDER BY name", (b,))
            rh = "".join(f"<tr><td>{esc(r['name'])}</td><td>{r['stock']}</td><td>{money(r['purchase_price'], cur)}</td><td>{money(r['stock'] * r['purchase_price'], cur)}</td><td>{money(r['stock'] * r['selling_price'], cur)}</td></tr>" for r in rows)
            cv = sum(r["stock"] * r["purchase_price"] for r in rows)
            sv = sum(r["stock"] * r["selling_price"] for r in rows)
            body = f'<div class="card"><p>Total stock value at cost: {money(cv, cur)} | at selling price: {money(sv, cur)}</p>' + table(["Product", "Stock", "Cost price", "Value (cost)", "Value (selling)"], rh) + '</div>'
            return self.send_html(200, page(user, biz, "Inventory report", body))
        if slug == "customer-dues":
            rows = q("SELECT * FROM customers WHERE business_id=? ORDER BY name", (b,))
            rh = "".join(f"<tr><td>{esc(r['name'])}</td><td>{money(r['opening_balance'], cur)}</td><td>{money(customer_due(b, r['id']), cur)}</td></tr>" for r in rows)
            tot = sum(customer_due(b, r["id"]) for r in rows)
            aging = due_aging(b, "customer")
            ah = "".join(f"<tr><td>{esc(a['name'])}</td><td>{money(a['buckets']['0-30'], cur)}</td><td>{money(a['buckets']['31-60'], cur)}</td><td>{money(a['buckets']['61-90'], cur)}</td><td>{money(a['buckets']['90+'], cur)}</td><td>{money(sum(a['buckets'].values()), cur)}</td></tr>" for a in aging)
            body = (f'<div class="card"><p>Total customer dues: {money(tot, cur)}</p>' + table(["Customer", "Opening balance", "Total due"], rh) + '</div>'
                    '<div class="card"><h3>Due aging (by invoice age)</h3>' + table(["Customer", "0-30 days", "31-60 days", "61-90 days", "90+ days", "Total"], ah) + '</div>')
            return self.send_html(200, page(user, biz, "Customer dues report", body))
        if slug == "supplier-dues":
            rows = q("SELECT * FROM suppliers WHERE business_id=? ORDER BY name", (b,))
            rh = "".join(f"<tr><td>{esc(r['name'])}</td><td>{money(r['opening_balance'], cur)}</td><td>{money(supplier_due(b, r['id']), cur)}</td></tr>" for r in rows)
            tot = sum(supplier_due(b, r["id"]) for r in rows)
            aging = due_aging(b, "supplier")
            ah = "".join(f"<tr><td>{esc(a['name'])}</td><td>{money(a['buckets']['0-30'], cur)}</td><td>{money(a['buckets']['31-60'], cur)}</td><td>{money(a['buckets']['61-90'], cur)}</td><td>{money(a['buckets']['90+'], cur)}</td><td>{money(sum(a['buckets'].values()), cur)}</td></tr>" for a in aging)
            body = (f'<div class="card"><p>Total supplier dues: {money(tot, cur)}</p>' + table(["Supplier", "Opening balance", "Total due"], rh) + '</div>'
                    '<div class="card"><h3>Due aging (by purchase age)</h3>' + table(["Supplier", "0-30 days", "31-60 days", "61-90 days", "90+ days", "Total"], ah) + '</div>')
            return self.send_html(200, page(user, biz, "Supplier dues report", body))
        if slug == "profit-loss":
            rev = q("SELECT COALESCE(SUM(total),0) FROM sales WHERE business_id=? AND sale_date BETWEEN ? AND ?", (b, frm, to), one=True)[0]
            cogs = self._cogs(b, frm, to)
            exps = q("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE business_id=? AND expense_date BETWEEN ? AND ?", (b, frm, to), one=True)[0]
            purch = q("SELECT COALESCE(SUM(total),0) FROM purchases WHERE business_id=? AND purchase_date BETWEEN ? AND ?", (b, frm, to), one=True)[0]
            body = filt + ('<div class="card"><h3>Profit / Loss (inventory-accurate)</h3>'
                f'<p>Revenue: {money(rev, cur)}<br>Cost of goods sold: {money(cogs, cur)}<br>Gross profit: {money(rev - cogs, cur)}<br>Expenses: {money(exps, cur)}<br><b>Net profit: {money(rev - cogs - exps, cur)}</b></p>'
                '<h3>Simple view (as originally specified)</h3>'
                f'<p>Sales {money(rev, cur)} - Purchases {money(purch, cur)} - Expenses {money(exps, cur)} = {money(rev - purch - exps, cur)}<br><i>Note: purchases buy stock, they are not all sold yet, so the inventory-accurate figure above is the correct profit.</i></p></div>')
            return self.send_html(200, page(user, biz, "Profit / Loss report", body))
        if slug == "balance-sheet":
            inv_val = q("SELECT COALESCE(SUM(stock * purchase_price),0) FROM products WHERE business_id=?", (b,), one=True)[0]
            recv = sum(customer_due(b, r["id"]) for r in q("SELECT id FROM customers WHERE business_id=?", (b,)))
            pay = sum(supplier_due(b, r["id"]) for r in q("SELECT id FROM suppliers WHERE business_id=?", (b,)))
            sales_paid = q("SELECT COALESCE(SUM(paid),0) FROM sales WHERE business_id=?", (b,), one=True)[0]
            purch_paid = q("SELECT COALESCE(SUM(paid),0) FROM purchases WHERE business_id=?", (b,), one=True)[0]
            exps_all = q("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE business_id=?", (b,), one=True)[0]
            cash = sales_paid - purch_paid - exps_all
            assets = inv_val + recv + cash
            body = ('<div class="card"><h3>Balance sheet (all time, this business only)</h3>'
                f'<p><b>Assets</b><br>Inventory at cost: {money(inv_val, cur)}<br>Receivables (customer dues): {money(recv, cur)}<br>Cash (sales paid - purchases paid - expenses): {money(cash, cur)}<br><b>Total assets: {money(assets, cur)}</b></p>'
                f'<p><b>Liabilities</b><br>Payables (supplier dues): {money(pay, cur)}<br><b>Total liabilities: {money(pay, cur)}</b></p>'
                f'<p><b>Equity (assets - liabilities): {money(assets - pay, cur)}</b></p></div>')
            return self.send_html(200, page(user, biz, "Balance sheet", body))
        if slug == "cash-flow":
            sin = q("SELECT COALESCE(SUM(paid),0) FROM sales WHERE business_id=? AND sale_date BETWEEN ? AND ?", (b, frm, to), one=True)[0]
            pout = q("SELECT COALESCE(SUM(paid),0) FROM purchases WHERE business_id=? AND purchase_date BETWEEN ? AND ?", (b, frm, to), one=True)[0]
            exps = q("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE business_id=? AND expense_date BETWEEN ? AND ?", (b, frm, to), one=True)[0]
            body = filt + ('<div class="card"><h3>Cash flow</h3>'
                f'<p>Cash in (sales paid): {money(sin, cur)}<br>Cash out: purchases paid {money(pout, cur)} + expenses {money(exps, cur)} = {money(pout + exps, cur)}<br><b>Net cash flow: {money(sin - pout - exps, cur)}</b></p>'
                '<p><i>Payments recorded in /payments are already reflected in the sales/purchases paid totals (they move paid/due), so they are not added again here — that would double-count.</i></p></div>')
            return self.send_html(200, page(user, biz, "Cash flow report", body))
        if slug == "receivables":
            rows = q("SELECT * FROM sales WHERE business_id=? AND due>0 AND sale_date BETWEEN ? AND ? ORDER BY id", (b, frm, to))
            rh = ""
            for r in rows:
                nm = q("SELECT name FROM customers WHERE id=?", (r["customer_id"],), one=True) if r["customer_id"] else None
                rh += f"<tr><td>{esc(r['invoice_no'])}</td><td>{esc(nm['name']) if nm else 'Walk-in'}</td><td>{esc(r['sale_date'])}</td><td>{money(r['due'], cur)}</td></tr>"
            tot = sum(r["due"] for r in rows)
            body = filt + f'<div class="card"><p>Total receivables (outstanding sales): {money(tot, cur)}</p>' + table(["Invoice", "Customer", "Date", "Due"], rh) + '</div>'
            return self.send_html(200, page(user, biz, "Receivables report", body))
        if slug == "payables":
            rows = q("SELECT * FROM purchases WHERE business_id=? AND due>0 AND purchase_date BETWEEN ? AND ? ORDER BY id", (b, frm, to))
            rh = ""
            for r in rows:
                nm = q("SELECT name FROM suppliers WHERE id=?", (r["supplier_id"],), one=True) if r["supplier_id"] else None
                rh += f"<tr><td>{r['id']}</td><td>{esc(nm['name']) if nm else '-'}</td><td>{esc(r['purchase_date'])}</td><td>{money(r['due'], cur)}</td></tr>"
            tot = sum(r["due"] for r in rows)
            body = filt + f'<div class="card"><p>Total payables (outstanding purchases): {money(tot, cur)}</p>' + table(["Purchase #", "Supplier", "Date", "Due"], rh) + '</div>'
            return self.send_html(200, page(user, biz, "Payables report", body))
        if slug == "imei":
            if method == "POST":
                self.require_role(user, "owner", "admin")
                form = self.form()
                self.check_csrf(user, form)
                if self.fv(form, "action") == "sold":
                    row = scoped_row("imeis", int(self.fv(form, "imei_id") or 0), b)
                    exec_db("UPDATE imeis SET status='sold' WHERE id=?", (row["id"],))
                else:
                    prod = scoped_row("products", int(self.fv(form, "product_id") or 0), b)
                    imei = self.fv(form, "imei").strip()
                    if imei and not q("SELECT id FROM imeis WHERE business_id=? AND imei=?", (b, imei), one=True):
                        exec_db("INSERT INTO imeis(business_id,product_id,imei,status,note,imei_date,created_at) VALUES(?,?,?,?,?,?,?)",
                                (b, prod["id"], imei, "in-stock", self.fv(form, "note").strip(), today(), now_iso()))
                return self.redirect("/reports/imei")
            rows = q("""SELECT i.*, p.name AS pname FROM imeis i JOIN products p ON p.id=i.product_id
                        WHERE i.business_id=? ORDER BY i.id DESC""", (b,))
            rh = "".join(
                f"<tr><td>{esc(r['imei'])}</td><td>{esc(r['pname'])}</td><td>{esc(r['status'])}</td><td>{esc(r['imei_date'])}</td>"
                f"<td>{'' if r['status'] == 'sold' else f'''<form method='post' action='/reports/imei'><input type='hidden' name='csrf' value='{esc(user['_csrf'])}'><input type='hidden' name='action' value='sold'><input type='hidden' name='imei_id' value='{r['id']}'><button type='submit'>Mark sold</button></form>'''}</td></tr>"
                for r in rows)
            prods = q("SELECT id, name FROM products WHERE business_id=? ORDER BY name", (b,))
            opts = "".join(f'<option value="{p["id"]}">{esc(p["name"])}</option>' for p in prods)
            body = ('<div class="card"><h3>IMEI / serial register</h3><p>Record a device IMEI when it arrives in stock; mark it sold when it leaves. This is the data the IMEI report reads.</p>'
                    + table(["IMEI", "Product", "Status", "Date", ""], rh) + '</div>')
            if user["role"] in ("owner", "admin"):
                body += ('<div class="card"><h3>Register IMEI</h3>' + form_start("/reports/imei", user["_csrf"])
                         + f'<p><label>Product</label><br><select name="product_id" required>{opts}</select></p>'
                         + field("IMEI / serial", "imei", "", "text", True) + field("Note", "note")
                         + '<button type="submit">Register</button></form></div>')
            return self.send_html(200, page(user, biz, "IMEI report", body))
        if slug == "expense-cost":
            rows = q("SELECT COALESCE(category,'') AS cat, COUNT(*), COALESCE(SUM(amount),0) FROM expenses WHERE business_id=? AND expense_date BETWEEN ? AND ? GROUP BY cat ORDER BY 3 DESC", (b, frm, to))
            tot = sum(r[2] for r in rows)
            rh = "".join(f"<tr><td>{esc(r[0]) or '(no category)'}</td><td>{r[1]}</td><td>{money(r[2], cur)}</td><td>{(r[2] / tot * 100 if tot else 0):.1f}%</td></tr>" for r in rows)
            body = filt + f'<div class="card"><p>Total expenses: {money(tot, cur)}</p>' + table(["Category", "Count", "Amount", "Share"], rh) + '</div>'
            return self.send_html(200, page(user, biz, "Expense-cost report", body))
        if slug == "gross-profit":
            rev = q("SELECT COALESCE(SUM(total),0) FROM sales WHERE business_id=? AND sale_date BETWEEN ? AND ?", (b, frm, to), one=True)[0]
            cogs = self._cogs(b, frm, to)
            margin = ((rev - cogs) / rev * 100 if rev else 0)
            body = filt + ('<div class="card"><h3>Gross profit</h3>'
                f'<p>Revenue: {money(rev, cur)}<br>Cost of goods sold: {money(cogs, cur)}<br><b>Gross profit: {money(rev - cogs, cur)} (margin {margin:.1f}%)</b><br><i>COGS uses the purchase price stored on each sale line at sale time.</i></p></div>')
            return self.send_html(200, page(user, biz, "Gross profit report", body))
        if slug == "real-profit":
            rev = q("SELECT COALESCE(SUM(total),0) FROM sales WHERE business_id=? AND sale_date BETWEEN ? AND ?", (b, frm, to), one=True)[0]
            cogs = self._cogs(b, frm, to)
            exps = q("SELECT COALESCE(SUM(amount),0) FROM expenses WHERE business_id=? AND expense_date BETWEEN ? AND ?", (b, frm, to), one=True)[0]
            body = filt + ('<div class="card"><h3>Real profit</h3>'
                f'<p>Revenue: {money(rev, cur)}<br>Less COGS: {money(cogs, cur)}<br>Gross profit: {money(rev - cogs, cur)}<br>Less expenses: {money(exps, cur)}<br><b>Real profit: {money(rev - cogs - exps, cur)}</b></p></div>')
            return self.send_html(200, page(user, biz, "Real profit report", body))
        if slug == "profit-by-category":
            rows = q("""SELECT COALESCE(c.name,'Uncategorized') AS cat,
                        COALESCE(SUM(si.amount),0) AS rev,
                        COALESCE(SUM(si.qty * COALESCE(si.cost_price, p.purchase_price, 0)),0) AS cost
                        FROM sale_items si JOIN sales s ON s.id=si.sale_id
                        LEFT JOIN products p ON p.id=si.product_id
                        LEFT JOIN categories c ON c.id=p.category_id
                        WHERE s.business_id=? AND s.sale_date BETWEEN ? AND ?
                        GROUP BY cat ORDER BY rev DESC""", (b, frm, to))
            rh = "".join(f"<tr><td>{esc(r['cat'])}</td><td>{money(r['rev'], cur)}</td><td>{money(r['cost'], cur)}</td><td>{money(r['rev'] - r['cost'], cur)}</td></tr>" for r in rows)
            body = filt + '<div class="card">' + table(["Category", "Revenue", "COGS", "Profit"], rh) + '</div>'
            return self.send_html(200, page(user, biz, "Profit by category report", body))
        if slug == "profit-by-product":
            rows = q("""SELECT si.product_name AS nm, COALESCE(SUM(si.amount),0) AS rev,
                        COALESCE(SUM(si.qty * COALESCE(si.cost_price, p.purchase_price, 0)),0) AS cost
                        FROM sale_items si JOIN sales s ON s.id=si.sale_id
                        LEFT JOIN products p ON p.id=si.product_id
                        WHERE s.business_id=? AND s.sale_date BETWEEN ? AND ?
                        GROUP BY nm ORDER BY rev DESC""", (b, frm, to))
            rh = "".join(f"<tr><td>{esc(r['nm'])}</td><td>{money(r['rev'], cur)}</td><td>{money(r['cost'], cur)}</td><td>{money(r['rev'] - r['cost'], cur)}</td></tr>" for r in rows)
            return self.send_html(200, page(user, biz, "Profit by product", filt + '<div class="card">' + table(["Product", "Revenue", "COGS", "Profit"], rh) + '</div>'))
        if slug == "profit-by-brand":
            rows = q("""SELECT COALESCE(p.brand,'') AS br, COALESCE(SUM(si.amount),0) AS rev,
                        COALESCE(SUM(si.qty * COALESCE(si.cost_price, p.purchase_price, 0)),0) AS cost
                        FROM sale_items si JOIN sales s ON s.id=si.sale_id
                        LEFT JOIN products p ON p.id=si.product_id
                        WHERE s.business_id=? AND s.sale_date BETWEEN ? AND ?
                        GROUP BY br ORDER BY rev DESC""", (b, frm, to))
            rh = "".join(f"<tr><td>{esc(r['br']) or '(no brand)'}</td><td>{money(r['rev'], cur)}</td><td>{money(r['cost'], cur)}</td><td>{money(r['rev'] - r['cost'], cur)}</td></tr>" for r in rows)
            return self.send_html(200, page(user, biz, "Profit by brand", filt + '<div class="card">' + table(["Brand", "Revenue", "COGS", "Profit"], rh) + '</div>'))
        if slug == "profit-by-customer":
            rows = q("""SELECT COALESCE(c.name,'Walk-in') AS nm,
                        COALESCE(SUM(si.amount),0) AS rev,
                        COALESCE(SUM(si.qty * COALESCE(si.cost_price, p.purchase_price, 0)),0) AS cost
                        FROM sale_items si JOIN sales s ON s.id=si.sale_id
                        LEFT JOIN products p ON p.id=si.product_id
                        LEFT JOIN customers c ON c.id=s.customer_id
                        WHERE s.business_id=? AND s.sale_date BETWEEN ? AND ?
                        GROUP BY nm ORDER BY rev DESC""", (b, frm, to))
            rh = "".join(f"<tr><td>{esc(r['nm'])}</td><td>{money(r['rev'], cur)}</td><td>{money(r['cost'], cur)}</td><td>{money(r['rev'] - r['cost'], cur)}</td></tr>" for r in rows)
            return self.send_html(200, page(user, biz, "Profit by customer", filt + '<div class="card">' + table(["Customer", "Revenue", "COGS", "Profit"], rh) + '</div>'))
        if slug in ("sales-daily", "sales-weekly", "sales-monthly"):
            fmt = {"sales-daily": "%Y-%m-%d", "sales-weekly": "%Y-W%W", "sales-monthly": "%Y-%m"}[slug]
            rows = q(f"""SELECT strftime('{fmt}', sale_date) AS per, COUNT(*) AS n, COALESCE(SUM(total),0) AS tot,
                        COALESCE(SUM(paid),0) AS paid FROM sales WHERE business_id=? AND sale_date BETWEEN ? AND ?
                        GROUP BY per ORDER BY per""", (b, frm, to))
            rh = "".join(f"<tr><td>{esc(r['per'])}</td><td>{r['n']}</td><td>{money(r['tot'], cur)}</td><td>{money(r['paid'], cur)}</td></tr>" for r in rows)
            return self.send_html(200, page(user, biz, {"sales-daily": "Daily sales", "sales-weekly": "Weekly sales", "sales-monthly": "Monthly sales"}[slug], filt + '<div class="card">' + table(["Period", "Sales", "Total", "Paid"], rh) + '</div>'))
        if slug == "discount":
            rows = q("SELECT invoice_no, sale_date, discount, total FROM sales WHERE business_id=? AND discount>0 AND sale_date BETWEEN ? AND ? ORDER BY id DESC", (b, frm, to))
            tot = sum(r["discount"] for r in rows)
            rh = "".join(f"<tr><td>{esc(r['invoice_no'])}</td><td>{esc(r['sale_date'])}</td><td>{money(r['discount'], cur)}</td><td>{money(r['total'], cur)}</td></tr>" for r in rows)
            return self.send_html(200, page(user, biz, "Discount report", filt + f'<div class="card"><p>Total discounts given: {money(tot, cur)}</p>' + table(["Invoice", "Date", "Discount", "Total"], rh) + '</div>'))
        if slug == "returns":
            rows = q("SELECT * FROM purchase_returns WHERE business_id=? AND return_date BETWEEN ? AND ? ORDER BY id DESC", (b, frm, to))
            tot = sum(r["total"] for r in rows)
            rh = "".join(f"<tr><td>{esc(r['return_date'])}</td><td>Purchase #{r['purchase_id']}</td><td>{money(r['total'], cur)}</td><td>{esc(r['note'])}</td></tr>" for r in rows)
            return self.send_html(200, page(user, biz, "Purchase returns report", filt + f'<div class="card"><p>Total purchase returns: {money(tot, cur)}</p>' + table(["Date", "Purchase", "Amount", "Note"], rh) + '</div>'))
        if slug == "stock-valuation":
            rows = q("SELECT name, COALESCE(brand,'') AS br, stock, purchase_price, selling_price FROM products WHERE business_id=? ORDER BY name", (b,))
            rh = "".join(f"<tr><td>{esc(r['name'])}</td><td>{esc(r['br'])}</td><td>{r['stock']}</td><td>{money(r['stock'] * r['purchase_price'], cur)}</td><td>{money(r['stock'] * r['selling_price'], cur)}</td></tr>" for r in rows)
            cv = sum(r["stock"] * r["purchase_price"] for r in rows); sv = sum(r["stock"] * r["selling_price"] for r in rows)
            return self.send_html(200, page(user, biz, "Stock valuation", f'<div class="card"><p>At cost: {money(cv, cur)} | At selling price: {money(sv, cur)}</p>' + table(["Product", "Brand", "Stock", "Value (cost)", "Value (selling)"], rh) + '</div>'))
        if slug == "stock-movement":
            rows = q("""SELECT m.*, p.name AS pname FROM stock_movements m JOIN products p ON p.id=m.product_id
                        WHERE m.business_id=? AND m.move_date BETWEEN ? AND ? ORDER BY m.id DESC LIMIT 200""", (b, frm, to))
            rh = "".join(f"<tr><td>{esc(r['move_date'])}</td><td>{esc(r['pname'])}</td><td>{esc(r['kind'])}</td><td>{r['qty']}</td><td>{esc(r['note'])}</td></tr>" for r in rows)
            return self.send_html(200, page(user, biz, "Stock movement", filt + '<div class="card">' + table(["Date", "Product", "Kind", "Qty", "Note"], rh) + '</div>'))
        if slug == "user-activity":
            rows = q("""SELECT u.email, u.role, COUNT(s.token_hash) AS sess, u.created_at FROM users u
                        LEFT JOIN sessions s ON s.user_id=u.id WHERE u.business_id=? GROUP BY u.id ORDER BY u.id""", (b,))
            rh = "".join(f"<tr><td>{esc(r['email'])}</td><td>{esc(r['role'])}</td><td>{r['sess']}</td><td>{esc(r['created_at'])}</td></tr>" for r in rows)
            body = ('<div class="card"><h3>User activity</h3><p>Note: LABH records sessions per user, not a per-action audit log — '
                    'so this report shows accounts and their active sessions, not every click.</p>'
                    + table(["User", "Role", "Active sessions", "Created"], rh) + '</div>')
            return self.send_html(200, page(user, biz, "User activity", body))
        if slug == "business-analytics":
            top_prod = q("""SELECT si.product_name AS nm, COALESCE(SUM(si.qty),0) AS qq, COALESCE(SUM(si.amount),0) AS rev
                            FROM sale_items si JOIN sales s ON s.id=si.sale_id WHERE s.business_id=? AND s.sale_date BETWEEN ? AND ?
                            GROUP BY nm ORDER BY rev DESC LIMIT 5""", (b, frm, to))
            top_cust = q("""SELECT COALESCE(c.name,'Walk-in') AS nm, COALESCE(SUM(s.total),0) AS rev FROM sales s
                            LEFT JOIN customers c ON c.id=s.customer_id WHERE s.business_id=? AND s.sale_date BETWEEN ? AND ?
                            GROUP BY nm ORDER BY rev DESC LIMIT 5""", (b, frm, to))
            monthly = q("""SELECT strftime('%Y-%m', sale_date) AS per, COALESCE(SUM(total),0) AS tot FROM sales
                           WHERE business_id=? AND sale_date BETWEEN ? AND ? GROUP BY per ORDER BY per""", (b, frm, to))
            body = (filt + '<div class="card"><h3>Top products</h3>' + table(["Product", "Qty", "Revenue"], "".join(f"<tr><td>{esc(r['nm'])}</td><td>{r['qq']}</td><td>{money(r['rev'], cur)}</td></tr>" for r in top_prod)) + '</div>'
                    '<div class="card"><h3>Top customers</h3>' + table(["Customer", "Sales"], "".join(f"<tr><td>{esc(r['nm'])}</td><td>{money(r['rev'], cur)}</td></tr>" for r in top_cust)) + '</div>'
                    '<div class="card"><h3>Monthly sales</h3>' + table(["Month", "Sales"], "".join(f"<tr><td>{esc(r['per'])}</td><td>{money(r['tot'], cur)}</td></tr>" for r in monthly)) + '</div>')
            return self.send_html(200, page(user, biz, "Business analytics", body))
        raise NotFound("Report not found")

def main():
    start_auto_backup_thread()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    server.daemon_threads = True
    print(f"LABH running at http://{HOST}:{PORT}  (Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass

if __name__ == "__main__":
    main()
