"""
services/attendance.py
Daily and weekly attendance calculation.
"""
from datetime import datetime, timedelta, date
from db import get_conn, get_setting

DUPLICATE_WINDOW = 60  # seconds

def _parse_dt(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s)[:19])
    except Exception:
        return None

def get_daily_attendance(target_date: date, employee_id=None, device_id=None) -> dict:
    """
    Returns {employee_id: {name, in, out, worked_seconds, hours, flags}}
    for a given date.
    """
    conn = get_conn()
    sql = """
        SELECT e.employee_id, COALESCE(u.name, e.name, e.employee_id) as name,
               e.timestamp, e.direction, e.device_id,
               d.stage AS device_stage,
               COALESCE(u.requires_production, 0) AS requires_production
        FROM events e
        LEFT JOIN users u ON u.employee_id = e.employee_id
        LEFT JOIN devices d ON d.id = e.device_id
        WHERE DATE(e.timestamp) = ?
          AND e.employee_id IS NOT NULL
    """
    params = [target_date.isoformat()]
    if employee_id:
        sql += " AND e.employee_id = ?"
        params.append(str(employee_id))
    if device_id:
        sql += " AND e.device_id = ?"
        params.append(int(device_id))
    sql += " ORDER BY e.employee_id, e.timestamp"

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    grouped = {}
    for r in rows:
        emp = r["employee_id"]
        dt = _parse_dt(r["timestamp"])
        if not dt:
            continue
        if emp not in grouped:
            grouped[emp] = {"name": r["name"], "events": [],
                            "requires_production": bool(r["requires_production"])}
        grouped[emp]["events"].append({
            "dt": dt, "direction": r["direction"],
            "stage": (r["device_stage"] or "").lower()})

    result = {}
    for emp, data in grouped.items():
        events = sorted(data["events"], key=lambda x: x["dt"])
        events = _deduplicate(events)

        ins  = [e["dt"] for e in events if e.get("direction") in ("IN", None)]
        outs = [e["dt"] for e in events if e.get("direction") in ("OUT", None)]

        first_in  = min(ins)  if ins  else (events[0]["dt"] if events else None)
        last_out  = max(outs) if outs else (events[-1]["dt"] if events else None)

        flags = []

        # Two-stage users: the official start is their first PRODUCTION-stage
        # punch, not the entrance/gate. The arrival (first non-production punch)
        # is still surfaced separately so HR can see the gap to the floor.
        # Falls back to normal first-in when there's no production punch yet, so
        # a missing floor punch doesn't erase the day — it's flagged instead.
        entrance_in = None
        if data.get("requires_production"):
            prod_punches = [e["dt"] for e in events if e.get("stage") == "production"]
            ent_punches  = [e["dt"] for e in events if e.get("stage") != "production"]
            entrance_in = min(ent_punches) if ent_punches else None
            if prod_punches:
                first_in = min(prod_punches)
            else:
                flags.append("no_production")

        if len(events) == 1:
            flags.append("single_punch")
        if not ins:
            flags.append("no_in")
        if not outs:
            flags.append("no_out")

        worked = 0
        if first_in and last_out:
            lo = last_out
            if lo < first_in:
                lo += timedelta(days=1)
            worked = max(0, int((lo - first_in).total_seconds()))

        if worked < 600:
            flags.append("short_day")

        result[emp] = {
            "name": data["name"],
            "in":   first_in.strftime("%H:%M") if first_in else None,
            "out":  last_out.strftime("%H:%M") if last_out else None,
            "in_dt": first_in,
            "out_dt": last_out,
            "entrance_in": entrance_in.strftime("%H:%M") if entrance_in else None,
            "requires_production": bool(data.get("requires_production")),
            "worked_seconds": worked,
            "hours": round(worked / 3600, 2),
            "punch_count": len(events),
            "flags": flags,
        }
    return result

def _deduplicate(events):
    cleaned, last = [], None
    for e in events:
        if last and (e["dt"] - last).total_seconds() <= DUPLICATE_WINDOW and \
           e.get("direction") == events[max(0, events.index(e)-1)].get("direction"):
            continue
        cleaned.append(e)
        last = e["dt"]
    return cleaned

