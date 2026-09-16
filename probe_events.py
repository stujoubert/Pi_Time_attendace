#!/usr/bin/env python3
"""
Diagnostic: what event types does the terminal actually record, and which
carry an employee ID? Run on the Pi:  sudo python3 probe_events.py
Read-only — only queries the device, changes nothing.
"""
import sqlite3, requests, os, json
from requests.auth import HTTPDigestAuth
import urllib3
urllib3.disable_warnings()

DB = "/var/lib/attendance/attendance.db"
DEVICE_ID = 1  # Caseta
tz = os.environ.get("TZ_OFFSET", "-06:00")

conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
d = conn.execute(
    "SELECT ip, username, password FROM devices WHERE id=?", (DEVICE_ID,)
).fetchone()
conn.close()

ip, user, pw = d["ip"], d["username"], d["password"]
url = "http://%s/ISAPI/AccessControl/AcsEvent?format=json" % ip

seen = {}          # (major, minor) -> [total, with_emp]
first_with_emp = None
pos = 0
pages = 0

while pos < 600 and pages < 25:
    pages += 1
    auth = HTTPDigestAuth(user, pw)   # fresh each page
    payload = {"AcsEventCond": {
        "searchID": "0",
        "searchResultPosition": pos,
        "maxResults": 30,
        "major": 5,
        "startTime": "2026-09-15T00:00:00" + tz,
        "endTime":   "2026-09-15T23:59:59" + tz,
    }}
    r = requests.post(url, json=payload, auth=auth, timeout=30, verify=False)
    if r.status_code != 200:
        print("HTTP", r.status_code, r.text[:200])
        break
    root = r.json().get("AcsEvent", {})
    il = root.get("InfoList", [])
    if not il:
        print("no events returned at pos", pos)
        break
    for e in il:
        mj = e.get("major")
        mn = e.get("minor")
        emp = str(e.get("employeeNoString") or e.get("employeeNo") or "").strip()
        key = "major=%s minor=%s" % (mj, mn)
        if key not in seen:
            seen[key] = [0, 0]
        seen[key][0] += 1
        if emp:
            seen[key][1] += 1
            if first_with_emp is None:
                first_with_emp = e
    if root.get("responseStatusStrg") != "MORE":
        break
    pos += len(il)

print("\n=== event types seen today (major=5) ===")
print("code : total / with-employee")
for k in sorted(seen):
    print("  %s : %d total, %d with employee" % (k, seen[k][0], seen[k][1]))

print("\n=== sample raw event that HAS an employee (all fields) ===")
if first_with_emp:
    print(json.dumps(first_with_emp, indent=2, ensure_ascii=False))
else:
    print("none found with an employee ID")
