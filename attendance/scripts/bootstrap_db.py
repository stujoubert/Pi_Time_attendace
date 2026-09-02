"""
scripts/bootstrap_db.py
Idempotent schema bootstrap — safe to run on every startup.
Creates missing tables, adds missing columns.
"""
import sqlite3
import os

def main():
    db_path = os.environ.get("ATT_DB")
    if not db_path:
        return
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = OFF")
    cur = conn.cursor()

    _exec(cur, """
        CREATE TABLE IF NOT EXISTS accounts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            username      TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL CHECK(role IN ('admin','manager','viewer','employee','foreman')),
            employee_id   TEXT,
            active        INTEGER NOT NULL DEFAULT 1
        )
    """)

    _exec(cur, """
        CREATE TABLE IF NOT EXISTS departments (
            id   INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE
        )
    """)

    _exec(cur, """
        CREATE TABLE IF NOT EXISTS users (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id          TEXT UNIQUE,
            name                 TEXT NOT NULL,
            email                TEXT,
            department_id        INTEGER REFERENCES departments(id),
            is_active            INTEGER NOT NULL DEFAULT 1,
            schedule_template_id INTEGER,
            picture_url          TEXT,
            photo_path           TEXT,
            requires_production  INTEGER NOT NULL DEFAULT 0,
            created_at           TEXT DEFAULT (datetime('now'))
        )
    """)

    _exec(cur, """
        CREATE TABLE IF NOT EXISTS devices (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            name             TEXT,
            ip               TEXT NOT NULL,
            username         TEXT,
            password         TEXT,
            active           INTEGER DEFAULT 1,
            supports_fdlib   INTEGER DEFAULT 0,
            last_fetch_at    TEXT,
            last_fetch_count INTEGER,
            last_seen_at     TEXT,
            profile          TEXT,
            stage            TEXT
        )
    """)

    _exec(cur, """
        CREATE TABLE IF NOT EXISTS events (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id   INTEGER,
            employee_id TEXT,
            name        TEXT,
            timestamp   TEXT NOT NULL,
            direction   TEXT,
            picture_url TEXT,
            promoted    INTEGER DEFAULT 0
        )
    """)

    _exec(cur, """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_events_unique
        ON events (device_id, employee_id, timestamp, direction)
    """)

    _exec(cur, "CREATE INDEX IF NOT EXISTS idx_events_ts ON events(timestamp)")
    _exec(cur, "CREATE INDEX IF NOT EXISTS idx_events_emp ON events(employee_id)")

    _exec(cur, """
        CREATE TABLE IF NOT EXISTS user_faces (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id     TEXT NOT NULL,
            picture_url     TEXT NOT NULL,
            created_at      TEXT DEFAULT (datetime('now')),
            UNIQUE(employee_id, picture_url)
        )
    """)

    _exec(cur, """
        CREATE TABLE IF NOT EXISTS settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        )
    """)

    _exec(cur, """
        CREATE TABLE IF NOT EXISTS schedule_templates (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            name        TEXT NOT NULL UNIQUE,
            description TEXT,
            created_at  TEXT DEFAULT (datetime('now'))
        )
    """)

    _exec(cur, """
        CREATE TABLE IF NOT EXISTS schedule_rules (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            template_id INTEGER NOT NULL REFERENCES schedule_templates(id),
            weekdays    TEXT NOT NULL,
            priority    INTEGER DEFAULT 0
        )
    """)

    _exec(cur, """
        CREATE TABLE IF NOT EXISTS schedule_shifts (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            rule_id       INTEGER NOT NULL REFERENCES schedule_rules(id),
            start_time    TEXT NOT NULL,
            end_time      TEXT NOT NULL,
            grace_minutes INTEGER DEFAULT 0,
            break_minutes INTEGER DEFAULT 0
        )
    """)

    _exec(cur, """
        CREATE TABLE IF NOT EXISTS user_schedule_assignments (
            employee_id TEXT PRIMARY KEY,
            template_id INTEGER NOT NULL REFERENCES schedule_templates(id),
            assigned_at TEXT DEFAULT (datetime('now'))
        )
    """)

    _exec(cur, """
        CREATE TABLE IF NOT EXISTS device_users (
            device_id   INTEGER NOT NULL,
            employee_id TEXT    NOT NULL,
            name        TEXT,
            PRIMARY KEY (device_id, employee_id)
        )
    """)

    # ── Holiday / vacation module ─────────────────────────────────────────────
    _exec(cur, """
        CREATE TABLE IF NOT EXISTS holiday_requests (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id  TEXT NOT NULL,
            start_date   TEXT NOT NULL,
            end_date     TEXT NOT NULL,
            days_count   INTEGER NOT NULL,
            note         TEXT,
            status       TEXT NOT NULL DEFAULT 'pending'
                         CHECK(status IN ('pending','approved','rejected','cancelled')),
            requested_at TEXT DEFAULT (datetime('now')),
            decided_at   TEXT,
            decided_by   TEXT,
            decision_note TEXT
        )
    """)
    _exec(cur, "CREATE INDEX IF NOT EXISTS idx_holreq_emp ON holiday_requests(employee_id)")
    _exec(cur, "CREATE INDEX IF NOT EXISTS idx_holreq_status ON holiday_requests(status)")

    # ── Phone check-in / geofencing ──────────────────────────────────────────
    _exec(cur, """
        CREATE TABLE IF NOT EXISTS geofence_sites (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            name      TEXT NOT NULL,
            latitude  REAL NOT NULL,
            longitude REAL NOT NULL,
            radius_m  INTEGER NOT NULL DEFAULT 150,
            active    INTEGER NOT NULL DEFAULT 1
        )
    """)
    _exec(cur, """
        CREATE TABLE IF NOT EXISTS phone_checkins (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id  TEXT NOT NULL,
            timestamp    TEXT NOT NULL,
            direction    TEXT NOT NULL,
            latitude     REAL,
            longitude    REAL,
            accuracy_m   REAL,
            site_id      INTEGER,
            distance_m   REAL,
            within_fence INTEGER NOT NULL DEFAULT 0
        )
    """)
    _exec(cur, "CREATE INDEX IF NOT EXISTS idx_phone_ci_emp ON phone_checkins(employee_id, timestamp)")

    _exec(cur, """
        CREATE TABLE IF NOT EXISTS gps_sites (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            name     TEXT NOT NULL,
            lat      REAL NOT NULL,
            lng      REAL NOT NULL,
            radius_m INTEGER NOT NULL DEFAULT 150,
            active   INTEGER NOT NULL DEFAULT 1
        )
    """)
    _exec(cur, """
        CREATE TABLE IF NOT EXISTS gps_checkins (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            employee_id  TEXT NOT NULL,
            timestamp    TEXT NOT NULL,
            direction    TEXT NOT NULL,
            lat          REAL, lng REAL,
            accuracy_m   REAL,
            site_id      INTEGER,
            distance_m   REAL,
            within_fence INTEGER NOT NULL DEFAULT 0,
            accepted     INTEGER NOT NULL DEFAULT 0,
            event_id     INTEGER,
            photo_path   TEXT,
            has_face     INTEGER,
            recorded_by  TEXT,
            match_score  REAL
        )
    """)
    _exec(cur, "CREATE INDEX IF NOT EXISTS idx_gpschk_emp ON gps_checkins(employee_id)")
    _exec(cur, """
        CREATE TABLE IF NOT EXISTS holiday_allowance (
            employee_id TEXT NOT NULL,
            year        INTEGER NOT NULL,
            days        INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (employee_id, year)
        )
    """)

    # Seed default admin if no accounts exist
    row = cur.execute("SELECT COUNT(*) FROM accounts").fetchone()
    if row[0] == 0:
        import secrets
        from werkzeug.security import generate_password_hash
        default_pw = os.environ.get("DEFAULT_ADMIN_PASSWORD", "admin")
        cur.execute(
            "INSERT INTO accounts (username, password_hash, role, active) VALUES (?,?,?,1)",
            ("admin", generate_password_hash(default_pw), "admin"))
        print(f"[BOOTSTRAP] Created default admin account (password: {default_pw})")

    # Seed default settings
    defaults = [
        ("week_type",       "mon_sat"),
        ("work_start_time", "08:00"),
        ("work_end_time",   "17:00"),
        ("tz_offset",       "-06:00"),
    ]
    for key, val in defaults:
        cur.execute("INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)", (key, val))

    conn.execute("PRAGMA foreign_keys = ON")
    conn.commit()
    conn.close()

def _exec(cur, sql):
    try:
        cur.execute(sql)
    except Exception as e:
        if "already exists" not in str(e).lower():
            print(f"[BOOTSTRAP] Warning: {e}")

if __name__ == "__main__":
    main()

def _add_sync_log_table():
    """Add device_sync_log table for tracking sync history."""
    db_path = os.environ.get("ATT_DB")
    if not db_path:
        return
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    _exec(cur, """
        CREATE TABLE IF NOT EXISTS device_sync_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            device_id  INTEGER,
            sync_type  TEXT NOT NULL,  -- 'user_push', 'face_pull', 'face_push', 'event_fetch'
            emp_count  INTEGER DEFAULT 0,
            ok         INTEGER DEFAULT 1,
            error_msg  TEXT,
            synced_at  TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    conn.close()
