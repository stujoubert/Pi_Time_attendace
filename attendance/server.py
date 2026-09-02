#!/usr/bin/env python3
import os
import sys
import logging

logging.basicConfig(level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

# ── Safety guards ─────────────────────────────────────────────────────────────
ATT_ENV = os.environ.get("ATT_ENV", "prod")
ATT_DB  = os.environ.get("ATT_DB")

if not ATT_DB:
    raise RuntimeError("ATT_DB env var not set — refusing to start")

if ATT_ENV == "dev" and "/var/lib/attendance/" in ATT_DB:
    raise RuntimeError("DEV env pointing at PROD db — refusing to start")

SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    if ATT_ENV == "prod":
        raise RuntimeError("SECRET_KEY must be set in production")
    SECRET_KEY = "dev-only-insecure-key"

# ── Path setup ────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# ── Bootstrap DB ──────────────────────────────────────────────────────────────
from scripts.bootstrap_db import main as bootstrap_db
bootstrap_db()
from scripts.bootstrap_payroll import main as bootstrap_payroll
bootstrap_payroll()
from db import _ensure_columns
_ensure_columns()

# ── Flask app ─────────────────────────────────────────────────────────────────
from flask import Flask, redirect, url_for, session, g
app = Flask(__name__,
    static_folder="static",
    template_folder="templates")

app.secret_key = SECRET_KEY
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("COOKIE_SECURE", "0") == "1"
app.config["SESSION_COOKIE_PATH"] = "/"

@app.before_request
def _enforce_password_change():
    """Accounts logged in with the default password may only reach the
    change-password page (and logout/static) until they set a new one."""
    from flask import request as _rq
    if not session.get("must_change_password"):
        return None
    allowed = {"holidays.change_password", "auth.logout", "static"}
    if _rq.endpoint in allowed or _rq.endpoint is None:
        return None
    return redirect(url_for("holidays.change_password"))


@app.after_request
def no_cache(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@app.after_request
def security_headers(response):
    """Defense-in-depth HTTP security headers. The CSP is written to match what
    the app actually loads — Bootstrap (jsdelivr), Leaflet (cdnjs), Google Fonts,
    and OpenStreetMap tiles — so maps, icons and fonts keep working. Inline
    styles/scripts are allowed because the templates rely on them heavily;
    tightening that further would mean nonce-ing every inline handler app-wide.
    Permissions-Policy allows camera + geolocation for our OWN origin only,
    which the check-in selfie and GPS features need."""
    csp = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdnjs.cloudflare.com https://fonts.googleapis.com; "
        "font-src 'self' https://cdn.jsdelivr.net https://fonts.gstatic.com data:; "
        "img-src 'self' data: blob: https://tile.openstreetmap.org https://*.tile.openstreetmap.org; "
        "connect-src 'self' https://tile.openstreetmap.org https://*.tile.openstreetmap.org; "
        "frame-ancestors 'self'; "
        "base-uri 'self'; "
        "form-action 'self'"
    )
    response.headers["Content-Security-Policy"] = csp
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    # Allow camera + geolocation for self (check-in selfie / GPS); deny the rest.
    response.headers["Permissions-Policy"] = (
        "camera=(self), geolocation=(self), microphone=(), "
        "payment=(), usb=(), interest-cohort=()"
    )
    return response
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024  # 4 MB

# ── i18n ──────────────────────────────────────────────────────────────────────
from translations import init_i18n, LANG
init_i18n(app)

# ── Context processors ────────────────────────────────────────────────────────
@app.context_processor
def inject_globals():
    from services.settings import get_company_settings
    try:
        company = get_company_settings()
    except Exception:
        company = {"name": "", "rfc": "", "logo_url": None}
    return {"company": company, "session": session}

# ── Jinja filters ─────────────────────────────────────────────────────────────
@app.template_filter("weekdays_label")
def weekdays_label(value, T=None):
    if not value:
        return ""
    if T is None:
        T = LANG["en"]
    parts = []
    for d in str(value).split(","):
        d = d.strip()
        parts.append(T.get(f"weekday_{d}", d))
    return ", ".join(parts)

# ── Blueprints ────────────────────────────────────────────────────────────────
from routes.auth         import bp as auth_bp
from routes.attendance   import bp as attendance_bp
from routes.dashboard    import bp as dashboard_bp
from routes.users        import bp_users, bp_depts, bp_accounts
from routes.devices_payroll import bp_devices, bp_payroll
from routes.misc_routes  import bp_schedules, bp_company, bp_misc
from routes.rates import bp as rates_bp
from routes.faces         import bp as faces_bp
from routes.payroll_mx    import bp as nomina_bp
from routes.holidays      import bp as holidays_bp
from routes.noi_export    import bp as noi_export_bp
from routes.gps           import bp as gps_bp

app.register_blueprint(auth_bp)
app.register_blueprint(attendance_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(bp_users)
app.register_blueprint(bp_depts)
app.register_blueprint(bp_accounts)
app.register_blueprint(bp_devices)
app.register_blueprint(bp_payroll)
app.register_blueprint(bp_schedules)
app.register_blueprint(bp_company)
app.register_blueprint(bp_misc)
app.register_blueprint(rates_bp)
app.register_blueprint(faces_bp)
app.register_blueprint(nomina_bp)
app.register_blueprint(holidays_bp)
app.register_blueprint(noi_export_bp)
app.register_blueprint(gps_bp)

@app.route("/")
def index():
    return redirect(url_for("dashboard.dashboard"))

# ── Scheduler ─────────────────────────────────────────────────────────────────
if ATT_ENV != "test":
    from scheduler import start_scheduler
    start_scheduler(app)

# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("ATT_PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=(ATT_ENV == "dev"))
