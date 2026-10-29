#!/usr/bin/env bash
# Fetch a file from the repo with NO caching, via GitHub's API (not raw CDN).
# Usage:  ./getfile.sh routes/misc_routes.py
#
# Hardened: downloads to a temp file first and only replaces the real file if
# the response looks valid. A GitHub 404/error (e.g. {"message":"Not Found"})
# or an empty body is REFUSED, so a bad pull can never clobber a working file.

set -euo pipefail

REPO="stujoubert/Pi_Time_attendace"
REF="main"
FILE="${1:?usage: getfile.sh <path/in/repo>}"

TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

# Capture HTTP status separately from the body.
HTTP_CODE="$(curl -sL -w '%{http_code}' \
  -H "Accept: application/vnd.github.raw" \
  -H "Cache-Control: no-cache, no-store, must-revalidate" \
  -H "Pragma: no-cache" \
  "https://api.github.com/repos/${REPO}/contents/${FILE}?ref=${REF}&cb=$(date +%s%N)" \
  -o "$TMP")"

# 1) HTTP must be 200.
if [[ "$HTTP_CODE" != "200" ]]; then
  echo "REFUSED: $FILE — GitHub returned HTTP $HTTP_CODE (file left unchanged)" >&2
  exit 1
fi

# 2) Body must be non-empty.
if [[ ! -s "$TMP" ]]; then
  echo "REFUSED: $FILE — empty response (file left unchanged)" >&2
  exit 1
fi

# 3) Body must not be a GitHub API error JSON.
if head -c 200 "$TMP" | grep -q '"message".*"Not Found"\|"documentation_url".*rest/repos/contents'; then
  echo "REFUSED: $FILE — GitHub error body, not file content (file left unchanged)" >&2
  exit 1
fi

# Looks good — move into place atomically.
mkdir -p "$(dirname "$FILE")"
mv "$TMP" "$FILE"
trap - EXIT
echo "OK: $FILE ($(stat -c '%s' "$FILE") bytes)"
