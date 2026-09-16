#!/usr/bin/env bash
# Fetch a file from the repo with NO caching, via GitHub's API (not raw CDN).
# Usage:  ./getfile.sh trace.py
# The API content endpoint returns fresh data and we also send no-cache headers
# plus a unique cache-buster, so you always get the latest committed version.
set -e
REPO="stujoubert/Pi_Time_attendace"
BRANCH="main"
FILE="$1"
if [ -z "$FILE" ]; then echo "usage: $0 <filename-in-repo>"; exit 1; fi

# GitHub API raw media type returns the file bytes, uncached.
curl -sL \
  -H "Accept: application/vnd.github.raw" \
  -H "Cache-Control: no-cache, no-store, must-revalidate" \
  -H "Pragma: no-cache" \
  "https://api.github.com/repos/${REPO}/contents/${FILE}?ref=${BRANCH}&cb=$(date +%s%N)" \
  -o "$FILE"

echo "Downloaded ${FILE} ($(wc -c < "$FILE") bytes)"
