"""
routes/faces.py

UI for face management:
  GET  /faces/                – overview: all users + local face status
  GET  /faces/devices         – per-device gap analysis
  POST /faces/pull/<device_id>– pull all faces from one device
  POST /faces/pull-all        – pull faces from all devices
  POST /faces/push/<device_id>– push all faces to one device
  POST /faces/push-all        – push all faces to all devices
  POST /faces/sync            – full pull-then-push cycle
  POST /faces/upload/<emp_id> – upload face image via browser (AJAX)
  GET  /faces/img/<emp_id>    – serve face image from local storage
  DELETE /faces/<emp_id>      – delete local face + remove from all devices
"""
import io
import logging
from flask import Blueprint, render_template, request, redirect, url_for, flash, \
    send_file, jsonify, g
from authz import login_required, role_required
from services.face_sync import (
    pull_faces_from_device, pull_faces_from_all_devices,
    push_faces_to_device,  push_faces_to_all_devices,
    full_sync_faces,       list_user_faces,
    get_device_face_status, store_face_bytes, get_face_bytes,
    _face_path,
)
from db import get_conn

log = logging.getLogger(__name__)
bp = Blueprint("faces", __name__, url_prefix="/faces")


# ── Overview ──────────────────────────────────────────────────────────────────

@bp.route("/")
@login_required
@role_required("admin", "manager")
def faces_overview():
    users = list_user_faces()
    has_face  = sum(1 for u in users if u["has_local"])
    no_face   = len(users) - has_face
    conn = get_conn()
    devices = conn.execute(
        "SELECT id, name, ip, active FROM devices WHERE active=1 ORDER BY name"
    ).fetchall()
    conn.close()
    return render_template("faces.html", T=g.T,
        users=users, devices=devices,
        has_face=has_face, no_face=no_face)


@bp.route("/devices")
@login_required
@role_required("admin")
def face_device_status():
    """Show per-device gap report (users missing / faces missing)."""
    status = get_device_face_status()
    return render_template("faces_devices.html", T=g.T, status=status)


# ── Pull from device(s) ───────────────────────────────────────────────────────

@bp.route("/pull/<int:device_id>", methods=["POST"])
@login_required
@role_required("admin")
def pull_from_device(device_id):
    result = pull_faces_from_device(device_id)
    if "error" in result:
        flash(f"Pull error: {result['error']}", "danger")
    else:
        flash(f"Pulled {result['pulled']} new faces "
              f"(skipped {result['skipped']} existing, {result['failed']} failed)",
              "success" if result["pulled"] >= 0 else "warning")
    return redirect(request.referrer or url_for("faces.faces_overview"))


@bp.route("/pull-all", methods=["POST"])
@login_required
@role_required("admin")
def pull_all():
    results = pull_faces_from_all_devices()
    total = sum(v.get("pulled", 0) for v in results.values())
    flash(f"Pulled {total} new faces across {len(results)} devices", "success")
    return redirect(url_for("faces.faces_overview"))


# ── Push to device(s) ─────────────────────────────────────────────────────────

@bp.route("/push/<int:device_id>", methods=["POST"])
@login_required
@role_required("admin")
def push_to_device(device_id):
    force = request.form.get("force") == "1"
    result = push_faces_to_device(device_id, force=force)
    if "error" in result:
        flash(f"Push error: {result['error']}", "danger")
    else:
        flash(f"Pushed {result['pushed']} faces to device "
              f"(skipped {result['skipped']}, {result['no_face']} had no image, "
              f"{result['failed']} failed)",
              "success")
    return redirect(request.referrer or url_for("faces.faces_overview"))


@bp.route("/push-all", methods=["POST"])
@login_required
@role_required("admin")
def push_all():
    force = request.form.get("force") == "1"
    results = push_faces_to_all_devices(force=force)
    total = sum(v.get("pushed", 0) for v in results.values())
    flash(f"Pushed {total} faces across {len(results)} devices", "success")
    return redirect(url_for("faces.faces_overview"))


@bp.route("/sync", methods=["POST"])
@login_required
@role_required("admin")
def sync_faces():
    """Full pull-then-push cycle."""
    result = full_sync_faces()
    flash(f"Face sync complete — pulled {result['total_pulled']}, "
          f"pushed {result['total_pushed']}", "success")
    return redirect(url_for("faces.faces_overview"))



