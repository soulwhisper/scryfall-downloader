"""Local REST server and job runner for the Scryfall downloader."""

import argparse
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import deepcopy
from datetime import datetime, timezone
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import threading
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from diagnostics import Diagnostics
from forge import ForgeEditions, plan_images
from imagefiles import QUALITY
from maintenance import Rename, maintain
from ownership import repair_ownership, set_identity
from set_catalog import SetCatalog
from sync_list import SyncConflict, SyncList, SyncUnavailable
from upstream import Asset, DownloadInterrupted, SavedImage, Scryfall, UpstreamError, Web, check_interrupted, download


SET_CODE = re.compile(r"[a-zA-Z0-9]{1,12}\Z")
IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
BEARER_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*\Z")
DEFAULT_LANGUAGES = ("zhs", "en")
LANGUAGE_CODES = frozenset("en es fr de it pt ja ko ru zhs zht he la grc ar sa ph qya dw tlh".split())
LANGUAGE_ALIASES = {
    "zh-cn": "zhs", "zhcn": "zhs", "zh-hans": "zhs",
    "zh-tw": "zht", "zhtw": "zht", "zh-hant": "zht",
}
MAX_ACTIVE_JOBS = 16
MAX_JOBS = 100
TERMINAL = frozenset({"completed", "partial", "failed"})
COUNTERS = ("downloaded", "cached", "unavailable", "failed", "missing", "corrupt", "tidied")
OPERATIONS = frozenset({"download", "scan", "tidy"})
PAGE_SIZE = 100
API_PREFIX = "/api/v1"
WEB_ROOT = Path(__file__).resolve().with_name("web")
WEB_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
}
CONTENT_POLICY = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                  "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class QueueFull(Exception):
    pass


class JobConflict(Exception):
    pass


def new_progress(languages: list[str]) -> dict:
    return {
        "status": "queued", "printings": None, "total_images": None,
        **dict.fromkeys(COUNTERS, 0), "languages": dict.fromkeys(languages, 0),
        "below_quality": 0, "legacy": 0, "renames": 0,
        "warnings": [], "errors": [], "issues": [],
    }


