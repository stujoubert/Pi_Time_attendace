from flask import Blueprint, render_template, request, redirect, url_for, flash, session
from werkzeug.security import generate_password_hash
from db import get_conn, list_devices
from authz import login_required, role_required
import sqlite3

# ─── USERS ───────────────────────────────────────────────────────────────────

bp_users = Blueprint("users", __name__, url_prefix="/users")

@bp_users.route("/")
@login_required
@role_required("admin", "manager")
def users_list():
    show_inactive = request.args.get("show_inactive") == "1"
    conn = get_conn()
    where = "" if show_inactive else "WHERE u.is_active=1"
    rows = conn.execute(f"""
        SELECT u.employee_id, u.name, u.is_active,
               COALESCE(d.name,'—') as dept,
               COUNT(uf.id) as face_count
        FROM users u
        LEFT JOIN departments d ON d.id=u.department_id
        LEFT JOIN user_faces uf ON uf.employee_id=u.employee_id
        {where} {'' if session.get('role')=='admin' else 'AND COALESCE(d.hidden,0)=0'}
        GROUP BY u.employee_id
        ORDER BY CASE WHEN CAST(u.employee_id AS INTEGER)>0
                 THEN CAST(u.employee_id AS INTEGER) ELSE 999999 END
    """).fetchall()
    conn.close()
    return render_template("users.html", users=rows, show_inactive=show_inactive)

@bp_users.route("/add", methods=["GET", "POST"])
@login_required
@role_required("admin")
def users_add():
    conn = get_conn()
    if request.method == "POST":
        emp_id = request.form.get("employee_id", "").strip()
        name   = request.form.get("name", "").strip()
        dept   = request.form.get("department_id") or None
        if not emp_id or not name:
            flash("Employee ID and name are required", "danger")
        else:
            try:
                conn.execute(
                    "INSERT INTO users (employee_id, name, is_active, department_id) VALUES (?,?,1,?)",
                    (emp_id, name, dept))
                conn.commit()
                conn.close()
                # Optional face photo captured at creation time: save it as the
                # enrolled face and push to the terminals immediately.
                f = request.files.get("face_photo")
                if f and f.filename:
                    try:
                        from services.face_sync import store_face_bytes
                        from routes.faces import _push_face_to_devices
                        data = f.read()
                        if store_face_bytes(emp_id, data):
                            try:
                                _push_face_to_devices(emp_id, data)
                                flash("User added with face photo (pushed to devices)", "success")
                            except Exception:
                                flash("User added with face photo (device push pending)", "success")
                        else:
                            flash("User added, but the photo was not a valid image", "warning")
                    except Exception as e:
                        flash(f"User added; photo failed: {e}", "warning")
                else:
                    flash("User added", "success")
                return redirect(url_for("users.users_list"))
            except sqlite3.IntegrityError:
                flash("Employee ID already exists", "danger")

    next_id = (conn.execute("SELECT MAX(CAST(employee_id AS INTEGER)) FROM users").fetchone()[0] or 0) + 1
    depts   = conn.execute("SELECT id, name FROM departments ORDER BY name").fetchall()
    conn.close()
    return render_template("users_add.html", next_id=next_id, depts=depts, devices=list_devices())

@bp_users.route("/edit/<emp_id>", methods=["GET", "POST"])
@login_required
@role_required("admin")
def users_edit(emp_id):
    conn = get_conn()
    if request.method == "POST":
        req_prod = 1 if request.form.get("requires_production") else 0
        conn.execute(
            "UPDATE users SET name=?, department_id=?, requires_production=? WHERE employee_id=?",
            (request.form["name"], request.form.get("department_id") or None,
             req_prod, emp_id))
        conn.commit()
        conn.close()
        flash("User updated", "success")
        return redirect(url_for("users.users_list"))
    user  = conn.execute("SELECT * FROM users WHERE employee_id=?", (emp_id,)).fetchone()
    depts = conn.execute("SELECT id, name FROM departments ORDER BY name").fetchall()
    conn.close()
    if not user:
        flash("User not found", "danger")
        return redirect(url_for("users.users_list"))
    return render_template("users_edit.html", user=user, depts=depts)

