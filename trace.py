#!/usr/bin/env python3
# Does the app's OWN fetch_events return RIDOLFO's 18:07? And how many total?
import sys
sys.path.insert(0, "/opt/attendance")
from datetime import datetime, timedelta
from devices.hikvision_isapi import HikvisionISAPI
import sqlite3
c=sqlite3.connect("/var/lib/attendance/attendance.db"); c.row_factory=sqlite3.Row
d=c.execute("SELECT ip,username,password,profile FROM devices WHERE id=1").fetchone(); c.close()

api=HikvisionISAPI(d["ip"], d["username"], d["password"])

# Exactly what the app does: full-day window
start="2026-09-15T00:00:00-06:00"; end="2026-09-15T23:59:59-06:00"
evs=api.fetch_events(start, end)
print("fetch_events returned:", len(evs), "total events")

# order check: first and last few timestamps
times=[e.get("time") for e in evs]
print("first 3 times:", times[:3])
print("last 3 times :", times[-3:])

# is RIDOLFO 18:07 in there?
rid=[e for e in evs if str(e.get("employeeNoString") or "").strip()=="220026"]
print("\nRIDOLFO events in fetch_events result:", len(rid))
for e in rid: print("  ", e.get("time"), e.get("attendanceStatus"))
