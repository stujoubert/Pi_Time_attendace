import os
import sqlite3
from dotenv import load_dotenv
load_dotenv()

def get_db_path():
    db_path = os.environ.get("ATT_DB")
    if not db_path:
        raise RuntimeError(
            "ATT_DB environment variable is not set. "
            "Refusing to start without explicit database path."
        )
    return db_path

def _ensure_columns():
    """Lightweight migration: add columns that newer code expects but older
    installs may lack. Safe to run on every startup."""
    try:
        path = get_db_path()
    except Exception:
        return
    try:
        conn = sqlite3.connect(path, timeout=30)
        cur = conn.cursor()
        cols = {r[1] for r in cur.execute("PRAGMA table_info(devices)").fetchall()}
        if "profile" not in cols:
            cur.execute("ALTER TABLE devices ADD COLUMN profile TEXT")

        # Accounts: add employee_id link, and widen the role CHECK to allow
        # 'employee'. SQLite can't alter a CHECK in place, so if the constraint
        # is the old one we rebuild the table.
        acct_cols = {r[1] for r in cur.execute("PRAGMA table_info(accounts)").fetchall()}
        if "employee_id" not in acct_cols:
            cur.execute("ALTER TABLE accounts ADD COLUMN employee_id TEXT")
        # Detect old CHECK by reading the table's SQL
        tbl_sql = cur.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='accounts'"
        ).fetchone()
        if tbl_sql and ("'employee'" not in tbl_sql[0] or "'foreman'" not in tbl_sql[0]):
            # Rebuild accounts with the wider CHECK, preserving data
            cur.executescript("""
                PRAGMA foreign_keys=OFF;
                CREATE TABLE accounts_new (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    username      TEXT UNIQUE NOT NULL,
                    password_hash TEXT NOT NULL,
                    role          TEXT NOT NULL CHECK(role IN ('admin','manager','viewer','employee','foreman')),
                    employee_id   TEXT,
                    active        INTEGER NOT NULL DEFAULT 1
                );
                INSERT INTO accounts_new (id, username, password_hash, role, employee_id, active)
                    SELECT id, username, password_hash, role,
                           (SELECT employee_id FROM accounts a2 WHERE a2.id = accounts.id),
                           active
                    FROM accounts;
                DROP TABLE accounts;
                ALTER TABLE accounts_new RENAME TO accounts;
                PRAGMA foreign_keys=ON;
            """)

        # Holiday module tables (no-op if they already exist)
        cur.execute("""
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
        cur.execute("CREATE INDEX IF NOT EXISTS idx_holreq_emp ON holiday_requests(employee_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_holreq_status ON holiday_requests(status)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS gps_sites (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                name     TEXT NOT NULL,
                lat      REAL NOT NULL,
                lng      REAL NOT NULL,
                radius_m INTEGER NOT NULL DEFAULT 150,
                active   INTEGER NOT NULL DEFAULT 1
            )
        """)
        cur.execute("""
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
                event_id     INTEGER
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_gpschk_emp ON gps_checkins(employee_id)")
        gps_cols = {r[1] for r in cur.execute("PRAGMA table_info(gps_checkins)").fetchall()}
        if "photo_path" not in gps_cols:
            cur.execute("ALTER TABLE gps_checkins ADD COLUMN photo_path TEXT")
        if "has_face" not in gps_cols:
            cur.execute("ALTER TABLE gps_checkins ADD COLUMN has_face INTEGER")
        if "recorded_by" not in gps_cols:
            cur.execute("ALTER TABLE gps_checkins ADD COLUMN recorded_by TEXT")
        if "match_score" not in gps_cols:
            cur.execute("ALTER TABLE gps_checkins ADD COLUMN match_score REAL")
        dev_cols = {r[1] for r in cur.execute("PRAGMA table_info(devices)").fetchall()}
        if "stage" not in dev_cols:
            cur.execute("ALTER TABLE devices ADD COLUMN stage TEXT")
        usr_cols = {r[1] for r in cur.execute("PRAGMA table_info(users)").fetchall()}
        if "requires_production" not in usr_cols:
            cur.execute("ALTER TABLE users ADD COLUMN requires_production INTEGER NOT NULL DEFAULT 0")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS holiday_allowance (
                employee_id TEXT NOT NULL,
                year        INTEGER NOT NULL,
                days        INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (employee_id, year)
            )
        """)

        # Phone check-in / geofencing tables
        cur.execute("""
            CREATE TABLE IF NOT EXISTS geofence_sites (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                name      TEXT NOT NULL,
                latitude  REAL NOT NULL,
                longitude REAL NOT NULL,
                radius_m  INTEGER NOT NULL DEFAULT 150,
                active    INTEGER NOT NULL DEFAULT 1
            )
        """)
        cur.execute("""
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
        cur.execute("CREATE INDEX IF NOT EXISTS idx_phone_ci_emp ON phone_checkins(employee_id, timestamp)")
        # Pseudo-device so phone events show a source name in reports.
        # active=0 keeps it out of the collector, health checks and user sync.
        row = cur.execute("SELECT id FROM devices WHERE ip='phone'").fetchone()
        if not row:
            cur.execute("INSERT INTO devices (name, ip, active) VALUES ('Teléfono', 'phone', 0)")

        conn.commit()
        conn.close()
    except Exception:
        # Never let a migration failure block startup
        pass


def get_conn():
    conn = sqlite3.connect(get_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA mmap_size = 67108864")
    return conn

def get_setting(key, default=None):
    try:
        conn = get_conn()
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        conn.close()
        return row["value"] if row else default
    except Exception:
        return default

def set_setting(key, value):
    conn = get_conn()
    conn.execute(
        "INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
        (key, value)
    )
    conn.commit()
    conn.close()

def list_devices(active_only=True):
    try:
        conn = get_conn()
        sql = "SELECT * FROM devices"
        if active_only:
            sql += " WHERE active = 1"
        sql += " ORDER BY name"
        rows = conn.execute(sql).fetchall()
        conn.close()
        return rows
    except Exception:
        return []
