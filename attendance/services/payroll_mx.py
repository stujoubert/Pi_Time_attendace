"""
services/payroll_mx.py

Mexican payroll calculation engine.

Covers:
  - Quincena (1-15 / 16-end of month)
  - Catorcena (rolling 14-day cycles)
  - Salario diario, por hora, mensual
  - Factor de integración IMSS (FC = 1 + vacaciones/365 + prima/365 + aguinaldo/365)
  - Cuotas obreras IMSS 2026
  - ISR 2026 por período (Anexo 8)
  - Subsidio al empleo 2026
  - Percepciones exentas / gravadas
  - Horas extra (dobles y triples)
  - Descuento por faltas
  - Días festivos trabajados

All monetary amounts are in MXN, rounded to 2 decimal places.
"""
import calendar
import logging
from datetime import date, timedelta
from db import get_conn

log = logging.getLogger(__name__)

# ── IMSS cuotas obreras 2026 ─────────────────────────────────────────────────
# Source: IMSS Acuerdo ACDO.SA3.HCT.280124
IMSS_CUOTAS = {
    "enfermedad_maternidad": 0.0040,   # 0.40% sobre SBC hasta 3 UMA
    "enfermedad_mat_excedente": 0.0040,# 0.40% sobre excedente de 3 UMA
    "invalidez_vida":        0.00625,  # 0.625%
    "cesantia_vejez":        0.01125,  # 1.125%
}

# UMA 2026
UMA_DIARIO  = 113.14   # MXN/día (INEGI 2026)
UMA_MENSUAL = 3438.00
UMA_ANUAL   = 41259.12

# Salario mínimo 2026
SMG_GENERAL  = 278.80   # /día zona general
SMG_FRONTERA = 419.88   # /día zona libre frontera norte

# Factor integración mínimo legal (15 días vacaciones, 25% prima, 15 días aguinaldo)
# Empresas pueden dar más; usamos mínimo de ley como base configurable
FACTOR_INTEGRACION_MIN = round(1 + (15/365) + (15*0.25/365) + (15/365), 6)
# = 1 + 0.041096 + 0.010274 + 0.041096 ≈ 1.0924


# ─────────────────────────────────────────────────────────────────────────────
# Period helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_quincena_bounds(year: int, month: int, half: int) -> tuple[date, date]:
    """
    Returns (start_date, end_date) for a quincena period.
    half=1 → 1st to 15th
    half=2 → 16th to last day of month
    """
    if half == 1:
        return date(year, month, 1), date(year, month, 15)
    else:
        last = calendar.monthrange(year, month)[1]
        return date(year, month, 16), date(year, month, last)


def get_catorcena_bounds(anchor: date, cycle_num: int) -> tuple[date, date]:
    """
    Returns (start_date, end_date) for a catorcena cycle.
    cycle_num=0 → current cycle containing today
    cycle_num=-1 → previous, etc.
    """
    start = anchor + timedelta(days=cycle_num * 14)
    end   = start + timedelta(days=13)
    return start, end


def get_current_period(pay_period_type: str, catorcena_start: str | None = None) -> tuple[date, date]:
    """Return (start, end) for the current open period."""
    today = date.today()
    if pay_period_type == "quincena":
        half = 1 if today.day <= 15 else 2
        return get_quincena_bounds(today.year, today.month, half)
    else:
        if not catorcena_start:
            # Default anchor: Jan 1 of current year
            anchor = date(today.year, 1, 1)
        else:
            anchor = date.fromisoformat(catorcena_start)
        # Find which cycle we're in
        delta = (today - anchor).days
        cycle = delta // 14
        return get_catorcena_bounds(anchor, cycle)


