# Scryfall downloader for Forge

Download images for selected Magic: The Gathering sets through a local REST API. Requires Python 3.11+; no pip packages needed.

## Run

```sh
python3 downloader.py
```

The server listens on `127.0.0.1:8765`. Open `http://127.0.0.1:8765/` for the
Web UI. `--data DIR` selects the output and cache directory (default: `data/`
next to the program). `--forge-editions /path/to/Forge/res/editions` uses the
edition files from your Forge release instead of looking them up on GitHub.
Keep one downloader per data directory.

Keep the service on a trusted network. When publishing the port on a LAN, set
`SCRYFALL_API_TOKEN` to protect management operations and use an HTTPS reverse
proxy for the token in transit. The console keeps the token in tab memory only;
API clients use `Authorization: Bearer …`. Serve the console at the proxy's
origin root and preserve the `Host` header.

## Web UI

- **Sync list:** Search the Scryfall catalogue by name or code, save your sets,
  and optionally include linked tokens and recent paper core/expansion sets.
  Set the language order, quality floor, and refresh options. **Run saved list**
  uses the saved policy; blank **Repeat every** is manual-only.
- **Library operations:** Download, repair, upgrade, or scan chosen sets without
  changing the sync list. Preview legacy renames, then review and confirm the
  entire plan in its job receipt.
- **Jobs:** Follow per-set progress and findings, pause or resume a running job,
  and retry unfinished sets from terminal downloads or scans.
- **Diagnostics:** Check worker and scheduler liveness, process UID/GID, the WebP
  gate, and directory permissions. **Check Scryfall & CDN** makes a live sample
  request. Ownership repair, UID/GID, and proxy configuration stay on the host.

A saved list and its schedule are durable in `images.sqlite3`. Schedules run
in the downloader even when the browser is closed; after downtime, at most
one missed run is made up. A still-active or paused sync blocks another run,
and pausing a job does not change its schedule. Use either this schedule or
an external timer for the same sets, not both.

## Submit and control downloads

```sh
curl -i http://127.0.0.1:8765/jobs \
  -H 'Content-Type: application/json' \
  -d '{"sets":["mom","neo"],"languages":["zh-CN","en"]}'

# Set job_id to the id from the response body or follow its Location header.
job_id='paste-id-here'
curl "http://127.0.0.1:8765/jobs/$job_id"
curl http://127.0.0.1:8765/jobs

curl -X POST "http://127.0.0.1:8765/jobs/$job_id/pause"
# Wait for GET /jobs/$job_id to report "paused" before resuming.
curl -X POST "http://127.0.0.1:8765/jobs/$job_id/resume"
```

