"""
services/face_sync.py

Face management pipeline:
  1. pull_faces_from_device(device_id)       – download faces from one device → local storage
  2. pull_faces_from_all_devices()           – pull from every active device
  3. push_faces_to_device(device_id)         – push all locally-stored faces to one device
  4. full_sync_faces()                        – pull from all, then push to all
  5. get_face_bytes(employee_id)             – load face bytes from disk for a given employee
  6. store_face_bytes(employee_id, data)     – save face bytes to disk + register in DB

Local face storage: /opt/attendance/static/uploads/faces/<employee_id>.jpg
DB table         : user_faces(employee_id, picture_url, created_at)
"""

import os
import logging
import time
from pathlib import Path
from db import get_conn
from devices.hikvision_isapi import HikvisionISAPI

log = logging.getLogger(__name__)

FACE_DIR = Path(os.getenv("FACE_DIR", "/var/lib/attendance/faces"))


def _ensure_face_dir():
    FACE_DIR.mkdir(parents=True, exist_ok=True)


def _face_path(employee_id: str) -> Path:
    return FACE_DIR / f"{employee_id}.jpg"


def get_face_bytes(employee_id: str) -> bytes | None:
    """Return face image bytes from local disk, or None."""
    p = _face_path(str(employee_id))
    if p.exists() and p.stat().st_size > 100:
        return p.read_bytes()
    return None


def store_face_bytes(employee_id: str, data: bytes) -> bool:
    """
    Save face image bytes to disk and register in the DB.
    Returns True on success.
    """
    if not data or len(data) < 100:
        return False
    if not (data[:2] == b'\xff\xd8' or data[:4] == b'\x89PNG'):
        log.warning(f"[FACE] Skipping non-image data for {employee_id}")
        return False

    _ensure_face_dir()
    p = _face_path(str(employee_id))
    p.write_bytes(data)

    # Register in DB
    local_url = f"/static/uploads/faces/{employee_id}.jpg"
    conn = get_conn()
    try:
        conn.execute(
            """INSERT OR REPLACE INTO user_faces (employee_id, picture_url, created_at)
               VALUES (?, ?, datetime('now'))""",
            (str(employee_id), local_url)
        )
        conn.commit()
    finally:
        conn.close()

    return True


# ─── Pull faces FROM a device ─────────────────────────────────────────────────

def pull_faces_from_device(device_id: int) -> dict:
    """Download all enrolled faces from FDLib and store locally."""
    import requests as _requests
    from requests.auth import HTTPDigestAuth as _Digest

    conn = get_conn()
    d = conn.execute(
        "SELECT id, name, ip, username, password FROM devices WHERE id=? AND active=1",
        (device_id,)
    ).fetchone()
    conn.close()
    if not d:
        return {"error": "Device not found or inactive"}

    auth = _Digest(d["username"], d["password"])
    ip   = d["ip"]
    log.info(f"[FACE PULL] Starting pull from {d['name']} ({ip})")

    all_faces = []
    pos = 0
    while True:
        try:
            r = _requests.post(
                f"http://{ip}/ISAPI/Intelligent/FDLib/FDSearch?format=json",
                json={"searchResultPosition": pos, "maxResults": 30,
                      "faceLibType": "blackFD", "FDID": "1"},
                auth=auth, timeout=15, verify=False)
            if r.status_code >= 400:
                break
            data    = r.json()
            matches = data.get("MatchList", [])
            if not matches:
                break
            all_faces.extend(matches)
            if data.get("responseStatusStrg") != "MORE":
                break
            pos += len(matches)
        except Exception as e:
            log.debug(f"[FACE PULL] FDSearch error: {e}")
            break

    if not all_faces:
        return {"pulled": 0, "skipped": 0, "failed": 0, "employees": []}

    pulled, skipped, failed, emp_list = 0, 0, 0, []
    for m in all_faces:
        emp_id   = str(m.get("FPID", "")).strip()
        face_url = m.get("faceURL", "")
        if not emp_id or not face_url:
            continue
        existing = get_face_bytes(emp_id)
        if existing and len(existing) > 20000:
            skipped += 1
            continue
        if not face_url.startswith("http"):
            face_url = f"http://{ip}/{face_url}"
        try:
            r2 = _requests.get(face_url, auth=auth, timeout=20, verify=False)
            if r2.status_code == 200 and r2.content[:2] == b'\xff\xd8':
                if store_face_bytes(emp_id, r2.content):
                    pulled += 1
                    emp_list.append(emp_id)
                else:
                    failed += 1
            else:
                failed += 1
        except Exception as e:
            failed += 1
            log.debug(f"[FACE PULL] {emp_id} error: {e}")

    log.info(f"[FACE PULL] {d['name']}: pulled={pulled} skipped={skipped} failed={failed}")
    return {"pulled": pulled, "skipped": skipped, "failed": failed, "employees": emp_list}


