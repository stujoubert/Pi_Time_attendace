import csv
import io
from flask import Blueprint, render_template, request, g, Response
from datetime import datetime, date, timedelta
from db import get_conn, list_devices
from authz import login_required, role_required

bp = Blueprint("attendance", __name__)

# ─── Daily ───────────────────────────────────────────────────────────────────

@bp.route("/daily")
@login_required
@role_required("viewer", "manager", "admin")
def daily():
    T = g.T
    day_str  = request.args.get("date") or date.today().isoformat()
    emp_filt = request.args.get("user") or None
    dev_filt = request.args.get("device") or None

    conn = get_conn()
    sql = """
        SELECT e.employee_id, COALESCE(u.name, e.name, e.employee_id) as name,
               e.timestamp, e.direction, d.name as device_name
        FROM events e
        LEFT JOIN users u ON u.employee_id=e.employee_id
        LEFT JOIN devices d ON d.id=e.device_id
        WHERE DATE(e.timestamp)=?
    """
    params = [day_str]
    if emp_filt:
        sql += " AND e.employee_id=?"; params.append(emp_filt)
    if dev_filt:
        sql += " AND e.device_id=?"; params.append(dev_filt)
    sql += " ORDER BY e.employee_id, e.timestamp"

    rows = conn.execute(sql, params).fetchall()
    users_list = conn.execute(
        "SELECT DISTINCT employee_id, COALESCE(name, employee_id) as name FROM users WHERE is_active=1 ORDER BY name"
    ).fetchall()
    # Employees on approved holiday this day (shown as Vacaciones, not absent)
    from services.holidays import employees_on_holiday
    try:
        on_holiday_ids = employees_on_holiday(conn, day_str)
    except Exception:
        on_holiday_ids = set()
    on_holiday = [
        {"employee_id": u["employee_id"], "name": u["name"]}
        for u in users_list if str(u["employee_id"]) in on_holiday_ids
    ]
    conn.close()

    grouped = {}
    for r in rows:
        emp = r["employee_id"]
        grouped.setdefault(emp, {"name": r["name"], "events": []})
        grouped[emp]["events"].append(r)

    return render_template("daily.html", T=T, grouped=grouped,
        daily_date=day_str, devices=list_devices(False),
        users=users_list, selected_user=emp_filt, selected_device=dev_filt,
        on_holiday=on_holiday)

# ─── Weekly ───────────────────────────────────────────────────────────────────

@bp.route("/weekly")
@login_required
@role_required("viewer", "manager", "admin")
def weekly():
    from services.attendance import get_weekly_attendance
    T = g.T
    week_type = request.args.get("week_type", "mon_sat")
    date_param = request.args.get("date")
    emp_filt   = request.args.get("user") or None

    today = date.today()
    sel_date = datetime.fromisoformat(date_param).date() if date_param else today
    week_start, week_end = _week_bounds(sel_date, week_type)

    summary = get_weekly_attendance(week_start, week_end, emp_filt)

    week_list = _build_week_list(week_type, today)
    conn = get_conn()
    users_list = conn.execute(
        "SELECT employee_id, COALESCE(name, employee_id) as name FROM users WHERE is_active=1 ORDER BY name"
    ).fetchall()
    conn.close()

    return render_template("weekly.html", T=T,
        week_type=week_type, week_start=week_start.isoformat(),
        week_end=week_end.isoformat(), summary=summary,
        week_list=week_list,
        users=users_list, selected_user=emp_filt,
        selected_date=sel_date.isoformat())

# ─── Audit ───────────────────────────────────────────────────────────────────

@bp.route("/audit")
@login_required
@role_required("viewer", "manager", "admin")
def daily_audit():
    T = g.T
    day_str  = request.args.get("date") or date.today().isoformat()
    emp_filt = request.args.get("user") or None
    dev_filt = request.args.get("device") or None

    conn = get_conn()
    sql = """
        SELECT e.id, e.employee_id, COALESCE(u.name, e.name, e.employee_id) as name,
               e.timestamp, e.direction, d.name as device_name
        FROM events e
        LEFT JOIN users u ON u.employee_id=e.employee_id
        LEFT JOIN devices d ON d.id=e.device_id
        WHERE DATE(e.timestamp)=?
    """
    params = [day_str]
    if emp_filt:
        sql += " AND e.employee_id=?"; params.append(emp_filt)
    if dev_filt:
        sql += " AND e.device_id=?"; params.append(dev_filt)
    sql += " ORDER BY e.employee_id, e.timestamp"

    rows = conn.execute(sql, params).fetchall()
    users_list = conn.execute(
        "SELECT DISTINCT e.employee_id, COALESCE(u.name, e.employee_id) as name FROM events e LEFT JOIN users u ON u.employee_id=e.employee_id ORDER BY name"
    ).fetchall()
    conn.close()

    # Flag duplicates
    grouped = {}
    for r in rows:
        emp = r["employee_id"]
        grouped.setdefault(emp, {"name": r["name"], "events": []})
        ev = dict(r)
        ev["duplicate"] = False
        events = grouped[emp]["events"]
        if events:
            prev_ts = datetime.fromisoformat(events[-1]["timestamp"][:19])
            curr_ts = datetime.fromisoformat(r["timestamp"][:19])
            if (curr_ts - prev_ts).total_seconds() <= 60:
                ev["duplicate"] = True
        grouped[emp]["events"].append(ev)

    return render_template("audit.html", T=T, grouped=grouped,
        selected_date=day_str, devices=list_devices(False),
        users=users_list, selected_user=emp_filt, selected_device=dev_filt)

@bp.route("/audit/export")
@login_required
@role_required("viewer", "manager", "admin")
def audit_export():
    day_str  = request.args.get("date") or date.today().isoformat()
    emp_filt = request.args.get("user") or None
    conn = get_conn()
    sql = """
        SELECT e.employee_id, COALESCE(u.name, e.name, e.employee_id) as name,
               e.timestamp, e.direction, d.name as device_name
        FROM events e
        LEFT JOIN users u ON u.employee_id=e.employee_id
        LEFT JOIN devices d ON d.id=e.device_id
        WHERE DATE(e.timestamp)=?
    """
    params = [day_str]
    if emp_filt:
        sql += " AND e.employee_id=?"; params.append(emp_filt)
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["Employee ID", "Name", "Timestamp", "Direction", "Device"])
    for r in rows:
        w.writerow([r["employee_id"], r["name"], r["timestamp"], r["direction"], r["device_name"]])

    return Response(out.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=audit_{day_str}.csv"})

# ─── Helpers ──────────────────────────────────────────────────────────────────

def _week_bounds(base: date, week_type: str):
    wd = base.weekday()
    if week_type == "sat_fri":
        start = base - timedelta(days=(wd - 5) % 7)
        end   = start + timedelta(days=6)
    elif week_type == "mon_fri":
        start = base - timedelta(days=wd)
        end   = start + timedelta(days=4)
    elif week_type == "sun_sat":
        start = base - timedelta(days=(wd + 1) % 7)
        end   = start + timedelta(days=6)
    else:  # mon_sat default
        start = base - timedelta(days=wd)
        end   = start + timedelta(days=5)
    return start, end

def _build_week_list(week_type: str, today: date, count: int = 12):
    weeks = []
    for i in range(count):
        ref = today - timedelta(days=i * 7)
        ws, we = _week_bounds(ref, week_type)
        weeks.append((ws.isoformat(), we.isoformat(), f"{ws.strftime('%b %d')} → {we.strftime('%b %d')}"))
    return weeks
