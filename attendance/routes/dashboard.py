from flask import Blueprint, render_template, request, g, session, redirect, url_for
from datetime import date, datetime
from calendar import monthrange
from db import get_conn
from authz import login_required

bp = Blueprint("dashboard", __name__, url_prefix="/dashboard")

@bp.route("/", methods=["GET"])
@login_required
def dashboard():
    # Employees have no business on the staff dashboard — send them to their
    # own holidays page.
    if session.get("role") == "employee":
        return redirect(url_for("holidays.my_holidays"))
    sel = request.args.get("date") or date.today().isoformat()
    sel_date = datetime.fromisoformat(sel).date()
    conn = get_conn()

    # Daily stats
    expected = conn.execute("SELECT COUNT(*) FROM users WHERE is_active=1").fetchone()[0]
    daily = conn.execute("""
        SELECT e.employee_id, MIN(e.timestamp) as first_in, MAX(e.timestamp) as last_out, COUNT(*) as cnt
        FROM events e
        WHERE DATE(e.timestamp)=?
        GROUP BY e.employee_id
    """, (sel,)).fetchall()
    attended = len(daily)
    # Employees on approved holiday today aren't absent — they're on vacation.
    try:
        from services.holidays import employees_on_holiday
        attended_ids = {str(r["employee_id"]) for r in daily}
        on_holiday_count = len(employees_on_holiday(conn, sel) - attended_ids)
    except Exception:
        on_holiday_count = 0

    # Department breakdown
    dept_stats = conn.execute("""
        SELECT COALESCE(d.name,'No Dept') as dept,
               COUNT(DISTINCT u.employee_id) as total,
               COUNT(DISTINCT e.employee_id) as present
        FROM users u
        LEFT JOIN departments d ON d.id=u.department_id
        LEFT JOIN events e ON e.employee_id=u.employee_id AND DATE(e.timestamp)=?
        WHERE u.is_active=1
        GROUP BY d.name ORDER BY d.name
    """, (sel,)).fetchall()

    # Monthly
    year, month = sel_date.year, sel_date.month
    days_in_month = monthrange(year, month)[1]
    month_start = f"{year}-{month:02d}-01"
    month_end   = f"{year}-{month:02d}-{days_in_month}"

    monthly = conn.execute("""
        SELECT e.employee_id,
               COALESCE(u.name, e.employee_id) as name,
               COALESCE(d.name,'—') as dept,
               COUNT(DISTINCT DATE(e.timestamp)) as days_present
        FROM events e
        LEFT JOIN users u ON u.employee_id=e.employee_id
        LEFT JOIN departments d ON d.id=u.department_id
        WHERE DATE(e.timestamp) BETWEEN ? AND ?
        GROUP BY e.employee_id
        ORDER BY u.name
    """, (month_start, month_end)).fetchall()

    # Recent events (last 20)
    recent = conn.execute("""
        SELECT e.employee_id, COALESCE(u.name, e.name, e.employee_id) as name,
               e.timestamp, e.direction, d.name as device_name
        FROM events e
        LEFT JOIN users u ON u.employee_id=e.employee_id
        LEFT JOIN devices d ON d.id=e.device_id
        ORDER BY e.timestamp DESC LIMIT 20
    """).fetchall()

    conn.close()

    T = g.T
    months = T.get("months_short", [])
    month_label = (months[month-1] + f" {year}") if months and len(months)>=month else sel_date.strftime("%B %Y")

    return render_template("dashboard.html",
        selected_date=sel,
        expected=expected,
        attended=attended,
        absent=max(0, expected - attended - on_holiday_count),
        on_holiday_count=on_holiday_count,
        attendance_rate=round(attended/expected*100,1) if expected else 0,
        dept_stats=dept_stats,
        monthly=monthly,
        days_in_month=days_in_month,
        month_label=month_label,
        recent=recent,
    )
