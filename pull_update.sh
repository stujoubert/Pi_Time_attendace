#!/usr/bin/env bash
# pull_update.sh — pull the latest app files from GitHub and restart the service.
#
# Run as:  sudo bash pull_update.sh
#
# Safe by design:
#   * reads files from the repo under the "attendance/" prefix (the repo layout)
#     and writes them to /opt/attendance/<path>
#   * validates every download (HTTP 200, non-empty, not a GitHub error body)
#     BEFORE overwriting — a 404 or network glitch leaves the existing file intact
#   * refuses to restart if any file was refused or fails to compile
#   * prints a verification summary at the end

set -u

REPO="stujoubert/Pi_Time_attendace"
REF="main"
APP="/opt/attendance"
PREFIX="attendance"   # files live under this folder in the repo

# Files to update (paths relative to the app root).
FILES=(
  "scheduler.py"
  "services/email_report.py"
  "routes/misc_routes.py"
  "routes/faces.py"
  "templates/company.html"
  "templates/faces.html"
)

echo "=== Attendance update — pulling from ${REPO}@${REF} ==="
cd "$APP" || { echo "ERROR: $APP not found"; exit 1; }

ok_count=0
fail_count=0
tmp="$(mktemp)"
trap 'rm -f "$tmp"' EXIT

for F in "${FILES[@]}"; do
  code="$(curl -sL -w '%{http_code}' \
    -H "Accept: application/vnd.github.raw" \
    -H "Cache-Control: no-cache, no-store, must-revalidate" \
    -H "Pragma: no-cache" \
    "https://api.github.com/repos/${REPO}/contents/${PREFIX}/${F}?ref=${REF}&cb=$(date +%s%N)" \
    -o "$tmp")"

  if [[ "$code" != "200" ]]; then
    echo "  REFUSED  $F  (HTTP $code — left unchanged)"
    fail_count=$((fail_count+1)); continue
  fi
  if [[ ! -s "$tmp" ]]; then
    echo "  REFUSED  $F  (empty body — left unchanged)"
    fail_count=$((fail_count+1)); continue
  fi
  if head -c 200 "$tmp" | grep -q '"message".*"Not Found"\|rest/repos/contents'; then
    echo "  REFUSED  $F  (GitHub error body — left unchanged)"
    fail_count=$((fail_count+1)); continue
  fi

  mkdir -p "$(dirname "$F")"
  cp "$tmp" "$F"
  echo "  OK       $F  ($(stat -c '%s' "$F") bytes)"
  ok_count=$((ok_count+1))
done

echo "--- pulled $ok_count file(s), $fail_count refused ---"

if [[ "$fail_count" -gt 0 ]]; then
  echo "One or more files were refused — NOT restarting. Fix the repo path/branch and re-run."
  exit 1
fi

echo "Clearing Python cache..."
find "$APP" -name '*.pyc' -delete 2>/dev/null
find "$APP" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null

echo "Compile check..."
if ! python3 -m py_compile "$APP"/scheduler.py "$APP"/routes/misc_routes.py \
       "$APP"/routes/faces.py "$APP"/services/email_report.py; then
  echo "ERROR: a pulled file failed to compile — NOT restarting."
  exit 1
fi

echo "Restarting service..."
systemctl restart attendance
sleep 3

echo ""
echo "=== Verification ==="
echo -n "daily email job in scheduler : "; grep -c "daily 09:00 email" "$APP"/scheduler.py
echo -n "daily_report toggle in routes: "; grep -c "daily_report_enabled" "$APP"/routes/misc_routes.py
echo -n "face download route          : "; grep -c "download_all_faces" "$APP"/routes/faces.py
echo "Service status:"
systemctl is-active attendance
journalctl -u attendance -n 15 --no-pager | grep -iE "Scheduler started|Traceback|ImportError" | tail -3
echo "=== Done ==="
