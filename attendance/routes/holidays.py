"""
routes/holidays.py

Self-contained holiday / vacation module.

PRIVACY MODEL (important):
  - Employee-facing routes derive the employee_id from the SESSION only
    (session["employee_id"]). They never accept an employee_id from the
    request, so an employee can only ever see or affect their OWN records.
  - HR/manager routes are gated by role_required("admin","manager") and are
    the only place multiple employees' data is visible.
"""
from flask import (Blueprint, render_template, request, redirect, url_for,
                   flash, session, g, jsonify, abort)
from datetime import date, datetime, timedelta
from db import get_conn
from authz import login_required, role_required
from services.holidays import (count_working_days, get_balance,
                               working_days_list, mx_public_holidays)

bp = Blueprint("holidays", __name__, url_prefix="/holidays")

HR_ROLES = ("admin", "manager")


def _today_year():
    return date.today().year


def _parse_date(s):
    return datetime.strptime(s, "%Y-%m-%d").date()


# ── Employee self-service ─────────────────────────────────────────────────────

@bp.route("/")
@login_required
def my_holidays():
    """Employee's own dashboard: balance + their request list. Own data only."""
    emp_id = session.get("employee_id")
    if not emp_id:
        # Staff accounts without an employee link: send them to the HR view
        if session.get("role") in HR_ROLES:
            return redirect(url_for("holidays.hr_queue"))
        flash("Your account isn't linked to an employee record. Contact HR.", "warning")
        return redirect(url_for("dashboard.dashboard"))

    year = _today_year()
    conn = get_conn()
    balance = get_balance(conn, emp_id, year)
    requests_ = conn.execute(
        """SELECT * FROM holiday_requests
           WHERE employee_id=? ORDER BY start_date DESC""",
        (str(emp_id),)
    ).fetchall()
    conn.close()
    return render_template("holidays_my.html",
                           balance=balance, requests=requests_, year=year)


@bp.route("/request", methods=["POST"])
@login_required
def submit_request():
    """Employee submits a holiday request for THEMSELVES."""
    emp_id = session.get("employee_id")
    if not emp_id:
        abort(403)

    try:
        start = _parse_date(request.form.get("start_date", ""))
        end = _parse_date(request.form.get("end_date", ""))
    except ValueError:
        flash("Please provide valid start and end dates.", "danger")
        return redirect(url_for("holidays.my_holidays"))

    note = (request.form.get("note") or "").strip()[:500]

    if end < start:
        flash("End date can't be before start date.", "danger")
        return redirect(url_for("holidays.my_holidays"))
    if start < date.today():
        flash("You can't request holidays in the past.", "danger")
        return redirect(url_for("holidays.my_holidays"))

    days = count_working_days(start, end)
    if days <= 0:
        flash("That range contains no working days (weekends and public "
              "holidays don't count).", "warning")
        return redirect(url_for("holidays.my_holidays"))

    conn = get_conn()
    # Balance enforcement: employee can't request more than remaining minus
    # what's already pending (pending days are committed until decided).
    bal = get_balance(conn, emp_id, start.year)
    available = bal["remaining"] - bal["pending"]
    if days > available:
        conn.close()
        flash(f"Not enough days: this request needs {days} working day(s) but "
              f"you only have {max(0, available)} available "
              f"({bal['remaining']} remaining, {bal['pending']} already pending).",
              "danger")
        return redirect(url_for("holidays.my_holidays"))

    # Block overlapping pending/approved requests for the same employee
    clash = conn.execute(
        """SELECT COUNT(*) AS c FROM holiday_requests
           WHERE employee_id=? AND status IN ('pending','approved')
             AND NOT (end_date < ? OR start_date > ?)""",
        (str(emp_id), start.isoformat(), end.isoformat())
    ).fetchone()
    if clash and clash["c"] > 0:
        conn.close()
        flash("You already have a request that overlaps those dates.", "warning")
        return redirect(url_for("holidays.my_holidays"))

    conn.execute(
        """INSERT INTO holiday_requests
           (employee_id, start_date, end_date, days_count, note, status)
           VALUES (?,?,?,?,?,'pending')""",
        (str(emp_id), start.isoformat(), end.isoformat(), days, note))
    conn.commit()
    conn.close()
    flash(f"Request submitted for {days} working day(s). Awaiting HR approval.",
          "success")
    return redirect(url_for("holidays.my_holidays"))


