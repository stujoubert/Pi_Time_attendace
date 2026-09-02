#!/usr/bin/env bash
# =============================================================================
# Attendance System — Raspberry Pi Installer
# Tested on: Raspberry Pi OS Lite 64-bit (Bookworm / Debian 12)
# Run as:    sudo bash install.sh
# =============================================================================

APP_DIR=/opt/attendance
DATA_DIR=/var/lib/attendance
BACKUP_DIR=/var/lib/attendance/backups
FACE_DIR=/var/lib/attendance/faces
UPLOADS_DIR=/var/lib/attendance/uploads
ENV_FILE=$APP_DIR/.env
SERVICE_FILE=/etc/systemd/system/attendance.service
LOG_FILE=/var/log/attendance-install.log
APP_USER=attendance

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

info()    { echo -e "${BLUE}[INFO]${NC}  $*" | tee -a "$LOG_FILE"; }
ok()      { echo -e "${GREEN}[ OK ]${NC}  $*" | tee -a "$LOG_FILE"; }
warn()    { echo -e "${YELLOW}[WARN]${NC}  $*" | tee -a "$LOG_FILE"; }
die()     { echo -e "${RED}[FAIL]${NC}  $*" | tee -a "$LOG_FILE"; exit 1; }
section() { echo -e "\n${BOLD}━━━  $*  ━━━${NC}" | tee -a "$LOG_FILE"; }

[ "$EUID" -ne 0 ] && die "Run as root: sudo bash install.sh"

mkdir -p "$(dirname "$LOG_FILE")"
echo "=== Install started $(date) ===" >> "$LOG_FILE"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
[ ! -f "$SCRIPT_DIR/server.py" ] && die "server.py not found. Run from the attendance directory."

echo -e "${BOLD}"
echo "  ┌──────────────────────────────────────────┐"
echo "  │   Attendance System — Raspberry Pi        │"
echo "  └──────────────────────────────────────────┘"
echo -e "${NC}"

# ── 1. System packages ────────────────────────────────────────────────────────
section "1/9  System packages"
apt-get update -qq 2>>"$LOG_FILE"
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip python3-dev \
    sqlite3 curl rsync ca-certificates \
    libjpeg-dev zlib1g-dev libopenjp2-7 libwebp-dev \
    nginx ufw 2>>"$LOG_FILE"
ok "System packages installed"

# ── 1b. Timezone ─────────────────────────────────────────────────────────────
section "1b/9  Timezone"
# Set to Mexico City by default — change if needed
DEFAULT_TZ="America/Mexico_City"
CURRENT_TZ=$(timedatectl show -p Timezone --value 2>/dev/null || echo "")
if [ "$CURRENT_TZ" != "$DEFAULT_TZ" ]; then
    timedatectl set-timezone "$DEFAULT_TZ"
    ok "Timezone set to $DEFAULT_TZ"
else
    ok "Timezone already set to $DEFAULT_TZ"
fi

# Enable NTP sync
timedatectl set-ntp true 2>/dev/null || true
ok "NTP sync enabled"

# ── 2. Service user ───────────────────────────────────────────────────────────
section "2/9  Service user"
if ! id "$APP_USER" >/dev/null 2>&1; then
    useradd --system --no-create-home --shell /bin/false "$APP_USER"
    ok "Created system user: $APP_USER"
else
    ok "User $APP_USER already exists"
fi

# ── 3. Directories ────────────────────────────────────────────────────────────
section "3/9  Directories"
mkdir -p "$APP_DIR" "$DATA_DIR" "$BACKUP_DIR" "$FACE_DIR" "$UPLOADS_DIR" /var/lib/attendance/checkin_photos /var/lib/attendance/models
mkdir -p "$APP_DIR/static/uploads"
chown -R "$APP_USER:$APP_USER" "$DATA_DIR"
chown "$APP_USER:$APP_USER" "$APP_DIR/static/uploads" 2>/dev/null || true
ok "Directories created"

# ── 4. Application files ──────────────────────────────────────────────────────
section "4/9  Application files"
rsync -a --checksum \
    --exclude='.env' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='venv/' \
    --exclude='*.db' \
    --exclude='*.db-wal' \
    --exclude='*.db-shm' \
    --exclude='.git/' \
    "$SCRIPT_DIR/" "$APP_DIR/"

ln -sfn "$FACE_DIR"    "$APP_DIR/static/uploads/faces"   2>/dev/null || true
ln -sfn "$UPLOADS_DIR" "$APP_DIR/static/uploads/company" 2>/dev/null || true

