"""
routes/rates.py
Admin page to update government-mandated payroll rates:
  - UMA (INEGI, published every February)
  - Salario mínimo (CONASAMI, published every January)
  - IMSS cuotas obreras (IMSS, changes rarely)
  - ISR tabla anual (SAT Anexo 8, every January)
  - Subsidio al empleo tabla (SAT, every January)
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, g
from authz import login_required, role_required
from db import get_conn
import services.payroll_mx as pmx

bp = Blueprint("rates", __name__, url_prefix="/rates")


def _get_rates():
    """Read current rates from the payroll_mx module."""
    return {
        "uma_diario":    pmx.UMA_DIARIO,
        "uma_mensual":   pmx.UMA_MENSUAL,
        "uma_anual":     pmx.UMA_ANUAL,
        "smg_general":   pmx.SMG_GENERAL,
        "smg_frontera":  pmx.SMG_FRONTERA,
        "imss_em":       pmx.IMSS_CUOTAS["enfermedad_maternidad"],
        "imss_em_exc":   pmx.IMSS_CUOTAS["enfermedad_mat_excedente"],
        "imss_iv":       pmx.IMSS_CUOTAS["invalidez_vida"],
        "imss_cv":       pmx.IMSS_CUOTAS["cesantia_vejez"],
    }


def _get_isr_tabla(year, period_type):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, li, ls, cuota_fija, tasa_excedente FROM isr_tabla "
        "WHERE year=? AND period_type=? ORDER BY li",
        (year, period_type)
    ).fetchall()
    conn.close()
    return rows


def _get_subsidio_tabla(year, period_type):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, li, ls, subsidio FROM subsidio_tabla "
        "WHERE year=? AND period_type=? ORDER BY li",
        (year, period_type)
    ).fetchall()
    conn.close()
    return rows


@bp.route("/", methods=["GET"])
@login_required
@role_required("admin")
def rates_index():
    T = g.T
    year      = int(request.args.get("year", 2026))
    ptype     = request.args.get("period_type", "quincena")
    rates     = _get_rates()
    isr       = _get_isr_tabla(year, ptype)
    subsidio  = _get_subsidio_tabla(year, ptype)
    return render_template("rates.html", T=T,
        rates=rates, year=year, period_type=ptype,
        isr=isr, subsidio=subsidio)


@bp.route("/update-uma", methods=["POST"])
@login_required
@role_required("admin")
def update_uma():
    f = request.form
    try:
        pmx.UMA_DIARIO  = float(f["uma_diario"])
        pmx.UMA_MENSUAL = float(f["uma_mensual"])
        pmx.UMA_ANUAL   = float(f["uma_anual"])
        pmx.SMG_GENERAL  = float(f["smg_general"])
        pmx.SMG_FRONTERA = float(f["smg_frontera"])
        flash("UMA y Salario Mínimo actualizados correctamente.", "success")
    except Exception as e:
        flash(f"Error: {e}", "danger")
    return redirect(url_for("rates.rates_index"))


@bp.route("/update-imss", methods=["POST"])
@login_required
@role_required("admin")
def update_imss():
    f = request.form
    try:
        pmx.IMSS_CUOTAS["enfermedad_maternidad"]    = float(f["imss_em"])
        pmx.IMSS_CUOTAS["enfermedad_mat_excedente"] = float(f["imss_em_exc"])
        pmx.IMSS_CUOTAS["invalidez_vida"]           = float(f["imss_iv"])
        pmx.IMSS_CUOTAS["cesantia_vejez"]           = float(f["imss_cv"])
        flash("Cuotas IMSS actualizadas correctamente.", "success")
    except Exception as e:
        flash(f"Error: {e}", "danger")
    return redirect(url_for("rates.rates_index"))


@bp.route("/update-isr", methods=["POST"])
@login_required
@role_required("admin")
def update_isr():
    f    = request.form
    year  = int(f.get("year", 2026))
    ptype = f.get("period_type", "quincena")
    conn  = get_conn()
    try:
        # Delete existing rows for this year+period
        conn.execute(
            "DELETE FROM isr_tabla WHERE year=? AND period_type=?", (year, ptype)
        )
        # Re-insert from form
        ids      = f.getlist("isr_id[]")
        lis      = f.getlist("isr_li[]")
        lss      = f.getlist("isr_ls[]")
        cuotas   = f.getlist("isr_cuota[]")
        tasas    = f.getlist("isr_tasa[]")
        for li, ls, cuota, tasa in zip(lis, lss, cuotas, tasas):
            if li.strip() == "":
                continue
            conn.execute(
                "INSERT INTO isr_tabla(year,period_type,li,ls,cuota_fija,tasa_excedente) VALUES(?,?,?,?,?,?)",
                (year, ptype, float(li), float(ls) if ls.strip() else 9999999,
                 float(cuota), float(tasa))
            )
        conn.commit()
        flash(f"Tabla ISR {year} ({ptype}) actualizada correctamente.", "success")
    except Exception as e:
        flash(f"Error actualizando ISR: {e}", "danger")
    finally:
        conn.close()
    return redirect(url_for("rates.rates_index", year=year, period_type=ptype))


@bp.route("/update-subsidio", methods=["POST"])
@login_required
@role_required("admin")
def update_subsidio():
    f     = request.form
    year  = int(f.get("year", 2026))
    ptype = f.get("period_type", "quincena")
    conn  = get_conn()
    try:
        conn.execute(
            "DELETE FROM subsidio_tabla WHERE year=? AND period_type=?", (year, ptype)
        )
        lis      = f.getlist("sub_li[]")
        lss      = f.getlist("sub_ls[]")
        subsidios = f.getlist("sub_subsidio[]")
        for li, ls, sub in zip(lis, lss, subsidios):
            if li.strip() == "":
                continue
            conn.execute(
                "INSERT INTO subsidio_tabla(year,period_type,li,ls,subsidio) VALUES(?,?,?,?,?)",
                (year, ptype, float(li), float(ls) if ls.strip() else 9999999, float(sub))
            )
        conn.commit()
        flash(f"Tabla Subsidio {year} ({ptype}) actualizada correctamente.", "success")
    except Exception as e:
        flash(f"Error actualizando Subsidio: {e}", "danger")
    finally:
        conn.close()
    return redirect(url_for("rates.rates_index", year=year, period_type=ptype))


@bp.route("/add-year", methods=["POST"])
@login_required
@role_required("admin")
def add_year():
    """Copy ISR and subsidio tables from one year to a new year for editing."""
    f        = request.form
    src_year = int(f.get("src_year", 2026))
    new_year = int(f.get("new_year", 2027))
    conn     = get_conn()
    try:
        for ptype in ("quincena", "catorcena"):
            # Copy ISR
            rows = conn.execute(
                "SELECT li,ls,cuota_fija,tasa_excedente FROM isr_tabla WHERE year=? AND period_type=?",
                (src_year, ptype)
            ).fetchall()
            for r in rows:
                conn.execute(
                    "INSERT OR IGNORE INTO isr_tabla(year,period_type,li,ls,cuota_fija,tasa_excedente) VALUES(?,?,?,?,?,?)",
                    (new_year, ptype, r["li"], r["ls"], r["cuota_fija"], r["tasa_excedente"])
                )
            # Copy Subsidio
            rows = conn.execute(
                "SELECT li,ls,subsidio FROM subsidio_tabla WHERE year=? AND period_type=?",
                (src_year, ptype)
            ).fetchall()
            for r in rows:
                conn.execute(
                    "INSERT OR IGNORE INTO subsidio_tabla(year,period_type,li,ls,subsidio) VALUES(?,?,?,?,?)",
                    (new_year, ptype, r["li"], r["ls"], r["subsidio"])
                )
        conn.commit()
        flash(f"Tablas del {src_year} copiadas al {new_year}. Ahora puedes editarlas.", "success")
    except Exception as e:
        flash(f"Error: {e}", "danger")
    finally:
        conn.close()
    return redirect(url_for("rates.rates_index", year=new_year))
