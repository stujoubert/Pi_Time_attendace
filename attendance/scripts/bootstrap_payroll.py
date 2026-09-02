"""
scripts/bootstrap_payroll.py
Mexican payroll schema — idempotent, safe to run on every startup.
Adds tables for:
  - employee_payroll    : salary type, SD, pay period, IMSS data
  - percepciones        : configurable allowances per employee
  - payroll_periods     : quincena / catorcena period definitions
  - payroll_runs        : calculated payroll per employee per period
  - payroll_concepts    : line items per run (percepciones / deducciones)
"""
import sqlite3, os

def main():
    db_path = os.environ.get("ATT_DB")
    if not db_path:
        return
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = OFF")
    cur = conn.cursor()

    # ── Employee payroll configuration ────────────────────────────────────────
    _exec(cur, """
        CREATE TABLE IF NOT EXISTS employee_payroll (
            employee_id     TEXT PRIMARY KEY REFERENCES users(employee_id),
            salary_type     TEXT NOT NULL DEFAULT 'daily',
                            -- 'daily' = salario diario fijo
                            -- 'hourly' = salario por hora
                            -- 'monthly' = salario mensual
            salary_amount   REAL NOT NULL DEFAULT 0,
                            -- daily rate, hourly rate, or monthly amount
            pay_period_type TEXT NOT NULL DEFAULT 'quincena',
                            -- 'quincena'  = 1-15 / 16-end of month
                            -- 'catorcena' = every 14 days from start_date
            catorcena_start TEXT,
                            -- YYYY-MM-DD: the anchor date for catorcena cycles
            num_dias_pago   INTEGER NOT NULL DEFAULT 15,
                            -- Days per period used for proration (15 or 14)
            imss_num        TEXT,   -- Número de seguridad social
            rfc             TEXT,   -- RFC del empleado
            curp            TEXT,   -- CURP del empleado
            zona_libre      INTEGER NOT NULL DEFAULT 0,
                            -- 1 = Zona Libre Frontera Norte
            has_subsidio    INTEGER NOT NULL DEFAULT 1,
                            -- 1 = eligible for subsidio al empleo
            active          INTEGER NOT NULL DEFAULT 1,
            updated_at      TEXT DEFAULT (datetime('now'))
        )
    """)

    # ── Percepciones exentas / gravadas per employee ──────────────────────────
    _exec(cur, """
        CREATE TABLE IF NOT EXISTS percepciones_config (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id     TEXT NOT NULL REFERENCES users(employee_id),
            concepto        TEXT NOT NULL,
                            -- 'despensa', 'transporte', 'telefono', etc.
            tipo            TEXT NOT NULL DEFAULT 'exenta',
                            -- 'exenta' or 'gravada'
            monto_fijo      REAL DEFAULT 0,
                            -- Fixed amount per period
            monto_diario    REAL DEFAULT 0,
                            -- Per day worked (alternative to fixed)
            activo          INTEGER NOT NULL DEFAULT 1,
            UNIQUE(employee_id, concepto)
        )
    """)

    # ── Payroll periods ───────────────────────────────────────────────────────
    _exec(cur, """
        CREATE TABLE IF NOT EXISTS payroll_periods (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            period_type     TEXT NOT NULL,   -- 'quincena' or 'catorcena'
            period_num      INTEGER,         -- 1..24 for quincena, sequential for catorcena
            year            INTEGER NOT NULL,
            month           INTEGER,         -- for quincena: 1..12
            half            INTEGER,         -- for quincena: 1 or 2
            start_date      TEXT NOT NULL,   -- YYYY-MM-DD
            end_date        TEXT NOT NULL,   -- YYYY-MM-DD
            payment_date    TEXT,            -- YYYY-MM-DD: actual pay date
            status          TEXT NOT NULL DEFAULT 'open',
                            -- 'open', 'calculated', 'paid'
            notes           TEXT,
            created_at      TEXT DEFAULT (datetime('now')),
            UNIQUE(period_type, start_date)
        )
    """)

    # ── Payroll runs (one per employee per period) ────────────────────────────
    _exec(cur, """
        CREATE TABLE IF NOT EXISTS payroll_runs (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            period_id           INTEGER NOT NULL REFERENCES payroll_periods(id),
            employee_id         TEXT NOT NULL REFERENCES users(employee_id),

            -- Attendance
            dias_trabajados     REAL NOT NULL DEFAULT 0,
            horas_trabajadas    REAL NOT NULL DEFAULT 0,
            dias_periodo        INTEGER NOT NULL DEFAULT 15,

            -- Salario base
            salario_diario      REAL NOT NULL DEFAULT 0,
            salario_integrado   REAL NOT NULL DEFAULT 0,  -- SD * factor integracion

            -- Percepciones gravadas
            sueldo_gravado      REAL NOT NULL DEFAULT 0,
            horas_extra_monto   REAL NOT NULL DEFAULT 0,
            vacaciones_monto    REAL NOT NULL DEFAULT 0,
            prima_vacacional    REAL NOT NULL DEFAULT 0,
            aguinaldo_monto     REAL NOT NULL DEFAULT 0,
            otros_gravados      REAL NOT NULL DEFAULT 0,

            -- Percepciones exentas
            despensa            REAL NOT NULL DEFAULT 0,
            transporte          REAL NOT NULL DEFAULT 0,
            otros_exentos       REAL NOT NULL DEFAULT 0,

            -- Totales percepciones
            total_percepciones  REAL NOT NULL DEFAULT 0,
            total_gravado       REAL NOT NULL DEFAULT 0,
            total_exento        REAL NOT NULL DEFAULT 0,

            -- Deducciones IMSS (cuota obrera)
            imss_enfermedad_mat REAL NOT NULL DEFAULT 0,  -- 0.75% gravado base
            imss_invalidez_vida REAL NOT NULL DEFAULT 0,  -- 0.625%
            imss_cesantia_vejez REAL NOT NULL DEFAULT 0,  -- 1.125%
            imss_total          REAL NOT NULL DEFAULT 0,

            -- ISR
            base_isr            REAL NOT NULL DEFAULT 0,
            isr_causado         REAL NOT NULL DEFAULT 0,
            subsidio_empleo     REAL NOT NULL DEFAULT 0,
            isr_retenido        REAL NOT NULL DEFAULT 0,  -- causado - subsidio (min 0)

            -- Otras deducciones
            infonavit           REAL NOT NULL DEFAULT 0,
            faltas_descuento    REAL NOT NULL DEFAULT 0,
            anticipos           REAL NOT NULL DEFAULT 0,
            otras_deducciones   REAL NOT NULL DEFAULT 0,

            -- Totales deducciones
            total_deducciones   REAL NOT NULL DEFAULT 0,

            -- Neto
            neto_pagar          REAL NOT NULL DEFAULT 0,

            status              TEXT NOT NULL DEFAULT 'draft',
            calculated_at       TEXT DEFAULT (datetime('now')),
            notes               TEXT,

            UNIQUE(period_id, employee_id)
        )
    """)

    # ── ISR tables 2026 (quincena) ────────────────────────────────────────────
    # Source: SAT Anexo 8 — tarifa para el cálculo del impuesto por período
    _exec(cur, """
        CREATE TABLE IF NOT EXISTS isr_tabla (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            year            INTEGER NOT NULL,
            period_type     TEXT NOT NULL,   -- 'quincena' or 'catorcena'
            li              REAL NOT NULL,   -- límite inferior
            ls              REAL NOT NULL,   -- límite superior
            cuota_fija      REAL NOT NULL,
            tasa_excedente  REAL NOT NULL,   -- as decimal e.g. 0.064
            UNIQUE(year, period_type, li)
        )
    """)

    # ── Subsidio al empleo tables 2026 (quincena) ─────────────────────────────
    _exec(cur, """
        CREATE TABLE IF NOT EXISTS subsidio_tabla (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            year            INTEGER NOT NULL,
            period_type     TEXT NOT NULL,
            li              REAL NOT NULL,
            ls              REAL NOT NULL,
            subsidio        REAL NOT NULL,
            UNIQUE(year, period_type, li)
        )
    """)

    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()

    # ── Seed ISR 2026 quincena tabla ─────────────────────────────────────────
    _seed_isr_2026(cur, conn)
    _seed_subsidio_2026(cur, conn)

    conn.commit()
    conn.close()


