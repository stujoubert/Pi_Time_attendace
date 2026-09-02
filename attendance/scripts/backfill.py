#!/usr/bin/env python3
"""
backfill.py — Pull historical events from all active Hikvision devices.

Usage:
  # Backfill last 30 days (default)
  sudo /opt/attendance/venv/bin/python3 /opt/attendance/scripts/backfill.py

  # Backfill specific date range
  sudo /opt/attendance/venv/bin/python3 /opt/attendance/scripts/backfill.py --start 2026-03-01 --end 2026-04-13

  # Backfill specific device only
  sudo /opt/attendance/venv/bin/python3 /opt/attendance/scripts/backfill.py --device 192.168.0.91

  # Dry run — show what would be fetched without storing
  sudo /opt/attendance/venv/bin/python3 /opt/attendance/scripts/backfill.py --dry-run
"""

import sys
import os
import argparse
import sqlite3
from datetime import date, datetime, timedelta

# Add app to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('ATT_DB', '/var/lib/attendance/attendance.db')
os.environ.setdefault('SECRET_KEY', 'backfill')
os.environ.setdefault('TZ_OFFSET', '-06:00')

from db import get_conn
from services.collector import fetch_from_device


def parse_args():
    p = argparse.ArgumentParser(description='Backfill attendance events from Hikvision devices')
    p.add_argument('--start', type=str, help='Start date YYYY-MM-DD (default: 30 days ago)')
    p.add_argument('--end',   type=str, help='End date YYYY-MM-DD (default: today)')
    p.add_argument('--device', type=str, help='Filter by device IP address')
    p.add_argument('--dry-run', action='store_true', help='Show what would be fetched without storing')
    p.add_argument('--chunk-days', type=int, default=1,
                   help='Days per request chunk (default 1 — one request per day, safer for large date ranges)')
    return p.parse_args()


def main():
    args = parse_args()

    # Date range
    end_date   = date.fromisoformat(args.end)   if args.end   else date.today()
    start_date = date.fromisoformat(args.start) if args.start else end_date - timedelta(days=30)

    if start_date > end_date:
        print(f"ERROR: start date {start_date} is after end date {end_date}")
        sys.exit(1)

    total_days = (end_date - start_date).days + 1
    tz = os.environ.get('TZ_OFFSET', '-06:00')

    print("=" * 60)
    print(f"  Attendance Backfill")
    print(f"  Date range : {start_date} → {end_date} ({total_days} days)")
    print(f"  Timezone   : {tz}")
    print(f"  Dry run    : {'YES — nothing will be stored' if args.dry_run else 'NO — events will be stored'}")
    print("=" * 60)

    # Get devices
    conn = get_conn()
    query = "SELECT id, ip, username, password, name FROM devices WHERE active=1"
    params = []
    if args.device:
        query += " AND ip=?"
        params.append(args.device)
    devices = conn.execute(query, params).fetchall()
    conn.close()

    if not devices:
        print("ERROR: No active devices found" + (f" matching IP {args.device}" if args.device else ""))
        sys.exit(1)

    print(f"\nDevices to backfill ({len(devices)}):")
    for d in devices:
        print(f"  [{d['id']}] {d['name']} ({d['ip']})")
    print()

    grand_total = 0

    for device in devices:
        print(f"\n{'─' * 50}")
        print(f"Device: {device['name']} ({device['ip']})")
        print(f"{'─' * 50}")

        device_total = 0
        current = start_date

        while current <= end_date:
            chunk_end = min(current + timedelta(days=args.chunk_days - 1), end_date)

            start_iso = datetime.combine(current, datetime.min.time()).strftime(f"%Y-%m-%dT%H:%M:%S{tz}")
            end_iso   = datetime.combine(chunk_end, datetime.max.time().replace(microsecond=0)).strftime(f"%Y-%m-%dT%H:%M:%S{tz}")

            print(f"  Fetching {current} → {chunk_end}...", end=' ', flush=True)

            try:
                if args.dry_run:
                    # Just test connectivity — fetch but don't store
                    from devices.hikvision_isapi import HikvisionISAPI
                    api = HikvisionISAPI(device['ip'], device['username'], device['password'])
                    events = api.fetch_events(start_iso, end_iso)
                    count = len(events)
                    print(f"{count} events found (not stored)")
                else:
                    count = fetch_from_device(
                        ip=device['ip'],
                        username=device['username'],
                        password=device['password'],
                        device_id=device['id'],
                        start=start_iso,
                        end=end_iso,
                    )
                    print(f"{count} new events stored")

                device_total += count

            except Exception as e:
                print(f"ERROR: {e}")

            current = chunk_end + timedelta(days=1)

        print(f"\n  → Device total: {device_total} {'events found' if args.dry_run else 'new events stored'}")
        grand_total += device_total

    print(f"\n{'=' * 60}")
    print(f"  COMPLETE — Grand total: {grand_total} {'events found' if args.dry_run else 'new events stored'}")
    print(f"{'=' * 60}")

    # Show current event count in DB
    if not args.dry_run:
        conn = get_conn()
        total_in_db = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        earliest = conn.execute("SELECT MIN(timestamp) FROM events").fetchone()[0]
        latest   = conn.execute("SELECT MAX(timestamp) FROM events").fetchone()[0]
        conn.close()
        print(f"\n  Database now has {total_in_db} total events")
        print(f"  Earliest: {earliest}")
        print(f"  Latest:   {latest}")


if __name__ == '__main__':
    main()
