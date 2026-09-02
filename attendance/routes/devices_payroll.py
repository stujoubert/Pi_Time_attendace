import json
from flask import Blueprint, render_template, request, redirect, url_for, flash, send_file, g, jsonify
from datetime import datetime, date, timedelta
from db import get_conn, list_devices
from authz import login_required, role_required
import io

# ─── DEVICES ─────────────────────────────────────────────────────────────────

bp_devices = Blueprint("devices", __name__, url_prefix="/devices")


@bp_devices.route("/health")
@login_required
def devices_health():
    """AJAX: ping every active device concurrently. Returns online/offline + stale-fetch flag."""
    import requests as _requests
    from requests.auth import HTTPDigestAuth as _Digest
    from concurrent.futures import ThreadPoolExecutor

    conn = get_conn()
    devices = conn.execute(
        "SELECT id, name, ip, username, password, last_fetch_at FROM devices WHERE active=1"
    ).fetchall()
    conn.close()

    def _check(d):
        online = False
        try:
            r = _requests.get(f"http://{d['ip']}/ISAPI/System/deviceInfo",
                              auth=_Digest(d["username"], d["password"]),
                              timeout=4, verify=False)
            online = r.status_code < 500
        except Exception:
            online = False
        stale = True
        if d["last_fetch_at"]:
            try:
                last = datetime.fromisoformat(str(d["last_fetch_at"])[:19])
                stale = (datetime.now() - last) > timedelta(hours=2)
            except Exception:
                pass
        return {"id": d["id"], "name": d["name"], "ip": d["ip"],
                "online": online, "stale": stale,
                "last_fetch": str(d["last_fetch_at"] or "")[:16]}

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_check, devices))
    offline = [r["name"] for r in results if not r["online"]]
    return jsonify({"devices": results, "offline": offline,
                    "all_ok": len(offline) == 0})


@bp_devices.route("/")
@login_required
@role_required("admin")
def devices_page():
    conn = get_conn()
    devices = conn.execute("""
        SELECT id, name, ip, active, last_fetch_at, last_fetch_count, username, stage
        FROM devices ORDER BY name
    """).fetchall()
    conn.close()
    return render_template("devices.html", devices=devices)


@bp_devices.route("/production-terminals", methods=["POST"])
@login_required
@role_required("admin")
def set_production_terminals():
    """Mark which devices are production-floor terminals via checkboxes.
    Checked → stage='production'; unchecked → stage=NULL. Replaces the old
    per-device hardcoded entrance/production dropdown."""
    checked = set(request.form.getlist("production"))
    conn = get_conn()
    for r in conn.execute("SELECT id FROM devices").fetchall():
        is_prod = str(r["id"]) in checked
        conn.execute("UPDATE devices SET stage=? WHERE id=?",
                     ("production" if is_prod else None, r["id"]))
    conn.commit()
    conn.close()
    flash("Production-floor terminals updated", "success")
    return redirect(url_for("devices.devices_page"))

@bp_devices.route("/add", methods=["POST"])
@login_required
@role_required("admin")
def device_add():
    name     = request.form.get("name", "").strip()
    ip       = request.form.get("ip", "").strip()
    username = request.form.get("username", "admin")
    password = request.form.get("password", "")
    if not name or not ip:
        flash("Name and IP are required", "danger")
        return redirect(url_for("devices.devices_page"))
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO devices (name, ip, username, password, active) VALUES (?,?,?,?,1)",
        (name, ip, username, password))
    device_id = cur.lastrowid
    conn.commit()
    conn.close()

    # Auto-detect the device's ISAPI dialect and store it as a profile.
    try:
        import json as _json
        from devices.hikvision_isapi import HikvisionISAPI
        api = HikvisionISAPI(ip, username, password)
        profile = api.detect_profile()
        conn = get_conn()
        conn.execute("UPDATE devices SET profile=? WHERE id=?",
                     (_json.dumps(profile), device_id))
        conn.commit()
        conn.close()
        if profile.get("auth_scheme") is None:
            flash("Device added, but writes were rejected (401). This is a "
                  "device-side account/permission issue — check that the account "
                  "is an Administrator on the device. Detection notes: "
                  + "; ".join(profile.get("notes", [])), "warning")
        else:
            bits = [f"auth={profile.get('auth_scheme')}"]
            if profile.get("rightplan") is not None:
                bits.append(f"RightPlan={'yes' if profile['rightplan'] else 'no'}")
            if profile.get("delete_verb"):
                bits.append(f"delete={profile['delete_verb']}")
            flash(f"Device added and profiled ({', '.join(bits)})", "success")
    except Exception as e:
        flash(f"Device added, but capability detection failed: {e}. "
              f"You can re-run it from the device's Probe page.", "warning")
    return redirect(url_for("devices.devices_page"))