@bp.route("/cancel/<int:req_id>", methods=["POST"])
@login_required
def cancel_request(req_id):
    """Employee cancels their OWN still-pending request."""
    emp_id = session.get("employee_id")
    if not emp_id:
        abort(403)
    conn = get_conn()
    row = conn.execute(
        "SELECT employee_id, status FROM holiday_requests WHERE id=?",
        (req_id,)).fetchone()
    # Ownership check: must belong to the logged-in employee
    if not row or str(row["employee_id"]) != str(emp_id):
        conn.close()
        abort(403)
    if row["status"] != "pending":
        conn.close()
        flash("Only pending requests can be cancelled.", "warning")
        return redirect(url_for("holidays.my_holidays"))
    conn.execute("UPDATE holiday_requests SET status='cancelled' WHERE id=?", (req_id,))
    conn.commit()
    conn.close()
    flash("Request cancelled.", "success")
    return redirect(url_for("holidays.my_holidays"))


@bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    """Any logged-in account can change its OWN password (mainly for the
    employee role, which has no other self-service path)."""
    from werkzeug.security import generate_password_hash, check_password_hash
    if request.method == "POST":
        current = request.form.get("current_password", "")
        new = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if len(new) < 6:
            flash("New password must be at least 6 characters.", "danger")
            return redirect(url_for("holidays.change_password"))
        if new == "admin":
            flash("That is the default password — choose a different one.", "danger")
            return redirect(url_for("holidays.change_password"))
        if new != confirm:
            flash("New passwords don't match.", "danger")
            return redirect(url_for("holidays.change_password"))
        conn = get_conn()
        acct = conn.execute("SELECT password_hash FROM accounts WHERE id=?",
                            (session.get("account_id"),)).fetchone()
        if not acct or not check_password_hash(acct["password_hash"], current):
            conn.close()
            flash("Current password is incorrect.", "danger")
            return redirect(url_for("holidays.change_password"))
        conn.execute("UPDATE accounts SET password_hash=? WHERE id=?",
                     (generate_password_hash(new), session.get("account_id")))
        conn.commit()
        conn.close()
        session.pop("must_change_password", None)
        flash("Password updated.", "success")
        if session.get("role") == "employee":
            return redirect(url_for("holidays.my_holidays"))
        return redirect(url_for("dashboard.dashboard"))
    return render_template("change_password.html")


@bp.route("/my-calendar-data")
@login_required
def my_calendar_data():
    """JSON of the employee's OWN approved + pending days for the personal
    calendar. Own data only."""
    emp_id = session.get("employee_id")
    if not emp_id:
        abort(403)
    year = int(request.args.get("year", _today_year()))
    conn = get_conn()
    rows = conn.execute(
        """SELECT start_date, end_date, status FROM holiday_requests
           WHERE employee_id=? AND status IN ('approved','pending')""",
        (str(emp_id),)
    ).fetchall()
    conn.close()
    days = {}
    for r in rows:
        try:
            s = _parse_date(r["start_date"]); e = _parse_date(r["end_date"])
        except ValueError:
            continue
        for d in working_days_list(s, e):
            if d.year == year:
                # approved beats pending if overlap
                if days.get(d.isoformat()) != "approved":
                    days[d.isoformat()] = r["status"]
    hols = [h.isoformat() for h in mx_public_holidays(year)]
    return jsonify({"days": days, "holidays": hols, "year": year})


# ── HR / manager ──────────────────────────────────────────────────────────────

