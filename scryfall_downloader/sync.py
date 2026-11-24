"""Run a one-shot Scryfall downloader sync against the headless REST API."""

import argparse
from datetime import datetime, timezone
from hashlib import sha256
import http.client
import json
import math
import os
from queue import Empty, SimpleQueue
import re
import shlex
import sys
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .protocol import API_PREFIX, BEARER_TOKEN, DEFAULT_LANGUAGES, DEFAULT_PORT, TERMINAL, default_port, normalize_languages
from .set_catalog import expand_sets


ACTIVE = {"queued", "running", "pausing", "paused"}
MAX_RESPONSE_BYTES = 16 * 1024 * 1024


class SyncError(Exception):
    pass


class Deadline(SyncError):
    pass


class HTTPFailure(SyncError):
    def __init__(self, method, path, status):
        super().__init__(f"{method} {path} returned HTTP {status}")
        self.status = status


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class API:
    def __init__(self, base, token, deadline):
        self.base = base.rstrip("/")
        self.headers = {"Accept": "application/json"}
        if token:
            self.headers["Authorization"] = f"Bearer {token}"
        self.deadline = deadline
        self.opener = build_opener(NoRedirects(), ProxyHandler({}))

    def remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise Deadline("--wait deadline reached")
        return remaining

    def request(self, path, payload=None, key=None):
        # Socket timeouts are per operation; DNS or a slow response must not
        # keep the scheduler past its absolute deadline.
        self.remaining()
        result = SimpleQueue()

        def run():
            try:
                result.put((self._request(path, payload, key), None))
            except Exception as exc:
                result.put((None, exc))

        threading.Thread(target=run, daemon=True).start()
        try:
            response, error = result.get(timeout=min(self.remaining(), threading.TIMEOUT_MAX))
        except Empty:
            raise Deadline("--wait deadline reached") from None
        self.remaining()
        if error is not None:
            raise error
        return response

    def _request(self, path, payload, key):
        path = API_PREFIX + path
        headers = self.headers.copy()
        data = None
        if payload is not None:
            headers.update({"Content-Type": "application/json", "Idempotency-Key": key})
            data = json.dumps(payload).encode()
        method = "GET" if data is None else "POST"
        try:
            request = Request(self.base + path, data=data, headers=headers, method=method)
            with self.opener.open(request, timeout=min(10, self.remaining())) as reply:
                expected = 200 if data is None else 202
                if reply.status != expected:
                    raise HTTPFailure(method, path, reply.status)
                raw = bytearray()
                while True:
                    self.remaining()
                    chunk = reply.read1(min(65536, MAX_RESPONSE_BYTES + 1 - len(raw)))
                    if not chunk:
                        break
                    raw.extend(chunk)
                    if len(raw) > MAX_RESPONSE_BYTES:
                        raise SyncError(f"{method} {path} response exceeds the size limit")
            self.remaining()
            result = json.loads(raw)
        except HTTPError as exc:
            with exc:
                raise HTTPFailure(method, path, exc.code) from None
        except (ValueError, RecursionError) as exc:
            raise SyncError(f"{method} {path} returned an invalid response: {exc}") from None
        except (OSError, http.client.HTTPException) as exc:
            self.remaining()
            reason = exc.reason if isinstance(exc, URLError) else exc
            raise SyncError(f"{method} {path} failed: {reason}") from None
        if not isinstance(result, dict):
            raise SyncError(f"{method} {path} did not return a JSON object")
        return result


def select_sets(args, rows, today):
    if not isinstance(rows, list) or any(
        not isinstance(row, dict) or not isinstance(row.get("code"), str)
        or not re.fullmatch(r"[a-z0-9]{1,12}", row["code"]) for row in rows
    ):
        raise SyncError("Downloader returned an invalid set catalogue")
    explicit = (code.strip().lower() for code in args.sets)
    try:
        return expand_sets(rows, explicit, include_tokens=args.include_tokens,
                           new_releases_days=args.new_releases_days, today=today)
    except ValueError as exc:
        raise SyncError(str(exc)) from None


def policy_for(args):
    return {
        "operation": "download", "apply": False,
        "languages": normalize_languages(args.language or DEFAULT_LANGUAGES),
        "min_quality": args.min_quality, "refresh_language": args.refresh_language,
        "refresh_scan": args.refresh_scan,
    }


def idempotency_key(round_name, code, policy):
    data = json.dumps([round_name, code, policy], sort_keys=True, separators=(",", ":"))
    return "sync-v1:" + sha256(data.encode()).hexdigest()


