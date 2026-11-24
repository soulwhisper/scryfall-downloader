"""Same-origin Web UI and versioned JSON API."""

from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from urllib.parse import parse_qs, urlsplit

from .imagefiles import QUALITY
from .jobs import JobConflict, Jobs, QueueFull
from .protocol import API_PREFIX, DEFAULT_LANGUAGES, LANGUAGE_CODES, SET_CODE, normalize_languages
from .sync_list import SyncConflict, SyncUnavailable
from .upstream import UpstreamError


IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9._:-]{1,128}\Z")
OPERATIONS = frozenset({"download", "scan", "tidy"})
WEB_ROOT = Path(__file__).resolve().with_name("web")
WEB_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/api.js": ("api.js", "text/javascript; charset=utf-8"),
    "/ui.js": ("ui.js", "text/javascript; charset=utf-8"),
    "/sets.js": ("sets.js", "text/javascript; charset=utf-8"),
}
CONTENT_POLICY = ("default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                  "img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


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
    normalized = normalize_languages(languages)
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
            if not jobs.worker.is_alive() or jobs.stopping:
                return self.respond(503, {"status": "worker_unavailable"})
            if not jobs.sync_list.thread.is_alive():
                return self.respond(503, {"status": "scheduler_unavailable"})
            return self.respond(200, {"status": "ok"})
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
            if not (saving or (post and path in ("/jobs", "/sync-list/run", "/archive/run"))
                    or action in ("apply", "retry")):
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
                elif path == "/archive/run":
                    job = jobs.submit_archive(key)
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
