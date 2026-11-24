from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO, StringIO
import json
from pathlib import Path
import tempfile
from threading import Thread
import unittest
from unittest.mock import patch

from set_catalog import SetCatalog
import sync
from upstream import UpstreamError


def set_row(code, kind="expansion", *, parent=None, days_ago=0, digital=False):
    return {"code": code, "name": code.upper(), "set_type": kind, "parent_set_code": parent,
            "released_at": (datetime.now(timezone.utc).date() - timedelta(days=days_ago)).isoformat(),
            "digital": digital, "card_count": 1}


class CatalogueTest(unittest.TestCase):
    def test_cache_is_normalized_revalidated_and_not_replaced_by_invalid_upstream(self):
        class Response(BytesIO):
            status = 200

        row = set_row("MOM")
        row["ignored"] = "extra upstream metadata"
        page = {"object": "list", "has_more": False, "data": [row]}

        class Web:
            requests = []

            def open(self, *args):
                self.requests.append(args)
                return Response(json.dumps(page).encode())

        with tempfile.TemporaryDirectory() as root, patch("set_catalog.time.sleep"):
            path = Path(root) / "sets.json"
            web = Web()
            catalog = SetCatalog(web, path)
            expected = [set_row("mom")]
            self.assertEqual(catalog.read(), expected)
            self.assertEqual(json.loads(path.read_text()), expected)
            self.assertEqual(catalog.read(), expected)
            self.assertEqual(web.requests, [("https://api.scryfall.com/sets", "api.scryfall.com", "application/json")])

            corrupt = '[{"code":"mom"}]'
            path.write_text(corrupt)
            page["data"] = [row, row]
            with self.assertRaisesRegex(UpstreamError, "duplicate set code"):
                catalog.read()
            self.assertEqual(path.read_text(), corrupt)

            page["data"] = [row]
            self.assertEqual(catalog.read(), expected)
            self.assertEqual(len(web.requests), 3)


