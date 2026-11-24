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
  **Sync full catalogue** visits every set and writes only Forge image keys;
  it does not use or change the saved list.
- **Jobs:** Open a receipt to follow progress and review per-set findings.
  [Pause or resume jobs and retry unfinished sets](api.md#job-controls).
- **Diagnostics:** Check the worker and scheduler, process UID/GID, the WebP
  setting, and directory permissions (not a write test). **Check Scryfall &
  CDN** runs the [upstream probe](api.md#health-and-diagnostics). Change proxy,
  UID/GID, WebP, and ownership settings on the host, not in the console.

## First-start archive

Use `--archive-all` to queue the [full catalogue sync](library.md#full-catalogue-sync)
once for a new `--data` directory. It runs in the same downloader process
and needs neither cron nor a browser. The startup marker and job survive
restarts. Interrupted work resumes; an explicitly paused job stays paused.
Other queued jobs may run between archive sets.

The first metadata download and complete image collection can take
substantial time and disk space. Keep `cache/scryfall/` writable for an
on-disk index and its replacement. Review the **Full catalogue sync** job under
**Jobs**; if it reports a failed import, use **Retry unfinished sets**.
To include new printings after the first run, use the console or
`POST /api/v1/archive/run`. The bulk cache lasts 24 hours.
Use a staging data directory if you keep a separate Forge library.

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

`--api` takes the base URL without `/api/v1`. If omitted, the client connects
to `127.0.0.1` on [`DEFAULT_PORT`](#listening-port), or `8765` when unset.
The client adds the prefix and reads `DOWNLOADER_API_TOKEN` when configured.
Direct HTTP clients submit downloads to `/api/v1/jobs`. Use `--language CODE`
repeatedly for language priority, and `--min-quality`, `--refresh-language`, or
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

## Listening port

Set `DEFAULT_PORT` to an integer from `1` to `65535`. If unset, the server
listens on `8765`. On a native launch, `--port` overrides the environment:

```sh
DEFAULT_PORT=18765 python3 -m scryfall_downloader
```

In a container, pass `-e DEFAULT_PORT=18765` and publish the matching internal
port with `-p 127.0.0.1:18765:18765`. Use the environment variable rather than
`--port` so the bundled health check and `docker exec` sync client use the same
port. Image metadata still exposes `8765`, so publish the custom port explicitly.
To change only the host-side port, leave `DEFAULT_PORT` unset and use
`-p 127.0.0.1:18765:8765`.

## Network and proxies

Keep the service on a trusted network. For LAN access, set
`DOWNLOADER_API_TOKEN` to a dedicated secret (for example, generated with
`openssl rand -hex 32`) and put the service behind an HTTPS reverse proxy to
protect the token in transit. Do not reuse a password. Serve the console at
the proxy's origin root and preserve the `Host` and `Authorization` headers.
The console keeps the token in tab memory only; scripts use
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
or `-e DOWNLOADER_API_TOKEN`. Preserve the writable `/data` mount across
replacements so the service can resume jobs. The bundled health check uses
`/api/v1/health` on the [configured internal port](#listening-port). Set
`SCRYFALL_HEALTH_UPSTREAM=1` to probe Scryfall's bulk metadata API as well,
using `HTTPS_PROXY`/`NO_PROXY` from the container environment. Do not
restart the service merely because the independent upstream check fails.

On POSIX the server uses the launching UID/GID (image default: `10001:10001`).
Use `--user UID:GID` for a container or pass both `--uid` and `--gid` to the
server. If started as root with these flags, it drops privileges before
opening SQLite or starting HTTP. On startup it tests file creation, mode
changes, replacement, readback and cleanup in the state, cache and image
directories as the serving identity. New images are mode `0644`.

If a fresh **named state volume** is root-owned, a root launcher can pass
`--init-state-owner --uid <uid> --gid <gid>` to change only the data root's
owner before dropping privileges. With all container capabilities dropped,
that launcher needs `CHOWN`, `SETUID` and `SETGID` restored. This option
does not recurse into state or change ownership on a separate pictures
mount. Ensure any existing state files and the picture mount are already
writable by the selected identity.

To fix older image ownership, stop the server and preview on the same data
directory or mounts:

```sh
sudo python3 -m scryfall_downloader --data "$PWD/data" \
  --uid 10001 --gid 10001 --repair-ownership --dry-run
```

Remove `--dry-run` to apply. The command runs offline, only adjusting
mismatched ownership in `pics/cards`, `pics/tokens`, and their parent `pics`.
It does not follow symlinks or change hard-linked images, non-image files,
permission bits, names, or SQLite ownership.

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
Use the bundled health command instead of a deployment-specific probe;
it checks the versioned endpoint and can check upstream separately. Open
the Web UI and check health before resuming scheduled work.

When upgrading from v0.4.0 or earlier with authentication configured, rename
`SCRYFALL_API_TOKEN` to `DOWNLOADER_API_TOKEN` in the server environment and
sync scripts. Keep the token value and change the server environment key
together with the image. With only the old variable, the server refuses to
start rather than leaving the API unprotected. Restore the old variable name
when rolling back to an image that expects it.

To downgrade, restore the matching pre-upgrade state and pictures before
starting the previous image. Version v0.2.0 cannot write the newer database
schema, and older releases do not preserve saved schedules or approved
rename plans.