@bp.route("/hr")
@login_required
@role_required(*HR_ROLES)
def hr_queue():
    """HR view: pending queue + recent decisions. Multi-employee (HR only)."""
    conn = get_conn()
    pending = conn.execute(
        """SELECT hr.*, u.name AS emp_name
           FROM holiday_requests hr
           LEFT JOIN users u ON u.employee_id = hr.employee_id
           WHERE hr.status='pending'
           ORDER BY hr.requested_at ASC""").fetchall()
    # Attach each requester's remaining balance so HR sees over-balance asks
    pending_rows = []
    for r in pending:
        year = int(str(r["start_date"])[:4]) if r["start_date"] else _today_year()
        bal = get_balance(conn, r["employee_id"], year)
        d = dict(r)
        d["remaining"] = bal["remaining"]
        d["over_balance"] = r["days_count"] > bal["remaining"]
        pending_rows.append(d)
    recent = conn.execute(
        """SELECT hr.*, u.name AS emp_name
           FROM holiday_requests hr
           LEFT JOIN users u ON u.employee_id = hr.employee_id
           WHERE hr.status IN ('approved','rejected')
           ORDER BY hr.decided_at DESC LIMIT 25""").fetchall()
    employees = conn.execute(
        "SELECT employee_id, name FROM users WHERE is_active=1 ORDER BY name"
    ).fetchall()
    conn.close()
    return render_template("holidays_hr.html", pending=pending_rows,
                           recent=recent, employees=employees)


@bp.route("/hr/calendar")
@login_required
@role_required(*HR_ROLES)
def hr_calendar():
    """HR team calendar: month grid of approved holidays for all employees,
    filterable by employee or department. HR/manager only."""
    from calendar import monthrange
    today = date.today()
    try:
        year = int(request.args.get("year", today.year))
        month = int(request.args.get("month", today.month))
        if not (1 <= month <= 12):
            raise ValueError
    except ValueError:
        year, month = today.year, today.month
    emp_filt = request.args.get("user") or None
    dept_filt = request.args.get("department") or None

    month_start = date(year, month, 1)
    month_end = date(year, month, monthrange(year, month)[1])

    conn = get_conn()
    sql = """SELECT hr.employee_id, hr.start_date, hr.end_date, hr.status,
                    COALESCE(u.name, hr.employee_id) AS name,
                    u.department_id
             FROM holiday_requests hr
             LEFT JOIN users u ON u.employee_id = hr.employee_id
             WHERE hr.status IN ('approved','pending')
               AND NOT (hr.end_date < ? OR hr.start_date > ?)"""
    params = [month_start.isoformat(), month_end.isoformat()]
    if emp_filt:
        sql += " AND hr.employee_id = ?"
        params.append(emp_filt)
    if dept_filt:
        sql += " AND u.department_id = ?"
        params.append(int(dept_filt))
    rows = conn.execute(sql, params).fetchall()

    employees = conn.execute(
        "SELECT employee_id, name FROM users WHERE is_active=1 ORDER BY name"
    ).fetchall()
    departments = conn.execute(
        "SELECT id, name FROM departments ORDER BY name").fetchall()
    conn.close()

    # day (iso) -> [ {employee_id, name, status} ], working days only.
    # Approved chips list before pending ones within each day.
    from services.holidays import working_days_list
    day_map = {}
    for r in rows:
        try:
            rs = date.fromisoformat(str(r["start_date"]))
            re_ = date.fromisoformat(str(r["end_date"]))
        except ValueError:
            continue
        for d in working_days_list(max(rs, month_start), min(re_, month_end)):
            day_map.setdefault(d.isoformat(), []).append(
                {"employee_id": str(r["employee_id"]), "name": r["name"],
                 "status": r["status"]})
    for day in day_map:
        day_map[day].sort(key=lambda p: (p["status"] != "approved", p["name"]))

    # Month grid: list of weeks, each week a list of 7 cells (None = padding)
    first_dow = month_start.weekday()  # Mon=0
    weeks, week = [], [None] * first_dow
    d = month_start
    while d <= month_end:
        week.append(d)
        if len(week) == 7:
            weeks.append(week)
            week = []
        d += timedelta(days=1)
    if week:
        weeks.append(week + [None] * (7 - len(week)))

    hols = mx_public_holidays(year)
    prev_y, prev_m = (year - 1, 12) if month == 1 else (year, month - 1)
    next_y, next_m = (year + 1, 1) if month == 12 else (year, month + 1)

    return render_template("holidays_calendar.html",
                           year=year, month=month, weeks=weeks,
                           day_map=day_map,
                           public_holidays={h.isoformat() for h in hols},
                           employees=employees, departments=departments,
                           emp_filt=emp_filt, dept_filt=dept_filt,
                           prev_y=prev_y, prev_m=prev_m,
                           next_y=next_y, next_m=next_m,
                           today_iso=today.isoformat())


