#!/usr/bin/env bash
set -euo pipefail

engine=${CONTAINER_ENGINE:-docker}
port=${DEFAULT_PORT:-8765}
token="smoke-test-token-${RANDOM}"
container=$("$engine" run -d -e DEFAULT_PORT -e "DOWNLOADER_API_TOKEN=$token" -p "127.0.0.1::$port" "$1")
trap '"$engine" rm -f "$container" >/dev/null' EXIT
address=$("$engine" port "$container" "$port/tcp")
api="http://$address/api/v1"

if ! curl --fail --silent --show-error --retry 5 --retry-all-errors --retry-delay 1 \
    --max-time 5 "$api/health" | jq -e '.status == "ok"' >/dev/null; then
    "$engine" logs "$container"
    exit 1
fi

status=$(curl --silent --show-error --max-time 5 --output /dev/null --write-out '%{http_code}' "$api/sync-list")
if [[ "$status" != 401 ]]; then
    printf 'Expected HTTP 401 for /sync-list without a token, got %s.\n' "$status" >&2
    exit 1
fi
curl --fail --silent --show-error --max-time 5 -H "Authorization: Bearer $token" "$api/sync-list" >/dev/null

for path in / /app.css /app.js /api.js /sets.js /ui.js; do
    curl --fail --silent --show-error --max-time 5 "http://$address$path" >/dev/null
done
"$engine" exec "$container" python -m scryfall_downloader.sync --help >/dev/null
"$engine" exec "$container" python -m scryfall_downloader.healthcheck
printf 'Container serves the portal, versioned health endpoint, and authenticated API on %s/tcp.\n' "$port"
