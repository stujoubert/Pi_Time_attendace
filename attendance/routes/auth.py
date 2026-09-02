from flask import Blueprint, render_template, request, redirect, url_for, session, flash
from werkzeug.security import check_password_hash
from datetime import datetime, timedelta
from db import get_conn
import logging
import os

bp = Blueprint("auth", __name__, url_prefix="/auth")
log = logging.getLogger(__name__)

# ── Login throttling (in-memory; single-process app) ─────────────────────────
MAX_ATTEMPTS   = 5          # failures allowed per window
WINDOW_MINUTES = 15         # sliding window
_failures: dict = {}        # key -> [datetime, ...]


def _client_ip():
    """Real client IP: behind cloudflared everything arrives from localhost,
    so trust CF-Connecting-IP / X-Forwarded-For before remote_addr."""
    return (request.headers.get("CF-Connecting-IP")
            or (request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                or None)
            or request.remote_addr or "?")


def _prune(key, now):
    cutoff = now - timedelta(minutes=WINDOW_MINUTES)
    _failures[key] = [t for t in _failures.get(key, []) if t > cutoff]
    if not _failures[key]:
        _failures.pop(key, None)


def _is_locked(key, now):
    _prune(key, now)
    return len(_failures.get(key, [])) >= MAX_ATTEMPTS


def _record_failure(key, now):
    _failures.setdefault(key, []).append(now)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        ip  = _client_ip()
        now = datetime.now()

        # Throttle by IP and by IP+username so one attacker can't burn
        # a shared office IP for everyone with a single username.
        for key in (f"ip:{ip}", f"acct:{ip}:{username.lower()}"):
            if _is_locked(key, now):
                log.warning(f"[AUTH] LOCKED OUT ip={ip} user={username!r}")
                flash(f"Too many failed attempts. Try again in {WINDOW_MINUTES} minutes.",
                      "danger")
                return render_template("login.html"), 429

        conn = get_conn()
        acct = conn.execute(
            "SELECT * FROM accounts WHERE username=? AND active=1", (username,)
        ).fetchone()
        conn.close()
        if not acct or not check_password_hash(acct["password_hash"], password):
            _record_failure(f"ip:{ip}", now)
            _record_failure(f"acct:{ip}:{username.lower()}", now)
            log.warning(f"[AUTH] FAILED login ip={ip} user={username!r}")
            flash("Invalid username or password", "danger")
            return render_template("login.html")

        # Success: clear this user's failure history for the IP
        _failures.pop(f"acct:{ip}:{username.lower()}", None)
        log.info(f"[AUTH] login ok ip={ip} user={username!r} role={acct['role']}")

        session.clear()
        lang = session.get("lang", "en")
        session["account_id"] = acct["id"]
        session["role"]       = acct["role"]
        session["username"]   = acct["username"]
        session["lang"]       = lang
        # Link to an employee record (for the holiday module / self-service)
        try:
            session["employee_id"] = acct["employee_id"]
        except (KeyError, IndexError):
            session["employee_id"] = None

        # Default-credential gate: any account still using the literal
        # default password must change it before doing anything else.
        if password == "admin":
            session["must_change_password"] = True
            flash("You are using the default password — set a new one to continue.",
                  "warning")
            return redirect(url_for("holidays.change_password"))

        # Employees land on their holiday page; staff on the dashboard
        if acct["role"] == "employee":
            return redirect(request.args.get("next") or url_for("holidays.my_holidays"))
        if acct["role"] == "foreman":
            return redirect(request.args.get("next") or url_for("gps.team_page"))
        return redirect(request.args.get("next") or url_for("dashboard.dashboard"))
    return render_template("login.html")


@bp.route("/logout", methods=["GET", "POST"])
def logout():
    session.clear()
    return redirect(url_for("auth.login"))
