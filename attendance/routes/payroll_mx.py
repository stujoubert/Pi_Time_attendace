"""
routes/payroll_mx.py
Mexican payroll UI routes:
  /nomina/                    – period selector + results
  /nomina/empleado/<id>       – employee payroll config
  /nomina/calcular            – run calculation for a period
  /nomina/export/completa     – full nomina Excel
  /nomina/export/recibos      – individual recibos Excel
  /nomina/export/sua          – SUA CSV for IMSS
"""
from flask import Blueprint, render_template, request, redirect, url_for, \
    flash, send_file, g
from datetime import date, datetime
from db import get_conn
from authz import login_required, role_required
from services.payroll_mx import (
    get_quincena_bounds, get_catorcena_bounds, list_periods,
    calcular_nomina_periodo, calcular_nomina,
)
import services.payroll_mx_export as _px_export
from services.settings import get_company_settings

bp = Blueprint("nomina", __name__, url_prefix="/nomina")


# ── Period selector + results ─────────────────────────────────────────────────

@bp.route("/", methods=["GET"])
@login_required
@role_required("admin", "manager")
def nomina_index():
    T    = g.T
    year = int(request.args.get("year",  date.today().year))
    ptype = request.args.get("period_type", "quincena")
    pid   = request.args.get("period_id")

    periods_q = list_periods("quincena",  year)
    periods_c = list_periods("catorcena", year)

    selected_period = None
    results = []

    if pid:
        # Find the selected period
        all_periods = periods_q if ptype == "quincena" else periods_c
        for p in all_periods:
            if str(p["period_num"]) == str(pid):
                selected_period = p
                break

    if selected_period:
        results = calcular_nomina_periodo(
            selected_period["start"],
            selected_period["end"],
            ptype,
        )

    # Summary totals — always initialize with zeros
    totals = {k: 0.0 for k in ["total_percepciones","imss_total","isr_retenido",
                                 "total_deducciones","neto_pagar"]}
    if results:
        for key in ["total_percepciones","imss_total","isr_retenido",
                    "total_deducciones","neto_pagar"]:
            totals[key] = round(sum(r.get(key, 0) for r in results), 2)

    return render_template("nomina.html", T=T,
        year=year, period_type=ptype,
        periods_q=periods_q, periods_c=periods_c,
        selected_period=selected_period, selected_pid=pid,
        results=results, totals=totals)


# ── Employee payroll config ───────────────────────────────────────────────────

@bp.route("/empleado/<emp_id>", methods=["GET", "POST"])
@login_required
@role_required("admin")
def empleado_config(emp_id):
    T    = g.T
    conn = get_conn()

    if request.method == "POST":
        f = request.form
        # Upsert employee_payroll
        conn.execute("""
            INSERT INTO employee_payroll
                (employee_id, salary_type, salary_amount, pay_period_type,
                 catorcena_start, imss_num, rfc, curp, zona_libre, has_subsidio, active)
            VALUES (?,?,?,?,?,?,?,?,?,?,1)
            ON CONFLICT(employee_id) DO UPDATE SET
                salary_type      = excluded.salary_type,
                salary_amount    = excluded.salary_amount,
                pay_period_type  = excluded.pay_period_type,
                catorcena_start  = excluded.catorcena_start,
                imss_num         = excluded.imss_num,
                rfc              = excluded.rfc,
                curp             = excluded.curp,
                zona_libre       = excluded.zona_libre,
                has_subsidio     = excluded.has_subsidio,
                updated_at       = datetime('now')
        """, (
            emp_id,
            f.get("salary_type", "daily"),
            float(f.get("salary_amount", 0) or 0),
            f.get("pay_period_type", "quincena"),
            f.get("catorcena_start") or None,
            f.get("imss_num", "").strip() or None,
            f.get("rfc", "").strip().upper() or None,
            f.get("curp", "").strip().upper() or None,
            1 if f.get("zona_libre") else 0,
            1 if f.get("has_subsidio") else 0,
        ))

        # Update percepciones
        conn.execute(
            "DELETE FROM percepciones_config WHERE employee_id=?", (emp_id,)
        )
        conceptos = f.getlist("concepto[]")
        tipos     = f.getlist("tipo[]")
        montos    = f.getlist("monto_fijo[]")
        diarios   = f.getlist("monto_diario[]")
        for i, concepto in enumerate(conceptos):
            concepto = concepto.strip()
            if not concepto:
                continue
            conn.execute("""
                INSERT INTO percepciones_config
                    (employee_id, concepto, tipo, monto_fijo, monto_diario, activo)
                VALUES (?,?,?,?,?,1)
            """, (
                emp_id, concepto,
                tipos[i] if i < len(tipos) else "exenta",
                float(montos[i] or 0) if i < len(montos) else 0,
                float(diarios[i] or 0) if i < len(diarios) else 0,
            ))

        conn.commit()
        conn.close()
        flash("Configuración guardada", "success")
        return redirect(url_for("nomina.empleado_config", emp_id=emp_id))

    user = conn.execute(
        "SELECT employee_id, name FROM users WHERE employee_id=?", (emp_id,)
    ).fetchone()
    ep   = conn.execute(
        "SELECT * FROM employee_payroll WHERE employee_id=?", (emp_id,)
    ).fetchone()
    percs = conn.execute(
        "SELECT * FROM percepciones_config WHERE employee_id=? ORDER BY id",
        (emp_id,)
    ).fetchall()
    conn.close()

    if not user:
        flash("Empleado no encontrado", "danger")
        return redirect(url_for("users.users_list"))

    return render_template("nomina_empleado.html", T=T,
        user=user, ep=ep, percs=percs)