@bp_devices.route("/<int:device_id>/edit", methods=["GET", "POST"])
@login_required
@role_required("admin")
def device_edit(device_id):
    conn = get_conn()
    if request.method == "POST":
        pw = request.form.get("password", "")
        if pw:
            conn.execute(
                "UPDATE devices SET name=?,ip=?,username=?,password=?,active=? WHERE id=?",
                (request.form["name"], request.form["ip"], request.form.get("username"),
                 pw, int(request.form.get("active", 1)), device_id))
        else:
            conn.execute(
                "UPDATE devices SET name=?,ip=?,username=?,active=? WHERE id=?",
                (request.form["name"], request.form["ip"], request.form.get("username"),
                 int(request.form.get("active", 1)), device_id))
        conn.commit()
        conn.close()
        flash("Device updated", "success")
        return redirect(url_for("devices.devices_page"))
    device = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
    conn.close()
    if not device:
        flash("Device not found", "danger")
        return redirect(url_for("devices.devices_page"))
    return render_template("device_edit.html", device=device)

@bp_devices.route("/<int:device_id>/toggle", methods=["POST"])
@login_required
@role_required("admin")
def device_toggle(device_id):
    conn = get_conn()
    conn.execute(
        "UPDATE devices SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?",
        (device_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("devices.devices_page"))

@bp_devices.route("/<int:device_id>/delete", methods=["POST"])
@login_required
@role_required("admin")
def device_delete(device_id):
    conn = get_conn()
    conn.execute("DELETE FROM devices WHERE id=?", (device_id,))
    conn.commit()
    conn.close()
    flash("Device deleted", "success")
    return redirect(url_for("devices.devices_page"))

@bp_devices.route("/<int:device_id>/fetch", methods=["POST"])
@login_required
@role_required("admin")
def device_fetch_now(device_id):
    from services.collector import fetch_from_device
    conn = get_conn()
    d = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
    conn.close()
    if not d:
        flash("Device not found", "danger")
        return redirect(url_for("devices.devices_page"))
    try:
        count = fetch_from_device(d["ip"], d["username"], d["password"], device_id)
        flash(f"Fetched {count} new events from {d['name']}", "success")
    except Exception as e:
        flash(f"Fetch error: {e}", "danger")
    return redirect(url_for("devices.devices_page"))

@bp_devices.route("/fetch-all", methods=["POST"])
@login_required
@role_required("admin")
def fetch_all():
    from services.collector import fetch_all_active_devices
    results = fetch_all_active_devices()
    total = sum(v["count"] for v in results.values())
    flash(f"Fetched {total} new events from {len(results)} devices", "success")
    return redirect(url_for("devices.devices_page"))

@bp_devices.route("/sync-users", methods=["POST"])
@login_required
@role_required("admin")
def sync_users():
    from services.collector import sync_users_across_devices
    summary = sync_users_across_devices()
    pushed = sum(v["pushed"] for v in summary.values())
    flash(f"Pushed {pushed} users across {len(summary)} devices", "success")
    return redirect(url_for("devices.devices_page"))


@bp_devices.route("/pull-users", methods=["POST"])
@login_required
@role_required("admin")
def pull_users():
    """Manual: pull users FROM devices into the DB (download direction)."""
    from services.collector import sync_users_from_devices
    summary = sync_users_from_devices()
    added   = sum(v.get("added", 0)   for v in summary.values())
    updated = sum(v.get("updated", 0) for v in summary.values())
    flash(f"Pulled users: {added} added, {updated} updated from {len(summary)} devices", "success")
    return redirect(url_for("devices.devices_page"))


@bp_devices.route("/<int:device_id>/write-test", methods=["POST"])
@login_required
@role_required("admin")
def device_write_test(device_id):
    """
    DESTRUCTIVE-but-self-cleaning write test. Creates a throwaway user
    (_PROBE_TEST), optionally pushes an uploaded face, then deletes both.
    Gated behind its own button + confirm dialog. For test devices only.
    """
    from devices.hikvision_isapi import HikvisionISAPI
    conn = get_conn()
    d = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
    conn.close()
    if not d:
        flash("Device not found", "danger")
        return redirect(url_for("devices.devices_page"))

    test_image = None
    f = request.files.get("test_face")
    if f and f.filename:
        test_image = f.read()

    api = HikvisionISAPI.from_row(d)
    results = api.run_write_test(test_image=test_image)
    return render_template("device_probe.html",
                           device=d, results=results, write_test=True)


@bp_devices.route("/<int:device_id>/detect", methods=["POST"])
@login_required
@role_required("admin")
def device_detect(device_id):
    """Re-run capability detection and store the resulting profile."""
    import json as _json
    from devices.hikvision_isapi import HikvisionISAPI
    conn = get_conn()
    d = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
    conn.close()
    if not d:
        flash("Device not found", "danger")
        return redirect(url_for("devices.devices_page"))

    api = HikvisionISAPI.from_row(d)
    try:
        profile = api.detect_profile()
        conn = get_conn()
        conn.execute("UPDATE devices SET profile=? WHERE id=?",
                     (_json.dumps(profile), device_id))
        conn.commit()
        conn.close()
        if profile.get("auth_scheme") is None:
            flash("Detection finished: writes rejected (401). This is a device-side "
                  "account/permission issue. Notes: " + "; ".join(profile.get("notes", [])),
                  "warning")
        else:
            flash(f"Profile updated: {_json.dumps(profile)}", "success")
    except Exception as e:
        flash(f"Detection failed: {e}", "danger")
    return redirect(url_for("devices.device_probe", device_id=device_id))


@bp_devices.route("/<int:device_id>/probe", methods=["POST", "GET"])
@login_required
@role_required("admin")
def device_probe(device_id):
    """Read-only capability probe for a (typically newly added) device."""
    from devices.hikvision_isapi import HikvisionISAPI
    conn = get_conn()
    d = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
    conn.close()
    if not d:
        flash("Device not found", "danger")
        return redirect(url_for("devices.devices_page"))

    api = HikvisionISAPI.from_row(d)
    results = api.probe_capabilities()
    import json as _json
    try:
        stored_profile = _json.loads(d["profile"]) if d["profile"] else None
    except Exception:
        stored_profile = None
    return render_template("device_probe.html",
                           device=d, results=results, stored_profile=stored_profile)


@bp_devices.route("/<int:device_id>/test", methods=["POST"])
@login_required
@role_required("admin")
def device_test(device_id):
    """Test connectivity and show what we can reach on the device."""
    from devices.hikvision_isapi import HikvisionISAPI
    conn = get_conn()
    d = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
    conn.close()
    if not d:
        flash("Device not found", "danger")
        return redirect(url_for("devices.devices_page"))

    api = HikvisionISAPI.from_row(d)
    ok  = api.ping()
    if ok:
        users = api.list_users()
        faces = api.list_faces_on_device()
        flash(
            f"{d['name']} ({d['ip']}) — reachable ✓ | "
            f"{len(users)} users on device | {len(faces)} faces on device",
            "success"
        )
    else:
        flash(
            f"{d['name']} ({d['ip']}) — UNREACHABLE. "
            f"Check IP, credentials, and that the device is on the same network.",
            "danger"
        )
    return redirect(url_for("devices.devices_page"))

# ─── PAYROLL ──────────────────────────────────────────────────────────────────

bp_payroll = Blueprint("payroll", __name__, url_prefix="/payroll")

def _week_bounds(base: date, week_type: str):
    wd = base.weekday()
    if week_type == "sat_fri":
        start = base - timedelta(days=(wd - 5) % 7); end = start + timedelta(days=6)
    elif week_type == "mon_fri":
        start = base - timedelta(days=wd); end = start + timedelta(days=4)
    elif week_type == "sun_sat":
        start = base - timedelta(days=(wd+1)%7); end = start + timedelta(days=6)
    else:
        start = base - timedelta(days=wd); end = start + timedelta(days=5)
    return start, end


def _quincena_bounds(base: date):
    """Return (start, end) for the quincena containing base date."""
    import calendar
    if base.day <= 15:
        return date(base.year, base.month, 1), date(base.year, base.month, 15)
    else:
        last = calendar.monthrange(base.year, base.month)[1]
        return date(base.year, base.month, 16), date(base.year, base.month, last)


def _quincena_list(today: date, count: int = 12):
    """Return list of (start, end, label) for last N quincenas."""
    result = []
    y, m = today.year, today.month
    half = 2 if today.day > 15 else 1
    for _ in range(count):
        if half == 1:
            s = date(y, m, 1); e = date(y, m, 15)
        else:
            import calendar
            last = calendar.monthrange(y, m)[1]
            s = date(y, m, 16); e = date(y, m, last)
        result.append((s, e, f"Q {s.strftime('%d/%m')} – {e.strftime('%d/%m/%Y')}"))
        # Go back one quincena
        if half == 2:
            half = 1
        else:
            half = 2
            m -= 1
            if m == 0:
                m = 12; y -= 1
    return result

@bp_payroll.route("/")
@login_required
@role_required("viewer", "manager", "admin")
def payroll_page():
    from services.attendance import get_weekly_attendance
    T          = g.T
    period_type = request.args.get("period_type", "weekly")  # weekly or quincena
    week_type  = request.args.get("week_type", "mon_sat")
    period_param = request.args.get("week") or request.args.get("period")
    emp_filt   = request.args.get("user") or None
    dept_filt  = request.args.get("dept") or None
    today      = date.today()

    if period_type == "quincena":
        sel_date = datetime.fromisoformat(period_param).date() if period_param else today
        week_start, week_end = _quincena_bounds(sel_date)
        week_list = _quincena_list(today)
    else:
        sel_date = datetime.fromisoformat(period_param).date() if period_param else today
        week_start, week_end = _week_bounds(sel_date, week_type)
        week_list = []
        for i in range(12):
            ref = today - timedelta(days=i*7)
            ws, we = _week_bounds(ref, week_type)
            week_list.append((ws, we, f"{ws.strftime('%b %d')} → {we.strftime('%b %d')}"))

    week_dates = []
    d = week_start
    while d <= week_end:
        week_dates.append(d); d += timedelta(days=1)

    data = get_weekly_attendance(week_start, week_end, emp_filt)

    # Filter by department if selected
    if dept_filt:
        conn = get_conn()
        dept_emps = set(str(r["employee_id"]) for r in conn.execute(
            "SELECT employee_id FROM users WHERE department_id=? AND is_active=1",
            (dept_filt,)
        ).fetchall())
        conn.close()
        data = {k: v for k, v in data.items() if str(k) in dept_emps}

    conn = get_conn()
    users_list = conn.execute(
        "SELECT employee_id, COALESCE(name, employee_id) as name FROM users WHERE is_active=1 ORDER BY name"
    ).fetchall()
    depts_list = conn.execute(
        "SELECT id, name FROM departments ORDER BY name"
    ).fetchall()
    conn.close()

    return render_template("payroll.html", T=T,
        period_type=period_type,
        week_type=week_type, week_start=week_start, week_end=week_end,
        week_dates=week_dates, data=data, week_list=week_list,
        users=users_list, depts=depts_list,
        selected_user=emp_filt, selected_dept=dept_filt,
        selected_week=week_start)

@bp_payroll.route("/export/weekly")
@login_required
@role_required("viewer", "manager", "admin")
def export_weekly():
    from services.reports import export_weekly_fifo
    period_type = request.args.get("period_type", "weekly")
    week_type   = request.args.get("week_type", "mon_sat")
    week_param  = request.args.get("week")
    today       = date.today()
    sel_date    = datetime.fromisoformat(week_param).date() if week_param else today
    if period_type == "quincena":
        week_start, week_end = _quincena_bounds(sel_date)
    else:
        week_start, week_end = _week_bounds(sel_date, week_type)
    from flask import session
    lang     = session.get("lang", "es")
    dept_filt = request.args.get("dept") or None
    emp_ids   = None
    if dept_filt:
        conn = get_conn()
        emp_ids = [str(r["employee_id"]) for r in conn.execute(
            "SELECT employee_id FROM users WHERE department_id=? AND is_active=1",
            (dept_filt,)
        ).fetchall()]
        conn.close()
    buf  = export_weekly_fifo(week_start, week_end, lang=lang, emp_ids=emp_ids)
    label = "quincena" if period_type == "quincena" else "semana"
    return send_file(buf, as_attachment=True,
        download_name=f"asistencia_{label}_{week_start}_{week_end}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@bp_payroll.route("/export/department")
@login_required
@role_required("manager", "admin")
def export_department():
    from services.reports import export_department_report
    from flask import session
    week_type  = request.args.get("week_type", "mon_sat")
    week_param = request.args.get("week")
    today      = date.today()
    sel_date   = datetime.fromisoformat(week_param).date() if week_param else today
    week_start, week_end = _week_bounds(sel_date, week_type)
    lang      = session.get("lang", "es")
    dept_filt = request.args.get("dept") or None
    buf = export_department_report(week_start, week_end, lang=lang, dept_filter=dept_filt)
    return send_file(buf, as_attachment=True,
        download_name=f"dept_report_{week_start}_{week_end}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

@bp_payroll.route("/export/monthly")
@login_required
@role_required("manager", "admin")
def export_monthly():
    from services.reports import export_monthly_summary
    year  = int(request.args.get("year",  date.today().year))
    month = int(request.args.get("month", date.today().month))
    from flask import session
    lang      = session.get("lang", "es")
    dept_filt = request.args.get("dept") or None
    buf   = export_monthly_summary(year, month, lang=lang, dept_filter=dept_filt)
    return send_file(buf, as_attachment=True,
        download_name=f"monthly_{year}_{month:02d}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