def job_state(job):
    job_id = job.get("id")
    status = job.get("status")
    if (not isinstance(job_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", job_id)
            or not isinstance(status, str) or status not in TERMINAL | ACTIVE):
        raise SyncError("API returned an invalid job ID or status")
    return job_id, status


def existing_job(api, code, policy):
    jobs = api.request("/jobs").get("jobs")
    if not isinstance(jobs, list):
        raise SyncError("Downloader returned an invalid job list")
    for job in jobs:
        if not isinstance(job, dict) or not isinstance(job.get("sets"), dict):
            raise SyncError("Downloader returned an invalid job record")
        job_id, status = job_state(job)
        if (status in ACTIVE and code in job["sets"]
                and all(job.get(field) == value for field, value in policy.items())):
            return job_id, status
    return None


def report(code, job_id, status):
    label = f"pending ({status})" if status in ACTIVE and status != "paused" else status
    print(f"{code}: {label}; job {job_id} (GET {API_PREFIX}/jobs/{job_id})")
    return status == "completed"


def sync(api, args, today):
    codes = select_sets(args, api.request("/sets").get("sets"), today)
    if not codes:
        print("No Scryfall sets matched.")
        return 0
    success = True
    policy = policy_for(args)
    retry = f"rerun with the same --round {shlex.quote(args.round)} and options"
    for index, code in enumerate(codes):
        job_id = None
        submitted = False
        try:
            if (existing := existing_job(api, code, policy)) is not None:
                report(code, *existing)
                success = False
                continue
            payload = {"sets": [code], **policy}
            if not args.language:
                del payload["languages"]  # Use the server's default language order.
            api.remaining()
            submitted = True
            job = api.request("/jobs", payload, idempotency_key(args.round, code, policy))
            job_id, status = job_state(job)
            while status in ("queued", "running"):
                time.sleep(min(1, api.remaining()))
                job = api.request(f"/jobs/{job_id}")
                returned_id, next_status = job_state(job)
                if returned_id != job_id:
                    raise SyncError("API returned the wrong job ID while polling")
                status = next_status
            success = report(code, job_id, status) and success
        except SyncError as exc:
            if job_id is not None:
                advice = f"server job left intact; check GET {API_PREFIX}/jobs/{job_id}"
            elif isinstance(exc, HTTPFailure) and exc.status == 503:
                advice = f"service unavailable or queue full; {retry} when ready"
            elif submitted and (not isinstance(exc, HTTPFailure) or exc.status >= 500):
                advice = f"submission may have been accepted; {retry}"
            else:
                advice = retry if isinstance(exc, Deadline) else "check the API and job options"
            print(f"{code}: {'pending' if job_id else 'not confirmed'}; {exc}; {advice}", file=sys.stderr)
            for remaining in codes[index + 1:]:
                print(f"{remaining}: not submitted", file=sys.stderr)
            return 1
    return 0 if success else 1


def main(argv=None):
    today = datetime.now(timezone.utc).date()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sets", metavar="SET", nargs="*")
    parser.add_argument("--api", help=f"downloader base URL, without {API_PREFIX} "
                        f"(default: loopback on SCRYFALL_PORT, or {DEFAULT_PORT} if unset)")
    parser.add_argument("--include-tokens", action="store_true", help="also sync token sets whose catalog parent is selected")
    parser.add_argument("--new-releases-days", type=int, metavar="N", help="also sync paper core and expansion sets released in the last N days (UTC)")
    parser.add_argument("--language", action="append", metavar="CODE", help="language in priority order (repeatable; default zhs, en)")
    parser.add_argument("--min-quality", choices=("small", "normal", "large", "png"))
    parser.add_argument("--refresh-language", action="store_true")
    parser.add_argument("--refresh-scan", action="store_true")
    parser.add_argument("--round", default=today.isoformat(), metavar="ID", help="idempotency round (default: today's UTC date)")
    parser.add_argument("--wait", type=float, default=300, metavar="SECONDS", help="overall deadline in seconds (default: 300)")
    args = parser.parse_args(argv)
    if not args.sets and args.new_releases_days is None:
        parser.error("specify one or more set codes or opt into --new-releases-days")
    if args.new_releases_days is not None and args.new_releases_days < 0:
        parser.error("--new-releases-days must be nonnegative")
    if not math.isfinite(args.wait) or args.wait <= 0:
        parser.error("--wait must be a positive finite number of seconds")
    if not args.round.strip():
        parser.error("--round must not be blank")
    if args.api is None:
        try:
            args.api = f"http://127.0.0.1:{default_port()}"
        except ValueError as exc:
            parser.error(str(exc))
    try:
        parts = urlsplit(args.api)
        if (parts.scheme not in ("http", "https") or not parts.hostname or parts.port == 0
                or parts.username is not None or parts.query or parts.fragment or "?" in args.api or "#" in args.api):
            raise ValueError
    except ValueError:
        parser.error("--api must be an HTTP(S) URL without credentials, query or fragment")
    token = os.environ.get("SCRYFALL_API_TOKEN")
    if token and not BEARER_TOKEN.fullmatch(token):
        parser.error("SCRYFALL_API_TOKEN is not a valid Bearer token")
    try:
        return sync(API(args.api, token, time.monotonic() + args.wait), args, today)
    except SyncError as exc:
        print(exc, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Interrupted; any accepted server job was left intact", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
