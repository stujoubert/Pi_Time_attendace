#!/usr/bin/env python3
print("=== TRACE VERSION: window-test v3 ===")
import sys, os
sys.path.insert(0, "/opt/attendance")
from datetime import datetime, timedelta
from devices.hikvision_isapi import HikvisionISAPI
import sqlite3

c=sqlite3.connect("/var/lib/attendance/attendance.db"); c.row_factory=sqlite3.Row
d=c.execute("SELECT ip,username,password FROM devices WHERE id=1").fetchone(); c.close()
api=HikvisionISAPI(d["ip"], d["username"], d["password"])

# Replicate EXACTLY what fetch_from_device builds
LOOKBACK = int(os.getenv("LOOKBACK_MINUTES","1440"))
tz = os.getenv("TZ_OFFSET","-06:00")
now = datetime.now()
start = (now - timedelta(minutes=LOOKBACK)).strftime("%Y-%m-%dT%H:%M:%S"+tz)
end   = now.strftime("%Y-%m-%dT%H:%M:%S"+tz)
print("Pi's now():", now.strftime("%Y-%m-%d %H:%M:%S"))
print("window start:", start)
print("window end  :", end)

evs = api.fetch_events(start, end)
print("fetch_events(app-window) returned:", len(evs))
rid=[e for e in evs if str(e.get("employeeNoString") or "").strip()=="220026"]
print("RIDOLFO events in app-window:", len(rid))
for e in rid: print("  ", e.get("time"))

# Compare: what's the LATEST event the app-window sees vs device reality
times = sorted(e.get("time","") for e in evs)
print("latest event in app-window:", times[-1] if times else "none")
