#!/usr/bin/env python3
"""Apply the DEMO-ONLY patch to a pristine copy of LABH app.py.

Every change is gated on LABH_DEMO=1, so the same file behaves
exactly like production when the flag is off. The patch:
  1. Bind host from LABH_HOST (Render needs 0.0.0.0; default stays
     127.0.0.1 for local runs).
  2. /demo-enter route + handler: one-click entry as the seeded demo
     user. No password, no Firebase, no Drive. 404 when demo is off.
  3. Login/register pages show an "Enter the demo" link instead of
     the Google button (Google could never verify on the demo
     domain, and no Firebase project is attached to the demo).
  4. Auto-sync forced OFF and Drive save disabled on demo (the demo
     server must never touch anyone's Google Drive).
  5. A yellow DEMO ribbon on every logged-in page.

Usage: python3 apply_demo_patch.py app.py   (edits in place)
"""
import sys


def once(src, old, new, label):
    assert src.count(old) == 1, f"anchor not unique/found: {label} (count={src.count(old)})"
    return src.replace(old, new, 1)


def main():
    path = sys.argv[1]
    src = open(path).read()
    assert "LABH_DEMO" not in src, "patch already applied?"

    src = once(src,
               'PORT = int(os.environ.get("LABH_PORT", "8000"))',
               'PORT = int(os.environ.get("LABH_PORT", "8000"))\n'
               'HOST = os.environ.get("LABH_HOST", "127.0.0.1")',
               "port line")
    src = once(src,
               'server = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)',
               'server = ThreadingHTTPServer((HOST, PORT), Handler)',
               "bind line")
    src = once(src,
               'print(f"LABH running at http://127.0.0.1:{PORT}  (Ctrl+C to stop)")',
               'print(f"LABH running at http://{HOST}:{PORT}  (Ctrl+C to stop)")',
               "print line")

    src = once(src,
               '    return get_setting("auto_sync") != "0"',
               '    if os.environ.get("LABH_DEMO") == "1":\n'
               '        return False\n'
               '    return get_setting("auto_sync") != "0"',
               "auto_sync gate")

    src = once(src,
               '    The browser route is NEVER auto-navigated to; the user must choose it."""\n'
               '    cfg = {k: FIREBASE_CONFIG.get(k, "") for k in ("apiKey", "authDomain", "projectId", "appId")}',
               '    The browser route is NEVER auto-navigated to; the user must choose it."""\n'
               '    if os.environ.get("LABH_DEMO") == "1":\n'
               '        # Demo site has no Firebase project: offer demo entry\n'
               '        # instead of a Google button that could never verify.\n'
               "        return ('<p style=\"text-align:center\">— or —</p>'\n"
               "                '<p><a class=\"btn\" href=\"/demo-enter\">Enter the demo (no sign-in needed)</a></p>'\n"
               "                '<p style=\"text-align:center;font-size:12px;color:#64748b\">Demo data only — nothing here touches a real business.</p>')\n"
               '    cfg = {k: FIREBASE_CONFIG.get(k, "") for k in ("apiKey", "authDomain", "projectId", "appId")}',
               "google button demo branch")

    src = once(src,
               '        form = self.form()\n        token = self.fv(form, "idToken")',
               '        if os.environ.get("LABH_DEMO") == "1":\n'
               '            return self.send_html(400, page(user, biz, "Backup",\n'
               "                '<div class=\"card err\">Google Drive is disabled on the demo site.</div>'\n"
               "                '<p><a class=\"btn\" href=\"/backup\">Back to Backup</a></p>'))\n"
               '        form = self.form()\n        token = self.fv(form, "idToken")',
               "drive-save demo block")

    src = once(src,
               '    try:\n        seed_notifications(user["business_id"], biz["currency"])',
               '    if os.environ.get("LABH_DEMO") == "1":\n'
               "        body = ('<div style=\"background:#f59e0b;color:#1c1917;text-align:center;font-weight:800;padding:8px;border-radius:12px;margin-bottom:12px\">'\n"
               "                'DEMO WEBSITE — demo data only. Nothing here is a real business, and nothing syncs to Google Drive.</div>') + body\n"
               '    try:\n        seed_notifications(user["business_id"], biz["currency"])',
               "demo ribbon")

    src = once(src,
               '            if path == "/auth/google" and method == "POST":',
               '            if path == "/demo-enter":\n'
               '                return self.demo_enter(method)\n'
               '            if path == "/auth/google" and method == "POST":',
               "demo route")

    demo_method = '''
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

'''
    src = once(src,
               '    def drive_save(self, user, biz):',
               demo_method + '    def drive_save(self, user, biz):',
               "demo method")

    open(path, "w").write(src)
    print("demo patch applied")


if __name__ == "__main__":
    main()