def pull_faces_from_all_devices() -> dict:
    """Pull faces from every active device. Returns per-device results."""
    conn = get_conn()
    devices = conn.execute(
        "SELECT id, name FROM devices WHERE active=1"
    ).fetchall()
    conn.close()

    results = {}
    for d in devices:
        try:
            results[d["id"]] = {"device_name": d["name"],
                                 **pull_faces_from_device(d["id"])}
        except Exception as e:
            log.error(f"[FACE PULL] Error on device {d['id']}: {e}")
            results[d["id"]] = {"device_name": d["name"], "error": str(e)}
    return results


# ─── Push faces TO a device ───────────────────────────────────────────────────

def push_faces_to_device(device_id: int, force: bool = False) -> dict:
    """Push faces to device. Fresh auth per request + 5s delay to avoid lockout."""
    import requests as _requests
    import json as _json
    from requests.auth import HTTPDigestAuth as _Digit

    conn = get_conn()
    d = conn.execute(
        "SELECT id, name, ip, username, password FROM devices WHERE id=? AND active=1",
        (device_id,)
    ).fetchone()
    db_users = conn.execute(
        "SELECT employee_id, name FROM users WHERE is_active=1 ORDER BY employee_id"
    ).fetchall()
    conn.close()
    if not d:
        return {"error": "Device not found or inactive"}

    ip       = d["ip"]
    username = d["username"]
    password = d["password"]
    log.info(f"[FACE PUSH] Pushing faces to {d['name']} ({ip})")

    faces_on_device = set()
    if not force:
        try:
            auth = _Digit(username, password)
            r = _requests.post(
                f"http://{ip}/ISAPI/Intelligent/FDLib/FDSearch?format=json",
                json={"searchResultPosition": 0, "maxResults": 1000,
                      "faceLibType": "blackFD", "FDID": "1"},
                auth=auth, timeout=15, verify=False)
            if r.status_code == 200:
                faces_on_device = set(
                    str(m.get("FPID","")) for m in r.json().get("MatchList",[])
                )
        except Exception:
            pass

    pushed, skipped, failed, no_face = 0, 0, 0, 0
    for u in db_users:
        emp = str(u["employee_id"])
        if emp in faces_on_device:
            skipped += 1
            continue
        data = get_face_bytes(emp)
        if not data:
            no_face += 1
            continue
        metadata = _json.dumps({
            "faceLibType": "blackFD", "FDID": "1",
            "FPID": emp, "name": emp, "employeeNo": emp,
        })
        auth = _Digit(username, password)
        try:
            r = _requests.put(
                f"http://{ip}/ISAPI/Intelligent/FDLib/FDSetUp?format=json",
                auth=auth,
                files={
                    "FaceDataRecord": (None, metadata, "application/json"),
                    "FaceImage":      (f"{emp}.jpg", data, "image/jpeg"),
                }, timeout=30, verify=False)
            if r.status_code < 400:
                pushed += 1
            else:
                failed += 1
                log.warning(f"[FACE PUSH] {emp} -> {d['name']} FAILED: {r.text[:80]}")
        except Exception as e:
            failed += 1
            log.warning(f"[FACE PUSH] {emp} -> {d['name']} ERROR: {e}")
        time.sleep(5)

    log.info(f"[FACE PUSH] {d['name']}: pushed={pushed} skipped={skipped} failed={failed} no_face={no_face}")
    return {"pushed": pushed, "skipped": skipped, "failed": failed, "no_face": no_face}


def push_faces_to_all_devices(force: bool = False) -> dict:
    """Push all locally-stored faces to every active device."""
    conn = get_conn()
    devices = conn.execute(
        "SELECT id, name FROM devices WHERE active=1"
    ).fetchall()
    conn.close()

    results = {}
    for d in devices:
        try:
            results[d["id"]] = {"device_name": d["name"],
                                 **push_faces_to_device(d["id"], force=force)}
        except Exception as e:
            log.error(f"[FACE PUSH] Error on device {d['id']}: {e}")
            results[d["id"]] = {"device_name": d["name"], "error": str(e)}
    return results