def list_periods(pay_period_type: str, year: int,
                 catorcena_start: str | None = None) -> list[dict]:
    """
    List all pay periods for a given year.
    Returns list of {label, start, end, period_num}
    """
    periods = []
    if pay_period_type == "quincena":
        for month in range(1, 13):
            for half in (1, 2):
                s, e = get_quincena_bounds(year, month, half)
                num  = (month - 1) * 2 + half
                periods.append({
                    "period_num": num,
                    "label": f"Q{num:02d} — {s.strftime('%d/%m')} al {e.strftime('%d/%m/%Y')}",
                    "start": s, "end": e,
                    "dias": (e - s).days + 1,
                })
    else:
        anchor = date.fromisoformat(catorcena_start) if catorcena_start else date(year, 1, 1)
        num = 1
        s, e = get_catorcena_bounds(anchor, 0)
        # Wind back to beginning of year
        while s.year >= year:
            s, e = get_catorcena_bounds(anchor, -(num))
            num += 1
        num = 1
        s, e = get_catorcena_bounds(anchor, 0)
        while s.year <= year:
            if s.year == year or e.year == year:
                periods.append({
                    "period_num": num,
                    "label": f"C{num:02d} — {s.strftime('%d/%m')} al {e.strftime('%d/%m/%Y')}",
                    "start": s, "end": e,
                    "dias": 14,
                })
            num += 1
            s, e = get_catorcena_bounds(anchor, num - 1)
            if s.year > year + 1:
                break
    return periods


# ─────────────────────────────────────────────────────────────────────────────
# Salary helpers
# ─────────────────────────────────────────────────────────────────────────────

def get_salario_diario(salary_type: str, salary_amount: float,
                       dias_periodo: int = 15) -> float:
    """Convert any salary type to salario diario."""
    if salary_type == "daily":
        return round(salary_amount, 6)
    elif salary_type == "hourly":
        return round(salary_amount * 8, 6)   # 8-hour workday assumed
    elif salary_type == "monthly":
        return round(salary_amount / 30.4, 6)
    return 0.0


def get_factor_integracion(dias_vacaciones: int = 15,
                            prima_pct: float = 0.25,
                            dias_aguinaldo: int = 15) -> float:
    """
    Calculate IMSS integration factor (factor de integración).
    FC = 1 + (vacaciones/365) + (prima_vacacional/365) + (aguinaldo/365)
    """
    vac   = dias_vacaciones / 365
    prima = (dias_vacaciones * prima_pct) / 365
    aguin = dias_aguinaldo / 365
    return round(1 + vac + prima + aguin, 6)


# ─────────────────────────────────────────────────────────────────────────────
# IMSS cuotas obreras
# ─────────────────────────────────────────────────────────────────────────────

def calcular_imss_obrero(sbc: float, dias: int) -> dict:
    """
    Calculate employee IMSS contributions (cuotas obreras).
    sbc = salario base de cotización (diario)
    dias = days in the period

    Returns breakdown dict with all concepts and total.
    """
    sbc_periodo = sbc * dias
    tope_3uma   = UMA_DIARIO * 3 * dias  # tope para EM

    # Enfermedad y maternidad
    base_em    = min(sbc_periodo, tope_3uma)
    em         = round(base_em * IMSS_CUOTAS["enfermedad_maternidad"], 2)
    excedente  = max(0.0, sbc_periodo - tope_3uma)
    em_exc     = round(excedente * IMSS_CUOTAS["enfermedad_mat_excedente"], 2)

    # Invalidez y vida
    iv         = round(sbc_periodo * IMSS_CUOTAS["invalidez_vida"], 2)

    # Cesantía y vejez
    cv         = round(sbc_periodo * IMSS_CUOTAS["cesantia_vejez"], 2)

    total      = round(em + em_exc + iv + cv, 2)

    return {
        "enfermedad_maternidad":    em,
        "enfermedad_mat_excedente": em_exc,
        "invalidez_vida":           iv,
        "cesantia_vejez":           cv,
        "total":                    total,
    }


# ─────────────────────────────────────────────────────────────────────────────
# ISR y Subsidio al empleo
# ─────────────────────────────────────────────────────────────────────────────

def _get_isr_row(base: float, year: int, period_type: str) -> dict | None:
    conn = get_conn()
    row = conn.execute("""
        SELECT li, ls, cuota_fija, tasa_excedente
        FROM isr_tabla
        WHERE year=? AND period_type=? AND li<=? AND ls>=?
        ORDER BY li DESC LIMIT 1
    """, (year, period_type, base, base)).fetchone()
    conn.close()
    return dict(row) if row else None


def calcular_isr(base_gravable: float, year: int, period_type: str) -> float:
    """
    Calculate ISR causado for a given base gravable and period.
    Returns ISR amount (before subsidio).
    """
    if base_gravable <= 0:
        return 0.0
    row = _get_isr_row(base_gravable, year, period_type)
    if not row:
        return 0.0
    excedente = base_gravable - row["li"]
    isr = row["cuota_fija"] + (excedente * row["tasa_excedente"])
    return round(max(0.0, isr), 2)


