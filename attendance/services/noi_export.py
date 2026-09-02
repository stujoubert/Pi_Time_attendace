"""
services/noi_export.py

Generates Aspel NOI import files for a payroll period. NOI 10/11's import
wizard accepts Excel/TXT files with named columns and lets HR map them once,
saving the definition for reuse — so we produce clean .xlsx files with literal
values (NO formulas: NOI imports data, not spreadsheets).

Two files:
  1. Incidencias (días y horas): clave_trabajador, nombre, dias_trabajados,
     faltas, retardos, dias_vacaciones
  2. Vacaciones: clave_trabajador, nombre, fecha_inicio, fecha_fin, dias
     (one row per approved request overlapping the period)

Definitions:
  - dias_trabajados: distinct days in the period with at least one punch
  - faltas: working days (Mon-Fri minus MX public holidays) with no punches
    and NOT covered by an approved holiday
  - retardos: days where first punch is later than the assigned schedule
    start + 10 min grace (only for employees with a schedule assignment)
  - dias_vacaciones: approved-holiday working days inside the period
"""
from datetime import date, datetime, timedelta
from io import BytesIO
from db import get_conn
from services.holidays import (is_working_day, working_days_list,
                               holiday_days_in_range)

GRACE_MINUTES = 10


def _period_days(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def compute_period_summary(start: date, end: date) -> dict:
    """Compute the NOI incidence numbers for every active employee.
    Returns {employee_id: {name, dias_trabajados, faltas, retardos,
                           dias_vacaciones}}"""
    conn = get_conn()

    employees = {str(r["employee_id"]): {"name": r["name"],
                                         "dias_trabajados": 0,
                                         "faltas": 0,
                                         "retardos": 0,
                                         "dias_vacaciones": 0}
                 for r in conn.execute(
                     "SELECT employee_id, name FROM users WHERE is_active=1"
                 ).fetchall()}

    # Punch days per employee (distinct dates with events). first_in is the
    # day's earliest punch, EXCEPT for two-stage employees, whose effective
    # start is their first production-floor punch (matching the daily/weekly
    # rule) so they aren't marked late for the walk from the gate to the floor.
    two_stage = {str(r["employee_id"])
                 for r in conn.execute(
                     "SELECT employee_id FROM users WHERE requires_production=1"
                 ).fetchall()}
    punch_days = {}
    for r in conn.execute(
            """SELECT employee_id, DATE(timestamp) AS d,
                      MIN(timestamp) AS first_in
               FROM events
               WHERE DATE(timestamp) BETWEEN ? AND ?
                 AND employee_id IS NOT NULL
               GROUP BY employee_id, DATE(timestamp)""",
            (start.isoformat(), end.isoformat())).fetchall():
        punch_days.setdefault(str(r["employee_id"]), {})[r["d"]] = r["first_in"]

    # Override first_in with the first PRODUCTION punch for two-stage employees.
    if two_stage:
        for r in conn.execute(
                """SELECT e.employee_id, DATE(e.timestamp) AS d,
                          MIN(e.timestamp) AS prod_in
                   FROM events e JOIN devices dv ON dv.id = e.device_id
                   WHERE DATE(e.timestamp) BETWEEN ? AND ?
                     AND e.employee_id IS NOT NULL
                     AND dv.stage = 'production'
                   GROUP BY e.employee_id, DATE(e.timestamp)""",
                (start.isoformat(), end.isoformat())).fetchall():
            emp = str(r["employee_id"])
            if emp in two_stage and emp in punch_days and r["d"] in punch_days[emp]:
                punch_days[emp][r["d"]] = r["prod_in"]

    # Approved-holiday working days per employee inside the period
    hol_map = holiday_days_in_range(conn, start, end)

    # Schedule start times: {employee_id: {weekday: ("HH:MM", grace_min)}}
    # Real schema: user_schedule_assignments -> schedule_templates ->
    # schedule_rules (weekdays = "0,1,2" comma string) -> schedule_shifts.
    # Earliest shift of the highest-priority rule covering the weekday wins.
    sched = {}
    for r in conn.execute(
            """SELECT usa.employee_id, sr.weekdays, sr.priority,
                      ss.start_time, ss.grace_minutes
               FROM user_schedule_assignments usa
               JOIN schedule_rules sr ON sr.template_id = usa.template_id
               JOIN schedule_shifts ss ON ss.rule_id = sr.id
               ORDER BY sr.priority DESC, ss.start_time ASC""").fetchall():
        emp = str(r["employee_id"])
        try:
            days = [int(x) for x in str(r["weekdays"]).split(",") if x.strip() != ""]
        except ValueError:
            continue
        for wd in days:
            # First hit wins (highest priority, earliest shift)
            sched.setdefault(emp, {}).setdefault(
                wd, (r["start_time"], r["grace_minutes"] or 0))

    conn.close()

    workdays = [d for d in _period_days(start, end) if is_working_day(d)]

    for emp, rec in employees.items():
        emp_punches = punch_days.get(emp, {})
        emp_hols = hol_map.get(emp, set())

        rec["dias_trabajados"] = len(emp_punches)
        rec["dias_vacaciones"] = len(emp_hols)

        # Faltas: working days with no punch and no approved holiday
        for d in workdays:
            iso = d.isoformat()
            if iso not in emp_punches and iso not in emp_hols:
                rec["faltas"] += 1

        # Retardos: first punch after schedule start + grace (per-shift grace,
        # falling back to the default when the shift has none)
        emp_sched = sched.get(emp)
        if emp_sched:
            for iso, first_in in emp_punches.items():
                try:
                    d = date.fromisoformat(iso)
                except ValueError:
                    continue
                entry = emp_sched.get(d.weekday())
                if not entry:
                    continue
                st, grace = entry
                grace = grace if grace and grace > 0 else GRACE_MINUTES
                try:
                    fin = datetime.fromisoformat(str(first_in)[:19])
                    h, m = map(int, str(st).split(":")[:2])
                    scheduled = fin.replace(hour=h, minute=m, second=0)
                    if (fin - scheduled).total_seconds() > grace * 60:
                        rec["retardos"] += 1
                except Exception:
                    continue

    return employees


def vacation_rows(start: date, end: date) -> list:
    """Approved requests overlapping the period, clipped to it.
    Returns [{employee_id, name, fecha_inicio, fecha_fin, dias}]"""
    conn = get_conn()
    rows = conn.execute(
        """SELECT hr.employee_id, hr.start_date, hr.end_date,
                  COALESCE(u.name, hr.employee_id) AS name
           FROM holiday_requests hr
           LEFT JOIN users u ON u.employee_id = hr.employee_id
           WHERE hr.status='approved'
             AND NOT (hr.end_date < ? OR hr.start_date > ?)
           ORDER BY hr.employee_id, hr.start_date""",
        (start.isoformat(), end.isoformat())).fetchall()
    conn.close()
    out = []
    for r in rows:
        try:
            rs = date.fromisoformat(str(r["start_date"]))
            re_ = date.fromisoformat(str(r["end_date"]))
        except ValueError:
            continue
        clip_s = max(rs, start)
        clip_e = min(re_, end)
        dias = len(working_days_list(clip_s, clip_e))
        if dias > 0:
            out.append({"employee_id": str(r["employee_id"]), "name": r["name"],
                        "fecha_inicio": clip_s.isoformat(),
                        "fecha_fin": clip_e.isoformat(), "dias": dias})
    return out


# ── Workbook builders (literal values, no formulas — these are import files) ──

def _style_header(ws):
    from openpyxl.styles import Font, PatternFill
    for cell in ws[1]:
        cell.font = Font(bold=True, name="Arial")
        cell.fill = PatternFill("solid", start_color="DDDDDD")


def build_incidencias_xlsx(start: date, end: date) -> BytesIO:
    from openpyxl import Workbook
    summary = compute_period_summary(start, end)
    wb = Workbook()
    ws = wb.active
    ws.title = "Incidencias"
    ws.append(["clave_trabajador", "nombre", "dias_trabajados",
               "faltas", "retardos", "dias_vacaciones"])
    _style_header(ws)
    for emp in sorted(summary, key=lambda e: (len(e), e)):
        r = summary[emp]
        ws.append([emp, r["name"], r["dias_trabajados"], r["faltas"],
                   r["retardos"], r["dias_vacaciones"]])
    for col, w in zip("ABCDEF", (16, 30, 16, 10, 10, 16)):
        ws.column_dimensions[col].width = w
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def build_vacaciones_xlsx(start: date, end: date) -> BytesIO:
    from openpyxl import Workbook
    rows = vacation_rows(start, end)
    wb = Workbook()
    ws = wb.active
    ws.title = "Vacaciones"
    ws.append(["clave_trabajador", "nombre", "fecha_inicio", "fecha_fin", "dias"])
    _style_header(ws)
    for r in rows:
        ws.append([r["employee_id"], r["name"], r["fecha_inicio"],
                   r["fecha_fin"], r["dias"]])
    for col, w in zip("ABCDE", (16, 30, 14, 14, 8)):
        ws.column_dimensions[col].width = w
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf
