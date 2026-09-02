from flask import Blueprint, render_template, request, redirect, url_for, flash, g, send_from_directory
from db import get_conn
from authz import login_required, role_required

# ─── SCHEDULE TEMPLATES ───────────────────────────────────────────────────────

bp_schedules = Blueprint("schedules", __name__, url_prefix="/schedules")

@bp_schedules.route("/")
@login_required
@role_required("admin")
def templates_page():
    conn = get_conn()
    templates = conn.execute(
        "SELECT id, name, description FROM schedule_templates ORDER BY name"
    ).fetchall()
    conn.close()
    return render_template("schedule_templates.html", templates=templates, T=g.T)

@bp_schedules.route("/create", methods=["POST"])
@login_required
@role_required("admin")
def create_template():
    name = request.form.get("name", "").strip()
    desc = request.form.get("description", "").strip()
    if not name:
        flash("Template name required", "danger")
        return redirect(url_for("schedules.templates_page"))
    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO schedule_templates (name, description) VALUES (?,?)", (name, desc))
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return redirect(url_for("schedules.edit_template", template_id=tid))

@bp_schedules.route("/<int:template_id>", methods=["GET", "POST"])
@login_required
@role_required("admin")
def edit_template(template_id):
    conn = get_conn()
    if request.method == "POST":
        conn.execute(
            "UPDATE schedule_templates SET name=?, description=? WHERE id=?",
            (request.form.get("name","").strip(),
             request.form.get("description","").strip(), template_id))
        conn.commit()
        flash("Template saved", "success")
    template = conn.execute(
        "SELECT * FROM schedule_templates WHERE id=?", (template_id,)).fetchone()
    rules_raw = conn.execute(
        "SELECT * FROM schedule_rules WHERE template_id=? ORDER BY priority,id",
        (template_id,)).fetchall()
    rules = []
    for r in rules_raw:
        shifts = conn.execute(
            "SELECT * FROM schedule_shifts WHERE rule_id=? ORDER BY start_time",
            (r["id"],)).fetchall()
        rules.append({**dict(r), "shifts": [dict(s) for s in shifts]})
    conn.close()
    if not template:
        flash("Template not found", "danger")
        return redirect(url_for("schedules.templates_page"))
    return render_template("schedule_edit.html", template=dict(template), rules=rules, T=g.T)

@bp_schedules.route("/<int:template_id>/delete", methods=["POST"])
@login_required
@role_required("admin")
def delete_template(template_id):
    conn = get_conn()
    conn.execute("DELETE FROM schedule_rules WHERE template_id=?", (template_id,))
    conn.execute("DELETE FROM schedule_templates WHERE id=?", (template_id,))
    conn.commit()
    conn.close()
    flash("Template deleted", "warning")
    return redirect(url_for("schedules.templates_page"))

@bp_schedules.route("/<int:template_id>/rules/add", methods=["POST"])
@login_required
@role_required("admin")
def add_rule(template_id):
    days     = request.form.getlist("weekdays[]")
    priority = int(request.form.get("priority", 0))
    if not days:
        flash("Select at least one day", "danger")
        return redirect(url_for("schedules.edit_template", template_id=template_id))
    conn = get_conn()
    conn.execute(
        "INSERT INTO schedule_rules (template_id, weekdays, priority) VALUES (?,?,?)",
        (template_id, ",".join(days), priority))
    conn.commit()
    conn.close()
    return redirect(url_for("schedules.edit_template", template_id=template_id))

@bp_schedules.route("/rules/<int:rule_id>/delete", methods=["POST"])
@login_required
@role_required("admin")
def delete_rule(rule_id):
    conn = get_conn()
    row = conn.execute("SELECT template_id FROM schedule_rules WHERE id=?", (rule_id,)).fetchone()
    if row:
        conn.execute("DELETE FROM schedule_shifts WHERE rule_id=?", (rule_id,))
        conn.execute("DELETE FROM schedule_rules WHERE id=?", (rule_id,))
        conn.commit()
        template_id = row["template_id"]
        conn.close()
        return redirect(url_for("schedules.edit_template", template_id=template_id))
    conn.close()
    return redirect(url_for("schedules.templates_page"))

@bp_schedules.route("/rules/<int:rule_id>/shifts/add", methods=["POST"])
@login_required
@role_required("admin")
def add_shift(rule_id):
    conn = get_conn()
    row = conn.execute("SELECT template_id FROM schedule_rules WHERE id=?", (rule_id,)).fetchone()
    if not row:
        conn.close()
        flash("Rule not found", "danger")
        return redirect(url_for("schedules.templates_page"))
    conn.execute(
        "INSERT INTO schedule_shifts (rule_id, start_time, end_time, grace_minutes, break_minutes) VALUES (?,?,?,?,?)",
        (rule_id, request.form["start_time"], request.form["end_time"],
         int(request.form.get("grace_minutes", 0)), int(request.form.get("break_minutes", 0))))
    conn.commit()
    template_id = row["template_id"]
    conn.close()
    return redirect(url_for("schedules.edit_template", template_id=template_id))

@bp_schedules.route("/shifts/<int:shift_id>/delete", methods=["POST"])
@login_required
@role_required("admin")
def delete_shift(shift_id):
    conn = get_conn()
    row = conn.execute("""
        SELECT r.template_id FROM schedule_shifts s
        JOIN schedule_rules r ON r.id=s.rule_id WHERE s.id=?
    """, (shift_id,)).fetchone()
    if row:
        template_id = row["template_id"]
        conn.execute("DELETE FROM schedule_shifts WHERE id=?", (shift_id,))
        conn.commit()
        conn.close()
        return redirect(url_for("schedules.edit_template", template_id=template_id))
    conn.close()
    return redirect(url_for("schedules.templates_page"))