def _push_face_to_devices(emp_id: str, face_bytes: bytes):
    """Push a single face to all active devices immediately after upload."""
    import requests as _requests
    import json as _json
    import subprocess as _sp
    from requests.auth import HTTPDigestAuth as _Digit
    from db import get_conn as _get_conn
    from datetime import datetime, timedelta
    try:
        pi_ip = _sp.run(["hostname","-I"], capture_output=True, text=True).stdout.strip().split()[0]
    except Exception:
        import socket
        pi_ip = socket.gethostbyname(socket.gethostname())
    face_url = f"http://{pi_ip}:5000/faces/img/{emp_id}"
    conn = _get_conn()
    devices = conn.execute(
        "SELECT ip, username, password, name, profile FROM devices WHERE active=1"
    ).fetchall()
    u = conn.execute("SELECT name FROM users WHERE employee_id=?", (emp_id,)).fetchone()
    conn.close()
    name = u["name"] if u else emp_id
    from devices.hikvision_isapi import HikvisionISAPI
    for d in devices:
        try:
            # Use the profile-aware client so auth scheme / RightPlan / 401-retry
            # all apply, instead of a hand-rolled request that bypasses them.
            api = HikvisionISAPI.from_row(d)
            # Ensure the user exists (this is the call that 401s on some models)
            api.create_or_update_user(str(emp_id), name)
            # Push the face via the faceURL the Nginx workaround serves
            auth = api._fresh_auth()
            r = _requests.put(
                f"http://{d['ip']}/ISAPI/AccessControl/UserInfo/SetUp?format=json",
                json={"UserInfo": {
                    "employeeNo": str(emp_id), "name": name, "userType": "normal",
                    "Valid": {"enable": True,
                        "beginTime": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
                        "endTime": (datetime.now() + timedelta(days=365*5)).strftime("%Y-%m-%dT%H:%M:%S"),
                        "timeType": "local"},
                    "doorRight": "1", "faceURL": face_url,
                }},
                auth=auth, timeout=30, verify=False)
            if r.status_code < 400:
                log.info(f"[FACE AUTO-PUSH] {emp_id} -> {d['name']} OK")
            else:
                log.warning(f"[FACE AUTO-PUSH] {emp_id} -> {d['name']} FAILED: {r.text[:80]}")
        except Exception as e:
            log.warning(f"[FACE AUTO-PUSH] {emp_id} -> {d['name']} ERROR: {e}")


@bp.route("/bulk-upload", methods=["POST"])
@login_required
@role_required("admin", "manager")
def bulk_upload_faces():
    """Accept multiple face images. Files must be named {employee_id}.jpg"""
    import requests as _requests
    import json as _json
    from requests.auth import HTTPDigestAuth as _Digit
    files = request.files.getlist("face_files")
    if not files:
        flash("No files selected", "warning")
        return redirect(url_for("faces.faces_overview"))
    conn = get_conn()
    devices = conn.execute(
        "SELECT ip, username, password, name, profile FROM devices WHERE active=1"
    ).fetchall()
    conn.close()
    saved, failed, pushed = 0, 0, 0
    for f in files:
        if not f.filename:
            continue
        emp_id = f.filename.rsplit(".", 1)[0].strip()
        data = f.read()
        ok = store_face_bytes(emp_id, data)
        if ok:
            saved += 1
            for d in devices:
                try:
                    metadata = _json.dumps({
                        "faceLibType": "blackFD", "FDID": "1",
                        "FPID": emp_id, "name": emp_id, "employeeNo": emp_id,
                    })
                    auth = _Digit(d["username"], d["password"])
                    r = _requests.put(
                        f"http://{d['ip']}/ISAPI/Intelligent/FDLib/FDSetUp?format=json",
                        auth=auth,
                        files={
                            "FaceDataRecord": (None, metadata, "application/json"),
                            "FaceImage": (f"{emp_id}.jpg", data, "image/jpeg"),
                        }, timeout=30, verify=False)
                    if r.status_code < 400:
                        pushed += 1
                except Exception as e:
                    log.warning(f"[BULK UPLOAD] Push failed for {emp_id}: {e}")
        else:
            failed += 1
    flash(f"Saved {saved} photos, pushed {pushed} to devices. {failed} failed.",
          "success" if saved > 0 else "danger")
    return redirect(url_for("faces.faces_overview"))