class Jobs:
    def __init__(self, root: Path, workers: int, editions: Path | None, proxy: str | None, *, webp: bool = False):
        self.root = root
        self.workers = workers
        self.webp = webp
        self.ready = threading.Condition()
        self.interrupt = threading.Event()
        self.stopping = False
        self.jobs = {}
        self.web = Web(proxy)
        self.scryfall = Scryfall(self.web, root / "cache" / "scryfall", self.interrupt)
        self.catalog = SetCatalog(self.web, root / "cache" / "scryfall" / "sets.json")
        self.forge = ForgeEditions(self.web, root / "cache" / "forge", editions)
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "images.sqlite3", check_same_thread=False)
        try:
            self._restore()
            self.sync_list = SyncList(self, (QueueFull, JobConflict))
        except BaseException:
            self.db.close()
            raise
        self.worker = threading.Thread(target=self.run, name="set-worker")
        self.diagnostics = Diagnostics(self)

    def start(self):
        self.worker.start()
        self.sync_list.start()

    def _restore(self):
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA foreign_keys = ON")
        with self.db:
            self.db.execute("BEGIN")
            columns = {row[1] for row in self.db.execute("PRAGMA table_info(images)")}
            migrate = columns and "language" not in columns
            if migrate:
                self.db.execute("ALTER TABLE images RENAME TO legacy_images")
            self.db.execute("""CREATE TABLE IF NOT EXISTS images (
                stem TEXT PRIMARY KEY, url TEXT, suffix TEXT NOT NULL,
                size INTEGER NOT NULL, sha256 TEXT, language TEXT,
                file_name TEXT, source_id TEXT, image_updated_at TEXT
            )""")
            if migrate:
                checksum = "sha256" if "sha256" in columns else "NULL"
                self.db.execute(f"""INSERT INTO images (stem, url, suffix, size, sha256)
                                    SELECT stem, url, suffix, size, {checksum} FROM legacy_images""")
                self.db.execute("DROP TABLE legacy_images")
            elif columns:
                for column in ("file_name", "source_id", "image_updated_at"):
                    if column not in columns:
                        self.db.execute(f"ALTER TABLE images ADD COLUMN {column} TEXT")
            self.db.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            self.db.execute("""CREATE TABLE IF NOT EXISTS tidy_renames (
                            job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                            set_code TEXT NOT NULL, stem TEXT NOT NULL,
                            source TEXT NOT NULL, target TEXT NOT NULL, sha256 TEXT NOT NULL,
                            PRIMARY KEY (job_id, set_code, stem)) WITHOUT ROWID""")
            for (payload,) in self.db.execute("SELECT payload FROM jobs ORDER BY rowid").fetchall():
                job = json.loads(payload)
                job.setdefault("min_quality", None)
                job.setdefault("operation", "download")
                job.setdefault("apply", False)
                job.setdefault("refresh_language", False)
                job.setdefault("refresh_scan", False)
                job.setdefault("source", "api")
                job.setdefault("preview_id", None)
                job.setdefault("updated_at", job["finished_at"] or job["started_at"] or job["created_at"])
                for progress in job["sets"].values():
                    progress.setdefault("below_quality", 0)
                    progress.setdefault("legacy", 0)
                    progress.setdefault("renames", 0)
                    progress.setdefault("issues", [])
                    for counter in COUNTERS:
                        progress.setdefault(counter, 0)
                self.jobs[job["id"]] = job
                if job["status"] in ("running", "pausing"):
                    self._park(job, "paused" if job["status"] == "pausing" else "queued")
                    self._save(job)

    def _save(self, job: dict):
        job["updated_at"] = now()
        self.db.execute("""INSERT INTO jobs (id, payload) VALUES (?, ?)
                           ON CONFLICT(id) DO UPDATE SET payload = excluded.payload""",
                        (job["id"], json.dumps(job, ensure_ascii=False)))

    @staticmethod
    def _park(job: dict, status: str):
        job["status"] = status
        if code := job["current_set"]:
            progress = job["sets"][code]
            if progress["status"] not in TERMINAL:
                progress["status"] = status

    def close(self):
        with self.ready:
            self.stopping = True
            self.interrupt.set()
            self.ready.notify_all()
        self.sync_list.stop()
        self.worker.join()
        self.db.close()

    def submit(self, codes: list[str], languages: list[str], min_quality: str | None, *,
               operation: str = "download", apply: bool = False, refresh_language: bool = False,
               refresh_scan: bool = False, key: str | None = None, source: str = "api",
               preview_id: str | None = None) -> dict:
        options = {
            "languages": languages, "min_quality": min_quality, "operation": operation, "apply": apply,
            "refresh_language": refresh_language, "refresh_scan": refresh_scan,
            "source": source, "preview_id": preview_id,
        }
        with self.ready:
            if key is not None:
                for job in self.jobs.values():
                    if job.get("_idempotency_key") == key:
                        if list(job["sets"]) != codes or any(job[name] != value for name, value in options.items()):
                            raise JobConflict("Idempotency-Key already belongs to a different request")
                        return self.snapshot(job)
            if self.stopping:
                raise QueueFull("Downloader is stopping")
            if sum(job["status"] not in TERMINAL for job in self.jobs.values()) >= MAX_ACTIVE_JOBS:
                raise QueueFull("Download queue is full")
            oldest = None
            if len(self.jobs) >= MAX_JOBS:
                oldest = next(key for key, job in self.jobs.items() if job["status"] in TERMINAL)
            job = {
                "id": uuid4().hex, "status": "queued", "created_at": now(),
                "started_at": None, "finished_at": None, "current_set": None,
                **options,
                "sets": {code: new_progress(languages) for code in codes},
            }
            if key is not None:
                job["_idempotency_key"] = key
            with self.db:
                self._save(job)
                if preview_id:
                    self.db.execute("""INSERT INTO tidy_renames
                                    SELECT ?, set_code, stem, source, target, sha256
                                    FROM tidy_renames WHERE job_id = ?""", (job["id"], preview_id))
                if oldest:
                    self.db.execute("DELETE FROM jobs WHERE id = ?", (oldest,))
            if oldest:
                del self.jobs[oldest]
            self.jobs[job["id"]] = job
            self.ready.notify()
            return self.snapshot(job)

    def retry(self, job_id: str, key: str | None) -> dict | None:
        with self.ready:
            old = self.jobs.get(job_id)
            if old is None:
                return None
            if old["operation"] not in ("download", "scan") or old["status"] not in ("partial", "failed"):
                raise JobConflict("Only failed or partial downloads and scans can be retried")
            codes = [code for code, progress in old["sets"].items() if progress["status"] in ("partial", "failed")]
            return self.submit(codes, old["languages"], old["min_quality"], operation=old["operation"],
                               refresh_language=old["refresh_language"], refresh_scan=old["refresh_scan"],
                               key=f"!retry:{job_id}:{key}" if key else None, source="retry")

    def apply_tidy(self, job_id: str) -> dict | None:
        key = f"!tidy:{job_id}"
        with self.ready:
            for job in self.jobs.values():
                if job.get("_idempotency_key") == key:
                    return self.snapshot(job)
            old = self.jobs.get(job_id)
            if old is None:
                return None
            if old["operation"] != "tidy" or old["apply"] or old["status"] not in ("completed", "partial"):
                raise JobConflict("A finished tidy preview is required")
            codes = [row[0] for row in self.db.execute(
                "SELECT DISTINCT set_code FROM tidy_renames WHERE job_id = ? ORDER BY set_code", (job_id,))]
            if not codes:
                raise JobConflict("The preview contains no approved renames")
            # A pre-console binary must not interpret this as an unbounded legacy apply.
            return self.submit(codes, old["languages"], old["min_quality"], operation="tidy_apply", apply=True,
                               key=key, source="tidy_apply", preview_id=job_id)

    def read_renames(self, job_id: str, offset: int):
        with self.ready:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            if job["operation"] not in ("tidy", "tidy_apply") or job["status"] not in TERMINAL:
                raise JobConflict("The tidy plan is not ready")
            total, = self.db.execute("SELECT count(*) FROM tidy_renames WHERE job_id = ?", (job_id,)).fetchone()
            rows = self.db.execute("""SELECT set_code, source, target FROM tidy_renames WHERE job_id = ?
                                   ORDER BY set_code, stem LIMIT ? OFFSET ?""", (job_id, PAGE_SIZE, offset))
            return {"renames": [{"set": code, "source": source, "target": target} for code, source, target in rows],
                    "total": total, "offset": offset, "next_offset": offset + PAGE_SIZE if offset + PAGE_SIZE < total else None}

    def control(self, job_id: str, *, pause: bool) -> dict | None:
        with self.ready:
            if self.stopping:
                raise QueueFull("Downloader is stopping")
            job = self.jobs.get(job_id)
            if job is None:
                return None
            status = job["status"]
            if status in TERMINAL:
                raise JobConflict(f"Job is already {status}")
            if status == "pausing" and not pause:
                raise JobConflict("Wait for the job to become paused")
            target = ({"queued": "paused", "running": "pausing"}.get(status)
                      if pause else {"paused": "queued"}.get(status))
            if target:
                # Persist the control request before interrupting the worker or acknowledging it.
                updated = deepcopy(job)
                self._park(updated, target)
                with self.db:
                    self._save(updated)
                self._park(job, target)
                job["updated_at"] = updated["updated_at"]
                if target == "pausing":
                    self.interrupt.set()
                elif target == "queued":
                    self.ready.notify()
            return self.snapshot(job)

    @staticmethod
    def snapshot(job: dict) -> dict:
        result = deepcopy(job)
        result.pop("_idempotency_key", None)
        if result["operation"] == "tidy_apply":
            result["operation"] = "tidy"
        sets = result["sets"].values()
        result["sets_finished"] = sum(info["status"] in TERMINAL for info in sets)
        for info in sets:
            info["processed_images"] = sum(info[counter] for counter in COUNTERS)
        return result

    def read(self, job_id: str | None, *, summary: bool = False):
        with self.ready:
            if job_id is None:
                if summary:
                    summaries = []
                    for job in reversed(self.jobs.values()):
                        row = {name: value for name, value in job.items() if name not in ("sets", "_idempotency_key")}
                        if row["operation"] == "tidy_apply":
                            row["operation"] = "tidy"
                        row["set_codes"] = list(job["sets"])
                        row["sets_finished"] = sum(info["status"] in TERMINAL for info in job["sets"].values())
                        summaries.append(row)
                    return {"jobs": summaries}
                return {"jobs": [self.snapshot(job) for job in reversed(self.jobs.values())]}
            job = self.jobs.get(job_id)
            return self.snapshot(job) if job else None

    def run(self):
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            while True:
                with self.ready:
                    while not self.stopping:
                        job = next((job for job in self.jobs.values() if job["status"] == "queued"), None)
                        if job is not None:
                            break
                        self.ready.wait()
                    if self.stopping:
                        return
                    self.interrupt.clear()
                    with self.db:
                        job.update(status="running", started_at=job["started_at"] or now())
                        self._save(job)
                try:
                    self._run_job(job, pool)
                except DownloadInterrupted:
                    pass
                with self.ready, self.db:
                    statuses = {progress["status"] for progress in job["sets"].values()}
                    if statuses <= TERMINAL:
                        status = next(iter(statuses)) if len(statuses) == 1 else "partial"
                        job.update(status=status, current_set=None, finished_at=now())
                    else:
                        self._park(job, "paused" if job["status"] == "pausing" else "queued")
                    self._save(job)

    def _run_job(self, job, pool):
        for code, progress in job["sets"].items():
            with self.ready, self.db:
                if progress["status"] in TERMINAL:
                    continue
                check_interrupted(self.interrupt)
                job["current_set"] = code
                progress.update(new_progress(job["languages"]), status="resolving")
                if job["operation"] == "tidy" and not job["apply"]:
                    self.db.execute("DELETE FROM tidy_renames WHERE job_id = ? AND set_code = ?", (job["id"], code))
                self._save(job)
            try:
                self.run_set(job, code, progress, pool)
            except DownloadInterrupted:
                raise
            except Exception as exc:
                with self.ready, self.db:
                    check_interrupted(self.interrupt)
                    progress["status"] = "failed"
                    if len(progress["errors"]) < 12:
                        progress["errors"].append(str(exc) or type(exc).__name__)
                    self._save(job)

    def run_set(self, job, code, progress, pool):
        operation = "tidy" if job["operation"] == "tidy_apply" else job["operation"]
        expected_renames = {}
        if job["apply"] and not job["preview_id"]:
            raise JobConflict("A new tidy preview is required for this job")
        warning = None
        printings = None
        collisions = 0
        if job["apply"]:
            with self.ready:
                rows = self.db.execute("""SELECT stem, source, target, sha256 FROM tidy_renames
                                       WHERE job_id = ? AND set_code = ? ORDER BY stem""", (job["id"], code))
                expected_renames = {stem: Rename(source, target, digest) for stem, source, target, digest in rows}
            if not expected_renames:
                raise JobConflict("No approved renames for this set")
            assets = [Asset(stem, (), legacy_border=True) for stem in expected_renames]
        else:
            languages = job["languages"]
            cards = self.scryfall.cards(code, languages)
            if not cards:
                raise UpstreamError("No card printings found for this set and its requested languages")
            check_interrupted(self.interrupt)
            edition, warning = self.forge.find(code, cards[0])
            assets, printings, collisions = plan_images(code, cards, edition, languages, self.webp)
        with self.ready, self.db:
            check_interrupted(self.interrupt)
            if warning:
                progress["warnings"].append(warning)
            if collisions:
                progress["warnings"].append(f"{collisions} image path collision(s) mapped under extras/{code.upper()}/")
            status = {"download": "downloading", "scan": "scanning", "tidy": "tidying"}[operation]
            progress.update(status=status, printings=printings, total_images=len(assets))
            self._save(job)

        # Keep only workers' worth of requests in flight so a pause can drain promptly.
        remaining = iter(assets)
        tasks = {}
        while True:
            with self.ready:
                while not self.interrupt.is_set() and len(tasks) < self.workers:
                    asset = next(remaining, None)
                    if asset is None:
                        break
                    row = self.db.execute("""SELECT url, suffix, size, sha256, language, file_name,
                                                    source_id, image_updated_at FROM images WHERE stem = ?""",
                                          (asset.stem,)).fetchone()
                    previous = SavedImage(*row) if row else None
                    args = (self.root, asset, previous, self.interrupt, job["min_quality"], self.webp)
                    if operation == "download":
                        task = pool.submit(download, self.web, *args, job["refresh_language"], job["refresh_scan"])
                    else:
                        task = pool.submit(maintain, *args, tidy=operation == "tidy", apply=job["apply"],
                                           expected=expected_renames.get(asset.stem))
                    tasks[task] = asset.stem, previous
            if not tasks:
                break
            done, _ = wait(tasks, return_when=FIRST_COMPLETED)
            for task in done:
                stem, previous = tasks.pop(task)
                error = issue = rename = None
                legacy = False
                try:
                    result = task.result()
                    if operation == "download":
                        outcome, saved, below_quality = result
                        legacy = saved is not None and saved.file_name is not None
                    else:
                        outcome, saved = result.outcome, result.saved
                        below_quality, legacy, issue = result.below_quality, result.legacy, result.issue
                        rename = result.rename
                except DownloadInterrupted:
                    continue
                except Exception as exc:
                    outcome, saved, below_quality = "failed", None, False
                    error = f"{stem}: {exc}"
                with self.ready, self.db:
                    if saved is not None and saved != previous:
                        self.db.execute("""INSERT OR REPLACE INTO images
                                        (stem, url, suffix, size, sha256, language, file_name, source_id, image_updated_at)
                                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                        (stem, saved.url, saved.suffix, saved.size, saved.sha256, saved.language,
                                         saved.file_name, saved.source_id, saved.image_updated_at))
                    if rename is not None:
                        self.db.execute("INSERT INTO tidy_renames VALUES (?, ?, ?, ?, ?, ?)",
                                        (job["id"], code, stem, rename.source, rename.target, rename.sha256))
                        progress["renames"] += 1
                    progress[outcome] += 1
                    progress["below_quality"] += below_quality
                    progress["legacy"] += legacy
                    if saved is not None:
                        lang = saved.language or "unknown"
                        counts = progress["languages"]
                        counts[lang] = counts.get(lang, 0) + 1
                    if error and len(progress["errors"]) < 12:
                        progress["errors"].append(error)
                    if issue and len(progress["issues"]) < 12:
                        progress["issues"].append(issue)
                    self._save(job)

        with self.ready, self.db:
            if sum(progress[counter] for counter in COUNTERS) != len(assets):
                raise DownloadInterrupted
            if progress["failed"] and not (progress["downloaded"] or progress["cached"] or progress["tidied"]):
                progress["status"] = "failed"
            elif any(progress[key] for key in ("failed", "unavailable", "below_quality", "missing", "corrupt")):
                progress["status"] = "partial"
            else:
                progress["status"] = "completed"
            self._save(job)


def codes_from(body, minimum, maximum):
    codes = body.get("sets")
    if (not isinstance(codes, list) or not minimum <= len(codes) <= maximum
            or not all(isinstance(code, str) and SET_CODE.fullmatch(code) for code in codes)):
        raise ValueError(f"Send {minimum}–{maximum} alphanumeric Scryfall set codes")
    return list(dict.fromkeys(code.lower() for code in codes))


def policy_from(body):
    languages = body.get("languages", list(DEFAULT_LANGUAGES))
    if (not isinstance(languages, list) or not 1 <= len(languages) <= len(LANGUAGE_CODES)
            or not all(isinstance(lang, str) for lang in languages)):
        raise ValueError("languages must be a nonempty list of Scryfall language codes")
    langs = (lang.strip().lower().replace("_", "-") for lang in languages)
    normalized = list(dict.fromkeys(LANGUAGE_ALIASES.get(lang, lang) for lang in langs))
    if not LANGUAGE_CODES.issuperset(normalized):
        raise ValueError("Unknown Scryfall language code")
    minimum = body.get("min_quality")
    if minimum is not None and (not isinstance(minimum, str) or minimum not in QUALITY):
        raise ValueError("min_quality must be small, normal, large, or png")
    return {"languages": normalized, "min_quality": minimum,
            "refresh_language": flag_from(body, "refresh_language"), "refresh_scan": flag_from(body, "refresh_scan")}


def flag_from(body, name):
    value = body.get(name, False)
    if not isinstance(value, bool):
        raise ValueError(f"{name} must be a boolean")
    return value


def optional_integer(body, name, maximum):
    value = body.get(name)
    if value is not None and (type(value) is not int or not 1 <= value <= maximum):
        raise ValueError(f"{name} must be null or an integer from 1 to {maximum}")
    return value


POLICY_FIELDS = {"sets", "languages", "min_quality", "refresh_language", "refresh_scan"}


def api_path(path: str) -> str | None:
    return path[len(API_PREFIX):] if path.startswith(API_PREFIX + "/") else None


class API(BaseHTTPRequestHandler):
    timeout = 10  # Don't leave API threads parked on stalled clients.

    def respond(self, status: int, body: dict, *, location: str | None = None):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.reply(status, data, "application/json; charset=utf-8", location=location)

    def reply(self, status, data, mime, *, location=None):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", CONTENT_POLICY)
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        if location:
            self.send_header("Location", location)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def log_request(self, code="-", size="-"):
        pass  # Progress polling is not useful server output.

    def authorized(self):
        expected = self.server.api_token
        if expected is None:
            return True
        scheme, _, token = self.headers.get("Authorization", "").partition(" ")
        if scheme.lower() == "bearer" and compare_digest(token.encode(), expected):
            return True
        self.respond(401, {"error": "Bearer token required"})
        return False

    def do_HEAD(self):
        if asset := self.server.assets.get(self.path.partition("?")[0]):
            return self.reply(200, *asset)
        return self.respond(404, {"error": "Not found"})

    def do_GET(self):
        path, _, raw_query = self.path.partition("?")
        if asset := self.server.assets.get(path):
            return self.reply(200, *asset)
        path = api_path(path)
        if path is None:
            return self.respond(404, {"error": "Not found"})
        jobs = self.server.jobs
        if path == "/health":
            alive = jobs.worker.is_alive() and not jobs.stopping
            return self.respond(200 if alive else 503, {"status": "ok" if alive else "worker_unavailable"})
        if not self.authorized():
            return
        try:
            query = parse_qs(raw_query, max_num_fields=16)
            parts = path.split("/")
            if path == "/sets":
                body = {"sets": jobs.catalog.read()}
            elif path == "/sync-list":
                body = jobs.sync_list.read()
            elif path == "/diagnostics":
                body = jobs.diagnostics.read(probe=query.get("probe") == ["1"])
            elif path == "/jobs":
                body = jobs.read(None, summary=query.get("summary") == ["1"])
            elif len(parts) == 3 and parts[1] == "jobs":
                body = jobs.read(parts[2])
            elif len(parts) == 4 and parts[1] == "jobs" and parts[3] == "renames":
                offset, = query.get("offset", ["0"])
                if not re.fullmatch(r"[0-9]{1,8}", offset):
                    raise ValueError("offset must be a nonnegative integer")
                body = jobs.read_renames(parts[2], int(offset))
            else:
                body = None
        except ValueError as exc:
            return self.respond(400, {"error": str(exc)})
        except JobConflict as exc:
            return self.respond(409, {"error": str(exc)})
        except (UpstreamError, OSError) as exc:
            return self.respond(503, {"error": str(exc)})
        return self.respond(200, body) if body is not None else self.respond(404, {"error": "Not found"})

    def browser_origin_allowed(self):
        site = self.headers.get("Sec-Fetch-Site")
        if site not in (None, "same-origin", "none"):
            return False
        origin = self.headers.get("Origin")
        if origin is None:
            return True
        try:
            uri = urlsplit(origin)
        except ValueError:
            return False
        return (uri.scheme in ("http", "https") and uri.netloc.lower() == self.headers.get("Host", "").lower()
                and not (uri.path or uri.query or uri.fragment))

    def read_object(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 1 <= length <= 4096 or self.headers.get("Transfer-Encoding") is not None:
                raise ValueError
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, RecursionError):
            raise ValueError("Expected a JSON object of at most 4096 bytes") from None
        return body

    def do_POST(self):
        self.mutate()

    def do_PUT(self):
        self.mutate()

    def mutate(self):
        path = api_path(self.path.partition("?")[0])
        if path is None:
            return self.respond(404, {"error": "Not found"})
        if not self.browser_origin_allowed():
            return self.respond(403, {"error": "Cross-origin writes are not allowed"})
        if not self.authorized():
            return
        parts = path.split("/")
        post = self.command == "POST"
        action = parts[3] if post and len(parts) == 4 and parts[1] == "jobs" else None
        jobs = self.server.jobs
        try:
            if action in ("pause", "resume"):
                job = jobs.control(parts[2], pause=action == "pause")
                if job is None:
                    return self.respond(404, {"error": "Not found"})
                return self.respond(200 if job["status"] == "paused" else 202, job)
            saving = path == "/sync-list" and not post
            if not (saving or (post and path in ("/jobs", "/sync-list/run")) or action in ("apply", "retry")):
                return self.respond(404, {"error": "Not found"})
            if self.headers.get_content_type() != "application/json":
                return self.respond(415, {"error": "Content-Type must be application/json"})
            body = self.read_object()
            key = self.headers.get("Idempotency-Key")
            if key is not None and not IDEMPOTENCY_KEY.fullmatch(key):
                raise ValueError("Invalid Idempotency-Key")
            if saving:
                if body.keys() - POLICY_FIELDS - {"include_tokens", "new_releases_days", "interval_hours"}:
                    raise ValueError("Unknown sync-list option")
                codes = codes_from(body, 0, 100)
                recent = optional_integer(body, "new_releases_days", 90)
                interval = optional_integer(body, "interval_hours", 720)
                if interval is not None and not (codes or recent):
                    raise ValueError("Select sets or enable recent-release discovery before scheduling")
                config = {"sets": codes, **policy_from(body), "include_tokens": flag_from(body, "include_tokens"),
                          "new_releases_days": recent, "interval_hours": interval}
                return self.respond(200, jobs.sync_list.configure(config))
            if path == "/jobs":
                if body.keys() - POLICY_FIELDS - {"operation", "apply"}:
                    raise ValueError("Unknown job option")
                operation = body.get("operation", "download")
                if not isinstance(operation, str) or operation not in OPERATIONS:
                    raise ValueError("operation must be download, scan, or tidy")
                if flag_from(body, "apply"):
                    raise ValueError(f"Create a tidy preview, then use POST {API_PREFIX}/jobs/{{id}}/apply")
                options = policy_from(body)
                if operation != "download" and (options["refresh_language"] or options["refresh_scan"]):
                    raise ValueError("Refresh options require download")
                job = jobs.submit(codes_from(body, 1, 10), operation=operation, key=key, **options)
            else:
                if body:
                    raise ValueError("This action accepts an empty JSON object only")
                if action == "apply":
                    job = jobs.apply_tidy(parts[2])
                elif action == "retry":
                    job = jobs.retry(parts[2], key)
                else:
                    job = jobs.sync_list.run(key)
        except ValueError as exc:
            return self.respond(400, {"error": str(exc)})
        except (JobConflict, SyncConflict) as exc:
            return self.respond(409, {"error": str(exc)})
        except (QueueFull, SyncUnavailable) as exc:
            return self.respond(503, {"error": str(exc)})
        if job is None:
            return self.respond(404, {"error": "Not found"})
        return self.respond(202, job, location=f"{API_PREFIX}/jobs/{job['id']}")


class Server(ThreadingHTTPServer):
    def __init__(self, address, jobs: Jobs, token: str | None = None):
        self.jobs = jobs
        self.api_token = token.encode() if token else None
        self.assets = {route: ((WEB_ROOT / name).read_bytes(), mime) for route, (name, mime) in WEB_FILES.items()}
        super().__init__(address, API)

    def service_actions(self):
        if not self.jobs.worker.is_alive():
            raise RuntimeError("Set worker exited unexpectedly")
        if not self.jobs.sync_list.thread.is_alive():
            raise RuntimeError("Sync scheduler exited unexpectedly")


def interrupt(_signum, _frame):
    raise KeyboardInterrupt


def posix_id(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = -1
    if not 0 <= number < 2**32 - 1:
        raise argparse.ArgumentTypeError("expected a numeric POSIX UID or GID")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1", help="HTTP listen address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="HTTP port (default: 8765)")
    parser.add_argument("--data", type=Path, default=Path(__file__).resolve().parent / "data",
                        help="Output and cache directory (default: data/ next to the program)")
    parser.add_argument("--forge-editions", type=Path, help="Forge's res/editions directory; overrides automatic edition lookup")
    parser.add_argument("--workers", type=int, default=4, choices=range(1, 9), help="Concurrent image downloads (default: 4)")
    parser.add_argument("--proxy", help="Outbound HTTP CONNECT proxy URL (default: use HTTPS_PROXY)")
    parser.add_argument("--uid", type=posix_id, help="Service UID or target UID for ownership repair")
    parser.add_argument("--gid", type=posix_id, help="Service GID or target GID for ownership repair")
    parser.add_argument("--repair-ownership", action="store_true", help="Repair image ownership offline, then exit")
    parser.add_argument("--dry-run", action="store_true", help="Preview --repair-ownership without changes")
    args = parser.parse_args()
    if not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    if (args.uid is None) != (args.gid is None) or (args.repair_ownership and args.uid is None):
        parser.error("--uid and --gid must be provided together; both are required for ownership repair")
    if args.dry_run and not args.repair_ownership:
        parser.error("--dry-run requires --repair-ownership")
    try:
        root = args.data.expanduser().resolve()
        if args.repair_ownership:
            changed, skipped = repair_ownership(root, args.uid, args.gid, dry_run=args.dry_run)
            action = "Would change" if args.dry_run else "Changed"
            print(f"{action} ownership on {changed} entries; skipped {skipped} links or non-regular images")
            return
        if args.uid is not None:
            set_identity(args.uid, args.gid)
        editions = args.forge_editions.expanduser().resolve() if args.forge_editions else None
        webp = os.environ.get("SCRYFALL_ENABLE_WEBP", "0")
        if webp not in ("0", "1"):
            raise ValueError("SCRYFALL_ENABLE_WEBP must be 0 or 1")
        token = os.environ.get("SCRYFALL_API_TOKEN")
        if token is not None and not BEARER_TOKEN.fullmatch(token):
            raise ValueError("SCRYFALL_API_TOKEN must be a nonempty Bearer token")
        jobs = Jobs(root, args.workers, editions, args.proxy, webp=webp == "1")
        server = Server((args.bind, args.port), jobs, token)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"Error: {exc}\n")
    signal.signal(signal.SIGTERM, interrupt)
    with server:
        jobs.start()
        try:
            print(f"Listening on http://{args.bind}:{server.server_port}; data: {jobs.root}", flush=True)
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            jobs.close()


if __name__ == "__main__":
    main()