chown -R root:"$APP_USER" "$APP_DIR"
chmod -R 750 "$APP_DIR"
chmod 755 "$FACE_DIR"
chmod 644 "$FACE_DIR"/*.jpg 2>/dev/null || true
ok "Application files installed to $APP_DIR"

# ── 5. Python environment ─────────────────────────────────────────────────────
section "5/9  Python environment"
python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --upgrade pip --quiet 2>>"$LOG_FILE"
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" --quiet 2>>"$LOG_FILE"
chown -R root:"$APP_USER" "$APP_DIR/venv"
ok "Python packages installed"

"$APP_DIR/venv/bin/python" -c "
from flask import Flask
from apscheduler.schedulers.background import BackgroundScheduler
from PIL import Image
print('Import check passed')
" 2>>"$LOG_FILE" || die "Python import check failed — see $LOG_FILE"

# ── 6. Environment file ───────────────────────────────────────────────────────
section "6/9  Environment file"
if [ -f "$ENV_FILE" ]; then
    warn ".env already exists — not overwriting (delete it to regenerate)"
else
    TZ_SYS=$(timedatectl show -p Timezone --value 2>/dev/null || echo "America/Mexico_City")
    TZ_OFFSET=$(python3 -c "
import subprocess
r = subprocess.run(['date','+%z'], capture_output=True, text=True)
z = r.stdout.strip()
print(z[:3] + ':' + z[3:]) if len(z)==5 else print('-06:00')
" 2>/dev/null || echo "-06:00")
    SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")

    cat > "$ENV_FILE" << ENVEOF
ATT_ENV=prod
ATT_DB=$DATA_DIR/attendance.db
ATT_PORT=5000
SECRET_KEY=$SECRET_KEY
# Set to 1 once the app is accessed only via HTTPS (Cloudflare Tunnel) —
# marks the session cookie Secure. Leave 0 while using http://LAN-IP:5000.
COOKIE_SECURE=0
TZ_OFFSET=$TZ_OFFSET
SCHEDULER_TZ=$TZ_SYS
FACE_DIR=$FACE_DIR
LOOKBACK_MINUTES=1440
ENVEOF

    chmod 640 "$ENV_FILE"
    chown root:"$APP_USER" "$ENV_FILE"
    ok "Environment file created"
    ok "  Timezone: $TZ_OFFSET ($TZ_SYS)"
    warn "Verify TZ_OFFSET matches your Hikvision device timezone"
fi

# ── 7. Database init ──────────────────────────────────────────────────────────
section "7/9  Database"
"$APP_DIR/venv/bin/python3" "$APP_DIR/scripts/bootstrap_db.py" 2>>"$LOG_FILE" || \
    warn "bootstrap_db.py not found or already initialized"

# Migrations
"$APP_DIR/venv/bin/python3" - << 'PYEOF'
import sqlite3, os
db = os.environ.get('ATT_DB', '/var/lib/attendance/attendance.db')
if os.path.exists(db):
    conn = sqlite3.connect(db)
    try:
        conn.execute("ALTER TABLE departments ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
        conn.commit()
        print("  Migration: added hidden column to departments")
    except sqlite3.OperationalError:
        pass
    conn.close()
PYEOF
ok "Database initialized"

# ── 8. Backup ─────────────────────────────────────────────────────────────────
section "8/9  Backup (daily 02:00)"

cat > /usr/local/bin/attendance-backup << 'BKEOF'
#!/bin/bash
BACKUP_DIR=/var/lib/attendance/backups
DB=/var/lib/attendance/attendance.db
DATE=$(date +%Y-%m-%d_%H%M)
LOGDATE=$(date '+%Y-%m-%d %H:%M')
[ ! -f "$DB" ] && echo "[$LOGDATE] No DB found, skipping" && exit 0
umask 077
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
sqlite3 "$DB" ".backup $BACKUP_DIR/attendance-$DATE.db"
gzip -f "$BACKUP_DIR/attendance-$DATE.db"
chmod 600 "$BACKUP_DIR/attendance-$DATE.db.gz"
find "$BACKUP_DIR" -name "*.db.gz" -mtime +30 -delete
# Optional offsite copy: put an rsync destination (e.g. pi@192.168.0.70:/backups/asistencia/)
# in /etc/attendance-backup-remote and the newest backup is pushed there nightly.
if [ -s /etc/attendance-backup-remote ]; then
    REMOTE=$(cat /etc/attendance-backup-remote)
    rsync -az --timeout=60 "$BACKUP_DIR/attendance-$DATE.db.gz" "$REMOTE" \
        || echo "WARNING: offsite backup copy failed" >&2
fi
SIZE=$(du -sh "$BACKUP_DIR/attendance-$DATE.db.gz" 2>/dev/null | cut -f1)
echo "[$LOGDATE] Backup: attendance-$DATE.db.gz ($SIZE)"
BKEOF

chmod +x /usr/local/bin/attendance-backup
(crontab -l 2>/dev/null | grep -v attendance-backup; echo "0 2 * * * /usr/local/bin/attendance-backup >> /var/log/attendance-backup.log 2>&1") | crontab - 2>/dev/null || true
ok "Daily backup scheduled at 02:00"

# ── 9. Firewall & Nginx ───────────────────────────────────────────────────────
section "9/9  Firewall & Nginx"

# Nginx for face image serving
# Faces are personal data: only the Hikvision terminals should read them.
# The ACL snippet starts as private-LAN-only; tighten it to the exact
# terminal IPs with:  sudo attendance-faces-acl
mkdir -p /etc/nginx/snippets
if [ ! -f /etc/nginx/snippets/attendance_faces_acl.conf ]; then
cat > /etc/nginx/snippets/attendance_faces_acl.conf << 'ACLEOF'
# Access control for /faces/ (regenerate with: attendance-faces-acl)
allow 10.0.0.0/8;
allow 172.16.0.0/12;
allow 192.168.0.0/16;
deny all;
ACLEOF
fi

cat > /usr/local/bin/attendance-faces-acl << 'ACLGENEOF'
#!/bin/bash
# Regenerates the nginx /faces/ allow-list from the ACTIVE devices in the DB,
# so only the face terminals can download employee photos.
DB=/var/lib/attendance/attendance.db
OUT=/etc/nginx/snippets/attendance_faces_acl.conf
IPS=$(sqlite3 "$DB" "SELECT ip FROM devices WHERE active=1;" 2>/dev/null)
if [ -z "$IPS" ]; then
    echo "No active devices in DB — leaving ACL unchanged."
    exit 1
fi
{
  echo "# Generated by attendance-faces-acl on $(date -Iseconds)"
  for ip in $IPS; do echo "allow $ip;"; done
  echo "deny all;"
} > "$OUT"
nginx -t && systemctl reload nginx
echo "ACL updated:"; cat "$OUT"
ACLGENEOF
chmod 755 /usr/local/bin/attendance-faces-acl

cat > /etc/nginx/sites-available/attendance << 'NGINXEOF'
server {
    listen 80 default_server;
    server_name _;
    location /faces/ {
        alias /var/lib/attendance/faces/;
        try_files $uri =404;
        include snippets/attendance_faces_acl.conf;
    }
}
NGINXEOF

ln -sf /etc/nginx/sites-available/attendance /etc/nginx/sites-enabled/attendance
rm -f /etc/nginx/sites-enabled/default
nginx -t 2>>"$LOG_FILE" && systemctl enable nginx 2>>"$LOG_FILE" && systemctl start nginx 2>>"$LOG_FILE" || warn "Nginx setup failed — face thumbnails on devices may not work"
ok "Nginx configured — face images served on port 80"

# Firewall
ufw --force reset    >/dev/null 2>&1 || true
ufw default deny incoming  >/dev/null 2>&1 || true
ufw default allow outgoing >/dev/null 2>&1 || true
ufw allow ssh        >/dev/null 2>&1 || true
ufw allow 5000/tcp comment "Attendance System UI" >/dev/null 2>&1 || true
ufw allow 80/tcp comment "Nginx face server" >/dev/null 2>&1 || true
ufw --force enable   >/dev/null 2>&1 || true
ok "Firewall enabled — SSH, port 80 and 5000 open"

# ── Systemd service ───────────────────────────────────────────────────────────
section "Starting service"

cat > "$SERVICE_FILE" << SVCEOF
[Unit]
Description=Attendance System (Hikvision)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$APP_USER
Group=$APP_USER
WorkingDirectory=$APP_DIR
EnvironmentFile=$ENV_FILE
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/server.py
Restart=on-failure
RestartSec=10
NoNewPrivileges=yes
PrivateTmp=yes
ProtectSystem=strict
ProtectHome=yes
ReadWritePaths=$DATA_DIR $APP_DIR/static
StandardOutput=journal
StandardError=journal
SyslogIdentifier=attendance

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable attendance
systemctl start attendance
sleep 5

if systemctl is-active --quiet attendance; then
    ok "Service is running"
else
    warn "Service may not have started. Check logs:"
    journalctl -u attendance --no-pager -n 20
fi

LOCAL_IP=$(hostname -I | awk '{print $1}')

echo ""
echo -e "${GREEN}${BOLD}"
echo "  ╔══════════════════════════════════════════╗"
echo "  ║   Installation complete!                 ║"
echo "  ╚══════════════════════════════════════════╝"
echo -e "${NC}"
echo -e "  Web UI:  ${BOLD}http://$LOCAL_IP:5000${NC}"
echo ""
echo -e "  Login:   ${BOLD}admin${NC} / ${BOLD}admin${NC}  ← change this now!"
echo ""
echo -e "  Next steps:"
echo -e "  1. Change the admin password (Accounts menu)"
echo -e "  2. Add your Hikvision devices (Devices menu)"
echo -e "  3. Run backfill: ${BLUE}sudo /opt/attendance/venv/bin/python3 /opt/attendance/scripts/backfill.py${NC}"
echo ""
echo "  Install log: $LOG_FILE"
echo ""