@contextmanager
def local_api(respond):
    class Handler(BaseHTTPRequestHandler):
        def handle_request(self):
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length)) if length else None
            response = respond(self.command, self.path, self.headers, payload)
            if response is None:
                self.close_connection = True
                return
            status, body, headers = response
            raw = json.dumps(body).encode()
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = do_POST = handle_request

        def log_message(self, *_args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


def run_sync(url, *args):
    stdout, stderr = StringIO(), StringIO()
    with patch.dict("os.environ", {"SCRYFALL_API_TOKEN": "private-test-token"}), redirect_stdout(stdout), redirect_stderr(stderr):
        result = sync.main(["--api", url, "--wait", "4", *args])
    return result, stdout.getvalue(), stderr.getvalue()


class SyncTest(unittest.TestCase):
    def test_exact_token_relationship_and_opt_in_release_window(self):
        rows = [set_row("keep", days_ago=20), set_row("weird", "token", parent="keep"),
                set_row("tkeep", "token", parent="other"), set_row("recent", "core", days_ago=1),
                set_row("future", days_ago=-1), set_row("arena", digital=True), set_row("promo", "promo")]
        posts = []

        def respond(method, path, headers, body):
            if path == "/api/v1/sets":
                return 200, {"sets": rows}, {}
            if method == "GET":
                return 200, {"jobs": []}, {}
            posts.append((body, headers.get("Authorization")))
            return 202, {"id": body["sets"][0], "status": "completed"}, {}

        with local_api(respond) as url:
            self.assertEqual(run_sync(url, "KEEP", "keep")[0], 0)
            self.assertEqual([body["sets"] for body, _ in posts], [["keep"]])
            self.assertEqual(run_sync(url, "--include-tokens", "--new-releases-days", "2", "--language", "ZH_CN", "KEEP")[0], 0)
            self.assertEqual([body["sets"] for body, _ in posts[1:]], [["keep"], ["recent"], ["weird"]])
            result, _, err = run_sync(url, "keep", "unknown")
            self.assertEqual(result, 1)
            self.assertIn("Unknown Scryfall set code(s)", err)
        self.assertEqual(len(posts), 4)
        body, token = posts[0]
        self.assertNotIn("languages", body)
        self.assertEqual(token, "Bearer private-test-token")
        self.assertEqual(posts[1][0]["languages"], ["zhs"])

    def test_lost_receipt_replays_same_key_and_active_or_paused_block_other_rounds(self):
        keys = []
        jobs = {}

        def respond(method, path, headers, body):
            if path == "/api/v1/sets":
                return 200, {"sets": [set_row("tst")]}, {}
            if method == "GET":
                return 200, {"jobs": list(jobs.values())}, {}
            key = headers.get("Idempotency-Key")
            keys.append(key)
            if key in jobs:
                return 202, jobs[key], {}
            jobs[key] = {**body, "languages": body.get("languages", ["zhs", "en"]),
                         "sets": {"tst": {}}, "id": "job-1", "status": "running"}
            return None  # The server accepted the submission, but the receipt was lost.

        with local_api(respond) as url:
            result, _, err = run_sync(url, "--round", "today", "TST")
            self.assertEqual(result, 1)
            self.assertIn("submission may have been accepted", err)
            result, out, _ = run_sync(url, "--round", "tomorrow", "tst")
            self.assertEqual(result, 1)
            self.assertIn("pending (running); job job-1", out)
            jobs[keys[0]]["status"] = "paused"
            result, out, _ = run_sync(url, "--round", "tomorrow", "tst")
            self.assertEqual(result, 1)
            self.assertIn("paused; job job-1", out)
            self.assertEqual(len(keys), 1)
            jobs[keys[0]]["status"] = "completed"
            result, out, _ = run_sync(url, "--round", "today", "--language", "zh_CN", "--language", "EN", "tst")
            self.assertEqual(result, 0)
            self.assertIn("completed; job job-1", out)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(keys, [keys[0], keys[0]])

    def test_receipt_is_polled_before_next_submission_and_503_stops_the_run(self):
        calls = []

        def respond(method, path, _headers, body):
            calls.append((method, path, body["sets"][0] if body else None))
            if path == "/api/v1/sets":
                return 200, {"sets": [set_row(code) for code in ("one", "two", "three")]}, {}
            if path == "/api/v1/jobs/first":
                return 200, {"id": "first", "status": "completed"}, {}
            if method == "GET":
                return 200, {"jobs": []}, {}
            if body["sets"] == ["one"]:
                return 202, {"id": "first", "status": "queued"}, {}
            return 503, {"error": "queue full"}, {}

        with local_api(respond) as url, patch("sync.time.sleep"):
            result, out, err = run_sync(url, "--round", "test", "one", "two", "three")
        self.assertEqual(result, 1)
        self.assertIn("one: completed; job first", out)
        self.assertIn("two: not confirmed; POST /api/v1/jobs returned HTTP 503", err)
        self.assertIn("rerun with the same --round test", err)
        self.assertIn("three: not submitted", err)
        self.assertEqual(calls, [
            ("GET", "/api/v1/sets", None), ("GET", "/api/v1/jobs", None), ("POST", "/api/v1/jobs", "one"),
            ("GET", "/api/v1/jobs/first", None), ("GET", "/api/v1/jobs", None), ("POST", "/api/v1/jobs", "two"),
        ])

    def test_cross_origin_redirect_is_not_followed_with_bearer(self):
        received = []

        def other(_method, _path, headers, _body):
            received.append(headers.get("Authorization"))
            return 200, {"sets": []}, {}

        with local_api(other) as other_url:
            def redirect(*_args):
                return 302, {}, {"Location": f"{other_url}/api/v1/sets"}

            with local_api(redirect) as url:
                result, _, err = run_sync(url, "tst")
        self.assertEqual(result, 1)
        self.assertIn("HTTP 302", err)
        self.assertEqual(received, [])