# ── List all employees with payroll config status ─────────────────────────────

@bp.route("/empleados")
@login_required
@role_required("admin", "manager")
def empleados_list():
    T    = g.T
    conn = get_conn()
    rows = conn.execute("""
        SELECT u.employee_id, u.name,
               COALESCE(d.name,'—') as dept,
               ep.salary_type, ep.salary_amount,
               ep.pay_period_type, ep.active as payroll_active
        FROM users u
        LEFT JOIN departments d ON d.id = u.department_id
        LEFT JOIN employee_payroll ep ON ep.employee_id = u.employee_id
        WHERE u.is_active = 1
        ORDER BY CAST(u.employee_id AS INTEGER), u.employee_id
    """).fetchall()
    conn.close()
    return render_template("nomina_empleados.html", T=T, employees=rows)


# ── Export routes ─────────────────────────────────────────────────────────────

def _get_results_for_export():
    """Pull period params from request args and calculate."""
    ptype = request.args.get("period_type", "quincena")
    start = request.args.get("start")
    end   = request.args.get("end")
    if not start or not end:
        return None, None, None
    pd_start = date.fromisoformat(start)
    pd_end   = date.fromisoformat(end)
    results  = calcular_nomina_periodo(pd_start, pd_end, ptype)
    return results, pd_start, pd_end


@bp.route("/export/completa")
@login_required
@role_required("admin", "manager")
def export_completa():
    results, pd_start, pd_end = _get_results_for_export()
    if not results:
        flash("No hay datos para exportar", "warning")
        return redirect(url_for("nomina.nomina_index"))
    company = get_company_settings()
    buf  = _px_export.export_nomina_completa(results, pd_start, pd_end, company.get("name",""))
    name = f"nomina_{pd_start}_{pd_end}.xlsx"
    return send_file(buf, as_attachment=True, download_name=name,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@bp.route("/export/recibos")
@login_required
@role_required("admin", "manager")
def export_recibos():
    results, pd_start, pd_end = _get_results_for_export()
    if not results:
        flash("No hay datos para exportar", "warning")
        return redirect(url_for("nomina.nomina_index"))
    company = get_company_settings()
    buf  = _px_export.export_recibos(results, pd_start, pd_end, company.get("name",""))
    name = f"recibos_{pd_start}_{pd_end}.xlsx"
    return send_file(buf, as_attachment=True, download_name=name,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


@bp.route("/export/sua")
@login_required
@role_required("admin")
def export_sua():
    results, pd_start, pd_end = _get_results_for_export()
    if not results:
        flash("No hay datos para exportar", "warning")
        return redirect(url_for("nomina.nomina_index"))
    buf  = _px_export.export_sua(results, pd_start, pd_end)
    name = f"SUA_{pd_start}_{pd_end}.csv"
    return send_file(buf, as_attachment=True, download_name=name, mimetype="text/csv")
