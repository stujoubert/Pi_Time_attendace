#!/usr/bin/env python3
"""
Backfill attendance events for a specific date from all active devices.

Usage:
    python backfill_day.py YYYY-MM-DD

Example:
    python backfill_day.py 2025-03-15
"""
import os
import sys
from datetime import datetime, timedelta, time

if len(sys.argv) != 2:
    print("Usage: backfill_day.py YYYY-MM-DD")
    sys.exit(1)

try:
    target_day = datetime.fromisoformat(sys.argv[1]).date()
except Exception:
    print("Invalid date. Use YYYY-MM-DD format.")
    sys.exit(1)

tz = os.getenv("TZ_OFFSET", "-06:00")
start_iso = datetime.combine(target_day, time.min).strftime(f"%Y-%m-%dT%H:%M:%S{tz}")
end_iso   = datetime.combine(target_day, time.max.replace(microsecond=0)).strftime(f"%Y-%m-%dT%H:%M:%S{tz}")

print(f"Backfilling {target_day.isoformat()}")
print(f"  Window: {start_iso}  →  {end_iso}")

from db import get_conn
from services.collector import fetch_from_device

conn = get_conn()
devices = conn.execute(
    "SELECT id, ip, username, password, name FROM devices WHERE active=1"
).fetchall()
conn.close()

if not devices:
    print("No active devices found.")
    sys.exit(0)

total = 0
for d in devices:
    try:
        count = fetch_from_device(
            d["ip"], d["username"], d["password"],
            d["id"], start_iso, end_iso
        )
        print(f"  [{d['name']}] stored {count} events")
        total += count
    except Exception as e:
        print(f"  [{d['name']}] ERROR: {e}")

print(f"\nDone — {total} total events stored.")