@bp_users.route("/toggle/<emp_id>", methods=["POST"])
@login_required
@role_required("admin")
def users_toggle(emp_id):
    conn = get_conn()
    conn.execute(
        "UPDATE users SET is_active=CASE WHEN is_active=1 THEN 0 ELSE 1 END WHERE employee_id=?",
        (emp_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("users.users_list"))

@bp_users.route("/delete/<emp_id>", methods=["POST"])
@login_required
@role_required("admin")
def users_delete(emp_id):
    conn = get_conn()
    conn.execute("DELETE FROM users WHERE employee_id=?", (emp_id,))
    conn.execute("DELETE FROM events WHERE employee_id=?", (emp_id,))
    conn.execute("DELETE FROM user_faces WHERE employee_id=?", (emp_id,))
    conn.commit()
    conn.close()
    import os
    face_path = f"/var/lib/attendance/faces/{emp_id}.jpg"
    if os.path.exists(face_path):
        os.remove(face_path)
    try:
        import requests as _requests
        from requests.auth import HTTPDigestAuth as _Digit
        from db import get_conn as _get_conn
        dconn = _get_conn()
        devices = dconn.execute("SELECT ip, username, password FROM devices WHERE active=1").fetchall()
        dconn.close()
        for d in devices:
            auth = _Digit(d["username"], d["password"])
            _requests.delete(f"http://{d['ip']}/ISAPI/Intelligent/FDLib/FDSetUp?format=json",
                json={"FDID": "1", "FPID": emp_id, "faceLibType": "blackFD"},
                auth=auth, timeout=10, verify=False)
            _requests.put(f"http://{d['ip']}/ISAPI/AccessControl/UserInfo/Delete?format=json",
                json={"UserInfoDelCond": {"EmployeeNoList": [{"employeeNo": emp_id}]}},
                auth=auth, timeout=10, verify=False)
    except Exception as e:
        log.warning(f"Device cleanup failed for {emp_id}: {e}")
    flash("User deleted", "success")
    return redirect(url_for("users.users_list"))

# ─── DEPARTMENTS ─────────────────────────────────────────────────────────────
bp_depts = Blueprint("departments", __name__, url_prefix="/departments")

@bp_depts.route("/")
@login_required
@role_required("admin")
def list_departments():
    conn = get_conn()
    depts = conn.execute("""
        SELECT d.id, d.name, COUNT(u.employee_id) as user_count
        FROM departments d
        LEFT JOIN users u ON u.department_id=d.id
        GROUP BY d.id ORDER BY d.name
    """).fetchall()
    conn.close()
    return render_template("departments.html", departments=depts)

@bp_depts.route("/create", methods=["POST"])
@login_required
@role_required("admin")
def create_department():
    name = (request.form.get("name") or "").strip()
    if not name:
        flash("Name required", "warning")
        return redirect(url_for("departments.list_departments"))
    conn = get_conn()
    try:
        conn.execute("INSERT INTO departments (name) VALUES (?)", (name,))
        conn.commit()
        flash("Department created", "success")
    except Exception as e:
        flash(f"Error: {e}", "danger")
    finally:
        conn.close()
    return redirect(url_for("departments.list_departments"))

@bp_depts.route("/delete/<int:dept_id>", methods=["POST"])
@login_required
@role_required("admin")
def delete_department(dept_id):
    conn = get_conn()
    count = conn.execute("SELECT COUNT(*) FROM users WHERE department_id=?", (dept_id,)).fetchone()[0]
    if count:
        conn.close()
        flash("Cannot delete: users are assigned to this department", "warning")
        return redirect(url_for("departments.list_departments"))
    conn.execute("DELETE FROM departments WHERE id=?", (dept_id,))
    conn.commit()
    conn.close()
    flash("Department deleted", "success")
    return redirect(url_for("departments.list_departments"))

@bp_depts.route("/edit/<int:dept_id>", methods=["POST"])
@login_required
@role_required("admin")
def edit_department(dept_id):
    name = (request.form.get("name") or "").strip()
    if name:
        conn = get_conn()
        conn.execute("UPDATE departments SET name=? WHERE id=?", (name, dept_id))
        conn.commit()
        conn.close()
        flash("Department updated", "success")
    return redirect(url_for("departments.list_departments"))

# ─── ACCOUNTS ────────────────────────────────────────────────────────────────

bp_accounts = Blueprint("accounts", __name__, url_prefix="/accounts")

@bp_accounts.route("/")
@login_required
@role_required("admin")
def accounts_list():
    conn = get_conn()
    rows = conn.execute("SELECT id, username, role, employee_id, active FROM accounts ORDER BY username").fetchall()
    conn.close()
    return render_template("accounts.html", accounts=rows)

@bp_accounts.route("/create", methods=["POST"])
@login_required
@role_required("admin")
def accounts_create():
    username = request.form.get("username", "").strip()
    password = request.form.get("password", "")
    role     = request.form.get("role")
    employee_id = (request.form.get("employee_id") or "").strip() or None
    if not username or not password or role not in ("admin", "manager", "viewer", "employee", "foreman"):
        flash("Invalid input", "danger")
        return redirect(url_for("accounts.accounts_list"))
    if role in ("employee", "foreman") and not employee_id and role == "employee":
        flash("Employee accounts must be linked to an employee ID.", "danger")
        return redirect(url_for("accounts.accounts_list"))
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO accounts (username, password_hash, role, employee_id, active) VALUES (?,?,?,?,1)",
            (username, generate_password_hash(password), role, employee_id))
        conn.commit()
        flash("Account created", "success")
    except sqlite3.IntegrityError:
        flash("Username already exists", "danger")
    finally:
        conn.close()
    return redirect(url_for("accounts.accounts_list"))

@bp_accounts.route("/reset/<int:account_id>", methods=["POST"])
@login_required
@role_required("admin")
def accounts_reset(account_id):
    pw = request.form.get("password", "")
    if not pw:
        flash("Password required", "danger")
        return redirect(url_for("accounts.accounts_list"))
    conn = get_conn()
    conn.execute("UPDATE accounts SET password_hash=? WHERE id=?",
                 (generate_password_hash(pw), account_id))
    conn.commit()
    conn.close()
    flash("Password reset", "success")
    return redirect(url_for("accounts.accounts_list"))

@bp_accounts.route("/toggle/<int:account_id>", methods=["POST"])
@login_required
@role_required("admin")
def accounts_toggle(account_id):
    if session.get("account_id") == account_id:
        flash("Cannot deactivate your own account", "danger")
        return redirect(url_for("accounts.accounts_list"))
    conn = get_conn()
    conn.execute(
        "UPDATE accounts SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?",
        (account_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("accounts.accounts_list"))
