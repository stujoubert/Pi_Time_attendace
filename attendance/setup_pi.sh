#!/usr/bin/env bash
# =============================================================================
# Pi 5 first-boot setup — run ONCE before install.sh
# Sets up NTP, timezone prompt, and system tweaks
# Usage: sudo bash setup_pi.sh
# =============================================================================
set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'

[[ $EUID -ne 0 ]] && echo "Run as root: sudo bash setup_pi.sh" && exit 1

echo -e "${BOLD}Pi 5 first-boot setup for Attendance System${NC}"
echo ""

# ── Timezone ──────────────────────────────────────────────────────────────────
echo -e "${BLUE}Common timezones:${NC}"
echo "  1) America/Mexico_City   (CST -06:00)"
echo "  2) America/Monterrey     (CST -06:00)"
echo "  3) America/Tijuana       (PST -08:00)"
echo "  4) America/Bogota        (COT -05:00)"
echo "  5) America/Lima          (PET -05:00)"
echo "  6) Enter manually"
echo ""
read -rp "Select timezone [1-6]: " TZ_CHOICE

case "$TZ_CHOICE" in
  1) TZ="America/Mexico_City" ;;
  2) TZ="America/Monterrey" ;;
  3) TZ="America/Tijuana" ;;
  4) TZ="America/Bogota" ;;
  5) TZ="America/Lima" ;;
  6) read -rp "Enter timezone (e.g. America/Hermosillo): " TZ ;;
  *) TZ="America/Mexico_City" ;;
esac

timedatectl set-timezone "$TZ"
echo -e "${GREEN}[ OK ]${NC}  Timezone set to $TZ"

# ── NTP ───────────────────────────────────────────────────────────────────────
apt-get install -y -qq ntp
systemctl enable ntp
systemctl start ntp
echo -e "${GREEN}[ OK ]${NC}  NTP enabled"

# ── Static IP (optional) ──────────────────────────────────────────────────────
echo ""
read -rp "Set a static IP address? (recommended) [y/N]: " SET_IP
if [[ "$SET_IP" =~ ^[Yy]$ ]]; then
    CURRENT_IP=$(hostname -I | awk '{print $1}')
    CURRENT_GW=$(ip route | awk '/default/ {print $3}' | head -1)
    echo "  Current IP: $CURRENT_IP"
    echo "  Current GW: $CURRENT_GW"
    read -rp "  Static IP (e.g. 192.168.1.10): " STATIC_IP
    read -rp "  Gateway    [$CURRENT_GW]: " GW
    GW="${GW:-$CURRENT_GW}"
    read -rp "  Subnet CIDR (e.g. 24 for /24): " CIDR
    CIDR="${CIDR:-24}"

    CON=$(nmcli -t -f NAME con show --active | head -1)
    nmcli con mod "$CON" \
        ipv4.addresses "$STATIC_IP/$CIDR" \
        ipv4.gateway   "$GW" \
        ipv4.dns       "8.8.8.8,8.8.4.4" \
        ipv4.method    manual
    echo -e "${GREEN}[ OK ]${NC}  Static IP $STATIC_IP/$CIDR configured (applies after reboot)"
fi

# ── Swap (helps with pip install on 4GB) ──────────────────────────────────────
if [[ ! -f /swapfile ]]; then
    fallocate -l 1G /swapfile
    chmod 600 /swapfile
    mkswap /swapfile >/dev/null
    swapon /swapfile
    echo "/swapfile none swap sw 0 0" >> /etc/fstab
    echo -e "${GREEN}[ OK ]${NC}  1GB swap file created"
fi

# ── System update ─────────────────────────────────────────────────────────────
echo ""
echo -e "${BLUE}Updating system packages (this may take a few minutes)...${NC}"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get full-upgrade -y -qq
echo -e "${GREEN}[ OK ]${NC}  System updated"

echo ""
echo -e "${GREEN}${BOLD}Setup complete!${NC}"
echo ""
echo "  Next: transfer attendance_rpi5.zip to this Pi, then run:"
echo -e "  ${BOLD}unzip attendance_rpi5.zip && cd attendance && sudo bash install.sh${NC}"
echo ""
echo "  Reboot now to apply static IP (if set): sudo reboot"
echo ""