def _seed_isr_2026(cur, conn):
    """ISR tabla quincena 2026 — SAT Anexo 8."""
    existing = cur.execute(
        "SELECT COUNT(*) FROM isr_tabla WHERE year=2026 AND period_type='quincena'"
    ).fetchone()[0]
    if existing:
        return

    # Quincena = annual / 24
    # Source: SAT Resolución Miscelánea Fiscal 2026, Anexo 8
    rows = [
        # li,        ls,          cuota_fija, tasa
        (0.01,       792.82,      0.00,       0.0192),
        (792.83,     6729.31,     15.22,      0.0640),
        (6729.32,    11832.47,    395.91,     0.1088),
        (11832.48,   13761.99,    951.05,     0.1600),
        (13762.00,   16466.17,    1259.80,    0.1792),
        (16466.18,   33219.38,    1744.44,    0.2136),
        (33219.39,   52358.62,    5322.19,    0.2352),
        (52358.63,   99824.19,    9818.86,    0.3000),
        (99824.20,   133105.12,   24054.71,   0.3200),
        (133105.13,  399315.37,   34679.09,   0.3400),
        (399315.38,  9999999.99,  125188.73,  0.3500),
    ]
    cur.executemany(
        "INSERT OR IGNORE INTO isr_tabla(year,period_type,li,ls,cuota_fija,tasa_excedente) VALUES(2026,'quincena',?,?,?,?)",
        rows
    )

    # Catorcena = annual / 26
    factor = 24 / 26
    cat_rows = [(round(li*factor,4), round(ls*factor,4), round(cf*factor,4), t)
                for li,ls,cf,t in rows]
    cur.executemany(
        "INSERT OR IGNORE INTO isr_tabla(year,period_type,li,ls,cuota_fija,tasa_excedente) VALUES(2026,'catorcena',?,?,?,?)",
        cat_rows
    )