# ─── Full sync (pull → push) ──────────────────────────────────────────────────

def full_sync_faces() -> dict:
    """
    Complete face sync cycle:
      1. Pull all faces from every device (fills local cache)
      2. Push all cached faces back to every device
    This ensures every device has every user's face regardless of which
    device they originally enrolled on.
    """
    log.info("[FACE SYNC] Starting full face sync cycle")
    pull_results = pull_faces_from_all_devices()
    push_results = push_faces_to_all_devices()

    total_pulled = sum(v.get("pulled", 0) for v in pull_results.values())
    total_pushed = sum(v.get("pushed", 0) for v in push_results.values())

    log.info(f"[FACE SYNC] Complete — pulled={total_pulled} pushed={total_pushed}")
    return {
        "pull": pull_results,
        "push": push_results,
        "total_pulled": total_pulled,
        "total_pushed": total_pushed,
    }


# ─── DB helpers ───────────────────────────────────────────────────────────────

def list_user_faces() -> list:
    """Return all users with their local face status."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT u.employee_id, u.name,
               COALESCE(d.name, '—') as dept,
               uf.picture_url,
               uf.created_at as face_updated
        FROM users u
        LEFT JOIN departments d ON d.id = u.department_id
        LEFT JOIN user_faces uf ON uf.employee_id = u.employee_id
        WHERE u.is_active = 1
        ORDER BY CAST(u.employee_id AS INTEGER), u.employee_id
    """).fetchall()
    conn.close()

    result = []
    for r in rows:
        has_local = _face_path(str(r["employee_id"])).exists()
        result.append({
            "employee_id": r["employee_id"],
            "name":        r["name"],
            "dept":        r["dept"],
            "picture_url": r["picture_url"],
            "face_updated": r["face_updated"],
            "has_local":   has_local,
        })
    return result


def get_device_face_status() -> list:
    """
    For each active device, compare DB users vs faces on device.
    Uses numOfFace from UserInfo — works regardless of how face was enrolled.
    """
    import requests as _requests
    from requests.auth import HTTPDigestAuth as _Digit

    conn = get_conn()
    devices = conn.execute(
        "SELECT id, name, ip, username, password FROM devices WHERE active=1"
    ).fetchall()
    db_employees = {str(r["employee_id"])
                    for r in conn.execute("SELECT employee_id FROM users WHERE is_active=1")}
    conn.close()

    results = []
    for d in devices:
        try:
            auth = _Digit(d["username"], d["password"])
            ip   = d["ip"]
            users_on_device = set()
            faces_on_device = set()
            pos = 0
            while True:
                r = _requests.post(
                    f"http://{ip}/ISAPI/AccessControl/UserInfo/Search?format=json",
                    json={"UserInfoSearchCond": {"searchID": "1",
                          "searchResultPosition": pos, "maxResults": 50}},
                    auth=auth, timeout=15, verify=False)
                if r.status_code != 200:
                    break
                root  = r.json().get("UserInfoSearch") or r.json().get("UserInfoSearchResult") or {}
                users = root.get("UserInfo", [])
                if isinstance(users, dict):
                    users = [users]
                if not users:
                    break
                for u in users:
                    emp = str(u.get("employeeNo", "")).strip()
                    if emp:
                        users_on_device.add(emp)
                        if u.get("numOfFace", 0) > 0:
                            faces_on_device.add(emp)
                if root.get("responseStatusStrg") != "MORE":
                    break
                pos += len(users)

            missing_users = db_employees - users_on_device
            missing_faces = db_employees - faces_on_device
            results.append({
                "id":              d["id"],
                "name":            d["name"],
                "ip":              d["ip"],
                "ok":              True,
                "users_on_device": len(users_on_device),
                "faces_on_device": len(faces_on_device),
                "db_users":        len(db_employees),
                "missing_users":   sorted(missing_users, key=lambda x: int(x) if x.isdigit() else x),
                "missing_faces":   sorted(missing_faces, key=lambda x: int(x) if x.isdigit() else x),
            })
        except Exception as e:
            results.append({"id": d["id"], "name": d["name"], "ok": False, "error": str(e)})
    return results
