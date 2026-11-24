"""Inspect local permissions and sample the actual outbound image path."""

from datetime import datetime, timezone
import json
import os
import threading
import time

from .upstream import NETWORK_ERRORS, UpstreamError


SAMPLE = "https://api.scryfall.com/cards/b0faa7f2-b547-42c4-a810-839da50dadfe"


class Diagnostics:
    def __init__(self, jobs):
        self.jobs = jobs
        self.lock = threading.Lock()
        self.checked = 0.0
        self.result = None

    def read(self, *, probe=False):
        jobs = self.jobs
        paths = []
        for label, path in (("State", jobs.root), ("Pictures", jobs.root / "pics"),
                            ("Cards", jobs.root / "pics" / "cards"), ("Tokens", jobs.root / "pics" / "tokens"),
                            ("Printings", jobs.root / "pics" / "printings")):
            if path.is_symlink():
                status, detail = "not_writable", "Symbolic links are not used as image directories"
            elif not path.exists():
                status, detail = "missing", "Directory does not exist"
            elif not path.is_dir() or not os.access(path, os.R_OK | os.W_OK | os.X_OK):
                status, detail = "not_writable", "Directory requires read, write, and search permissions"
            else:
                status, detail = "ok", "Process has read, write, and search permissions"
            paths.append({"label": label, "path": str(path), "status": status, "detail": detail})
        if probe:
            with self.lock:
                if self.result is None or time.monotonic() - self.checked >= 60:
                    self.result = self._probe()
                    self.checked = time.monotonic()
        return {"worker": "ok" if jobs.worker.is_alive() and not jobs.stopping else "unavailable",
                "scheduler": "ok" if jobs.sync_list.thread.is_alive() else "unavailable",
                "uid": os.geteuid() if hasattr(os, "geteuid") else None,
                "gid": os.getegid() if hasattr(os, "getegid") else None,
                "paths": paths, "webp": jobs.webp, "probe": self.result}

    def _probe(self):
        result = {"checked_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "cdn": {"ok": None, "detail": "Not checked without a usable Scryfall image URL"}}
        try:
            with self.jobs.web.open(SAMPLE, "api.scryfall.com", "application/json") as reply:
                card = json.loads(reply.read(65537)) if reply.status == 200 else None
            if not isinstance(card, dict) or card.get("object") != "card":
                raise UpstreamError("Expected a Scryfall card")
            result["scryfall"] = {"ok": True, "detail": "Scryfall card API responded"}
        except (UpstreamError, *NETWORK_ERRORS) as exc:
            result["scryfall"] = {"ok": False, "detail": str(exc)}
            return result
        urls = card.get("image_uris")
        url = (urls.get("jpeg") or urls.get("normal")) if isinstance(urls, dict) else None
        if isinstance(url, str):
            try:
                with self.jobs.web.open(url, "cards.scryfall.io", "image/jpeg") as reply:
                    if reply.status != 200 or reply.read(2) != b"\xff\xd8":
                        raise UpstreamError("Expected JPEG bytes from Scryfall CDN")
            except (UpstreamError, *NETWORK_ERRORS) as exc:
                result["cdn"] = {"ok": False, "detail": str(exc)}
            else:
                result["cdn"] = {"ok": True, "detail": "Scryfall CDN returned image bytes"}
        return result