def _seed_subsidio_2026(cur, conn):
    """Subsidio al empleo quincena 2026 — SAT Anexo 8."""
    existing = cur.execute(
        "SELECT COUNT(*) FROM subsidio_tabla WHERE year=2026 AND period_type='quincena'"
    ).fetchone()[0]
    if existing:
        return

    rows = [
        # li,       ls,         subsidio
        (0.01,      2981.83,    758.33),
        (2981.84,   3177.41,    758.33),
        (3177.42,   3490.20,    721.52),
        (3490.21,   3832.52,    685.21),
        (3832.53,   4094.48,    649.00),
        (4094.49,   4462.24,    612.79),
        (4462.25,   4689.60,    551.25),
        (4689.61,   5091.50,    509.17),
        (5091.51,   5416.34,    467.09),
        (5416.35,   6500.00,    425.00),
        (6500.01,   9999999.99, 0.00),
    ]
    cur.executemany(
        "INSERT OR IGNORE INTO subsidio_tabla(year,period_type,li,ls,subsidio) VALUES(2026,'quincena',?,?,?)",
        rows
    )

    factor = 24 / 26
    cat_rows = [(round(li*factor,4), round(ls*factor,4), round(s*factor,4))
                for li,ls,s in rows]
    cur.executemany(
        "INSERT OR IGNORE INTO subsidio_tabla(year,period_type,li,ls,subsidio) VALUES(2026,'catorcena',?,?,?)",
        cat_rows
    )


def _exec(cur, sql):
    try:
        cur.execute(sql)
    except Exception as e:
        if "already exists" not in str(e).lower():
            print(f"[PAYROLL BOOTSTRAP] Warning: {e}")


if __name__ == "__main__":
    main()
