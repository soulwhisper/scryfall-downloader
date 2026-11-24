"""Durable job queue and per-set image processing."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
from uuid import uuid4

from .diagnostics import Diagnostics
from .forge import ForgeEditions, plan_images
from .maintenance import Rename, maintain
from .protocol import TERMINAL
from .set_catalog import SetCatalog
from .sync_list import SyncList
from .upstream import Asset, DownloadInterrupted, SavedImage, Scryfall, UpstreamError, Web, check_interrupted, download


MAX_ACTIVE_JOBS = 16
MAX_JOBS = 100
COUNTERS = ("downloaded", "cached", "unavailable", "failed", "missing", "corrupt", "tidied")
PAGE_SIZE = 100


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
