"""One saved sync policy, executed by the server independently of its clients."""

from copy import deepcopy
from datetime import datetime, timezone
import json
import sys
import threading
import time

from .protocol import TERMINAL, default_languages
from .set_catalog import expand_sets
from .upstream import UpstreamError


MAX_SYNC_SETS = 256


class SyncConflict(Exception):
    pass


class SyncUnavailable(Exception):
    pass


def timestamp(seconds):
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds")


class SyncList:
    def __init__(self, jobs, admission_errors):
        self.jobs = jobs
        self.admission_errors = admission_errors
        self.wake = threading.Event()
        self.stopped = threading.Event()
        self.dispatch = threading.Lock()
        self.revision = 0
        with jobs.db:
            jobs.db.execute("""CREATE TABLE IF NOT EXISTS sync_list (
                            id INTEGER PRIMARY KEY CHECK (id = 1), payload TEXT NOT NULL)""")
            row = jobs.db.execute("SELECT payload FROM sync_list WHERE id = 1").fetchone()
        self.state = json.loads(row[0]) if row else {
            "config": {"sets": [], "languages": default_languages(), "min_quality": None,
                       "refresh_language": False, "refresh_scan": False, "include_tokens": False,
                       "new_releases_days": None, "interval_hours": None},
            "due": None, "last_run_at": None, "last_job_id": None, "last_error": None,
        }
        self.thread = threading.Thread(target=self._loop, name="sync-list", daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.stopped.set()
        self.wake.set()
        if self.thread.ident is not None:
            # Catalogue I/O does not hold the queue lock, and no subsequent state
            # writes are allowed once the scheduler is stopped.
            self.thread.join(timeout=1)

    def _save(self, state):
        with self.jobs.db:
            self.jobs.db.execute("INSERT OR REPLACE INTO sync_list VALUES (1, ?)", (json.dumps(state),))
        self.state = state

    def read(self):
        with self.jobs.ready:
            last = self.jobs.jobs.get(self.state["last_job_id"])
            return {**deepcopy(self.state["config"]), "next_run_at": timestamp(self.state["due"]),
                    "last_run_at": timestamp(self.state["last_run_at"]), "last_job_id": self.state["last_job_id"],
                    "last_job_status": last["status"] if last else None, "last_error": self.state["last_error"]}

    def configure(self, config):
        with self.jobs.ready:
            if self.jobs.stopping:
                raise SyncUnavailable("Downloader is stopping")
            old = self.state["config"]
            if old != config:
                hours = config["interval_hours"]
                due = self.state["due"]
                if hours != old["interval_hours"]:
                    due = int(time.time()) + hours * 3600 if hours else None
                self._save({**self.state, "config": deepcopy(config), "due": due, "last_error": None})
                self.revision += 1
                self.wake.set()
            return self.read()

    def _known(self, key):
        if key is None:
            return None
        return next((job for job in self.jobs.jobs.values() if job.get("_idempotency_key") == key), None)

    def run(self, key=None, *, due=None):
        scheduled = due is not None
        source = "sync_schedule" if scheduled else "sync_manual"
        if scheduled:
            key = f"!sync:schedule:{due}"
        elif key is not None:
            key = f"!sync:manual:{key}"
        if not self.dispatch.acquire(blocking=False):
            raise SyncConflict("A sync-list run is being prepared")
        try:
            with self.jobs.ready:
                if self.jobs.stopping or self.stopped.is_set():
                    raise SyncUnavailable("Downloader is stopping")
                if scheduled and due != self.state["due"]:
                    return None
                if known := self._known(key):
                    if scheduled or self.state["last_job_id"] is None:
                        self._record(known, scheduled)
                    return self.jobs.snapshot(known)
                previous = self.jobs.jobs.get(self.state["last_job_id"])
                if previous and previous["status"] not in TERMINAL:
                    raise SyncConflict(f"Sync job {previous['id']} is still {previous['status']}")
                config, revision = self.state["config"], self.revision

            codes = config["sets"]
            if config["include_tokens"] or config["new_releases_days"] is not None:
                try:
                    codes = expand_sets(self.jobs.catalog.read(), codes, include_tokens=config["include_tokens"],
                                        new_releases_days=config["new_releases_days"],
                                        today=datetime.now(timezone.utc).date())
                except (UpstreamError, OSError) as exc:
                    raise SyncUnavailable(f"Set catalogue unavailable: {exc}") from exc
                except ValueError as exc:
                    raise SyncConflict(str(exc)) from exc
            if len(codes) > MAX_SYNC_SETS:
                raise SyncConflict(f"Sync list resolves to more than {MAX_SYNC_SETS} sets; narrow the selection")
            with self.jobs.ready:
                if self.jobs.stopping or self.stopped.is_set():
                    raise SyncUnavailable("Downloader is stopping")
                if revision != self.revision:
                    raise SyncConflict("Sync list changed while resolving; run the saved list again")
                if not codes:
                    if not scheduled:
                        raise SyncConflict("No sets match the saved sync list")
                    self._record(None, True)
                    return None
                job = self.jobs.submit(codes, config["languages"], config["min_quality"],
                                       refresh_language=config["refresh_language"], refresh_scan=config["refresh_scan"],
                                       key=key, source=source)
                self._record(job, scheduled)
                return job
        finally:
            self.dispatch.release()

    def _record(self, job, scheduled):
        now = int(time.time())
        due = self.state["due"]
        if scheduled:
            due = now + self.state["config"]["interval_hours"] * 3600
        self._save({**self.state, "due": due, "last_run_at": now, "last_error": None,
                    "last_job_id": job["id"] if job else None})

    def _loop(self):
        while not self.stopped.is_set():
            self.wake.clear()
            if self.stopped.is_set():
                return
            with self.jobs.ready:
                due = self.state["due"]
                remaining = None if due is None else due - time.time()
                revision = self.revision
            if remaining is None or remaining > 0:
                self.wake.wait(remaining)
                continue
            try:
                self.run(due=due)
            except (SyncConflict, SyncUnavailable, *self.admission_errors) as exc:
                with self.jobs.ready:
                    if self.jobs.stopping or self.stopped.is_set():
                        return
                    if self.revision == revision and self.state["due"] == due and self.state["last_error"] != str(exc):
                        self._save({**self.state, "last_error": str(exc)})
                        print(json.dumps({"event": "sync_schedule_deferred", "entry_point": "sync_schedule",
                                          "scheduled_at": timestamp(due), "reason": str(exc)}), file=sys.stderr)
                self.wake.wait(60)
