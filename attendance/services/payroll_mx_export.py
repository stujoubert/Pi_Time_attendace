"""
services/payroll_mx_export.py
Excel exports for Mexican payroll:
  1. Nómina completa — one row per employee, all concepts
  2. Recibo individual — printable pay stub per employee
  3. SUA format — IMSS SUA upload file
"""
import io
from datetime import date
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side, numbers
from openpyxl.utils import get_column_letter


def _hdr(ws, row, cols, bg="1E293B"):
    fill = PatternFill("solid", fgColor=bg)
    font = Font(bold=True, color="FFFFFF", size=10)
    for i, label in enumerate(cols, 1):
        c = ws.cell(row=row, column=i, value=label)
        c.fill = fill
        c.font = font
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[row].height = 28

def _thin():
    s = Side(style="thin", color="CCCCCC")
    return Border(left=s, right=s, top=s, bottom=s)

def _money(ws, row, col, value):
    c = ws.cell(row=row, column=col, value=round(value, 2))
    c.number_format = '"$"#,##0.00'
    c.alignment = Alignment(horizontal="right")
    return c

def _pct(ws, row, col, value):
    c = ws.cell(row=row, column=col, value=value)
    c.number_format = '0.0000'
    c.alignment = Alignment(horizontal="right")
    return c