@bp.route("/hr/pending-count")
@login_required
@role_required(*HR_ROLES)
def hr_pending_count():
    """AJAX: number of pending requests, for the nav badge."""
    conn = get_conn()
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM holiday_requests WHERE status='pending'"
    ).fetchone()["c"]
    conn.close()
    return jsonify({"pending": n})


@bp.route("/hr/decide/<int:req_id>", methods=["POST"])
@login_required
@role_required(*HR_ROLES)
def hr_decide(req_id):
    """Approve or reject a pending request."""
    decision = request.form.get("decision")
    note = (request.form.get("decision_note") or "").strip()[:500]
    if decision not in ("approved", "rejected"):
        abort(400)
    conn = get_conn()
    row = conn.execute("SELECT * FROM holiday_requests WHERE id=?", (req_id,)).fetchone()
    if not row:
        conn.close()
        flash("Request not found.", "danger")
        return redirect(url_for("holidays.hr_queue"))
    if row["status"] != "pending":
        conn.close()
        flash("That request has already been decided.", "warning")
        return redirect(url_for("holidays.hr_queue"))
    conn.execute(
        """UPDATE holiday_requests
           SET status=?, decided_at=datetime('now'), decided_by=?, decision_note=?
           WHERE id=?""",
        (decision, session.get("username"), note, req_id))
    conn.commit()
    conn.close()
    flash(f"Request {decision}.", "success")
    return redirect(url_for("holidays.hr_queue"))


@bp.route("/hr/create", methods=["POST"])
@login_required
@role_required(*HR_ROLES)
def hr_create():
    """HR creates a request on behalf of an employee (verbal ask, no login).
    HR chooses whether it lands pending or directly approved."""
    emp_id = (request.form.get("employee_id") or "").strip()
    approve_now = request.form.get("approve_now") == "1"
    try:
        start = _parse_date(request.form.get("start_date", ""))
        end = _parse_date(request.form.get("end_date", ""))
    except ValueError:
        flash("Please provide valid start and end dates.", "danger")
        return redirect(url_for("holidays.hr_queue"))
    note = (request.form.get("note") or "").strip()[:500]

    if not emp_id:
        flash("Select an employee.", "danger")
        return redirect(url_for("holidays.hr_queue"))
    if end < start:
        flash("End date can't be before start date.", "danger")
        return redirect(url_for("holidays.hr_queue"))

    days = count_working_days(start, end)
    if days <= 0:
        flash("That range contains no working days.", "warning")
        return redirect(url_for("holidays.hr_queue"))

    conn = get_conn()
    emp = conn.execute("SELECT employee_id FROM users WHERE employee_id=?",
                       (emp_id,)).fetchone()
    if not emp:
        conn.close()
        flash(f"Employee {emp_id} not found.", "danger")
        return redirect(url_for("holidays.hr_queue"))
    clash = conn.execute(
        """SELECT COUNT(*) AS c FROM holiday_requests
           WHERE employee_id=? AND status IN ('pending','approved')
             AND NOT (end_date < ? OR start_date > ?)""",
        (emp_id, start.isoformat(), end.isoformat())).fetchone()
    if clash and clash["c"] > 0:
        conn.close()
        flash("The employee already has a request overlapping those dates.", "warning")
        return redirect(url_for("holidays.hr_queue"))

    # Show a warning if this pushes over balance, but let HR decide.
    bal = get_balance(conn, emp_id, start.year)
    if approve_now:
        conn.execute(
            """INSERT INTO holiday_requests
               (employee_id, start_date, end_date, days_count, note, status,
                decided_at, decided_by, decision_note)
               VALUES (?,?,?,?,?,'approved',datetime('now'),?,?)""",
            (emp_id, start.isoformat(), end.isoformat(), days, note,
             session.get("username"), "Created by HR"))
    else:
        conn.execute(
            """INSERT INTO holiday_requests
               (employee_id, start_date, end_date, days_count, note, status)
               VALUES (?,?,?,?,?,'pending')""",
            (emp_id, start.isoformat(), end.isoformat(), days, note))
    conn.commit()
    conn.close()
    msg = f"Request created for {emp_id}: {days} working day(s), " \
          f"{'approved' if approve_now else 'pending'}."
    if days > bal["remaining"]:
        msg += f" WARNING: exceeds remaining balance ({bal['remaining']})."
        flash(msg, "warning")
    else:
        flash(msg, "success")
    return redirect(url_for("holidays.hr_queue"))