@bp.route("/missing-count")
@login_required
def missing_face_count():
    """Return count of employees missing faces on devices — for dashboard AJAX."""
    try:
        status = get_device_face_status()
        missing = set()
        for ds in status:
            if ds.get("ok"):
                for emp in ds.get("missing_faces", []):
                    missing.add(str(emp))
        return jsonify({"count": len(missing)})
    except Exception:
        return jsonify({"count": 0})


# ── Upload from browser ───────────────────────────────────────────────────────

@bp.route("/upload/<emp_id>", methods=["POST"])
@login_required
@role_required("admin", "manager")
def upload_face(emp_id):
    """
    Accept a face image uploaded from the browser.
    Supports both multipart file upload and base64 data-URL (from webcam).
    """
    # Multipart file
    f = request.files.get("face_file")
    if f and f.filename:
        data = f.read()
        ok = store_face_bytes(emp_id, data)
        if ok:
            try:
                _push_face_to_devices(emp_id, data)
            except Exception as e:
                log.warning(f"Auto face push failed for {emp_id}: {e}")
        if request.headers.get("X-Requested-With") == "XMLHttpRequest":
            return jsonify({"ok": ok, "message": "Saved" if ok else "Invalid image"})
        flash("Face image saved" if ok else "Invalid image format", "success" if ok else "danger")
        return redirect(request.referrer or url_for("faces.faces_overview"))

    # Base64 data-URL from webcam
    data_url = request.form.get("face_data_url") or request.json and request.json.get("data_url")
    if data_url and "," in data_url:
        import base64
        try:
            raw = base64.b64decode(data_url.split(",", 1)[1])
            ok  = store_face_bytes(emp_id, raw)
            if request.is_json:
                return jsonify({"ok": ok})
            flash("Face saved" if ok else "Invalid image", "success" if ok else "danger")
        except Exception as e:
            flash(f"Upload error: {e}", "danger")
        return redirect(request.referrer or url_for("faces.faces_overview"))

    flash("No image data received", "warning")
    return redirect(request.referrer or url_for("faces.faces_overview"))


# ── Serve face image ──────────────────────────────────────────────────────────

@bp.route("/img/<emp_id>")
def face_image(emp_id):
    data = get_face_bytes(emp_id)
    if not data:
        # Return a tiny transparent PNG as placeholder
        placeholder = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01'
                       b'\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00'
                       b'\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00'
                       b'\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82')
        return send_file(io.BytesIO(placeholder), mimetype="image/png")
    return send_file(io.BytesIO(data), mimetype="image/jpeg",
                     max_age=3600, conditional=True)


# ── Delete face ───────────────────────────────────────────────────────────────

@bp.route("/delete/<emp_id>", methods=["POST"])
@login_required
@role_required("admin")
def delete_face(emp_id):
    """Delete local face file, DB record, and remove from all devices."""
    # Local file
    p = _face_path(emp_id)
    if p.exists():
        p.unlink()

    # DB record
    conn = get_conn()
    conn.execute("DELETE FROM user_faces WHERE employee_id=?", (emp_id,))
    conn.commit()
    conn.close()

    # Remove from every active device
    from db import get_conn as gc
    from devices.hikvision_isapi import HikvisionISAPI
    conn2 = gc()
    devices = conn2.execute(
        "SELECT ip, username, password, name, profile FROM devices WHERE active=1"
    ).fetchall()
    conn2.close()

    removed = 0
    for d in devices:
        try:
            api = HikvisionISAPI.from_row(d)
            if api.delete_face(emp_id):
                removed += 1
        except Exception:
            pass

    flash(f"Face deleted locally and removed from {removed} device(s)", "success")
    return redirect(request.referrer or url_for("faces.faces_overview"))


# ── API endpoints (for AJAX) ──────────────────────────────────────────────────

@bp.route("/api/status")
@login_required
@role_required("admin")
def api_status():
    """JSON summary of face coverage."""
    users = list_user_faces()
    total     = len(users)
    has_local = sum(1 for u in users if u["has_local"])
    return jsonify({
        "total_users": total,
        "with_face": has_local,
        "without_face": total - has_local,
        "coverage_pct": round(has_local / total * 100, 1) if total else 0,
    })
