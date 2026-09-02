"""
services/collector.py
Handles:
- Fetching events from Hikvision devices
- Storing events deduplicated
- Syncing users between devices
- Promoting new users found in events
"""
import os
import logging
from datetime import datetime, timedelta
from db import get_conn
from devices.hikvision_isapi import HikvisionISAPI

log = logging.getLogger(__name__)
LOOKBACK_MINUTES = int(os.getenv("LOOKBACK_MINUTES", "1440"))  # 24 hours — events are deduplicated so no risk of duplicates


def _tz_suffix():
    return os.getenv("TZ_OFFSET", "-06:00")


def _normalize_ts(ts: str) -> str:
    """
    Normalize any Hikvision timestamp to 'YYYY-MM-DD HH:MM:SS'.
    Handles: ISO with TZ offset, T separator, Z suffix.
    """
    if not ts:
        return ""
    try:
        # Strip timezone offset (+HH:MM or -HH:MM or Z)
        ts = ts.replace("T", " ")
        for suffix in ("Z", "+00:00", "+01:00", "+02:00",
                       "-05:00", "-06:00", "-07:00", "-08:00"):
            if ts.endswith(suffix):
                ts = ts[:-len(suffix)]
                break
        # Handle remaining offset like "+0530"
        import re
        ts = re.sub(r'[+-]\d{4}$', '', ts).strip()
        ts = ts[:19]  # keep only YYYY-MM-DD HH:MM:SS
        # Validate by parsing
        datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
        return ts
    except Exception:
        return ts[:19] if len(ts) >= 19 else ts


def _parse_direction(ev: dict) -> str:
    """
    Determine IN/OUT from event. Hikvision uses different fields across firmware:
    - direction: 0=entering, 1=exiting (some firmware)
    - type: "IN"/"OUT" (some firmware)
    - minor event code: 75=in, 76=out
    """
    # Check explicit type field first (original system used this)
    ev_type = ev.get("type") or ev.get("eventType") or ""
    if str(ev_type).upper() in ("IN", "ENTRY", "ENTERING"):
        return "IN"
    if str(ev_type).upper() in ("OUT", "EXIT", "EXITING"):
        return "OUT"

    # Check direction field
    direction = ev.get("direction")
    if direction in (0, "0", "entering", "in", "IN"):
        return "IN"
    if direction in (1, "1", "exiting", "out", "OUT"):
        return "OUT"

    # Check minor event code
    minor = ev.get("minor")
    if minor == 75:
        return "IN"
    if minor == 76:
        return "OUT"

    # Default to IN (most common for door access events)
    return "IN"


def store_events(device_id: int, raw_events: list) -> int:
    """Store events deduplicated by (device_id, employee_id, timestamp, direction)."""
    conn   = get_conn()
    stored = 0

    for ev in raw_events:
        try:
            # Employee ID — try multiple field names
            emp_id = str(
                ev.get("employeeNoString") or
                ev.get("employeeNo") or
                ev.get("cardNo") or ""
            ).strip()

            # Name
            name = str(ev.get("name") or ev.get("userName") or "").strip()

            # Timestamp — try multiple field names
            ts_raw = (ev.get("time") or ev.get("dateTime") or
                      ev.get("localTime") or ev.get("timestamp") or "")
            ts = _normalize_ts(str(ts_raw))

            # Direction
            direction = _parse_direction(ev)

            # Picture URL
            pic = (ev.get("pictureURL") or ev.get("picFilePath") or
                   ev.get("faceURL") or None)

            if not emp_id or not ts:
                continue

            conn.execute(
                """INSERT OR IGNORE INTO events
                   (device_id, employee_id, name, timestamp, direction, picture_url)
                   VALUES (?,?,?,?,?,?)""",
                (device_id, emp_id, name, ts, direction, pic)
            )
            if conn.execute("SELECT changes()").fetchone()[0]:
                stored += 1

            # Auto-create user if not exists
            conn.execute(
                "INSERT OR IGNORE INTO users (employee_id, name, is_active) VALUES (?,?,1)",
                (emp_id, name or emp_id)
            )
        except Exception as e:
            log.debug(f"[COLLECTOR] Event parse error: {e} | raw: {str(ev)[:100]}")
            continue

    conn.execute(
        "UPDATE devices SET last_fetch_at=datetime('now'), last_fetch_count=? WHERE id=?",
        (stored, device_id)
    )
    conn.commit()
    conn.close()

    # Download face photos from picture_url fields in events
    if stored > 0:
        pass  # Face sync handled by scheduled face pull from FDLib

    return stored