def calcular_subsidio(base_gravable: float, year: int, period_type: str) -> float:
    """
    Calculate subsidio al empleo for a given base gravable.
    Returns subsidio amount.
    """
    if base_gravable <= 0:
        return 0.0
    conn = get_conn()
    row = conn.execute("""
        SELECT subsidio
        FROM subsidio_tabla
        WHERE year=? AND period_type=? AND li<=? AND ls>=?
        ORDER BY li DESC LIMIT 1
    """, (year, period_type, base_gravable, base_gravable)).fetchone()
    conn.close()
    if not row:
        return 0.0
    return round(row["subsidio"], 2)


# ─────────────────────────────────────────────────────────────────────────────
# Overtime (horas extra)
# ─────────────────────────────────────────────────────────────────────────────

def calcular_horas_extra(salario_diario: float,
                          horas_extra_dobles: float,
                          horas_extra_triples: float) -> float:
    """
    Mexican law:
    - First 9 hours/week extra: double pay (dobles)
    - Beyond 9 hours/week: triple pay (triples)
    - 50% of double-pay overtime is ISR-exempt (up to 5x UMA/day limit)
    """
    hora_base    = salario_diario / 8
    monto_dobles  = round(hora_base * 2 * horas_extra_dobles, 2)
    monto_triples = round(hora_base * 3 * horas_extra_triples, 2)
    return round(monto_dobles + monto_triples, 2)


# ─────────────────────────────────────────────────────────────────────────────
# Percepciones exentas limits (ISR law)
# ─────────────────────────────────────────────────────────────────────────────

def exencion_despensa(monto: float, salario_diario: float, dias: int) -> tuple[float, float]:
    """
    Despensa: exempt up to 40% of UMA diario × días.
    Returns (exento, gravado).
    """
    tope   = round(UMA_DIARIO * 0.40 * dias, 2)
    exento = round(min(monto, tope), 2)
    gravado = round(max(0.0, monto - exento), 2)
    return exento, gravado


def exencion_transporte(monto: float) -> tuple[float, float]:
    """Transporte: fully exempt if provided by employer (Art. 93 LISR)."""
    return round(monto, 2), 0.0


def exencion_prima_vacacional(monto: float, dias: int) -> tuple[float, float]:
    """Prima vacacional: exempt up to 15 days of SMG per year, prorated."""
    tope   = round(SMG_GENERAL * 15 * (dias / 365), 2)
    exento = round(min(monto, tope), 2)
    gravado = round(max(0.0, monto - exento), 2)
    return exento, gravado


def exencion_aguinaldo(monto: float) -> tuple[float, float]:
    """Aguinaldo: exempt up to 30 days of SMG."""
    tope    = round(SMG_GENERAL * 30, 2)
    exento  = round(min(monto, tope), 2)
    gravado = round(max(0.0, monto - exento), 2)
    return exento, gravado


# ─────────────────────────────────────────────────────────────────────────────
# Main calculation function
# ─────────────────────────────────────────────────────────────────────────────

