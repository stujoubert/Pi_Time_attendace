# Attendance System — Raspberry Pi 5 Setup Guide

## What you need

| Item | Recommendation | Notes |
|------|---------------|-------|
| Raspberry Pi 5 | 4GB or 8GB | 4GB is enough for most deployments |
| Storage | NVMe SSD (M.2 2230 or 2242) + Pi 5 M.2 HAT+ | **Do not use microSD** — it will fail under constant DB writes |
| Power supply | Official Pi 5 27W USB-C | Underpowered supplies cause random reboots |
| Cooling | Official Pi 5 case with fan, or Active Cooler | App runs continuously — cooling is important |
| Network | Ethernet cable | More reliable than Wi-Fi for a 24/7 server |
| OS | Raspberry Pi OS Lite **64-bit** (Bookworm) | No desktop needed |

---

## Part 1 — First-time Pi setup (do this on your laptop)

### 1.1  Flash the OS

1. Download **Raspberry Pi Imager** from raspberrypi.com/software
2. Open Imager → Choose OS → **Raspberry Pi OS Lite (64-bit)**
3. Choose your NVMe SSD as the storage target  
   *(if using SD card temporarily, choose that)*
4. Click the **gear icon** before writing and set:

```
Hostname:       attendance
Username:       pi
Password:       (choose something strong)
Enable SSH:     ✓  (Use password authentication)
Wi-Fi:          (leave blank — use ethernet)
Locale:         your timezone and keyboard
```

5. Write the image, insert into Pi, connect ethernet, power on.

### 1.2  Find the Pi on your network

```bash
# From your laptop:
ping attendance.local

# Or check your router's connected devices list for the IP
```

### 1.3  SSH in and update

```bash
ssh pi@attendance.local

# Once in:
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

---

## Part 2 — Boot from NVMe SSD (skip if already on SSD)

If you flashed to SD card and want to move to NVMe:

```bash
sudo rpi-eeprom-update -a
sudo raspi-config
# → Advanced Options → Bootloader Version → Latest
# → Advanced Options → Boot Order → NVMe/USB Boot
# → Finish → Reboot

# After reboot, use Raspberry Pi Imager to write OS to the SSD
# OR use SD Card Copier (in the Pi desktop) to clone SD → SSD
```

Remove the SD card. Pi should now boot from NVMe.

---

## Part 3 — Set timezone

The timezone **must match your Hikvision devices**.

```bash
# List available timezones
timedatectl list-timezones | grep Mexico

# Set your timezone (example: Mexico City)
sudo timedatectl set-timezone America/Mexico_City

# Verify — should show NTP synchronized: yes
timedatectl
```

---

## Part 4 — Transfer the application files

### Option A — Copy from USB drive

```bash
# On your laptop, copy attendance_system_v2.zip to a USB drive
# Then on the Pi:
sudo mount /dev/sda1 /mnt
cp /mnt/attendance_system_v2.zip ~/
sudo umount /mnt

cd ~
unzip attendance_system_v2.zip
cd attendance
```

### Option B — Copy via SCP (from your laptop)

```bash
# Run this on your laptop, not the Pi:
scp attendance_system_v2.zip pi@attendance.local:~/

# Then on the Pi:
cd ~
unzip attendance_system_v2.zip
cd attendance
```

### Option C — Clone from Git

```bash
# On the Pi (if you pushed to a private repo):
cd ~
git clone https://github.com/YOUR_USER/YOUR_REPO.git attendance
cd attendance
```

---

## Part 5 — Run the installer

```bash
# Make sure you're in the attendance folder
cd ~/attendance

# Run as root
sudo bash install.sh
```

The installer will:
- Install system dependencies (`python3`, `libjpeg`, etc.)
- Create a dedicated `attendance` service user
- Copy app to `/opt/attendance`
- Create a Python virtualenv and install all packages
- Generate a random `SECRET_KEY` automatically
- Detect your timezone and pre-fill the `.env` file
- Install and start the systemd service
- Set up a daily 02:00 database backup
- Configure UFW firewall (SSH + port 5000 open)

When it finishes you should see:

```
  Installation complete!

  Web UI:   http://192.168.1.xx:5000
  Login:    admin / admin  ← change immediately
```

---

## Part 6 — First login checklist

Open `http://<pi-ip>:5000` in your browser.

**Do these in order:**

1. **Change admin password**  
   → Accounts → click "Reset password" next to admin

2. **Verify timezone**  
   ```bash
   sudo nano /opt/attendance/.env
   # Check TZ_OFFSET matches your Hikvision devices
   # e.g. -06:00 for Mexico City (CST)
   #      -05:00 for Mexico City during DST
   sudo systemctl restart attendance
   ```

3. **Add Hikvision devices**  
   → Devices → Add Device  
   Fill in: Name, IP address, username (usually `admin`), password

4. **Test fetch**  
   → Devices → click the cloud-download icon on a device  
   Check that events appear in the Daily view

5. **Set company name and SMTP** (optional)  
   → Company Settings