- `POST /jobs`: accepts 1–10 exact Scryfall set codes, case-insensitive. Returns `202 Accepted` and a `Location` header. Request a separate token set such as `tmom` explicitly. `languages` is an optional preference list shared by all requested sets, defaulting to `["zhs","en"]`. Use `["ja"]` for Japanese only or `["ja","en"]` for Japanese with English fallback. Use [Scryfall language codes](https://scryfall.com/docs/api/languages); `zh-CN` and `zh-TW` are aliases for `zhs` and `zht`.
- Send an `Idempotency-Key` header to retry a submission without creating another job. The same normalized request and key return the original job, even after a server restart; a changed request returns `409`. Keys are retained with the latest 100 jobs, not indefinitely.
- Include `"min_quality":"png"` to upgrade a valid image only to an improved scan meeting that floor. Other floors are `large` (672 × 936), `normal` (488 × 680), and `small` (146 × 204). The `png` floor also requires lossless PNG at 744 × 1040 or larger. Either orientation counts.
- `GET /jobs/{id}`: shows totals and per-set progress. `cached` includes validated files already on disk; imported files with no known language appear under `languages.unknown`. `below_quality` counts retained or newly fetched images that did not meet an explicit floor. Image totals are `null` until the set is resolved. Each face is counted separately.
- `GET /jobs`: lists recent jobs; `?summary=1` omits per-set details and lists `set_codes`. Terminal job states are `completed`, `partial`, and `failed`.
- `GET /sets`: returns the 24-hour Scryfall set catalogue, including release dates, set types, and `parent_set_code` for related token sets. `GET /health`: checks the local job worker (`200` or `503`), without contacting Scryfall or requiring a token. Use a job's `updated_at` and progress to check for stalls.
- `POST /jobs/{id}/pause`: parks a queued job immediately or returns `202` while a running job is `pausing`. Poll until `paused`; the downloader stops dispatching work and drains in-flight requests. `POST /jobs/{id}/resume` requeues a paused job under the same ID.
- `POST /jobs/{id}/retry` with `{}` submits only the unfinished sets of a terminal `partial` or `failed` download or scan. Workers do not automatically retry a job that finished with permanent gaps.
- `GET /sync-list` shows the saved policy and last/next run. `PUT /sync-list` replaces the policy with `sets` (0–100 codes), the job language/quality/refresh fields, `include_tokens` (boolean), `new_releases_days` (1–90 or `null`), and `interval_hours` (1–720 or `null`). `POST /sync-list/run` with `{}` queues the saved list and accepts an `Idempotency-Key`.
- `GET /diagnostics` reports local checks and the most recent upstream probe; `GET /diagnostics?probe=1` checks Scryfall's card API and its image CDN, reusing a result for up to one minute.

Job history, pause state, progress, and image checksums are stored in `images.sqlite3` under `--data`. After a restart, paused jobs stay paused; interrupted running jobs are requeued automatically. On resume, the active set's counts are rebuilt as its existing files are checked; valid images count as `cached` and are not fetched again. Earlier finished sets are not rerun.

### Scan, tidy, and refresh

- `{"sets":["msh"],"operation":"scan","min_quality":"large"}` inspects the local Forge keys without changing the library or fetching CDN images. Check `missing`, `unavailable`, `corrupt`, `below_quality`, `legacy`, and sample `issues` per set. Submit a download for that set to repair missing or corrupt images; include a quality floor only when upgrading.
- `{"sets":["msh"],"operation":"tidy"}` previews confirmed `.fullborder` → `.full` renames. Once the preview is terminal, read every page of `GET /jobs/{id}/renames` via `next_offset`, then send `POST /jobs/{id}/apply` with `{}`. Apply uses only that persisted plan and rechecks each source's checksum and destination; it will not touch a file that appeared or changed after the preview. Tidy preserves bytes and ownership, never overwrites a destination, and leaves conflicts alone. It requires POSIX directory operations and hard links. Stop other writers to these folders before applying.
- Download jobs can opt into `"refresh_language":true` to prefer a higher-ranked language for an image of known provenance, or `"refresh_scan":true` to accept a later `image_updated_at` for the same printing and language. Refreshes must preserve or improve pixel dimensions and cannot replace a PNG with a lossy format. Untracked local images are not assumed to be in any language. These flags use the regular 24-hour metadata cache; they do not force an upstream refresh.

Scryfall metadata is cached for 24 hours per set and language selection. English metadata is included as a naming and coverage reference even when English is not in the requested preference list.

## Images and Forge setup

For new images, the downloader checks language preferences first, skips placeholders, and prefers Scryfall's native full-card PNG (normally 744 × 1040), then the largest supported fallback in that language. It does not resize, crop, or re-encode images.

Regular cards are saved under `data/pics/cards/<SHORT_CODE>/` with English names. For example, `MOM/Invasion of Ravnica.full.png` and `MOM/Guildpact Paragon.full.png`. Forge edition files provide the short folder code (`Code2`, when defined) and art numbering. When no matching metadata is available, the downloader uses the uppercase Scryfall code and infers art numbering; check the job warning and use your Forge edition files for sets whose names or numbering differ. Filename collisions that cannot share a Forge key go to `data/extras/` rather than overwrite another printing. Tokens go to `data/pics/tokens/`, using Forge's token names when the edition defines them.

The downloader checks existing files at the Forge key before requesting a CDN image, including manually copied files and confirmed `.fullborder` aliases. It checks image structure and dimensions, verifies PNG chunk checksums on new or unindexed files, and records SHA-256 for later reuse. Missing files and checksum failures are eligible for repair without a quality request. Unreadable files surface as permission errors. Incomplete transfers and temporary CDN errors retry with backoff.

A valid current file is kept unless an explicitly requested upgrade or refresh is verified. An exact-key variant that would mask the replacement in Forge is removed only after a verified replacement is available. Use separate libraries for independent language collections.

Point `cardPicsDir` in `forge.profile.properties` to the absolute path of `data/pics/cards/` and restart Forge. To use tokens, copy the corresponding folders from `data/pics/tokens/` into your Forge cache's `pics/tokens/`. Prefer a separate card image directory: existing JPEGs with the same stem can take precedence over PNGs in Forge.

### WebP (opt-in)

Set `SCRYFALL_ENABLE_WEBP=1` on the server only when every Forge client reading
the library can load `.webp` images (see [Forge #12983](https://github.com/Card-Forge/forge/pull/12983)).
It defaults to `0`. PNG remains first within a language; the enabled fallback
order uses `display`, `grid`, and `thumb` at their corresponding quality tiers
ahead of same-size JPEGs. WebP is saved and validated natively and follows
Forge's JPG → PNG → WebP local lookup order. Older clients or a disabled
downloader do not see the WebP as an available local image. Disabling the gate
does not convert or delete previously saved WebP files.

### File ownership

On POSIX, the server uses the launching UID/GID by default (container: `10001:10001`). Specify `--uid` and `--gid` together to select the service identity. When launched as root with these flags, it drops privileges before opening the database or starting the API. For containers, you can instead set `--user UID:GID` and mount writable state and image directories for that identity. New images are mode `0644`; this does not change state-file modes.

For older or unmatched images, stop the server and preview the ownership repair on the same data directory or mounts:

```sh
sudo python3 downloader.py --data "$PWD/data" --uid 10001 --gid 10001 --repair-ownership --dry-run
```

Drop `--dry-run` to apply. The command runs offline and only changes mismatched directory and image ownership within `pics/cards`, `pics/tokens`, their parent `pics`, and `extras`. It skips symbolic links, hard-linked images, and non-image files and does not change permission bits, names, or SQLite ownership.

## Scheduled sync without Hermes

Use the built-in schedule for a saved sync list, or run one invocation per
tick from DSM Task Scheduler, systemd, or a Hermes script-only cron. No
browser, model, or persistent external client process is needed.

```sh
# Sync a curated set and all token children listed in the catalogue.
python3 sync.py --api http://nas.homelab.internal:8765 --include-tokens --wait 1800 msh

# Opt into recent paper core/expansion releases as well.
python3 sync.py --api http://nas.homelab.internal:8765 --include-tokens --new-releases-days 30 --wait 1800
```

The client processes one set at a time and reports job IDs and terminal
status. The default idempotency round is today's UTC date; set `--round ID`
to control it. Repeat the same round and arguments after an uncertain
submission. Matching active or paused jobs are reported without submitting
duplicates or resuming them. `partial`, `failed`, or pending results exit
nonzero; inspect the job instead of retrying a permanent missing image in
a loop. `--wait` is an overall deadline (default 300 seconds); it does not
cancel a submitted job. The client reads `SCRYFALL_API_TOKEN` when configured.
The container includes `/app/sync.py`; a NAS task on the container host can
also invoke `docker exec scryfall-downloader python /app/sync.py --include-tokens msh`
without publishing the REST port.

## Proxy and container

On the host, set `HTTPS_PROXY` (or `https_proxy`) before starting the downloader. For example:

```sh
export HTTPS_PROXY=http://proxy.example:3128
export NO_PROXY=127.0.0.1,localhost
python3 downloader.py
```

For a one-off override, pass `--proxy http://proxy.example:3128`. Both forms use an HTTP CONNECT proxy for the HTTPS origins and respect `NO_PROXY`. A TLS-inspecting proxy needs a trusted CA; set `SSL_CERT_FILE` to your CA bundle if needed. For a proxy on the host, use a container-reachable address rather than the container's own `127.0.0.1`.

```sh
mkdir -p data
podman build -t localhost/scryfall-downloader:local .
podman run --rm --stop-timeout 30 --userns=keep-id --user "$(id -u):$(id -g)" \
  -p 127.0.0.1:8765:8765 -v "$PWD/data:/data:Z" \
  -e HTTPS_PROXY -e NO_PROXY \
  localhost/scryfall-downloader:local
```

Keep the `/data` mount writable and reuse it across container replacements so the service can resume jobs. With Docker, replace `podman` with `docker` and omit `--userns=keep-id`. Pass lowercase environment names with `-e https_proxy -e no_proxy` if needed. For local Forge metadata, mount `res/editions` read-only and append `--forge-editions /editions` after the image name.
To opt into WebP or API authentication, pass the corresponding environment
variable with `-e SCRYFALL_ENABLE_WEBP` or `-e SCRYFALL_API_TOKEN`.

Before upgrading a library, take a consistent backup of `/data` (including
the SQLite state) and the mounted pictures. Restore the matching pre-upgrade
state and pictures before downgrading. Version v0.2.0 cannot write the new
database schema, and older releases do not preserve saved sync schedules or
approved rename plans.

## Release a version

Push an annotated tag from `main`, then run **Actions → Release → Run workflow**
on `main` with that tag:

```sh
git tag -a v1.0.0 -m "Release v1.0.0"
git push origin refs/tags/v1.0.0
gh workflow run release.yml --ref main -f tag=v1.0.0
```

Pushing a tag alone does not publish anything. The workflow tests and builds
the tagged commit, pushes `ghcr.io/soulwhisper/scryfall-downloader:<tag>`
(`linux/amd64`), and creates a GitHub release. For the first publication, set
the GHCR package visibility to public if you want anonymous pulls. Use the
versioned image in place of `localhost/scryfall-downloader:local` above.

Sources: [Scryfall image formats](https://scryfall.com/docs/api/images), [API limits](https://scryfall.com/docs/api/rate-limits), [Forge image keys](https://github.com/Card-Forge/forge/blob/master/forge-core/src/main/java/forge/util/ImageUtil.java) and [image lookup](https://github.com/Card-Forge/forge/blob/master/forge-core/src/main/java/forge/ImageKeys.java).