def calcular_nomina(
    employee_id: str,
    period_start: date,
    period_end: date,
    period_type: str,        # 'quincena' or 'catorcena'
    dias_trabajados: float,  # from attendance system
    horas_trabajadas: float,
    horas_extra_dobles: float   = 0.0,
    horas_extra_triples: float  = 0.0,
    dias_festivos_trabajados: int = 0,
    anticipos: float            = 0.0,
    otras_deducciones: float    = 0.0,
    infonavit: float            = 0.0,
    # Proration flags
    incluir_vacaciones: bool    = False,
    dias_vacaciones: float      = 0.0,
    incluir_aguinaldo: bool     = False,
) -> dict:
    """
    Full Mexican payroll calculation for one employee for one period.
    Returns a dict with all percepciones, deducciones, and totals.
    """
    conn = get_conn()
    # Load employee payroll config
    ep = conn.execute(
        "SELECT * FROM employee_payroll WHERE employee_id=?", (employee_id,)
    ).fetchone()

    if not ep:
        conn.close()
        return {"error": f"No payroll config for employee {employee_id}"}

    # Load percepciones config
    percs = conn.execute(
        "SELECT * FROM percepciones_config WHERE employee_id=? AND activo=1",
        (employee_id,)
    ).fetchall()
    conn.close()

    # ── Base salary ───────────────────────────────────────────────────────────
    dias_periodo = (period_end - period_start).days + 1
    sd = get_salario_diario(ep["salary_type"], ep["salary_amount"], dias_periodo)

    # Factor integración (using legal minimum; companies can configure higher)
    fi = get_factor_integracion()
    sbc = round(sd * fi, 6)  # salario base de cotización

    # ── Sueldo base del período ───────────────────────────────────────────────
    if ep["salary_type"] == "hourly":
        sueldo_base = round(ep["salary_amount"] * horas_trabajadas, 2)
    else:
        # Proportional to days worked
        sueldo_base = round(sd * dias_trabajados, 2)

    # ── Horas extra ───────────────────────────────────────────────────────────
    he_monto = calcular_horas_extra(sd, horas_extra_dobles, horas_extra_triples)

    # Overtime: 50% exempt up to 5 UMA/day limit
    tope_he_exento = round(UMA_DIARIO * 5 * dias_trabajados, 2)
    he_exento  = round(min(he_monto * 0.50, tope_he_exento), 2)
    he_gravado = round(he_monto - he_exento, 2)

    # ── Días festivos trabajados (triple pay) ─────────────────────────────────
    festivos_monto = round(sd * 2 * dias_festivos_trabajados, 2)  # extra 200%

    # ── Percepciones exentas from config ─────────────────────────────────────
    despensa_monto = transporte_monto = otros_exentos = otros_gravados = 0.0
    for p in percs:
        monto = p["monto_fijo"] + (p["monto_diario"] * dias_trabajados)
        concepto = p["concepto"].lower()
        if concepto == "despensa":
            ex, grav = exencion_despensa(monto, sd, dias_trabajados)
            despensa_monto += ex
            otros_gravados += grav
        elif concepto == "transporte":
            ex, _ = exencion_transporte(monto)
            transporte_monto += ex
        elif p["tipo"] == "exenta":
            otros_exentos += monto
        else:
            otros_gravados += monto

    # ── Vacaciones (if applicable) ────────────────────────────────────────────
    vac_monto = prima_vac_monto = 0.0
    if incluir_vacaciones and dias_vacaciones > 0:
        vac_monto = round(sd * dias_vacaciones, 2)
        prima_raw = round(vac_monto * 0.25, 2)
        pv_exento, pv_gravado = exencion_prima_vacacional(prima_raw, dias_periodo)
        prima_vac_monto = prima_raw
    else:
        pv_exento = pv_gravado = 0.0
        prima_vac_monto = 0.0

    # ── Aguinaldo proporcional (if applicable) ────────────────────────────────
    aguin_monto = 0.0
    if incluir_aguinaldo:
        # 15 días mínimo por ley; prorate per period
        aguin_raw = round(sd * 15 * (dias_periodo / 365), 2)
        ag_exento, ag_gravado = exencion_aguinaldo(aguin_raw)
        aguin_monto = aguin_raw
    else:
        ag_exento = ag_gravado = 0.0
        aguin_monto = 0.0

    # ── Totales percepciones ──────────────────────────────────────────────────
    total_gravado = round(
        sueldo_base + he_gravado + festivos_monto +
        otros_gravados + pv_gravado + ag_gravado, 2
    )
    total_exento = round(
        he_exento + despensa_monto + transporte_monto +
        otros_exentos + pv_exento + ag_exento, 2
    )
    total_percepciones = round(total_gravado + total_exento, 2)

    # ── IMSS cuotas obreras ───────────────────────────────────────────────────
    imss = calcular_imss_obrero(sbc, dias_trabajados)

    # ── ISR ───────────────────────────────────────────────────────────────────
    # Base ISR = total gravado − IMSS total
    base_isr      = round(max(0.0, total_gravado - imss["total"]), 2)
    year          = period_start.year
    isr_causado   = calcular_isr(base_isr, year, period_type)
    subsidio      = calcular_subsidio(base_isr, year, period_type) if ep["has_subsidio"] else 0.0
    isr_retenido  = round(max(0.0, isr_causado - subsidio), 2)

    # ── Faltas descuento ──────────────────────────────────────────────────────
    # Only deduct for absences if employee has explicit absence records.
    # Do NOT automatically assume absent for days with no attendance data —
    # the system may not have complete records for the full period.
    dias_faltados = 0.0
    faltas_desc   = 0.0

    # ── Totales deducciones ───────────────────────────────────────────────────
    total_deducciones = round(
        imss["total"] + isr_retenido + infonavit +
        faltas_desc + anticipos + otras_deducciones, 2
    )

    neto_pagar = round(max(0.0, total_percepciones - total_deducciones), 2)

    return {
        # Identity
        "employee_id":          employee_id,
        "period_start":         period_start.isoformat(),
        "period_end":           period_end.isoformat(),
        "period_type":          period_type,

        # Attendance
        "dias_periodo":         dias_periodo,
        "dias_trabajados":      round(dias_trabajados, 2),
        "horas_trabajadas":     round(horas_trabajadas, 2),
        "dias_faltados":        round(dias_faltados, 2),

        # Salary basis
        "salario_diario":       round(sd, 2),
        "salario_integrado":    round(sbc, 2),
        "factor_integracion":   round(fi, 4),

        # Percepciones gravadas
        "sueldo_gravado":       sueldo_base,
        "horas_extra_monto":    he_monto,
        "horas_extra_exento":   he_exento,
        "horas_extra_gravado":  he_gravado,
        "festivos_monto":       festivos_monto,
        "vacaciones_monto":     vac_monto,
        "prima_vacacional":     prima_vac_monto,
        "prima_vac_exento":     pv_exento,
        "prima_vac_gravado":    pv_gravado,
        "aguinaldo_monto":      aguin_monto,
        "aguinaldo_exento":     ag_exento,
        "aguinaldo_gravado":    ag_gravado,
        "otros_gravados":       round(otros_gravados, 2),

        # Percepciones exentas
        "despensa":             round(despensa_monto, 2),
        "transporte":           round(transporte_monto, 2),
        "otros_exentos":        round(otros_exentos, 2),

        # Totales percepciones
        "total_percepciones":   total_percepciones,
        "total_gravado":        total_gravado,
        "total_exento":         total_exento,

        # IMSS
        "imss_enfermedad_mat":  imss["enfermedad_maternidad"],
        "imss_enfermedad_exc":  imss["enfermedad_mat_excedente"],
        "imss_invalidez_vida":  imss["invalidez_vida"],
        "imss_cesantia_vejez":  imss["cesantia_vejez"],
        "imss_total":           imss["total"],

        # ISR
        "base_isr":             base_isr,
        "isr_causado":          isr_causado,
        "subsidio_empleo":      subsidio,
        "isr_retenido":         isr_retenido,

        # Otras deducciones
        "infonavit":            round(infonavit, 2),
        "faltas_descuento":     faltas_desc,
        "anticipos":            round(anticipos, 2),
        "otras_deducciones":    round(otras_deducciones, 2),

        # Totales
        "total_deducciones":    total_deducciones,
        "neto_pagar":           neto_pagar,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Batch calculation — full period for all employees
# ─────────────────────────────────────────────────────────────────────────────

def calcular_nomina_periodo(period_start: date, period_end: date,
                             period_type: str) -> list[dict]:
    """
    Calculate payroll for ALL active employees for a given period.
    Pulls attendance data automatically from the events table.
    Returns list of payroll result dicts.
    """
    from services.attendance import get_weekly_attendance

    conn = get_conn()
    employees = conn.execute("""
        SELECT ep.employee_id, u.name, ep.salary_type, ep.salary_amount,
               ep.pay_period_type, ep.has_subsidio, ep.zona_libre
        FROM employee_payroll ep
        JOIN users u ON u.employee_id = ep.employee_id
        WHERE ep.active = 1 AND u.is_active = 1
    """).fetchall()
    conn.close()

    # Get attendance for the period
    attendance = get_weekly_attendance(period_start, period_end)

    results = []
    for emp in employees:
        emp_id = emp["employee_id"]
        att    = attendance.get(emp_id, {})
        dias   = att.get("total_days", 0)
        horas  = att.get("total_hours", 0.0)

        result = calcular_nomina(
            employee_id      = emp_id,
            period_start     = period_start,
            period_end       = period_end,
            period_type      = period_type,
            dias_trabajados  = dias,
            horas_trabajadas = horas,
        )
        result["name"] = emp["name"]
        results.append(result)

    return results