@bp_schedules.route("/assign", methods=["GET", "POST"])
@login_required
@role_required("admin")
def assign_templates():
    conn = get_conn()
    if request.method == "POST":
        template_id  = request.form.get("template_id") or None
        employee_ids = request.form.getlist("employee_ids")
        if not employee_ids:
            flash("Select at least one user", "danger")
            return redirect(url_for("schedules.assign_templates"))
        if template_id:
            for emp in employee_ids:
                conn.execute(
                    "INSERT OR REPLACE INTO user_schedule_assignments (employee_id, template_id) VALUES (?,?)",
                    (emp, int(template_id)))
        else:
            for emp in employee_ids:
                conn.execute(
                    "DELETE FROM user_schedule_assignments WHERE employee_id=?", (emp,))
        conn.commit()
        flash(f"Schedule updated for {len(employee_ids)} users", "success")
        conn.close()
        return redirect(url_for("schedules.assign_templates"))

    templates = conn.execute(
        "SELECT id, name FROM schedule_templates ORDER BY name").fetchall()
    users = conn.execute("""
        SELECT u.employee_id, u.name,
               st.name as current_template
        FROM users u
        LEFT JOIN user_schedule_assignments usa ON usa.employee_id=u.employee_id
        LEFT JOIN schedule_templates st ON st.id=usa.template_id
        WHERE u.is_active=1
        ORDER BY CAST(u.employee_id AS INTEGER)
    """).fetchall()
    conn.close()
    return render_template("schedules_assign.html", templates=templates, users=users, T=g.T)

# ─── COMPANY SETTINGS ────────────────────────────────────────────────────────

bp_company = Blueprint("company", __name__, url_prefix="/company")

@bp_company.route("/", methods=["GET", "POST"])
@login_required
@role_required("admin")
def company_settings():
    from services.settings import get_company_settings, save_company_settings
    from db import get_setting, set_setting
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        rfc  = request.form.get("rfc", "").strip()
        if not name:
            flash("Company name required", "danger")
            return redirect(url_for("company.company_settings"))
        save_company_settings(name, rfc, request.files.get("logo"))
        # SMTP settings
        for key in ["smtp_sender","smtp_host","smtp_port","smtp_pass","report_recipient",
                    "work_start_time","work_end_time","week_type","tz_offset"]:
            val = request.form.get(key, "")
            if key != "smtp_pass" or val:  # only update password if provided
                set_setting(key, val)
        flash("Settings saved", "success")
        return redirect(url_for("company.company_settings"))
    from services.settings import get_company_settings
    from db import get_setting
    data = get_company_settings()
    smtp = {k: get_setting(k, "") for k in
            ["smtp_sender","smtp_host","smtp_port","report_recipient",
             "work_start_time","work_end_time","week_type","tz_offset"]}
    return render_template("company.html", data=data, smtp=smtp, T=g.T)

# ─── MISC ────────────────────────────────────────────────────────────────────

bp_misc = Blueprint("misc", __name__)

@bp_misc.route("/set-language")
def set_language():
    from flask import session
    from translations import LANG
    lang = request.args.get("lang", "en")
    if lang in LANG:
        session["lang"] = lang
    from flask import request as req
    return redirect(req.referrer or url_for("dashboard.dashboard"))

@bp_misc.route("/health")
def health():
    from flask import jsonify
    return jsonify({"status": "ok"})

@bp_misc.route("/api/scheduler/status")
@login_required
@role_required("admin")
def scheduler_status():
    from flask import jsonify
    from scheduler import get_scheduler
    s = get_scheduler()
    if not s:
        return jsonify({"running": False, "jobs": []})
    jobs = [{"id": j.id, "name": j.name,
             "next_run": str(j.next_run_time)} for j in s.get_jobs()]
    return jsonify({"running": s.running, "jobs": jobs})

@bp_misc.route("/api/fetch-now", methods=["POST"])
@login_required
@role_required("admin")
def api_fetch_now():
    from flask import jsonify
    from services.collector import fetch_all_active_devices
    results = fetch_all_active_devices()
    total = sum(v["count"] for v in results.values())
    return jsonify({"ok": True, "total_events": total, "devices": results})


@bp_misc.route('/sw.js')
def service_worker():
    """Serve service worker from root so it has full scope."""
    import os
    static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static')
    return send_from_directory(static_dir, 'sw.js',
        mimetype='application/javascript')


@bp_misc.route('/manifest.json')
def manifest():
    """Serve manifest from root with correct content type."""
    import os
    static_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'static')
    return send_from_directory(static_dir, 'manifest.json',
        mimetype='application/manifest+json')


@bp_misc.route('/departments/<int:dept_id>/toggle-hidden', methods=['POST'])
@login_required
@role_required('admin')
def toggle_dept_hidden(dept_id):
    conn = get_conn()
    d = conn.execute("SELECT hidden FROM departments WHERE id=?", (dept_id,)).fetchone()
    if d:
        conn.execute("UPDATE departments SET hidden=? WHERE id=?", (1 - (d['hidden'] or 0), dept_id))
        conn.commit()
    conn.close()
    return redirect(url_for('departments.list_departments'))