def export_nomina_completa(results: list[dict], period_start: date,
                            period_end: date, company_name: str = "") -> io.BytesIO:
    """
    Full payroll spreadsheet — one row per employee, all concepts.
    Suitable for accountant review and internal records.
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "Nómina"

    # Title
    title = f"Nómina — {period_start.strftime('%d/%m/%Y')} al {period_end.strftime('%d/%m/%Y')}"
    if company_name:
        title = f"{company_name} | {title}"
    ws.merge_cells("A1:AH1")
    c = ws["A1"]
    c.value = title
    c.font  = Font(bold=True, size=13, color="FFFFFF")
    c.fill  = PatternFill("solid", fgColor="1E293B")
    c.alignment = Alignment(horizontal="center", vertical="center")
    ws.row_dimensions[1].height = 30

    headers = [
        # Identity
        "ID", "Nombre",
        # Attendance
        "Días Período", "Días Trabajados", "Días Faltados", "Horas Trab.",
        # Salary
        "S.D.", "S.B.C.", "Factor Int.",
        # Percepciones gravadas
        "Sueldo", "H.Extra", "Festivos", "Vacaciones", "Prima Vac.", "Aguinaldo", "Otros Grav.",
        # Percepciones exentas
        "Despensa", "Transporte", "Otros Exentos",
        # Totals percepciones
        "Total Gravado", "Total Exento", "TOTAL PERCEPCIONES",
        # IMSS
        "IMSS E.M.", "IMSS I.V.", "IMSS C.V.", "IMSS TOTAL",
        # ISR
        "Base ISR", "ISR Causado", "Subsidio Emp.", "ISR Retenido",
        # Otras deducciones
        "INFONAVIT", "Faltas", "Anticipos", "Otras Ded.",
        # Final
        "TOTAL DEDUCC.", "NETO A PAGAR",
    ]
    _hdr(ws, 2, headers)

    # Column widths
    widths = [8, 28, 8, 8, 8, 8, 9, 9, 7,
              11, 9, 9, 10, 10, 10, 9,
              9, 9, 9,
              11, 11, 14,
              9, 9, 9, 10,
              9, 10, 11, 10,
              9, 9, 9, 9,
              11, 13]
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w

    # Data rows
    alt_fill = PatternFill("solid", fgColor="F8FAFC")
    bold_fill_green = PatternFill("solid", fgColor="D1FAE5")
    bold_fill_red   = PatternFill("solid", fgColor="FEE2E2")

    totals = {k: 0.0 for k in [
        "dias_trabajados","horas_trabajadas","sueldo_gravado","horas_extra_monto",
        "festivos_monto","vacaciones_monto","prima_vacacional","aguinaldo_monto",
        "otros_gravados","despensa","transporte","otros_exentos",
        "total_gravado","total_exento","total_percepciones",
        "imss_enfermedad_mat","imss_invalidez_vida","imss_cesantia_vejez","imss_total",
        "base_isr","isr_causado","subsidio_empleo","isr_retenido",
        "infonavit","faltas_descuento","anticipos","otras_deducciones",
        "total_deducciones","neto_pagar",
    ]}

    for i, r in enumerate(results, 3):
        fill = alt_fill if i % 2 == 0 else None

        def cell(col, val, fmt=None):
            c = ws.cell(row=i, column=col, value=val)
            c.alignment = Alignment(horizontal="center", vertical="center")
            c.border = _thin()
            if fill:
                c.fill = fill
            if fmt:
                c.number_format = fmt
            return c

        col = 1
        cell(col, r.get("employee_id", "")); col+=1
        c = cell(col, r.get("name", "")); c.alignment = Alignment(horizontal="left"); col+=1
        cell(col, r.get("dias_periodo", 0)); col+=1
        cell(col, r.get("dias_trabajados", 0)); col+=1
        cell(col, r.get("dias_faltados", 0)); col+=1
        cell(col, r.get("horas_trabajadas", 0)); col+=1

        for key in ["salario_diario","salario_integrado","factor_integracion"]:
            cell(col, r.get(key, 0), '"$"#,##0.0000'); col+=1

        for key in ["sueldo_gravado","horas_extra_monto","festivos_monto",
                    "vacaciones_monto","prima_vacacional","aguinaldo_monto","otros_gravados",
                    "despensa","transporte","otros_exentos"]:
            c = cell(col, r.get(key, 0), '"$"#,##0.00'); col+=1
            if key in totals:
                totals[key] += r.get(key, 0)

        # Subtotals percepciones
        for key in ["total_gravado","total_exento"]:
            c = cell(col, r.get(key, 0), '"$"#,##0.00')
            c.font = Font(bold=True); col+=1
            totals[key] += r.get(key, 0)
        c = cell(col, r.get("total_percepciones", 0), '"$"#,##0.00')
        c.font = Font(bold=True, color="0F6E56")
        c.fill = bold_fill_green
        col+=1
        totals["total_percepciones"] += r.get("total_percepciones", 0)

        for key in ["imss_enfermedad_mat","imss_invalidez_vida","imss_cesantia_vejez","imss_total"]:
            c = cell(col, r.get(key, 0), '"$"#,##0.00')
            if key == "imss_total":
                c.font = Font(bold=True)
            col+=1
            totals[key] += r.get(key, 0)

        for key in ["base_isr","isr_causado","subsidio_empleo","isr_retenido"]:
            c = cell(col, r.get(key, 0), '"$"#,##0.00')
            if key == "isr_retenido":
                c.font = Font(bold=True)
            col+=1
            totals[key] += r.get(key, 0)

        for key in ["infonavit","faltas_descuento","anticipos","otras_deducciones"]:
            cell(col, r.get(key, 0), '"$"#,##0.00'); col+=1
            totals[key] += r.get(key, 0)

        c = cell(col, r.get("total_deducciones", 0), '"$"#,##0.00')
        c.font = Font(bold=True, color="991B1B")
        c.fill = bold_fill_red; col+=1
        totals["total_deducciones"] += r.get("total_deducciones", 0)

        c = cell(col, r.get("neto_pagar", 0), '"$"#,##0.00')
        c.font = Font(bold=True, size=11, color="0F6E56")
        c.fill = PatternFill("solid", fgColor="D1FAE5")
        totals["neto_pagar"] += r.get("neto_pagar", 0)

    # Totals row
    tr = len(results) + 3
    ws.cell(tr, 1, "TOTALES").font = Font(bold=True, size=11)
    ws.cell(tr, 1).fill = PatternFill("solid", fgColor="1E293B")
    ws.cell(tr, 1).font = Font(bold=True, color="FFFFFF")
    # Net total highlighted
    neto_cell = ws.cell(tr, len(headers))
    neto_cell.value = round(totals["neto_pagar"], 2)
    neto_cell.number_format = '"$"#,##0.00'
    neto_cell.font = Font(bold=True, size=12, color="0F6E56")
    neto_cell.fill = PatternFill("solid", fgColor="D1FAE5")

    ws.freeze_panes = "C3"

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def export_recibos(results: list[dict], period_start: date,
                   period_end: date, company_name: str = "") -> io.BytesIO:
    """
    Individual pay stubs (recibos de nómina) — one sheet per employee.
    Printable A5/A4 format.
    """
    wb = Workbook()
    wb.remove(wb.active)

    for r in results:
        name  = r.get("name", r.get("employee_id", "?"))
        title = name[:28]
        ws    = wb.create_sheet(title=f"{r.get('employee_id','')}_{title}"[:31])

        # Header
        ws.column_dimensions["A"].width = 30
        ws.column_dimensions["B"].width = 18

        ws.merge_cells("A1:B1")
        ws["A1"] = company_name or "Recibo de Nómina"
        ws["A1"].font = Font(bold=True, size=14, color="FFFFFF")
        ws["A1"].fill = PatternFill("solid", fgColor="1E293B")
        ws["A1"].alignment = Alignment(horizontal="center")
        ws.row_dimensions[1].height = 24

        ws.merge_cells("A2:B2")
        ws["A2"] = f"Período: {period_start.strftime('%d/%m/%Y')} al {period_end.strftime('%d/%m/%Y')}"
        ws["A2"].font = Font(bold=True, size=11)
        ws["A2"].alignment = Alignment(horizontal="center")

        row = 3
        def sec(label):
            nonlocal row
            ws.merge_cells(f"A{row}:B{row}")
            ws[f"A{row}"] = label
            ws[f"A{row}"].font = Font(bold=True, color="FFFFFF", size=10)
            ws[f"A{row}"].fill = PatternFill("solid", fgColor="334155")
            ws[f"A{row}"].alignment = Alignment(horizontal="left")
            ws.row_dimensions[row].height = 18
            row += 1

        def line(label, value, bold=False, color=None):
            nonlocal row
            ws[f"A{row}"] = label
            c = ws[f"B{row}"]
            c.value = round(value, 2)
            c.number_format = '"$"#,##0.00'
            c.alignment = Alignment(horizontal="right")
            if bold:
                ws[f"A{row}"].font = Font(bold=True)
                c.font = Font(bold=True, color=color or "000000")
            ws.row_dimensions[row].height = 16
            row += 1

        def info(label, value):
            nonlocal row
            ws[f"A{row}"] = label
            ws[f"B{row}"] = str(value)
            ws[f"B{row}"].alignment = Alignment(horizontal="right")
            ws.row_dimensions[row].height = 15
            row += 1

        sec("DATOS DEL EMPLEADO")
        info("Nombre", name)
        info("ID", r.get("employee_id", ""))
        info("Período", r.get("period_type", "").title())
        info("Días trabajados", r.get("dias_trabajados", 0))
        info("Salario diario", f"${r.get('salario_diario', 0):,.2f}")

        sec("PERCEPCIONES")
        if r.get("sueldo_gravado", 0):      line("  Sueldo", r["sueldo_gravado"])
        if r.get("horas_extra_monto", 0):   line("  Horas extra", r["horas_extra_monto"])
        if r.get("festivos_monto", 0):      line("  Días festivos trabajados", r["festivos_monto"])
        if r.get("vacaciones_monto", 0):    line("  Vacaciones", r["vacaciones_monto"])
        if r.get("prima_vacacional", 0):    line("  Prima vacacional", r["prima_vacacional"])
        if r.get("aguinaldo_monto", 0):     line("  Aguinaldo", r["aguinaldo_monto"])
        if r.get("despensa", 0):            line("  Vales de despensa", r["despensa"])
        if r.get("transporte", 0):          line("  Transporte", r["transporte"])
        if r.get("otros_gravados", 0):      line("  Otros gravados", r["otros_gravados"])
        if r.get("otros_exentos", 0):       line("  Otros exentos", r["otros_exentos"])
        line("TOTAL PERCEPCIONES", r.get("total_percepciones", 0), bold=True, color="065F46")

        sec("DEDUCCIONES")
        if r.get("imss_total", 0):          line("  IMSS (cuota obrera)", r["imss_total"])
        if r.get("isr_causado", 0):         line("  ISR causado", r["isr_causado"])
        if r.get("subsidio_empleo", 0):     line("  Subsidio al empleo", -r["subsidio_empleo"])
        if r.get("isr_retenido", 0):        line("  ISR retenido", r["isr_retenido"])
        if r.get("infonavit", 0):           line("  INFONAVIT", r["infonavit"])
        if r.get("faltas_descuento", 0):    line("  Descuento por faltas", r["faltas_descuento"])
        if r.get("anticipos", 0):           line("  Anticipos", r["anticipos"])
        if r.get("otras_deducciones", 0):   line("  Otras deducciones", r["otras_deducciones"])
        line("TOTAL DEDUCCIONES", r.get("total_deducciones", 0), bold=True, color="991B1B")

        row += 1
        ws.merge_cells(f"A{row}:B{row}")
        neto = ws[f"A{row}"]
        neto.value = f"NETO A PAGAR:  ${r.get('neto_pagar', 0):,.2f}"
        neto.font  = Font(bold=True, size=14, color="FFFFFF")
        neto.fill  = PatternFill("solid", fgColor="065F46")
        neto.alignment = Alignment(horizontal="center", vertical="center")
        ws.row_dimensions[row].height = 30

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf


def export_sua(results: list[dict], period_start: date,
               period_end: date) -> io.BytesIO:
    """
    SUA-compatible CSV for IMSS electronic filing.
    Format: NSS, nombre, SBC, días, cuotas
    """
    import csv
    out = io.StringIO()
    w   = csv.writer(out)
    w.writerow(["NSS", "Nombre", "RFC", "SBC_Diario", "Dias",
                "IMSS_EM", "IMSS_IV", "IMSS_CV", "Total_IMSS"])
    from db import get_conn
    conn = get_conn()
    for r in results:
        ep = conn.execute(
            "SELECT imss_num, rfc FROM employee_payroll WHERE employee_id=?",
            (r.get("employee_id"),)
        ).fetchone()
        w.writerow([
            ep["imss_num"] if ep else "",
            r.get("name", ""),
            ep["rfc"] if ep else "",
            r.get("salario_integrado", 0),
            r.get("dias_trabajados", 0),
            r.get("imss_enfermedad_mat", 0),
            r.get("imss_invalidez_vida", 0),
            r.get("imss_cesantia_vejez", 0),
            r.get("imss_total", 0),
        ])
    conn.close()
    return io.BytesIO(out.getvalue().encode("utf-8-sig"))
