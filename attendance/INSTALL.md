# Installing on Raspberry Pi 5

## What you need before starting

- Raspberry Pi 5 (4GB or 8GB)
- Raspberry Pi OS Lite 64-bit (Bookworm) — headless, no desktop
- NVMe SSD (M.2 2230 or 2242) + M.2 HAT+ **strongly recommended**
- Official 27W USB-C power supply
- Ethernet cable connected to the same network as your Hikvision devices

---

## Step 1 — Flash the OS

Download **Raspberry Pi Imager** on your laptop:  
https://www.raspberrypi.com/software/

1. Choose **Raspberry Pi OS Lite (64-bit)**
2. Click the **gear/settings icon** before writing and configure:
   - Hostname: `attendance`
   - Username: `pi`  Password: (choose something strong)
   - Enable SSH: ✓
   - Leave Wi-Fi blank — use Ethernet
   - Set your locale/timezone
3. Write to your SD card or NVMe SSD

---

## Step 2 — Boot and update

```bash
# SSH in from your laptop
ssh pi@attendance.local

# Update everything
sudo apt update && sudo apt full-upgrade -y
sudo reboot
```

---

## Step 3 — Set timezone

This **must** match the timezone of your Hikvision devices.

```bash
sudo timedatectl set-timezone America/Mexico_City
timedatectl   # verify — should show NTP sync: yes
```

Common Mexico options:
- `America/Mexico_City` — Central (most of Mexico)
- `America/Monterrey`  — Northeast
- `America/Tijuana`    — Baja California

---

## Step 4 — (Recommended) Boot from NVMe SSD

SD cards fail under continuous database writes. Do this before installing the app.

```bash
# Update bootloader
sudo rpi-eeprom-update -a
sudo raspi-config
# → Advanced Options → Bootloader Version → Latest
# → Advanced Options → Boot Order → NVMe/USB Boot
sudo reboot

# After reboot: copy SD → NVMe, then boot from NVMe
# (Or flash Pi OS directly to the NVMe via Pi Imager on your laptop)
```

Verify you are running from NVMe:
```bash
lsblk    # root (/) should be on nvme0n1, not mmcblk0
```

---

## Step 5 — Transfer the files

On your laptop, transfer the zip to the Pi:

```bash
scp attendance_rpi5.zip pi@attendance.local:~
```

Then on the Pi:

```bash
cd ~
unzip attendance_rpi5.zip
cd attendance
```

---

## Step 6 — Run the installer

```bash
sudo bash install.sh
```

The installer will:
- Install all system packages (Python 3, SQLite, image libs)
- Create a dedicated `attendance` system user
- Set up `/opt/attendance` and `/var/lib/attendance`
- Create a Python virtualenv and install all pip packages
- Generate a random `SECRET_KEY` and write `/opt/attendance/.env`
- Install and enable the `systemd` service (auto-starts on boot)
- Set up a daily 02:00 database backup
- Enable `ufw` firewall (SSH + port 5000)

---

## Step 7 — Verify it's running

```bash
sudo systemctl status attendance

# Live logs
sudo journalctl -u attendance -f
```

You should see:
```
[INFO] Scheduler started — hourly fetch | 6h user sync | daily face sync | weekly email
[INFO] * Running on http://0.0.0.0:5000
```

Open a browser on your laptop:  
**http://attendance.local:5000** or **http://\<pi-ip\>:5000**

Default login: `admin` / `admin` — **change immediately**

---

## Step 8 — Finish configuration

### a) Change the admin password
Accounts → click Reset Password next to admin

### b) Verify the timezone offset
```bash
sudo nano /opt/attendance/.env
```
Check that `TZ_OFFSET` (e.g. `-06:00`) matches the timezone your
Hikvision devices send in their timestamps. If they send UTC, set `+00:00`.

### c) Set a static IP (optional but recommended)
```bash
# Replace values with your network
sudo nmcli con mod "Wired connection 1" \
  ipv4.addresses 192.168.1.10/24 \
  ipv4.gateway   192.168.1.1 \
  ipv4.dns       "8.8.8.8,8.8.4.4" \
  ipv4.method    manual
sudo nmcli con up "Wired connection 1"
```

### d) Add your Hikvision devices
- Devices menu → Add Device
- Enter the device IP, admin username, and password
- Click **Fetch Now** to verify connectivity
- If the device has enrolled faces, go to **Faces → Pull All**

---

## Background jobs (automatic — no cron needed)

| Job | Schedule | What it does |
|-----|----------|-------------|
| Event fetch | Every 1 hour | Downloads attendance events from all devices |
| User sync | Every 1h 5min | Creates user records for new employee IDs |
| Device user push | Every 6 hours | Pushes all users to every device |
| Face sync | Daily 03:00 | Pulls faces from all devices → pushes to all devices |
| Weekly email | Monday 06:00 | Sends Excel report (if SMTP configured) |

---

## Useful commands

```bash
# Live logs
sudo journalctl -u attendance -f

# Restart after config change
sudo systemctl restart attendance

# Check scheduler jobs
curl http://localhost:5000/api/scheduler/status

# Force immediate event fetch from all devices
curl -X POST http://localhost:5000/api/fetch-now

# Backfill a missed day
cd /opt/attendance
sudo -u attendance ./venv/bin/python backfill_day.py 2025-03-15

# Check database
sqlite3 /var/lib/attendance/attendance.db \
  "SELECT COUNT(*) as events FROM events; SELECT COUNT(*) as users FROM users;"

# Run manual backup
sudo /usr/local/bin/attendance-backup
```

---

## Updating the app

```bash
# Upload new zip from your laptop:
scp attendance_rpi5.zip pi@attendance.local:~

# On the Pi:
cd ~
unzip -o attendance_rpi5.zip
cd attendance
sudo bash install.sh        # safe to re-run — skips .env if it exists
sudo systemctl restart attendance
```

---

## Troubleshooting

**Service won't start**
```bash
sudo journalctl -u attendance -n 50 --no-pager
# Usually: wrong ATT_DB path, or missing SECRET_KEY
```

**Can't reach device**
```bash
# Test ISAPI from the Pi
curl -u admin:password http://192.168.1.100/ISAPI/System/deviceInfo
```

**Clock-in times are wrong by N hours**
```bash
# Fix TZ_OFFSET in .env
sudo nano /opt/attendance/.env
# Set TZ_OFFSET to match your device timezone, e.g. -06:00
sudo systemctl restart attendance
```

**Database is locked**
```bash
# WAL mode is already enabled. If you still see locks:
sqlite3 /var/lib/attendance/attendance.db "PRAGMA wal_checkpoint(FULL);"
```

**No faces on new device after adding it**
```bash
# Go to Faces → Push to Device (or Full Sync)
# Or trigger via API:
curl -X POST http://localhost:5000/faces/push-all \
  -H "Cookie: session=<your-session-cookie>"
```