def _download_faces_from_events():
    """
    Download face photos from picture_url fields stored in events.
    This is how the original system got faces — from event snapshots.
    The device embeds a snapshot URL in each access event.
    """
    import requests
    from services.face_sync import store_face_bytes, get_face_bytes, FACE_DIR

    conn = get_conn()

    # Find events that have a picture_url for employees who don't have a face yet
    rows = conn.execute("""
        SELECT DISTINCT e.employee_id, e.picture_url, d.username, d.password
        FROM events e
        JOIN devices d ON d.id = e.device_id
        WHERE e.picture_url IS NOT NULL
          AND e.picture_url != ''
          AND e.picture_url LIKE 'http%'
        ORDER BY e.id DESC
    """).fetchall()
    conn.close()

    downloaded = 0
    for r in rows:
        emp_id = str(r["employee_id"])

        # Skip if already have a good quality face (>20KB)
        existing = get_face_bytes(emp_id)
        if existing and len(existing) > 20000:
            continue

        pic_url = r["picture_url"]
        try:
            # Use basic auth (username:password) as original photo_sync.py did
            from requests.auth import HTTPDigestAuth
            resp = requests.get(
                pic_url,
                auth=HTTPDigestAuth(r["username"], r["password"]),
                verify=False, timeout=10
            )
            if resp.status_code == 200 and len(resp.content) > 100:
                ct = resp.headers.get("Content-Type", "")
                if "image" in ct or resp.content[:2] == b'\xff\xd8':
                    ok = store_face_bytes(emp_id, resp.content)
                    if ok:
                        downloaded += 1
                        log.info(f"[COLLECTOR] Face downloaded for {emp_id} from event snapshot")
        except Exception as e:
            log.debug(f"[COLLECTOR] Face download error for {emp_id}: {e}")

    if downloaded:
        log.info(f"[COLLECTOR] Downloaded {downloaded} faces from event snapshots")


def fetch_from_device(ip: str, username: str, password: str,
                      device_id: int, start: str = None, end: str = None) -> int:
    """Fetch and store events from one device. Returns count stored."""
    now = datetime.now()
    tz  = _tz_suffix()
    if not start:
        start = (now - timedelta(minutes=LOOKBACK_MINUTES)).strftime(
            f"%Y-%m-%dT%H:%M:%S{tz}")
    if not end:
        end = now.strftime(f"%Y-%m-%dT%H:%M:%S{tz}")

    _prof = None
    try:
        _c = get_conn()
        _row = _c.execute("SELECT profile FROM devices WHERE id=?", (device_id,)).fetchone()
        _c.close()
        _prof = _row["profile"] if _row else None
    except Exception:
        _prof = None
    api    = HikvisionISAPI(ip, username, password, profile=_prof)
    log.info(f"[COLLECTOR] Fetching {ip} window: {start} → {end}")
    events = api.fetch_events(start, end)
    log.info(f"[COLLECTOR] {ip} → {len(events)} raw events from device")
    if not events:
        return 0
    count = store_events(device_id, events)
    log.info(f"[COLLECTOR] {ip} → {count} new events stored (of {len(events)} fetched)")
    return count


def fetch_all_active_devices() -> dict:
    """Fetch events from all active devices. Returns {device_id: result}."""
    conn    = get_conn()
    devices = conn.execute(
        "SELECT id, ip, username, password, name, profile FROM devices WHERE active=1"
    ).fetchall()
    conn.close()

    results = {}
    for d in devices:
        try:
            count = fetch_from_device(
                ip=d["ip"], username=d["username"],
                password=d["password"], device_id=d["id"]
            )
            results[d["id"]] = {"name": d["name"], "count": count, "ok": True}
        except Exception as e:
            log.error(f"[COLLECTOR] Device {d['id']} ({d['ip']}) error: {e}")
            results[d["id"]] = {"name": d["name"], "count": 0,
                                "ok": False, "error": str(e)}
    return results



