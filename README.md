# Scryfall downloader for Forge

Download images for selected Magic: The Gathering sets through a local REST API. Requires Python 3.11+; no pip packages needed.

## Run

```sh
python3 downloader.py
```

The server listens on `127.0.0.1:8765`. `--data DIR` selects the output and cache directory (default: `data/` next to the program). `--forge-editions /path/to/Forge/res/editions` uses the edition files from your Forge release instead of looking them up on GitHub. Keep one downloader per data directory. The API has no authentication; do not expose it publicly.

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
- Include `"min_quality":"png"` to upgrade valid images only when a higher-quality scan meets that floor. Other floors are `large` (672 × 936), `normal` (488 × 680), and `small` (146 × 204). The `png` floor also requires lossless PNG at 744 × 1040 or larger. Either orientation counts. Without a floor, a valid local image is never replaced to change quality or language.
- `GET /jobs/{id}`: shows totals and per-set progress. `cached` includes validated files already on disk; imported files with no known language appear under `languages.unknown`. `below_quality` counts retained or newly fetched images that did not meet an explicit floor. Image totals are `null` until the set is resolved. Each face is counted separately.
- `GET /jobs`: lists recent jobs. Terminal job states are `completed`, `partial`, and `failed`.
- `POST /jobs/{id}/pause`: parks a queued job immediately or returns `202` while a running job is `pausing`. Poll until `paused`; the downloader stops dispatching work and drains in-flight requests. `POST /jobs/{id}/resume` requeues a paused job under the same ID. Terminal jobs cannot be resumed; submit a new job to retry them.

Job history, pause state, progress, and image checksums are stored in `images.sqlite3` under `--data`. After a restart, paused jobs stay paused; interrupted running jobs are requeued automatically. On resume, the active set's counts are rebuilt as its existing files are checked; valid images count as `cached` and are not fetched again. Earlier finished sets are not rerun.

Scryfall metadata is cached for 24 hours per set and language selection. English metadata is included as a naming and coverage reference even when English is not in the requested preference list.

## Images and Forge setup

The downloader checks each printing and face in language preference order, skipping Scryfall placeholders. In each language it prefers Scryfall's native full-card PNG (normally 744 × 1040), then the largest Forge-compatible JPEG offered. It does not resize, crop, or re-encode images.

Regular cards are saved under `data/pics/cards/<SHORT_CODE>/` with English names. For example, `MOM/Invasion of Ravnica.full.png` and `MOM/Guildpact Paragon.full.png`. Forge edition files provide the short folder code (`Code2`, when defined) and art numbering. When no matching metadata is available, the downloader uses the uppercase Scryfall code and infers art numbering; check the job warning and use your Forge edition files for sets whose names or numbering differ. Filename collisions that cannot share a Forge key go to `data/extras/` rather than overwrite another printing. Tokens go to `data/pics/tokens/`, using Forge's token names when the edition defines them.

The downloader checks existing `.jpg` and `.png` files at the exact Forge key before requesting a CDN image, even if they were copied in manually. It checks image headers, dimensions and end markers, verifies PNG chunk checksums on new or unindexed files, and records SHA-256 for later reuse. Missing files and checksum failures are eligible for repair without a quality request. Unreadable files result in a permission error, not a blind replacement. Incomplete transfers and temporary CDN errors retry with backoff.

An explicit quality upgrade cannot reduce either pixel dimension. A valid current file is kept when no acceptable upgrade is available. A same-key JPEG that would mask a replacement PNG in Forge is removed only after a valid PNG is available. The downloader does not infer identities or rename other legacy filenames. `languages` controls new fetches only; use separate libraries for separate language collections.

Point `cardPicsDir` in `forge.profile.properties` to the absolute path of `data/pics/cards/` and restart Forge. To use tokens, copy the corresponding folders from `data/pics/tokens/` into your Forge cache's `pics/tokens/`. Prefer a separate card image directory: existing JPEGs with the same stem can take precedence over PNGs in Forge.

### File ownership

On POSIX, the server uses the launching UID/GID by default (container: `10001:10001`). Specify `--uid` and `--gid` together to select the service identity. When launched as root with these flags, it drops privileges before opening the database or starting the API. For containers, you can instead set `--user UID:GID` and mount writable state and image directories for that identity. New images are mode `0644`; this does not change state-file modes.

For older or unmatched images, stop the server and preview the ownership repair on the same data directory or mounts:

```sh
sudo python3 downloader.py --data "$PWD/data" --uid 10001 --gid 10001 --repair-ownership --dry-run
```

Drop `--dry-run` to apply. The command runs offline and only changes mismatched directory and image ownership within `pics/cards`, `pics/tokens`, their parent `pics`, and `extras`. It skips symbolic links, hard-linked images, and non-image files and does not change permission bits, names, or SQLite ownership.

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
