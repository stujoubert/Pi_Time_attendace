#!/usr/bin/env python3
print("=== TRACE VERSION: fetch_events-test v2 ===")   # version stamp — confirms you have THIS file
import sys
sys.path.insert(0, "/opt/attendance")
from devices.hikvision_isapi import HikvisionISAPI
import sqlite3
c=sqlite3.connect("/var/lib/attendance/attendance.db"); c.row_factory=sqlite3.Row
d=c.execute("SELECT ip,username,password FROM devices WHERE id=1").fetchone(); c.close()

api=HikvisionISAPI(d["ip"], d["username"], d["password"])
start="2026-09-15T00:00:00-06:00"; end="2026-09-15T23:59:59-06:00"
evs=api.fetch_events(start, end)
print("fetch_events returned:", len(evs), "total events")

times=[e.get("time") for e in evs]
print("first 3 times:", times[:3])
print("last 3 times :", times[-3:])

rid=[e for e in evs if str(e.get("employeeNoString") or "").strip()=="220026"]
print("\nRIDOLFO (220026) events in fetch_events result:", len(rid))
for e in rid: print("  ", e.get("time"), e.get("attendanceStatus"))

# also report the device's own total for cross-check
print("\n(For reference: device reported these many minor=75 events in the window)")
