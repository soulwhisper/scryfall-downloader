#!/usr/bin/env bash
set -euo pipefail

engine=${CONTAINER_ENGINE:-docker}
container=$("$engine" run -d -p 127.0.0.1::8765 "$1")
trap '"$engine" rm -f "$container" >/dev/null' EXIT
address=$("$engine" port "$container" 8765/tcp)

if ! curl --fail --silent --show-error --retry 5 --retry-all-errors --retry-delay 1 \
    --max-time 5 "http://$address/api/v1/health" | jq -e '.status == "ok"' >/dev/null; then
    "$engine" logs "$container"
    exit 1
fi

for path in / /app.css /app.js /api.js /sets.js /ui.js; do
    curl --fail --silent --show-error --max-time 5 "http://$address$path" >/dev/null
done
"$engine" exec "$container" python -m scryfall_downloader.sync --help >/dev/null
"$engine" exec "$container" python -m scryfall_downloader.healthcheck
printf 'Container serves the portal, its assets, and the versioned health endpoint.\n'
