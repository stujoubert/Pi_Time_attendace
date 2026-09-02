"""
services/holidays.py

Holiday / vacation logic for the self-contained holiday module:
  - Mexican public holidays (días feriados oficiales) per year
  - Working-day counting (excludes weekends + public holidays)
  - Live balance computation from approved requests

Design notes:
  - Balance is computed, never stored as a mutable counter, so it can't drift.
  - Public holidays are the statutory federal ones (Art. 74 LFT) plus the
    sexenio transmission-of-power day. Bank/optional days are NOT included.
"""
from datetime import date, timedelta


def mx_public_holidays(year: int) -> set:
    """
    Return the set of statutory Mexican public holidays for a given year as
    date objects. Based on Art. 74 of the Ley Federal del Trabajo.

    Fixed-date holidays:
      Jan 1   - Año Nuevo
      May 1   - Día del Trabajo
      Sep 16  - Independencia
      Dec 25  - Navidad
    Movable (first Monday / third Monday rules):
      First Monday of February   - Día de la Constitución (observed)
      Third Monday of March      - Natalicio de Benito Juárez (observed)
      Third Monday of November   - Revolución Mexicana (observed)
    Sexenio:
      Oct 1 every 6 years from 2024 - Transmisión del Poder Ejecutivo
    """
    hols = set()
    # Fixed
    hols.add(date(year, 1, 1))
    hols.add(date(year, 5, 1))
    hols.add(date(year, 9, 16))
    hols.add(date(year, 12, 25))

    # First Monday of February
    hols.add(_nth_weekday(year, 2, 0, 1))
    # Third Monday of March
    hols.add(_nth_weekday(year, 3, 0, 3))
    # Third Monday of November
    hols.add(_nth_weekday(year, 11, 0, 3))

    # Transmission of executive power: Oct 1, every 6 years starting 2024
    if (year - 2024) % 6 == 0 and year >= 2024:
        hols.add(date(year, 10, 1))

    return hols


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    """nth occurrence of a weekday (0=Mon) in a month. n is 1-based."""
    d = date(year, month, 1)
    # days until the first desired weekday
    offset = (weekday - d.weekday()) % 7
    first = d + timedelta(days=offset)
    return first + timedelta(weeks=n - 1)


def is_working_day(d: date) -> bool:
    """A working day is Mon-Fri and not a public holiday.

    NOTE: weekends are Sat/Sun. If a site works Saturdays, this would need a
    per-company setting; for the holiday module we count Mon-Fri as is standard
    for vacation accounting.
    """
    if d.weekday() >= 5:  # Sat=5, Sun=6
        return False
    if d in mx_public_holidays(d.year):
        return False
    return True


def count_working_days(start: date, end: date) -> int:
    """Inclusive count of working days between start and end (Mon-Fri minus
    public holidays). Returns 0 if end < start."""
    if end < start:
        return 0
    n = 0
    d = start
    while d <= end:
        if is_working_day(d):
            n += 1
        d += timedelta(days=1)
    return n


def working_days_list(start: date, end: date) -> list:
    """List of working-day date objects in the inclusive range."""
    out = []
    d = start
    while d <= end:
        if is_working_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


# ── Balance ──────────────────────────────────────────────────────────────────

def get_allowance(conn, employee_id: str, year: int) -> int:
    """HR-entered allowance for the employee/year. 0 if not set."""
    row = conn.execute(
        "SELECT days FROM holiday_allowance WHERE employee_id=? AND year=?",
        (str(employee_id), year)
    ).fetchone()
    return int(row["days"]) if row else 0


def days_used(conn, employee_id: str, year: int) -> int:
    """Sum of approved working-days for the employee within the given year.

    A request is counted in the year of its start_date. (Cross-year requests
    are rare for vacation; start-date year keeps the accounting simple and
    predictable.)
    """
    row = conn.execute(
        """SELECT COALESCE(SUM(days_count),0) AS used
           FROM holiday_requests
           WHERE employee_id=? AND status='approved'
             AND substr(start_date,1,4)=?""",
        (str(employee_id), str(year))
    ).fetchone()
    return int(row["used"]) if row else 0


def days_pending(conn, employee_id: str, year: int) -> int:
    """Sum of pending working-days (not yet decided) for visibility."""
    row = conn.execute(
        """SELECT COALESCE(SUM(days_count),0) AS p
           FROM holiday_requests
           WHERE employee_id=? AND status='pending'
             AND substr(start_date,1,4)=?""",
        (str(employee_id), str(year))
    ).fetchone()
    return int(row["p"]) if row else 0


def get_balance(conn, employee_id: str, year: int) -> dict:
    """Return {allowance, used, pending, remaining} for an employee/year."""
    allowance = get_allowance(conn, employee_id, year)
    used = days_used(conn, employee_id, year)
    pending = days_pending(conn, employee_id, year)
    return {
        "allowance": allowance,
        "used": used,
        "pending": pending,
        "remaining": allowance - used,
    }


# ── Attendance-report integration ────────────────────────────────────────────

def employees_on_holiday(conn, target_date) -> set:
    """Set of employee_id strings with an APPROVED holiday covering the given
    date. Weekends/public holidays inside a range don't matter here — if the
    date falls within an approved request's range, the employee is on holiday.
    """
    iso = target_date.isoformat() if hasattr(target_date, "isoformat") else str(target_date)
    rows = conn.execute(
        """SELECT employee_id FROM holiday_requests
           WHERE status='approved' AND start_date <= ? AND end_date >= ?""",
        (iso, iso)).fetchall()
    return {str(r["employee_id"]) for r in rows}


def holiday_days_in_range(conn, start_date, end_date) -> dict:
    """{employee_id: set(iso_dates)} of approved-holiday WORKING days that fall
    within [start_date, end_date]. Used by weekly views to mark cells."""
    s = start_date.isoformat() if hasattr(start_date, "isoformat") else str(start_date)
    e = end_date.isoformat() if hasattr(end_date, "isoformat") else str(end_date)
    rows = conn.execute(
        """SELECT employee_id, start_date, end_date FROM holiday_requests
           WHERE status='approved' AND NOT (end_date < ? OR start_date > ?)""",
        (s, e)).fetchall()
    from datetime import date as _date
    out = {}
    lo = _date.fromisoformat(s); hi = _date.fromisoformat(e)
    for r in rows:
        try:
            rs = _date.fromisoformat(str(r["start_date"]))
            re_ = _date.fromisoformat(str(r["end_date"]))
        except ValueError:
            continue
        for d in working_days_list(max(rs, lo), min(re_, hi)):
            out.setdefault(str(r["employee_id"]), set()).add(d.isoformat())
    return out
