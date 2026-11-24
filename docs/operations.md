# Operating the downloader

## Web console

Open `/` on the downloader's origin. The console uses JavaScript; the
[REST API](api.md) is available without a browser.

- **Sync list:** Search the Scryfall catalogue by name or code. Choose sets,
  optionally include linked tokens and recent paper core or expansion sets,
  and set the language, [quality and refresh policy](library.md#quality-and-refresh).
  **Save sync list** before choosing **Run saved list**; it uses the saved policy.
- **Library operations:** Download, repair, upgrade, or scan selected sets
  without changing the saved list. See the [library guide](library.md) for
  integrity checks and [approving legacy renames](library.md#legacy-filename-approvals).
- **Jobs:** Open a receipt to follow progress and review per-set findings.
  [Pause or resume jobs and retry unfinished sets](api.md#job-controls).
- **Diagnostics:** Check the worker and scheduler, process UID/GID, the WebP
  setting, and directory permissions (not a write test). **Check Scryfall &
  CDN** runs the [upstream probe](api.md#health-and-diagnostics). Change proxy,
  UID/GID, WebP, and ownership settings on the host, not in the console.

## Built-in schedule

Set **Repeat every (hours)** on the sync list to schedule it, or leave the
field blank for manual runs only. The policy and schedule live in
`images.sqlite3`, and the server runs them even with the browser closed.
After downtime, it makes up at most one missed run. An active or paused sync
blocks another saved-list run. Pausing neither moves the schedule nor lets
it resume the job for you; **Run saved list** also leaves the schedule alone.
Use either the built-in schedule or an external timer for the same sets.

## External one-shot sync

Run one invocation per tick from DSM Task Scheduler, systemd, or a Hermes
script-only cron. No browser, model, or persistent client process is needed.
From the repository root:

```sh
# Sync a curated set and its token children.
python3 -m scryfall_downloader.sync --api https://downloader.example \
  --include-tokens --wait 1800 msh

# Sync recent paper core and expansion releases and their token children.
python3 -m scryfall_downloader.sync --api https://downloader.example \
  --include-tokens --new-releases-days 30 --wait 1800
```

`--api` takes the base URL without `/api/v1` and defaults to
`http://127.0.0.1:8765`. The client adds the prefix and reads
`SCRYFALL_API_TOKEN` when configured. Scripts that make HTTP calls directly
must use `/api/v1/jobs` to submit downloads. Use `--language CODE` repeatedly
for language priority, and `--min-quality`, `--refresh-language`, or
`--refresh-scan` for an explicit [image policy](library.md#quality-and-refresh).

The client processes one set at a time and reports job IDs and status.
Matching active or paused jobs are reported without duplication or resume.
The default idempotency round is today's UTC date; use `--round ID` to set
it explicitly and repeat the same round and arguments after an uncertain
submission. `--wait` is an overall deadline (300 seconds by default), not a
cancellation. Partial, failed, paused, and pending results exit nonzero;
inspect the receipt rather than looping on permanent gaps.

A NAS task on the container host can run the client without publishing the
REST port, using the container's loopback address:

```sh
docker exec scryfall-downloader python -m scryfall_downloader.sync --include-tokens msh
```

## Network and proxies

Keep the service on a trusted network. For LAN access, set
`SCRYFALL_API_TOKEN` and put the service behind an HTTPS reverse proxy to
protect the token in transit. Serve the console at the proxy's origin root
and preserve the `Host` header and the API's `Authorization` header. The
console keeps the token in tab memory only; scripts use
[Bearer authentication](api.md).

The source server and quick-start port mapping listen on loopback. If the
reverse proxy is on a different host, use `--bind <private-IP>` for the
source server or `-p <private-IP>:8765:8765` for the container, and restrict
access to the proxy.

For an outbound HTTP CONNECT proxy, set `HTTPS_PROXY` (or `https_proxy`)
and `NO_PROXY` (or `no_proxy`) before starting the server:

```sh
export HTTPS_PROXY=http://proxy.example:3128
export NO_PROXY=127.0.0.1,localhost
python3 -m scryfall_downloader
```

`--proxy http://proxy.example:3128` overrides the proxy for a single launch.
Both forms respect `NO_PROXY`. For a TLS-inspecting proxy, trust its CA (set
`SSL_CERT_FILE` to the CA bundle if needed). From a container, use a
container-reachable proxy address, not the container's own `127.0.0.1`.

## Container and file ownership

See the [quick start](../README.md#run) for Docker. For rootless Podman, build
with Docker format from the repository root:

```sh
mkdir -p data
podman build --format docker -f docker/Dockerfile -t localhost/scryfall-downloader:dev .
podman run --rm --name scryfall-downloader --stop-timeout 30 \
  --userns=keep-id --user "$(id -u):$(id -g)" \
  -p 127.0.0.1:8765:8765 -v "$PWD/data:/data:Z" \
  -e HTTPS_PROXY -e NO_PROXY localhost/scryfall-downloader:dev
```

Pass lowercase names with `-e https_proxy -e no_proxy` if needed. To opt into
[WebP](library.md#webp-opt-in) or authentication, pass `-e SCRYFALL_ENABLE_WEBP`
or `-e SCRYFALL_API_TOKEN`. Preserve the writable `/data` mount across
replacements so the service can resume jobs. The bundled health probe uses
`/api/v1/health` on the default internal port `8765`.

On POSIX the server uses the launching UID/GID (image default: `10001:10001`).
Use `--user UID:GID` for a container or pass both `--uid` and `--gid` to the
server. If started as root with these flags, it drops privileges before
opening SQLite or starting HTTP. Ensure the chosen identity can write to the
state and image directories. New images are mode `0644`; state modes do not
change.

To fix older image ownership, stop the server and preview on the same data
directory or mounts:

```sh
sudo python3 -m scryfall_downloader --data "$PWD/data" \
  --uid 10001 --gid 10001 --repair-ownership --dry-run
```

Remove `--dry-run` to apply. The command runs offline, only adjusting
mismatched ownership in `pics/cards`, `pics/tokens`, their parent `pics`, and
`extras`. It does not follow symlinks or change hard-linked images, non-image
files, permission bits, names, or SQLite ownership.

## Packaging, tests and releases

The Python package and its static Web UI live under `scryfall_downloader/`;
no JavaScript build tool is needed. Tests live under `tests/`. The
allowlisted `.dockerignore` keeps unrelated files out of the root build
context used with `docker/Dockerfile`.

From the repository root, run the same checks as CI:

```sh
python3 -m unittest discover -v
```

After building the quick-start image, run the container smoke test:

```sh
bash docker/smoke.sh scryfall-downloader:dev
```

The script defaults to Docker. For the Podman build above, run
`CONTAINER_ENGINE=podman bash docker/smoke.sh localhost/scryfall-downloader:dev`.

Push an annotated tag for a commit on `main` that contains `docker/Dockerfile`.
Use **Actions → Release → Run workflow** on `main` with that tag, or use the CLI
from that commit:

```sh
git tag -a v1.0.0 -m "Release v1.0.0"
git push origin refs/tags/v1.0.0
gh workflow run release.yml --ref main -f tag=v1.0.0
```

A tag push alone publishes nothing. The workflow tests the tagged commit,
builds the image, smoke-tests `/api/v1/health` and `/`, publishes
`ghcr.io/soulwhisper/scryfall-downloader:<tag>` (`linux/amd64`), and creates a
GitHub release. On first publication, set the GHCR package visibility to
public if you want anonymous pulls. Use the versioned image instead of the
local image tag in the run command.

## Upgrades and rollback

Before upgrading, stop writes and take a consistent backup of the data
directory (including SQLite state) and any separately mounted pictures.
When updating an old deployment, switch any custom local downloader probe
from `/jobs` to `/api/v1/health` with the image update. Keep independent
Scryfall checks. Open the Web UI and check the versioned health endpoint
before resuming scheduled work.

To downgrade, restore the matching pre-upgrade state and pictures before
starting the previous image. Version v0.2.0 cannot write the newer database
schema, and older releases do not preserve saved schedules or approved
rename plans.
