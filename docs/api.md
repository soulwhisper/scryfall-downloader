# REST API

Use `/api/v1` on the same origin as the Web UI at `/`. Unprefixed API routes
are not served. If the server has `DOWNLOADER_API_TOKEN` set, send
`Authorization: Bearer <token>` on all API requests except `GET /api/v1/health`.
This token authenticates to the downloader, not to Scryfall.
See [network and proxy setup](operations.md#network-and-proxies) for LAN access.

Send `Content-Type: application/json` with JSON request bodies. Bodies must be
objects of at most 4096 bytes. Except for health checks, API errors return
`{"error":"…"}`. Status codes include `400` for invalid input, `401` for a
missing or invalid token, `403` for a cross-origin write, `404` for a missing
route or job, `409` for a conflict, `415` for an unsupported content type,
and `503` if the service, queue, or required upstream is unavailable.

## Job requests and receipts

Submit a download and read its receipt (omit the authorization headers if the
server has no token):

```sh
api='http://127.0.0.1:8765/api/v1'
curl -i "$api/jobs" \
  -H "Authorization: Bearer $DOWNLOADER_API_TOKEN" \
  -H 'Content-Type: application/json' \
  -H 'Idempotency-Key: download-mom-neo-1' \
  -d '{"sets":["mom","neo"],"languages":["zh-CN","en"]}'

# Use the id from the response body, or follow its Location path.
job_id='paste-id-here'
curl -H "Authorization: Bearer $DOWNLOADER_API_TOKEN" "$api/jobs/$job_id"
```

`POST /api/v1/jobs` returns `202 Accepted`, a job receipt, and a versioned
`Location` header. Request fields:

| Field | Meaning |
| --- | --- |
| `sets` | 1–10 exact Scryfall set codes, case-insensitive. Request separate token sets such as `tmom` explicitly. |
| `operation` | `download` (default), `scan` (inspect without image writes), or `tidy` (preview legacy renames). |
| `languages` | Ordered [Scryfall language codes](https://scryfall.com/docs/api/languages), default `["zhs","en"]`. `zh-CN` and `zh-TW` are aliases for `zhs` and `zht`. Use `["ja"]` for Japanese only or `["ja","en"]` for English fallback. The `DOWNLOADER_IMAGE_LANGUAGE` environment variable supplies the same default when no request field is given; an empty value means `en`. |
| `min_quality` | `null` (default), `small`, `normal`, `large`, or `png`; see [quality floors](library.md#quality-and-refresh). |
| `refresh_language`, `refresh_scan` | Download-only booleans, both default `false`; see [refresh rules](library.md#quality-and-refresh). |

For example, `{"sets":["msh"],"operation":"scan","min_quality":"large"}`
reports repair and upgrade counts with sample issues. To rename files, use
the [preview and approval endpoints](#legacy-rename-plans), not `apply: true`
in this request.

Use an `Idempotency-Key` to repeat a submission after an uncertain response.
The same key and normalized request return the original job, even after a
restart; a different request using that key returns `409`. Use 1–128 ASCII
letters, digits, or `._:-`. The history retains up to 100 jobs, evicting older
terminal jobs and their keys as needed.

- `GET /api/v1/jobs/{id}` returns the receipt: `id`, `status`, timestamps,
  policy, `current_set`, `sets_finished`, and progress keyed by set code in
  `sets`. A set's `total_images` starts as `null` until resolved; each face is
  counted separately. `cached` counts validated local files;
  `languages.unknown` counts files without known language. `below_quality`
  counts retained or newly fetched images that missed an explicit floor.
- `GET /api/v1/jobs` returns `{"jobs":[…]}`, newest first. Use `?summary=1`
  for `set_codes` instead of per-set details.

Active job states are `queued`, `running`, `pausing`, and `paused`. Terminal
states are `completed` (all sets completed), `partial` (gaps or mixed results),
and `failed` (all sets failed). Check `updated_at` and per-set progress for
stalls. Jobs, image checksums and pause state persist in `images.sqlite3`.
After a restart, interrupted running jobs requeue and paused jobs stay paused.
On resumption, counts for the active set rebuild as valid files are reused;
finished sets are not rerun.

## Full catalogue sync

`POST /api/v1/archive/run` accepts `{}` and an optional `Idempotency-Key`.
It returns `202` and a usual job receipt (`archive: true`) with a
versioned `Location`. It does not edit or run the saved sync list.
An existing active or paused archive conflicts with a new one (`409`).

The set list is empty while the Scryfall bulk index is being prepared.
An import failure is reported in `error` on the job receipt; retrying
before any sets were resolved reruns the import. After resolution the
usual set-level progress and pause/resume controls apply. Image counters
cover only Forge `cards` and `tokens` images. Unmapped or conflicting keys
are reported in set `issues`. See [scope and image priority](library.md#full-catalogue-sync).

## Job controls

- `POST /api/v1/jobs/{id}/pause` needs no body. It parks a queued job with
  `200`, or returns `202` while a running job stops dispatching work and drains
  in-flight requests. Poll until `paused` before resuming.
- `POST /api/v1/jobs/{id}/resume` needs no body. It requeues a paused job under
  the same ID and returns `202`.
- `POST /api/v1/jobs/{id}/retry` with `{}` returns `202` and a new job for only
  the unfinished sets of a terminal `partial` or `failed` download or scan.
  It uses the original policy and accepts an `Idempotency-Key` scoped to the
  original job. Permanent gaps are not retried automatically.

## Legacy rename plans

Submit a `tidy` job to preview. Once it finishes with `completed` or
`partial`, call `GET /api/v1/jobs/{id}/renames`. The response includes
`total`, `offset`, `next_offset`, and a `renames` list with `set`, `source`,
and `target` per entry. Paths are relative to the data directory. Review
all pages using `?offset=<next_offset>` until `next_offset` is `null`.

`POST /api/v1/jobs/{id}/apply` with `{}` queues a new job for the persisted
plan (`202`), or returns `409` if no plan can be applied. Repeating approval
returns the same apply job while it is retained. Review the
[rename safeguards](library.md#legacy-filename-approvals) before approving.

## Catalogue

`GET /api/v1/sets` returns `{"sets":[…]}` from the 24-hour Scryfall catalogue
cache. Entries include `code`, `name`, `released_at`, `set_type`,
`parent_set_code`, `digital`, and `card_count`. See [Scryfall's API usage
limits](https://scryfall.com/docs/api/rate-limits).

## Saved sync list

- `GET /api/v1/sync-list` returns the policy, `next_run_at`, `last_run_at`,
  `last_job_id`, `last_job_status`, and `last_error`.
- `PUT /api/v1/sync-list` replaces the policy and returns it with `200`.
  Send `sets` (0–100 codes) and optionally the job language, quality and
  refresh fields. `include_tokens` adds token sets linked by `parent_set_code`.
  `new_releases_days` is 1–90 for recent paper core and expansion sets (UTC),
  or `null` to disable discovery. `interval_hours` is 1–720 or `null` for
  manual-only; enabling a schedule requires selected sets or recent discovery.
- `POST /api/v1/sync-list/run` with `{}` queues the saved list (`202`), not any
  unsaved changes. It accepts an `Idempotency-Key` for replaying that run.
  Expansion is limited to 256 sets. A new run conflicts with an existing
  active or paused sync job. See the [built-in schedule](operations.md#built-in-schedule).

## Health and diagnostics

- `GET /api/v1/health` is unauthenticated and checks the local worker and
  scheduler. It returns `200` with `{"status":"ok"}` or `503` with
  `worker_unavailable` or `scheduler_unavailable`. It does not contact Scryfall.
- `GET /api/v1/diagnostics` returns worker and scheduler status, effective
  `uid` and `gid`, path permissions, the `webp` gate, and `probe` (the most
  recent upstream sample, or `null`). With `?probe=1`, it checks Scryfall's
  card API and samples its image CDN, reusing a result for up to one minute.