def sync_users_from_devices() -> dict:
    """
    Pull users from device UserInfo and auto-create any missing in DB.
    Runs as part of the scheduled user sync cycle.
    Returns summary of users added per device.
    """
    conn = get_conn()
    devices = conn.execute(
        "SELECT id, ip, username, password, name, profile FROM devices WHERE active=1"
    ).fetchall()
    conn.close()

    import requests as _requests
    from requests.auth import HTTPDigestAuth as _Digest

    import uuid as _uuid
    summary = {}
    for d in devices:
        auth = _Digest(d["username"], d["password"])
        ip   = d["ip"]
        added, updated, skipped = 0, 0, 0
        pos = 0
        search_id = str(_uuid.uuid4())

        conn = get_conn()
        try:
            while True:
                try:
                    r = _requests.post(
                        f"http://{ip}/ISAPI/AccessControl/UserInfo/Search?format=json",
                        json={"UserInfoSearchCond": {"searchID": search_id,
                              "searchResultPosition": pos, "maxResults": 50}},
                        auth=auth, timeout=15, verify=False)
                    if r.status_code != 200:
                        break
                    body  = r.json()
                    root  = body.get("UserInfoSearch") or body.get("UserInfoSearchResult") or {}
                    users = root.get("UserInfo", [])
                    if isinstance(users, dict):
                        users = [users]
                    if not users:
                        break

                    for u in users:
                        emp_id = str(u.get("employeeNo", "")).strip()
                        name   = u.get("name", emp_id) or emp_id
                        if not emp_id:
                            continue
                        existing = conn.execute(
                            "SELECT employee_id, name FROM users WHERE employee_id=?", (emp_id,)
                        ).fetchone()
                        if existing:
                            # Update the name if it changed on the device
                            if name and name != (existing["name"] or ""):
                                conn.execute(
                                    "UPDATE users SET name=? WHERE employee_id=?",
                                    (name, emp_id))
                                updated += 1
                                log.info(f"[USER SYNC] Updated name for {emp_id}: {name}")
                            else:
                                skipped += 1
                        else:
                            conn.execute(
                                "INSERT INTO users (employee_id, name, is_active) VALUES (?,?,1)",
                                (emp_id, name)
                            )
                            added += 1
                            log.info(f"[USER SYNC] Auto-created user {emp_id}: {name} from {d['name']}")
                    conn.commit()

                    if root.get("responseStatusStrg") != "MORE":
                        break
                    pos += len(users)
                except Exception as e:
                    log.warning(f"[USER SYNC] Error pulling users from {d['name']}: {e}")
                    break
        finally:
            conn.close()

        summary[d["id"]] = {"name": d["name"], "added": added,
                            "updated": updated, "skipped": skipped}
        log.info(f"[USER SYNC] {d['name']}: added={added} updated={updated} skipped={skipped}")

    return summary


def sync_users_across_devices() -> dict:
    """
    For each active device:
    1. Pull current user list from device
    2. Push any DB users missing on that device
    Returns summary dict.
    """
    conn    = get_conn()
    devices = conn.execute(
        "SELECT id, ip, username, password, name, profile FROM devices WHERE active=1"
    ).fetchall()
    db_users = conn.execute(
        "SELECT employee_id, name FROM users WHERE is_active=1"
    ).fetchall()
    conn.close()

    summary = {}
    for d in devices:
        api    = HikvisionISAPI.from_row(d)
        on_dev = api.list_users()
        pushed = 0
        failed = 0
        import time as _time
        pushed_since_pause = 0
        for u in db_users:
            emp = str(u["employee_id"])
            if emp not in on_dev:
                ok, msg = api.create_or_update_user(emp, u["name"])
                if ok:
                    pushed += 1
                else:
                    failed += 1
                    log.warning(f"[SYNC] Push {emp} to {d['name']} failed: {msg[:80]}")
                # Throttle to avoid the device userCheck lockout (~10 rapid reqs)
                pushed_since_pause += 1
                _time.sleep(1.5)
                if pushed_since_pause >= 8:
                    _time.sleep(5)
                    pushed_since_pause = 0
        summary[d["id"]] = {
            "name":         d["name"],
            "device_users": len(on_dev),
            "pushed":       pushed,
            "failed":       failed,
        }
    return summary


def sync_new_users_from_events() -> int:
    """Create user records for employee IDs seen in events but not in users table."""
    conn = get_conn()
    rows = conn.execute("""
        SELECT DISTINCT e.employee_id, MAX(e.name) as name
        FROM events e
        LEFT JOIN users u ON u.employee_id = e.employee_id
        WHERE u.employee_id IS NULL
          AND e.employee_id IS NOT NULL
          AND TRIM(e.employee_id) != ''
        GROUP BY e.employee_id
    """).fetchall()
    count = 0
    for r in rows:
        conn.execute(
            "INSERT OR IGNORE INTO users (employee_id, name, is_active) VALUES (?,?,1)",
            (r["employee_id"], r["name"] or r["employee_id"])
        )
        count += 1
    conn.commit()
    conn.close()
    return count
