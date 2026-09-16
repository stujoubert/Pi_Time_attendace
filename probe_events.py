#!/usr/bin/env python3
"""Trace one specific punch end-to-end: fetch it, see its raw fields, its parsed
direction, and whether it's already in the DB. Read-only."""
import sqlite3, requests, os, json
from requests.auth import HTTPDigestAuth
import urllib3; urllib3.disable_warnings()

DB = "/var/lib/attendance/attendance.db"
EMP = "220026"          # RIDOLFO
DAY = "2026-09-15"
tz = os.environ.get("TZ_OFFSET", "-06:00")

conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
d = conn.execute("SELECT ip,username,password FROM devices WHERE id=1").fetchone()
# what's already stored for him
stored = conn.execute(
    "SELECT timestamp, direction FROM events WHERE employee_id=? AND DATE(timestamp)=? ORDER BY timestamp",
    (EMP, DAY)).fetchall()
conn.close()
ip, user, pw = d["ip"], d["username"], d["password"]

print("Already in DB for %s:" % EMP)
for s in stored:
    print("   ", s["timestamp"], s["direction"])

# Fetch the full day, minor=75, find all events for this employee
url = "http://%s/ISAPI/AccessControl/AcsEvent?format=json" % ip
found = []
pos = 0
while pos < 600:
    auth = HTTPDigestAuth(user, pw)
    payload = {"AcsEventCond":{"searchID":"0","searchResultPosition":pos,"maxResults":30,
        "major":5,"minor":75,
        "startTime":DAY+"T00:00:00"+tz,"endTime":DAY+"T23:59:59"+tz}}
    r = requests.post(url, json=payload, auth=auth, timeout=30, verify=False)
    if r.status_code != 200:
        print("HTTP", r.status_code); break
    root = r.json().get("AcsEvent",{})
    il = root.get("InfoList",[])
    if not il: break
    for e in il:
        emp = str(e.get("employeeNoString") or e.get("employeeNo") or "").strip()
        if emp == EMP:
            found.append(e)
    if root.get("responseStatusStrg") != "MORE": break
    pos += len(il)

print("\nOn DEVICE for %s today (minor=75): %d events" % (EMP, len(found)))
for e in found:
    print("   time=%s  attendanceStatus=%s  label=%s  serialNo=%s" % (
        e.get("time"), e.get("attendanceStatus"), e.get("label"), e.get("serialNo")))

print("\nTotal on device: %d   |   Total in DB: %d" % (len(found), len(stored)))
