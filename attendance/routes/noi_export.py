"""
routes/noi_export.py

"Export to Aspel NOI" — HR picks a payroll period and downloads the import
files NOI's wizard reads (incidencias / días y horas, and vacaciones).
"""
from flask import (Blueprint, render_template, request, send_file,
                   flash, redirect, url_for)
from datetime import date, timedelta, datetime
from authz import login_required, role_required

bp = Blueprint("noi_export", __name__, url_prefix="/noi-export")


def _default_period():
    """Default to the current quincena (1-15 / 16-end of month)."""
    today = date.today()
    if today.day <= 15:
        return date(today.year, today.month, 1), date(today.year, today.month, 15)
    if today.month == 12:
        end = date(today.year, 12, 31)
    else:
        end = date(today.year, today.month + 1, 1) - timedelta(days=1)
    return date(today.year, today.month, 16), end


def _parse_period():
    try:
        start = datetime.strptime(request.args.get("start", ""), "%Y-%m-%d").date()
        end = datetime.strptime(request.args.get("end", ""), "%Y-%m-%d").date()
        if end < start:
            raise ValueError
        return start, end
    except ValueError:
        return None, None


@bp.route("/")
@login_required
@role_required("admin", "manager")
def export_page():
    start, end = _parse_period()
    if not start:
        start, end = _default_period()
    from services.noi_export import compute_period_summary, vacation_rows
    summary = compute_period_summary(start, end)
    vacations = vacation_rows(start, end)
    totals = {
        "faltas": sum(r["faltas"] for r in summary.values()),
        "retardos": sum(r["retardos"] for r in summary.values()),
        "vac_rows": len(vacations),
    }
    return render_template("noi_export.html",
                           start=start.isoformat(), end=end.isoformat(),
                           summary=summary, vacations=vacations, totals=totals)


@bp.route("/incidencias.xlsx")
@login_required
@role_required("admin", "manager")
def download_incidencias():
    start, end = _parse_period()
    if not start:
        flash("Invalid period.", "danger")
        return redirect(url_for("noi_export.export_page"))
    from services.noi_export import build_incidencias_xlsx
    buf = build_incidencias_xlsx(start, end)
    fname = f"NOI_incidencias_{start.isoformat()}_{end.isoformat()}.xlsx"
    return send_file(buf, as_attachment=True, download_name=fname,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@bp.route("/vacaciones.xlsx")
@login_required
@role_required("admin", "manager")
def download_vacaciones():
    start, end = _parse_period()
    if not start:
        flash("Invalid period.", "danger")
        return redirect(url_for("noi_export.export_page"))
    from services.noi_export import build_vacaciones_xlsx
    buf = build_vacaciones_xlsx(start, end)
    fname = f"NOI_vacaciones_{start.isoformat()}_{end.isoformat()}.xlsx"
    return send_file(buf, as_attachment=True, download_name=fname,
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
