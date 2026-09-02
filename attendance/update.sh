#!/usr/bin/env bash
# =============================================================================
# Attendance System — Update script
# Upload a new attendance_rpi5.zip, then run: sudo bash update.sh
# =============================================================================
set -euo pipefail

APP_DIR=/opt/attendance
DATA_DIR=/var/lib/attendance
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; NC='\033[0m'

[[ $EUID -ne 0 ]] && echo "Run as root: sudo bash update.sh" && exit 1

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
[[ ! -f "$SCRIPT_DIR/server.py" ]] && \
    echo "server.py not found. Unzip first: unzip attendance_rpi5.zip && cd attendance" && exit 1

echo -e "${BOLD}Attendance System — Update${NC}"

# Backup before updating
echo -e "${YELLOW}Creating backup before update...${NC}"
/usr/local/bin/attendance-backup 2>/dev/null || true

# Stop service
systemctl stop attendance
echo -e "${GREEN}[ OK ]${NC}  Service stopped"

# Copy new files (preserve .env and data)
rsync -a --checksum \
    --exclude='.env' \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    --exclude='venv/' \
    --exclude='*.db' \
    --exclude='*.db-wal' \
    --exclude='*.db-shm' \
    --exclude='test_runner.py' \
    --exclude='.git/' \
    "$SCRIPT_DIR/" "$APP_DIR/"

chown -R root:attendance "$APP_DIR"
chmod -R 750 "$APP_DIR"
# Ensure uploads dir is writable by the service
mkdir -p "$APP_DIR/static/uploads"
chmod 777 "$APP_DIR/static/uploads"
echo -e "${GREEN}[ OK ]${NC}  Files updated"

# Update Python packages
"$APP_DIR/venv/bin/pip" install -r "$APP_DIR/requirements.txt" \
    --quiet --upgrade
echo -e "${GREEN}[ OK ]${NC}  Python packages updated"

# Restart
systemctl start attendance
sleep 4

if systemctl is-active --quiet attendance; then
    echo -e "${GREEN}[ OK ]${NC}  Service restarted and running"
else
    echo -e "${YELLOW}[WARN]${NC}  Service may not have started:"
    journalctl -u attendance --no-pager -n 20
fi

echo ""
echo -e "${GREEN}${BOLD}Update complete!${NC}"
echo -e "  ${BOLD}http://$(hostname -I | awk '{print $1}'):5000${NC}"