def get_weekly_attendance(week_start: date, week_end: date, employee_id=None) -> dict:
    """
    Returns {employee_id: {name, days: {iso_date: day_rec}, total_hours, total_days}}
    """
    conn = get_conn()
    sql = """
        SELECT e.employee_id, COALESCE(u.name, e.name, e.employee_id) as name,
               e.timestamp, e.direction,
               d.stage AS device_stage,
               COALESCE(u.requires_production, 0) AS requires_production
        FROM events e
        LEFT JOIN users u ON u.employee_id = e.employee_id
        LEFT JOIN devices d ON d.id = e.device_id
        WHERE DATE(e.timestamp) BETWEEN ? AND ?
          AND e.employee_id IS NOT NULL
    """
    params = [week_start.isoformat(), week_end.isoformat()]
    if employee_id:
        sql += " AND e.employee_id = ?"
        params.append(str(employee_id))
    sql += " ORDER BY e.employee_id, e.timestamp"

    rows = conn.execute(sql, params).fetchall()
    conn.close()

    by_emp_day = {}
    for r in rows:
        emp = r["employee_id"]
        dt = _parse_dt(r["timestamp"])
        if not dt:
            continue
        day = dt.date().isoformat()
        by_emp_day.setdefault(emp, {"name": r["name"], "days": {},
                                    "requires_production": bool(r["requires_production"])})
        by_emp_day[emp]["days"].setdefault(day, []).append({
            "dt": dt, "direction": r["direction"],
            "stage": (r["device_stage"] or "").lower()
        })

    result = {}
    for emp, data in by_emp_day.items():
        total_secs = 0
        days_out = {}
        for day, evts in data["days"].items():
            evts = sorted(evts, key=lambda x: x["dt"])
            evts = _deduplicate(evts)
            ins  = [e["dt"] for e in evts if e.get("direction") in ("IN", None)]
            outs = [e["dt"] for e in evts if e.get("direction") in ("OUT", None)]
            fi = min(ins)  if ins  else (evts[0]["dt"]  if evts else None)
            lo = max(outs) if outs else (evts[-1]["dt"] if evts else None)
            flags = []
            # Two-stage users: paid start is the first production-stage punch.
            if data.get("requires_production"):
                prod = [e["dt"] for e in evts if e.get("stage") == "production"]
                if prod:
                    fi = min(prod)
                else:
                    flags.append("no_production")
            if len(evts) == 1: flags.append("single_punch")
            if not ins:        flags.append("no_in")
            if not outs:       flags.append("no_out")
            worked = 0
            if fi and lo:
                if lo < fi: lo += timedelta(days=1)
                worked = max(0, int((lo - fi).total_seconds()))
            total_secs += worked
            days_out[day] = {
                "in":    fi.strftime("%H:%M") if fi else None,
                "out":   lo.strftime("%H:%M") if lo else None,
                "hours": round(worked / 3600, 2),
                "flags": flags,
                "punches": len(evts),
            }
        result[emp] = {
            "name": data["name"],
            "days": days_out,
            "total_hours": round(total_secs / 3600, 2),
            "total_days": len(days_out),
        }

    # ── Merge approved holidays as "vacation" day cells ──────────────────────
    # Employees on approved holiday show a vacation marker instead of looking
    # absent. Days with punches keep their punch data (holiday flag added).
    try:
        from services.holidays import holiday_days_in_range
        conn2 = get_conn()
        hol_map = holiday_days_in_range(conn2, week_start, week_end)
        if hol_map:
            # Names for employees who have no events this week
            missing = [e for e in hol_map if e not in result]
            names = {}
            if missing:
                q = ",".join("?" * len(missing))
                for r in conn2.execute(
                        f"SELECT employee_id, name FROM users WHERE employee_id IN ({q})",
                        missing).fetchall():
                    names[str(r["employee_id"])] = r["name"]
        conn2.close()
        for emp, days in hol_map.items():
            if emp not in result:
                result[emp] = {"name": names.get(emp, emp), "days": {},
                               "total_hours": 0, "total_days": 0}
            for day in sorted(days):
                if day in result[emp]["days"]:
                    result[emp]["days"][day].setdefault("flags", []).append("vacation")
                else:
                    result[emp]["days"][day] = {
                        "in": None, "out": None, "hours": 0,
                        "flags": ["vacation"], "punches": 0,
                    }
    except Exception:
        # Holiday integration must never break the attendance report
        pass

    return result

def get_department_summary(target_date: date) -> list:
    """Returns per-department attendance counts for a given date."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT
            COALESCE(d.name, 'No Department') as dept,
            COUNT(DISTINCT u.employee_id) as total_users,
            COUNT(DISTINCT e.employee_id) as present
        FROM users u
        LEFT JOIN departments d ON d.id = u.department_id
        LEFT JOIN events e ON e.employee_id = u.employee_id
            AND DATE(e.timestamp) = ?
        WHERE u.is_active = 1
        GROUP BY d.name
        ORDER BY d.name
    """, (target_date.isoformat(),)).fetchall()
    conn.close()
    return [dict(r) for r in rows]

def get_late_arrivals(target_date: date) -> list:
    """Returns employees who arrived late based on their schedule."""
    attendance = get_daily_attendance(target_date)
    conn = get_conn()
    late = []
    for emp_id, rec in attendance.items():
        if not rec.get("in_dt"):
            continue
        sched = conn.execute("""
            SELECT td.start_time
            FROM users u
            JOIN user_schedule_assignments usa ON usa.employee_id = u.employee_id
            JOIN schedule_templates st ON st.id = usa.template_id
            JOIN schedule_template_days td ON td.template_id = st.id
            WHERE u.employee_id = ? AND td.weekday = ?
        """, (emp_id, target_date.weekday())).fetchone()
        if not sched:
            continue
        try:
            sched_hour, sched_min = map(int, sched["start_time"].split(":"))
            scheduled_in = rec["in_dt"].replace(hour=sched_hour, minute=sched_min, second=0)
            grace = 10 * 60  # 10 min grace
            if (rec["in_dt"] - scheduled_in).total_seconds() > grace:
                delay = int((rec["in_dt"] - scheduled_in).total_seconds() / 60)
                late.append({**rec, "employee_id": emp_id, "delay_minutes": delay,
                              "scheduled": sched["start_time"]})
        except Exception:
            continue
    conn.close()
    return late
