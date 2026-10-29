import smtplib
import io
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from datetime import datetime, timedelta
from db import get_setting
from services.reports import export_weekly_fifo

def send_weekly_report():
    smtp_sender = get_setting("smtp_sender", "")
    smtp_host   = get_setting("smtp_host",   "smtp.gmail.com")
    smtp_port   = int(get_setting("smtp_port", "587") or 587)
    smtp_pass   = get_setting("smtp_pass",   "")
    recipient   = get_setting("report_recipient", smtp_sender)

    if not smtp_sender or not smtp_pass or not recipient:
        return False

    today = datetime.now().date()
    week_start = today - timedelta(days=today.weekday())
    week_end   = week_start + timedelta(days=5)

    buf = export_weekly_fifo(week_start, week_end)

    msg = MIMEMultipart()
    msg["From"]    = smtp_sender
    msg["To"]      = recipient
    msg["Subject"] = f"Weekly Attendance {week_start} – {week_end}"
    msg.attach(MIMEText(f"Weekly attendance report attached.\nPeriod: {week_start} to {week_end}", "plain"))

    part = MIMEApplication(buf.read(), Name="weekly_attendance.xlsx")
    part["Content-Disposition"] = 'attachment; filename="weekly_attendance.xlsx"'
    msg.attach(part)

    server = smtplib.SMTP(smtp_host, smtp_port, timeout=20)
    server.starttls()
    server.login(smtp_sender, smtp_pass)
    server.send_message(msg)
    server.quit()
    return True


def send_daily_report(target_date=None):
    """
    Email a one-day attendance report (first-IN / last-OUT grid).

    Runs from the scheduler at 09:00; with no target_date it reports the
    PREVIOUS full day (yesterday), which is the complete day's attendance at
    the time it's sent. Reuses the same SMTP settings and recipient as the
    weekly report, and only sends when enabled and SMTP is configured.
    """
    if get_setting("daily_report_enabled", "1") not in ("1", "true", "True", "on"):
        return False

    smtp_sender = get_setting("smtp_sender", "")
    smtp_host   = get_setting("smtp_host",   "smtp.gmail.com")
    smtp_port   = int(get_setting("smtp_port", "587") or 587)
    smtp_pass   = get_setting("smtp_pass",   "")
    recipient   = get_setting("report_recipient", smtp_sender)

    if not smtp_sender or not smtp_pass or not recipient:
        return False

    day = target_date or (datetime.now().date() - timedelta(days=1))

    # export_weekly_fifo with start == end produces a single-day IN/OUT grid.
    buf = export_weekly_fifo(day, day)

    msg = MIMEMultipart()
    msg["From"]    = smtp_sender
    msg["To"]      = recipient
    msg["Subject"] = f"Daily Attendance {day}"
    msg.attach(MIMEText(f"Daily attendance report attached.\nDate: {day}", "plain"))

    fname = f"daily_attendance_{day}.xlsx"
    part = MIMEApplication(buf.read(), Name=fname)
    part["Content-Disposition"] = f'attachment; filename="{fname}"'
    msg.attach(part)

    server = smtplib.SMTP(smtp_host, smtp_port, timeout=20)
    server.starttls()
    server.login(smtp_sender, smtp_pass)
    server.send_message(msg)
    server.quit()
    return True