@bp.route("/hr/revoke/<int:req_id>", methods=["POST"])
@login_required
@role_required(*HR_ROLES)
def hr_revoke(req_id):
    """HR revokes a previously APPROVED request (plans changed). The balance
    restores automatically because it is computed from approved requests."""
    note = (request.form.get("decision_note") or "").strip()[:500]
    conn = get_conn()
    row = conn.execute("SELECT status FROM holiday_requests WHERE id=?", (req_id,)).fetchone()
    if not row:
        conn.close()
        flash("Request not found.", "danger")
        return redirect(url_for("holidays.hr_queue"))
    if row["status"] != "approved":
        conn.close()
        flash("Only approved requests can be revoked.", "warning")
        return redirect(url_for("holidays.hr_queue"))
    conn.execute(
        """UPDATE holiday_requests
           SET status='cancelled', decided_at=datetime('now'), decided_by=?,
               decision_note=COALESCE(NULLIF(?,''), 'Revoked by HR')
           WHERE id=?""",
        (session.get("username"), note, req_id))
    conn.commit()
    conn.close()
    flash("Approved request revoked; the days return to the employee's balance.", "success")
    return redirect(url_for("holidays.hr_queue"))


@bp.route("/hr/allowances", methods=["GET", "POST"])
@login_required
@role_required(*HR_ROLES)
def hr_allowances():
    """HR sets each employee's annual allowance (days entered manually)."""
    year = int(request.args.get("year", _today_year()))
    conn = get_conn()
    if request.method == "POST":
        emp_id = request.form.get("employee_id")
        try:
            days = max(0, int(request.form.get("days", 0)))
        except ValueError:
            days = 0
        post_year = int(request.form.get("year", year))
        if emp_id:
            conn.execute(
                """INSERT INTO holiday_allowance (employee_id, year, days)
                   VALUES (?,?,?)
                   ON CONFLICT(employee_id, year) DO UPDATE SET days=excluded.days""",
                (str(emp_id), post_year, days))
            conn.commit()
            flash(f"Allowance for {emp_id} set to {days} days ({post_year}).", "success")
        conn.close()
        return redirect(url_for("holidays.hr_allowances", year=post_year))

    # GET: list active employees with allowance + used for the year
    rows = conn.execute(
        """SELECT u.employee_id, u.name,
                  COALESCE(ha.days,0) AS allowance,
                  COALESCE((SELECT SUM(days_count) FROM holiday_requests hr
                            WHERE hr.employee_id=u.employee_id AND hr.status='approved'
                              AND substr(hr.start_date,1,4)=?),0) AS used
           FROM users u
           LEFT JOIN holiday_allowance ha
                  ON ha.employee_id=u.employee_id AND ha.year=?
           WHERE u.is_active=1
           ORDER BY u.name""",
        (str(year), year)
    ).fetchall()
    conn.close()
    return render_template("holidays_allowances.html", rows=rows, year=year)


@bp.route("/hr/calendar-data")
@login_required
@role_required(*HR_ROLES)
def hr_calendar_data():
    """JSON of ALL approved holidays for the HR team calendar (HR only)."""
    year = int(request.args.get("year", _today_year()))
    conn = get_conn()
    rows = conn.execute(
        """SELECT hr.start_date, hr.end_date, hr.employee_id, u.name AS emp_name
           FROM holiday_requests hr
           LEFT JOIN users u ON u.employee_id = hr.employee_id
           WHERE hr.status='approved'""").fetchall()
    conn.close()
    events = []
    for r in rows:
        events.append({
            "employee_id": r["employee_id"],
            "name": r["emp_name"] or r["employee_id"],
            "start": r["start_date"],
            "end": r["end_date"],
        })
    hols = [h.isoformat() for h in mx_public_holidays(year)]
    return jsonify({"events": events, "holidays": hols, "year": year})