6. **Import faces from devices**  
   → Faces → Pull All  
   Wait for it to finish, then check Face overview shows enrolled users

7. **Add departments** (optional)  
   → Departments → Add

8. **Assign employees to departments and schedules** (optional)  
   → Users → edit each user for department  
   → Schedules → create template → Assign to Users

---

## Part 7 — Verify automatic jobs

The system runs all jobs automatically via APScheduler — no cron needed for the app itself.

```bash
# Check what's scheduled and when it runs next:
curl -s http://localhost:5000/api/scheduler/status | python3 -m json.tool
```

Expected output:
```json
{
  "running": true,
  "jobs": [
    { "name": "Hourly device event fetch",  "next_run": "2025-01-01 14:00:00" },
    { "name": "Sync new users from events", "next_run": "2025-01-01 14:05:00" },
    { "name": "Push users to all devices",  "next_run": "2025-01-01 20:00:00" },
    { "name": "Daily pull+push face sync",  "next_run": "2025-01-02 03:00:00" },
    { "name": "Weekly email report",        "next_run": "2025-01-06 06:00:00" }
  ]
}
```

```bash
# Trigger an immediate fetch right now (for testing):
curl -s -X POST http://localhost:5000/api/fetch-now
```

---

## Part 8 — Set a static IP (recommended)

Your Hikvision devices are configured to talk to a fixed server IP. If the Pi's IP changes, they stop syncing.

```bash
# Find your current connection name:
nmcli con show

# Set static IP (adjust to your network):
sudo nmcli con mod "Wired connection 1" \
  ipv4.addresses 192.168.1.10/24 \
  ipv4.gateway 192.168.1.1 \
  ipv4.dns "8.8.8.8,1.1.1.1" \
  ipv4.method manual

sudo nmcli con up "Wired connection 1"
ip addr show eth0    # verify
```

---

## Part 9 — Optional: remote access via Tailscale

Access the system securely from anywhere without opening ports on your router:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up

# Get your Tailscale IP:
tailscale ip -4
# Access from anywhere: http://<tailscale-ip>:5000
```

---

## Useful commands

```bash
# Follow live logs
sudo journalctl -u attendance -f

# Restart after config changes
sudo systemctl restart attendance

# Stop / start
sudo systemctl stop attendance
sudo systemctl start attendance

# Check service status
sudo systemctl status attendance

# View .env
sudo cat /opt/attendance/.env

# Edit .env
sudo nano /opt/attendance/.env && sudo systemctl restart attendance

# Backfill a missed day manually
cd /opt/attendance
sudo -u attendance ./venv/bin/python backfill_day.py 2025-01-15

# Check DB size and event count
du -sh /var/lib/attendance/attendance.db
sqlite3 /var/lib/attendance/attendance.db "SELECT COUNT(*) FROM events"

# List scheduled backups
ls -lh /var/lib/attendance/backups/

# Run backup manually
sudo /usr/local/bin/attendance-backup

# Disk usage
df -h /
```

---

## Updating the application

```bash
# Upload new attendance_system_v2.zip to the Pi, then:
cd ~
unzip -o attendance_system_v2.zip
cd attendance
sudo rsync -a --exclude='.env' --exclude='*.db' --exclude='__pycache__' \
    ./ /opt/attendance/
sudo /opt/attendance/venv/bin/pip install -r /opt/attendance/requirements.txt --quiet
sudo systemctl restart attendance
sudo journalctl -u attendance -f    # check it started OK
```

---

## Troubleshooting

### Service won't start

```bash
sudo journalctl -u attendance --no-pager -n 50
# Look for: ATT_DB not set, SECRET_KEY missing, import errors
```

### Can't reach the web UI

```bash
sudo systemctl is-active attendance    # should say "active"
sudo ufw status                        # port 5000 should be allowed
curl http://localhost:5000/health      # test locally on the Pi
```

### Wrong timestamps / attendance showing wrong day

```bash
# Check Pi time matches your devices:
date
# Adjust TZ_OFFSET in .env:
sudo nano /opt/attendance/.env
# Example for CST (UTC-6): TZ_OFFSET=-06:00
sudo systemctl restart attendance
```

### Face sync not working

```bash
# Check if devices are reachable:
curl -u admin:password http://192.168.1.100/ISAPI/System/deviceInfo

# Manual face pull from all devices:
curl -s -X POST http://localhost:5000/faces/pull-all
# OR use Faces → Pull All in the UI
```

### Database corruption (after power loss)

```bash
sqlite3 /var/lib/attendance/attendance.db "PRAGMA integrity_check"
# If not "ok": restore from backup
sudo cp /var/lib/attendance/backups/attendance-YYYY-MM-DD.db.gz /tmp/
gunzip /tmp/attendance-YYYY-MM-DD.db.gz
sudo systemctl stop attendance
sudo cp /tmp/attendance-YYYY-MM-DD.db /var/lib/attendance/attendance.db
sudo systemctl start attendance
```
