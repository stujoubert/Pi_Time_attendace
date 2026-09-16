#!/usr/bin/env python3
# Trace one employee's events: device vs DB, with the app's own direction parse.
import sys, sqlite3, requests, os
sys.path.insert(0, "/opt/attendance")
from requests.auth import HTTPDigestAuth
import urllib3; urllib3.disable_warnings()
from services.collector import _parse_direction, _normalize_ts

DB="/var/lib/attendance/attendance.db"; EMP="220026"; DAY="2026-09-15"; tz="-06:00"
c=sqlite3.connect(DB); c.row_factory=sqlite3.Row
d=c.execute("SELECT ip,username,password FROM devices WHERE id=1").fetchone()
indb=c.execute("SELECT timestamp,direction FROM events WHERE employee_id=? AND DATE(timestamp)=? ORDER BY timestamp",(EMP,DAY)).fetchall()
c.close()

print("=== IN DB (%d) ===" % len(indb))
for r in indb: print("  ", r["timestamp"], r["direction"])

url="http://%s/ISAPI/AccessControl/AcsEvent?format=json" % d["ip"]
pos=0; dev=[]
while pos<400:
    a=HTTPDigestAuth(d["username"],d["password"])
    pl={"AcsEventCond":{"searchID":"0","searchResultPosition":pos,"maxResults":30,
        "major":5,"minor":75,"startTime":DAY+"T00:00:00"+tz,"endTime":DAY+"T23:59:59"+tz}}
    r=requests.post(url,json=pl,auth=a,timeout=30,verify=False)
    root=r.json().get("AcsEvent",{}); il=root.get("InfoList",[])
    if not il: break
    for e in il:
        if str(e.get("employeeNoString") or "").strip()==EMP: dev.append(e)
    if root.get("responseStatusStrg")!="MORE": break
    pos+=len(il)

print("\n=== ON DEVICE (%d) ===" % len(dev))
for e in dev:
    raw=e.get("time"); norm=_normalize_ts(str(raw)); dirn=_parse_direction(e)
    print("  raw=%s  norm=%s  parsed_dir=%s  att=%s" % (raw, norm, dirn, e.get("attendanceStatus")))

# Now simulate the dedup key for each device event and check if it "would store"
print("\n=== DEDUP CHECK (device_id, emp, norm_ts, parsed_dir) vs DB ===")
c=sqlite3.connect(DB)
for e in dev:
    norm=_normalize_ts(str(e.get("time"))); dirn=_parse_direction(e)
    hit=c.execute("SELECT 1 FROM events WHERE device_id=1 AND employee_id=? AND timestamp=? AND direction=?",(EMP,norm,dirn)).fetchone()
    print("  %s %s -> %s" % (norm, dirn, "ALREADY IN DB" if hit else "WOULD INSERT (missing!)"))
c.close()
