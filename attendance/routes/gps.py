"""
routes/gps.py

Phone GPS check-in with geofencing.

PRIVACY MODEL: same as the holiday module. Employee routes derive the
employee_id from the SESSION only — an employee can only check themselves in
and see their own history. HR routes (site management, team map) are gated by
role_required("admin","manager").

TRUST MODEL (be honest with the client): browser GPS is spoofable. Every
check-in stores full audit data (coords, reported accuracy, distance to site)
so HR can review suspicious patterns, but a GPS check-in is inherently weaker
proof of presence than a face terminal.

Accepted check-ins are ALSO inserted into the events table (device_id NULL),
so they count in daily/weekly reports, the dashboard, and the NOI export
exactly like terminal punches.
"""
import math
import os
import base64
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, session, jsonify, abort, send_file)
from datetime import datetime, timedelta
from db import get_conn
from authz import login_required, role_required

CHECKIN_PHOTO_DIR = os.getenv("CHECKIN_PHOTO_DIR",
                              "/var/lib/attendance/checkin_photos")
ENROLLED_FACE_DIR = os.getenv("FACE_DIR", "/var/lib/attendance/faces")


def _detect_face(jpeg_bytes):
    """Best-effort face detection. Returns 1/0, or None when OpenCV isn't
    installed (detection is optional — never a hard dependency on the Pi)."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return None
    try:
        img = cv2.imdecode(np.frombuffer(jpeg_bytes, dtype=np.uint8),
                           cv2.IMREAD_GRAYSCALE)
        if img is None:
            return 0
        cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        faces = cascade.detectMultiScale(img, 1.1, 4)
        return 1 if len(faces) > 0 else 0
    except Exception:
        return None


def _save_checkin_photo(emp_id, ts, photo_b64):
    """Decode a dataURL/base64 JPEG and save it. Returns (path, has_face)
    or (None, None) on any failure — photos are best-effort."""
    try:
        if "," in photo_b64:  # strip data:image/jpeg;base64, prefix
            photo_b64 = photo_b64.split(",", 1)[1]
        raw = base64.b64decode(photo_b64)
        if len(raw) < 1000 or len(raw) > 8_000_000:
            return None, None
        os.makedirs(CHECKIN_PHOTO_DIR, exist_ok=True)
        fname = f"{emp_id}_{ts.replace(' ', '_').replace(':', '-')}.jpg"
        path = os.path.join(CHECKIN_PHOTO_DIR, fname)
        with open(path, "wb") as f:
            f.write(raw)
        return path, _detect_face(raw)
    except Exception:
        return None, None

def _score_checkin_async(checkin_id, employee_id, selfie_path):
    """Compute a 1:1 face-match score for one check-in and store it, in a
    background thread so the employee's check-in is never delayed by the
    (few-hundred-ms) embedding step. Best-effort: if scoring isn't available
    or anything fails, the score simply stays NULL."""
    import threading

    def _work():
        try:
            from services import face_match
            if not face_match.is_available():
                return
            enrolled = os.path.join(ENROLLED_FACE_DIR, f"{employee_id}.jpg")
            if not (selfie_path and os.path.exists(selfie_path)
                    and os.path.exists(enrolled)):
                return
            with open(selfie_path, "rb") as f:
                selfie_bytes = f.read()
            with open(enrolled, "rb") as f:
                enrolled_bytes = f.read()
            score = face_match.compare_jpegs(selfie_bytes, enrolled_bytes)
            if score is None:
                return
            # Fresh connection — this runs off the request thread.
            from db import get_conn
            conn = get_conn()
            conn.execute("UPDATE gps_checkins SET match_score=? WHERE id=?",
                         (round(score, 4), checkin_id))
            conn.commit()
            conn.close()
        except Exception:
            pass  # scoring is optional; never disrupt anything

    threading.Thread(target=_work, daemon=True).start()


bp = Blueprint("gps", __name__, url_prefix="/gps")

HR_ROLES = ("admin", "manager")
MIN_INTERVAL_MINUTES = 2      # anti-spam between check-ins
MAX_ACCURACY_M = 200          # reject fixes with worse reported accuracy


def haversine_m(lat1, lng1, lat2, lng2):
    """Distance in meters between two WGS84 points."""
    R = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return 2 * R * math.asin(math.sqrt(a))


def _nearest_site(conn, lat, lng):
    """(site_row, distance_m) of the nearest ACTIVE site, or (None, None)."""
    best, best_d = None, None
    for s in conn.execute("SELECT * FROM gps_sites WHERE active=1").fetchall():
        d = haversine_m(lat, lng, s["lat"], s["lng"])
        if best_d is None or d < best_d:
            best, best_d = s, d
    return best, best_d


# ── Employee ──────────────────────────────────────────────────────────────────

@bp.route("/checkin")
@login_required
def checkin_page():
    """Employee check-in page. Own data only."""
    emp_id = session.get("employee_id")
    if not emp_id:
        if session.get("role") in HR_ROLES:
            return redirect(url_for("gps.admin_page"))
        flash("Your account isn't linked to an employee record. Contact HR.", "warning")
        return redirect(url_for("dashboard.dashboard"))
    conn = get_conn()
    history = conn.execute(
        """SELECT gc.*, gs.name AS site_name
           FROM gps_checkins gc LEFT JOIN gps_sites gs ON gs.id = gc.site_id
           WHERE gc.employee_id=? ORDER BY gc.timestamp DESC LIMIT 20""",
        (str(emp_id),)).fetchall()
    sites = conn.execute(
        "SELECT id, name, lat, lng, radius_m FROM gps_sites WHERE active=1"
    ).fetchall()
    conn.close()
    return render_template("gps_checkin.html", history=history,
                           has_sites=len(sites) > 0,
                           sites_json=[dict(s) for s in sites],
                           history_json=[dict(h) for h in history])


@bp.route("/checkin", methods=["POST"])
@login_required
def checkin_submit():
    """Validate a GPS fix against the geofences and record the check-in.
    Employee can only check THEMSELVES in (id from session).

    Flow: out-of-zone / poor-accuracy / no-GPS check-ins are NOT rejected —
    the first attempt returns needs_confirm with a warning; the client asks
    the employee and resends with force=1. The check-in is then recorded
    (with coordinates when available) and flagged for HR review."""
    emp_id = session.get("employee_id")
    if not emp_id:
        abort(403)
    data = request.get_json(silent=True) or {}
    direction = data.get("direction")
    force = bool(data.get("force"))
    if direction not in ("IN", "OUT"):
        return jsonify({"ok": False, "msg": "Invalid direction."}), 400

    lat = lng = accuracy = None
    try:
        if data.get("lat") is not None and data.get("lng") is not None:
            lat = float(data.get("lat"))
            lng = float(data.get("lng"))
            accuracy = float(data.get("accuracy", 9999))
    except (TypeError, ValueError):
        lat = lng = accuracy = None

    now = datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn()

    # Anti-spam: minimum interval between accepted check-ins
    last = conn.execute(
        """SELECT timestamp FROM gps_checkins
           WHERE employee_id=? AND accepted=1
           ORDER BY timestamp DESC LIMIT 1""", (str(emp_id),)).fetchone()
    if last:
        try:
            prev = datetime.fromisoformat(str(last["timestamp"]))
            if (now - prev) < timedelta(minutes=MIN_INTERVAL_MINUTES):
                conn.close()
                return jsonify({"ok": False,
                                "msg": f"Please wait {MIN_INTERVAL_MINUTES} minutes between check-ins."}), 429
        except ValueError:
            pass

    site = dist = None
    within = False
    warn = None

    if lat is None:
        warn = ("No pudimos obtener tu ubicación. "
                "¿Registrar asistencia sin ubicación?")
    else:
        site, dist = _nearest_site(conn, lat, lng)
        if site is None:
            # No sites configured — record with coords, flag as outside
            warn = ("No hay sitios configurados. "
                    "¿Registrar asistencia de todas formas?")
        elif accuracy is not None and accuracy > MAX_ACCURACY_M:
            warn = (f"Tu señal GPS es imprecisa (±{int(accuracy)} m). "
                    f"¿Registrar asistencia de todas formas?")
        else:
            within = dist <= (site["radius_m"] + min(accuracy or 0, 50))
            if not within:
                warn = (f"Estás a {int(dist)} m de {site['name']} "
                        f"(zona: {site['radius_m']} m). "
                        f"¿Registrar asistencia fuera de zona?")

    if warn and not force:
        conn.close()
        return jsonify({"ok": False, "needs_confirm": True, "msg": warn})

    # Photo (best-effort selfie for HR verification)
    photo_path = has_face = None
    if data.get("photo"):
        photo_path, has_face = _save_checkin_photo(str(emp_id), ts, data["photo"])

    # Record (inside zone, or confirmed outside/no-GPS)
    cur = conn.execute(
        """INSERT INTO gps_checkins
           (employee_id, timestamp, direction, lat, lng, accuracy_m,
            site_id, distance_m, within_fence, accepted, photo_path, has_face)
           VALUES (?,?,?,?,?,?,?,?,?,1,?,?)""",
        (str(emp_id), ts, direction, lat, lng, accuracy,
         site["id"] if site else None,
         round(dist, 1) if dist is not None else None,
         1 if within else 0, photo_path, has_face))
    chk_id = cur.lastrowid

    u = conn.execute("SELECT name FROM users WHERE employee_id=?",
                     (str(emp_id),)).fetchone()
    cur = conn.execute(
        """INSERT INTO events (device_id, employee_id, name, timestamp, direction)
           VALUES (NULL, ?, ?, ?, ?)""",
        (str(emp_id), u["name"] if u else str(emp_id), ts, direction))
    conn.execute("UPDATE gps_checkins SET event_id=? WHERE id=?",
                 (cur.lastrowid, chk_id))
    conn.commit(); conn.close()

    # Score the selfie against the enrolled face in the background (never blocks
    # the check-in; stays NULL if face matching isn't installed).
    if photo_path:
        _score_checkin_async(chk_id, str(emp_id), photo_path)

    verb = "Entrada" if direction == "IN" else "Salida"
    if within:
        msg = f"{verb} registrada en {site['name']} ({int(dist)} m)."
    elif lat is None:
        msg = f"{verb} registrada SIN ubicación."
    elif site:
        msg = f"{verb} registrada FUERA DE ZONA ({int(dist)} m de {site['name']})."
    else:
        msg = f"{verb} registrada (sin sitios configurados)."
    return jsonify({"ok": True, "msg": msg, "within": within})


# ── Team / kiosk check-in (foreman) ──────────────────────────────────────────

def _team_employees(conn):
    """Employees the current account may check in. If the account is linked to
    an employee with a department, scope to that department (his crew);
    otherwise all active employees."""
    dept = None
    emp_id = session.get("employee_id")
    if emp_id:
        row = conn.execute(
            "SELECT department_id FROM users WHERE employee_id=?",
            (str(emp_id),)).fetchone()
        dept = row["department_id"] if row else None
    if dept:
        return conn.execute(
            """SELECT employee_id, name FROM users
               WHERE is_active=1 AND department_id=? ORDER BY name""",
            (dept,)).fetchall()
    return conn.execute(
        "SELECT employee_id, name FROM users WHERE is_active=1 ORDER BY name"
    ).fetchall()


@bp.route("/team")
@login_required
@role_required("foreman", "admin", "manager")
def team_page():
    conn = get_conn()
    employees = _team_employees(conn)
    # Crew memory: employees this foreman registered in the last 7 days sort
    # to the top, pre-selected — so the daily crew is one press away, with no
    # crew-management UI to maintain.
    crew_rows = conn.execute(
        """SELECT DISTINCT employee_id FROM gps_checkins
           WHERE recorded_by = ?
             AND timestamp >= datetime('now', '-7 days', 'localtime')""",
        (session.get("username"),)).fetchall()
    crew_ids = {str(r["employee_id"]) for r in crew_rows}
    employees = sorted(
        (dict(e) for e in employees),
        key=lambda e: (str(e["employee_id"]) not in crew_ids,
                       (e["name"] or "").lower()))
    recent = conn.execute(
        """SELECT gc.timestamp, gc.direction, gc.within_fence,
                  COALESCE(u.name, gc.employee_id) AS emp_name
           FROM gps_checkins gc
           LEFT JOIN users u ON u.employee_id = gc.employee_id
           WHERE gc.recorded_by = ?
           ORDER BY gc.timestamp DESC LIMIT 25""",
        (session.get("username"),)).fetchall()
    conn.close()
    return render_template("gps_team.html", employees=employees,
                           recent=recent, crew_ids=crew_ids)


@bp.route("/team", methods=["POST"])
@login_required
@role_required("foreman", "admin", "manager")
def team_submit():
    """Batch check-in: one GPS fix, N employees. Each gets a gps_checkin row
    (recorded_by = this account) and a real attendance event. Employees who
    checked in too recently are skipped, not failed."""
    data = request.get_json(silent=True) or {}
    direction = data.get("direction")
    force = bool(data.get("force"))
    emp_ids = data.get("employee_ids") or []
    if direction not in ("IN", "OUT") or not isinstance(emp_ids, list) or not emp_ids:
        return jsonify({"ok": False, "msg": "Select at least one employee."}), 400
    emp_ids = [str(e) for e in emp_ids][:200]

    lat = lng = accuracy = None
    try:
        if data.get("lat") is not None and data.get("lng") is not None:
            lat = float(data.get("lat"))
            lng = float(data.get("lng"))
            accuracy = float(data.get("accuracy", 9999))
    except (TypeError, ValueError):
        lat = lng = accuracy = None

    now = datetime.now()
    ts = now.strftime("%Y-%m-%d %H:%M:%S")
    conn = get_conn()

    # Only employees this account is allowed to manage
    allowed = {str(r["employee_id"]): r["name"] for r in _team_employees(conn)}
    emp_ids = [e for e in emp_ids if e in allowed]
    if not emp_ids:
        conn.close()
        return jsonify({"ok": False, "msg": "No valid employees selected."}), 400

    site = dist = None
    within = False
    warn = None
    # Field crews routinely work at sites nobody registered, so being out of
    # zone (or having no matching site) is the normal case — record it with GPS
    # and flag it, don't interrupt. Only genuinely missing/poor location, where
    # there's nothing to audit against, asks for confirmation.
    if lat is None:
        warn = "No pudimos obtener la ubicación. ¿Registrar sin ubicación?"
    else:
        site, dist = _nearest_site(conn, lat, lng)
        if accuracy is not None and accuracy > MAX_ACCURACY_M:
            warn = f"Señal GPS imprecisa (±{int(accuracy)} m). ¿Registrar de todas formas?"
        elif site is not None:
            within = dist <= (site["radius_m"] + min(accuracy or 0, 50))
    if warn and not force:
        conn.close()
        return jsonify({"ok": False, "needs_confirm": True, "msg": warn})

    recorded, skipped = [], []
    recorder = session.get("username")
    # Optional group photo: saved once, attached to every row in the batch.
    batch_photo_path = batch_has_face = None
    if data.get("photo"):
        batch_photo_path, batch_has_face = _save_checkin_photo(
            f"crew_{recorder}", ts, data["photo"])
    for emp in emp_ids:
        # Per-employee anti-spam
        last = conn.execute(
            """SELECT timestamp FROM gps_checkins
               WHERE employee_id=? AND accepted=1
               ORDER BY timestamp DESC LIMIT 1""", (emp,)).fetchone()
        if last:
            try:
                prev = datetime.fromisoformat(str(last["timestamp"]))
                if (now - prev) < timedelta(minutes=MIN_INTERVAL_MINUTES):
                    skipped.append(allowed[emp])
                    continue
            except ValueError:
                pass
        cur = conn.execute(
            """INSERT INTO gps_checkins
               (employee_id, timestamp, direction, lat, lng, accuracy_m,
                site_id, distance_m, within_fence, accepted, recorded_by,
                photo_path, has_face)
               VALUES (?,?,?,?,?,?,?,?,?,1,?,?,?)""",
            (emp, ts, direction, lat, lng, accuracy,
             site["id"] if site else None,
             round(dist, 1) if dist is not None else None,
             1 if within else 0, recorder,
             batch_photo_path, batch_has_face))
        chk_id = cur.lastrowid
        cur = conn.execute(
            """INSERT INTO events (device_id, employee_id, name, timestamp, direction)
               VALUES (NULL, ?, ?, ?, ?)""",
            (emp, allowed[emp], ts, direction))
        conn.execute("UPDATE gps_checkins SET event_id=? WHERE id=?",
                     (cur.lastrowid, chk_id))
        recorded.append(allowed[emp])
    conn.commit(); conn.close()

    verb = "Entrada" if direction == "IN" else "Salida"
    msg = f"{verb} registrada para {len(recorded)} empleado(s)."
    if lat is None:
        msg += " SIN ubicación."
    elif not within:
        msg += " Ubicación registrada (fuera de zona conocida)."
    if skipped:
        msg += f" Omitidos (registro reciente): {', '.join(skipped[:5])}" + \
               ("…" if len(skipped) > 5 else "")
    return jsonify({"ok": True, "msg": msg, "recorded": len(recorded),
                    "skipped": len(skipped), "within": within})


# ── Photo serving (authenticated) ────────────────────────────────────────────

@bp.route("/photo/<int:chk_id>")
@login_required
def checkin_photo(chk_id):
    """Serve a check-in selfie. Allowed: HR roles, or the employee who owns it."""
    conn = get_conn()
    row = conn.execute(
        "SELECT employee_id, photo_path FROM gps_checkins WHERE id=?",
        (chk_id,)).fetchone()
    conn.close()
    if not row or not row["photo_path"]:
        abort(404)
    is_hr = session.get("role") in HR_ROLES
    is_owner = str(session.get("employee_id") or "") == str(row["employee_id"])
    if not (is_hr or is_owner):
        abort(403)
    path = row["photo_path"]
    # Never serve outside the photo directory
    if not os.path.realpath(path).startswith(os.path.realpath(CHECKIN_PHOTO_DIR)):
        abort(404)
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="image/jpeg")


@bp.route("/enrolled/<employee_id>")
@login_required
@role_required(*HR_ROLES)
def enrolled_photo(employee_id):
    """Serve the enrolled face photo for side-by-side comparison. HR only."""
    safe = "".join(ch for ch in str(employee_id) if ch.isalnum() or ch in "_-")
    path = os.path.join(ENROLLED_FACE_DIR, f"{safe}.jpg")
    if not os.path.exists(path):
        abort(404)
    return send_file(path, mimetype="image/jpeg")


# ── HR / manager ─────────────────────────────────────────────────────────────

@bp.route("/admin")
@login_required
@role_required(*HR_ROLES)
def admin_page():
    """Sites management + map of recent check-ins."""
    conn = get_conn()
    sites = conn.execute("SELECT * FROM gps_sites ORDER BY name").fetchall()
    checkins = conn.execute(
        """SELECT gc.*, gs.name AS site_name,
                  COALESCE(u.name, gc.employee_id) AS emp_name
           FROM gps_checkins gc
           LEFT JOIN gps_sites gs ON gs.id = gc.site_id
           LEFT JOIN users u ON u.employee_id = gc.employee_id
           ORDER BY gc.timestamp DESC LIMIT 100""").fetchall()
    conn.close()
    return render_template("gps_admin.html",
                           sites=sites, checkins=checkins,
                           sites_json=[dict(s) for s in sites],
                           checkins_json=[dict(c) for c in checkins])


@bp.route("/admin/sites", methods=["POST"])
@login_required
@role_required(*HR_ROLES)
def site_create():
    name = (request.form.get("name") or "").strip()
    try:
        lat = float(request.form.get("lat"))
        lng = float(request.form.get("lng"))
        radius = max(20, min(2000, int(request.form.get("radius_m", 150))))
    except (TypeError, ValueError):
        flash("Invalid site data — set name, coordinates and radius.", "danger")
        return redirect(url_for("gps.admin_page"))
    if not name:
        flash("Site name is required.", "danger")
        return redirect(url_for("gps.admin_page"))
    conn = get_conn()
    conn.execute("INSERT INTO gps_sites (name, lat, lng, radius_m, active) VALUES (?,?,?,?,1)",
                 (name, lat, lng, radius))
    conn.commit(); conn.close()
    flash(f"Site '{name}' created.", "success")
    return redirect(url_for("gps.admin_page"))


@bp.route("/admin/sites/<int:site_id>/toggle", methods=["POST"])
@login_required
@role_required(*HR_ROLES)
def site_toggle(site_id):
    conn = get_conn()
    conn.execute("UPDATE gps_sites SET active = 1-active WHERE id=?", (site_id,))
    conn.commit(); conn.close()
    return redirect(url_for("gps.admin_page"))


@bp.route("/admin/sites/<int:site_id>/delete", methods=["POST"])
@login_required
@role_required("admin")
def site_delete(site_id):
    conn = get_conn()
    conn.execute("DELETE FROM gps_sites WHERE id=?", (site_id,))
    conn.commit(); conn.close()
    flash("Site deleted.", "success")
    return redirect(url_for("gps.admin_page"))
