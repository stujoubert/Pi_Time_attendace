# Attendance System

Hikvision ISAPI attendance management — cleanly rebuilt.

## Quick Install (Ubuntu/Debian)

```bash
chmod +x install.sh
sudo ./install.sh
```

Access at `http://<server-ip>:5000`
Default login: **admin / admin** — change on first login via Accounts

---

## Face Sync — How It Works

```
Device A          Server (local cache)        Device B
────────          ────────────────────        ────────
Employee          static/uploads/faces/       Employee
enrolls face  →   {employee_id}.jpg       →   can now
on Device A       registered in DB            clock in
                                              on Device B
```

### Daily automatic cycle (03:00)
1. **Pull** — download every face from every device → local disk
2. **Push** — upload all local faces → every device

Any employee enrolled on one device will automatically appear on all
others by the next morning.

### Manual controls (Faces page)
| Action | What it does |
|--------|-------------|
| Pull from device | Download faces from one device now |
| Push to device | Upload all faces to one device now |
| Pull All | Pull from every device simultaneously |
| Push All | Push all faces to every device |
| Full Sync | Pull all → push all in one click |
| Force Re-push | Overwrite existing faces on a device |
| Upload photo | Upload a JPEG/PNG from your browser |
| Delete face | Remove from local cache and all devices |

### Faces → Device Status page
Shows a gap analysis for every device: users missing, faces missing,
with a list of specific employee IDs that need to be pushed.

---

## Scheduler Jobs

All jobs run automatically inside the Flask process — no cron required.

| Job | Schedule | What it does |
|-----|----------|--------------|
| Event fetch | Every 1 hour | Fetches events from all active devices |
| User auto-create | Every 1h 5min | Creates DB users for new employee IDs seen in events |
| User push | Every 6 hours | Pushes all DB users to every device |
| Face sync | Daily 03:00 | Pulls faces from all devices, pushes to all devices |
| Weekly email | Monday 06:00 | Sends attendance Excel to configured recipient |

---

## Project Structure

```
attendance/
├── server.py                 # Flask entry point + scheduler start
├── db.py / authz.py / translations.py / scheduler.py
├── devices/
│   └── hikvision_isapi.py    # ISAPI client: users, faces, events
├── services/
│   ├── face_sync.py          # Pull/push/store/serve face images
│   ├── collector.py          # Event fetch + user sync
│   ├── attendance.py         # Direction-aware hour calculation
│   ├── reports.py            # Excel exports (weekly, dept, monthly)
│   └── email_report.py / settings.py
├── routes/
│   ├── attendance.py         # /daily /weekly /audit
│   ├── faces.py              # /faces/* — full face management
│   ├── users.py              # Users, departments, accounts
│   ├── devices_payroll.py    # Devices + payroll + exports
│   └── misc_routes.py        # Schedules, company, API
├── scripts/bootstrap_db.py   # Idempotent schema (runs on startup)
└── templates/                # 20 Jinja2 templates, EN+ES
```

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ATT_DB` | Yes | — | Absolute path to SQLite DB |
| `SECRET_KEY` | Yes (prod) | — | Flask session key |
| `ATT_ENV` | No | `prod` | `dev` or `prod` |
| `ATT_PORT` | No | `5000` | HTTP port |
| `TZ_OFFSET` | No | `-06:00` | Timezone offset for device queries |
| `LOOKBACK_MINUTES` | No | `70` | Overlap window for hourly fetch |
| `FACE_DIR` | No | `/opt/attendance/static/uploads/faces` | Face image storage |
| `SCHEDULER_TZ` | No | `America/Mexico_City` | Scheduler timezone |

---

## Manual Utilities

```bash
# Backfill a specific date from all devices
python backfill_day.py 2025-01-15

# Check scheduler jobs
curl http://localhost:5000/api/scheduler/status

# Trigger immediate fetch
curl -X POST http://localhost:5000/api/fetch-now

# Follow logs
journalctl -u attendance -f
```

---

## Roles

| Role | Access |
|------|--------|
| **admin** | Full access |
| **manager** | Attendance, payroll, reports, users, departments, face overview |
| **viewer** | Attendance views and payroll only |

---

## Security

- `SECRET_KEY` randomly generated during install
- Device passwords never rendered in HTML
- SMTP password never pre-filled in settings form
- All routes require `@login_required + @role_required`
- Face images served through authenticated route, not static files
