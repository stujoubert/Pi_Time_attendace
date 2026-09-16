#!/usr/bin/env python3
"""
Diagnostic v2: this terminal REQUIRES a minor code in AcsEvent queries (it
400s without one). So we probe candidate authentication minor codes one at a
time and count how many events with an employee ID each returns for today.
Whichever codes return employee events are the ones the fetch must include.

Run on the Pi:  sudo python3 probe_events.py     (read-only)
"""
import sqlite3, requests, os, json
from requests.auth import HTTPDigestAuth
import urllib3
urllib3.disable_warnings()

DB = "/var/lib/attendance/attendance.db"
DEVICE_ID = 1  # Caseta
tz = os.environ.get("TZ_OFFSET", "-06:00")

CANDIDATE_MINORS = [1, 38, 39, 75, 76, 77, 21, 22, 23, 8, 9, 10, 74, 90, 91, 92]

conn = sqlite3.connect(DB); conn.row_factory = sqlite3.Row
d = conn.execute("SELECT ip, username, password FROM devices WHERE id=?", (DEVICE_ID,)).fetchone()
conn.close()
ip, user, pw = d["ip"], d["username"], d["password"]
url = "http://%s/ISAPI/AccessControl/AcsEvent?format=json" % ip

def count_minor(minor):
    total, with_emp, sample = 0, 0, None
    pos = 0
    while pos < 300:
        auth = HTTPDigestAuth(user, pw)
        payload = {"AcsEventCond": {
            "searchID": "0", "searchResultPosition": pos, "maxResults": 30,
            "major": 5, "minor": minor,
            "startTime": "2026-09-15T00:00:00" + tz,
            "endTime":   "2026-09-15T23:59:59" + tz,
        }}
        try:
            r = requests.post(url, json=payload, auth=auth, timeout=30, verify=False)
        except Exception as e:
            return ("error", str(e)[:60], None)
        if r.status_code != 200:
            return ("http%d" % r.status_code, 0, None)
        root = r.json().get("AcsEvent", {})
        il = root.get("InfoList", [])
        if not il:
            break
        for e in il:
            total += 1
            emp = str(e.get("employeeNoString") or e.get("employeeNo") or "").strip()
            if emp:
                with_emp += 1
                if sample is None:
                    sample = e
        if root.get("responseStatusStrg") != "MORE":
            break
        pos += len(il)
    return (total, with_emp, sample)

print("Probing minor codes for major=5 (today's events)...\n")
print("minor : total events / with employee")
best_sample = None
for m in CANDIDATE_MINORS:
    total, with_emp, sample = count_minor(m)
    if isinstance(total, str):
        print("  minor=%-3d : %s" % (m, total))
    else:
        flag = "  <-- HAS PUNCHES" if with_emp else ""
        print("  minor=%-3d : %d total, %d with employee%s" % (m, total, with_emp, flag))
        if sample is not None and best_sample is None:
            best_sample = sample

print("\n=== sample event with an employee ===")
print(json.dumps(best_sample, indent=2, ensure_ascii=False) if best_sample else "none found")
