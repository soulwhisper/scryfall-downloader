from contextlib import closing, contextmanager
import hashlib
from http.client import HTTPConnection
from io import BytesIO
import json
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
import threading
import time
import unittest
from urllib.parse import parse_qs, urlsplit
import zlib

from downloader import Jobs, Server


IMAGE_ORIGIN = "https://cards.scryfall.io/"


def pixel_png(red, width=1, height=1):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\x00" + bytes((red, 0, 0)) * width) * height))
            + chunk(b"IEND", b""))


class Response(BytesIO):
    status = 200

    def __init__(self, body, length=None):
        super().__init__(body)
        self.headers = {"Content-Length": str(len(body) if length is None else length)}


def printing(number, lang, name, *scans):
    def images(scan):
        return {"png": IMAGE_ORIGIN + scan} if scan else {}

    card = {"set": "tst", "set_name": "Local Test Set", "set_type": "expansion", "name": name,
            "collector_number": str(number), "lang": lang, "image_status": "highres_scan"}
    if len(scans) == 2:
        card.update(layout="transform", card_faces=[{"name": face, "image_uris": images(scan)}
                    for face, scan in zip(name.split(" // "), scans, strict=True)])
    else:
        card.update(layout="normal", image_uris=images(scans[0]))
    return card


class Upstream:
    def __init__(self, cards, scans, *, sets=()):
        self.cards = cards
        self.scans = scans
        self.image_requests = []
        self.sets = sets

    def open(self, url, host, _accept):
        if host == "api.scryfall.com":
            if urlsplit(url).path == "/sets":
                return Response(json.dumps({"object": "list", "data": self.sets, "has_more": False}).encode())
            query = parse_qs(urlsplit(url).query)["q"][0]
            cards = [card for card in self.cards if f"lang:{card['lang']}" in query]
            return Response(json.dumps({"object": "list", "data": cards, "has_more": False}).encode())
        scan = url.removeprefix(IMAGE_ORIGIN)
        self.image_requests.append(scan)
        body = self.scans[scan]
        return body(self.image_requests.count(scan)) if callable(body) else Response(body)


class DownloaderTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.editions = self.root / "editions"
        self.editions.mkdir()

    @contextmanager
    def server(self, upstream, rows, *, token=None):
        (self.editions / "Test.txt").write_text("[metadata]\nCode=TST\nCode2=FT\n[cards]\n" + rows,
                                               encoding="utf-8")
        jobs = Jobs(self.root, 1, self.editions, None)
        jobs.web.open = upstream.open
        with Server(("127.0.0.1", 0), jobs, token) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
            jobs.start()
            thread.start()
            try:
                yield server.server_port
            finally:
                server.shutdown()
                thread.join()
                jobs.close()

    def api(self, port, method, path, payload=None, expect=200, *, headers=None):
        with closing(HTTPConnection("127.0.0.1", port, timeout=3)) as conn:
            body = None if payload is None else json.dumps(payload).encode()
            headers = dict(headers or {})
            if body is not None:
                headers["Content-Type"] = "application/json"
            conn.request(method, path, body, headers)
            with conn.getresponse() as response:
                data = json.load(response)
                self.assertEqual(response.status, expect, data)
                return data

    def wait_for(self, port, path, status, *, headers=None):
        deadline = time.monotonic() + 6
        while True:
            job = self.api(port, "GET", path, headers=headers)
            if job["status"] == status:
                return job
            if job["status"] in {"completed", "partial", "failed"} or time.monotonic() >= deadline:
                self.fail(f"Expected {status}, got {job}")
            time.sleep(0.01)

    def test_scan_preference_retry_and_detection_of_same_size_corruption(self):
        chinese, english, japanese = (pixel_png(color) for color in (100, 120, 140))
        cards = [printing(1, "en", "Vél // After", "en-front.png", "en-back.png"),
                 printing(1, "zhs", "Vél // After", "zh-front.png", None),
                 printing(1, "ja", "Vél // After", "ja-front.png", None),
                 printing(2, "en", "Oak", "oak-1.png"), printing(3, "en", "Oak", "oak-2.png")]
        chinese_uris = cards[1]["card_faces"][0]["image_uris"]
        chinese_uris["large"] = IMAGE_ORIGIN + "zh-large.jpg"
        cards[1]["image_status"] = "lowres"

        def retry_once(attempt):
            return Response(chinese[:-12] if attempt == 1 else chinese, len(chinese))

        upstream = Upstream(cards, {"zh-front.png": retry_once, "en-back.png": english,
                                   "oak-1.png": english, "oak-2.png": english, "ja-front.png": japanese})
        folder = self.root / "pics" / "cards" / "FT"
        folder.mkdir(parents=True)
        old_file = folder / "Vel.full.jpg"
        old_file.write_bytes(b"old receipt")
        with closing(sqlite3.connect(self.root / "images.sqlite3")) as db, db:
            db.execute("""CREATE TABLE images (stem TEXT PRIMARY KEY, url TEXT NOT NULL, suffix TEXT NOT NULL,
                         size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL)""")
            db.execute("INSERT INTO images VALUES (?, ?, ?, ?, ?)",
                       ("pics/cards/FT/Vel.full", chinese_uris["large"], ".jpg", old_file.stat().st_size,
                        old_file.stat().st_mtime_ns))

        with self.server(upstream, "1 R Vél\n2 C Oak\n3 C Oak\n") as port:
            job = self.api(port, "POST", "/jobs", {"sets": ["TST"]}, expect=202)
            progress = self.wait_for(port, f"/jobs/{job['id']}", "completed")["sets"]["tst"]
            self.assertEqual(progress["languages"], {"zhs": 1, "en": 3})
            self.assertEqual(upstream.image_requests.count("zh-front.png"), 2)
            front = folder / "Vel.full.png"
            self.assertEqual(front.read_bytes(), chinese)
            if os.name == "posix":
                self.assertEqual(front.stat().st_mode & 0o777, 0o644)
            self.assertFalse(old_file.exists())
            for filename in ("After.full.png", "Oak1.full.png", "Oak2.full.png"):
                self.assertEqual((folder / filename).read_bytes(), english)

            info = front.stat()
            altered = bytearray(chinese)
            altered[40] ^= 1
            front.write_bytes(altered)
            os.utime(front, ns=(info.st_atime_ns, info.st_mtime_ns))
            job = self.api(port, "POST", "/jobs", {"sets": ["tst"], "languages": ["zh-CN", "EN"]}, expect=202)
            progress = self.wait_for(port, f"/jobs/{job['id']}", "completed")["sets"]["tst"]
            self.assertEqual((progress["cached"], progress["downloaded"]), (3, 1))
            self.assertEqual(front.read_bytes(), chinese)

            # A strict language request does not overwrite validated existing copies.
            front.unlink()
            (folder / "Oak2.full.png").unlink()
            job = self.api(port, "POST", "/jobs", {"sets": ["tst"], "languages": ["ja"]}, expect=202)
            progress = self.wait_for(port, f"/jobs/{job['id']}", "partial")["sets"]["tst"]
            self.assertEqual((progress["unavailable"], progress["languages"]), (1, {"ja": 1, "en": 2}))
            self.assertEqual(front.read_bytes(), japanese)
            self.assertEqual((folder / "After.full.png").read_bytes(), english)

    def test_existing_files_are_adopted_and_only_explicitly_upgraded(self):
        normal = pixel_png(30, 488, 680)
        high = pixel_png(70, 744, 1040)
        scans = {"oak.png": high, "spruce.png": pixel_png(40, 146, 204)}
        upstream = Upstream([printing(1, "en", "Oak", "oak.png"),
                             printing(2, "en", "Spruce", "spruce.png")], scans)
        folder = self.root / "pics" / "cards" / "FT"
        folder.mkdir(parents=True)
        oak = folder / "Oak.full.png"
        spruce = folder / "Spruce.full.png"
        for path in (oak, spruce):
            path.write_bytes(normal)

        with self.server(upstream, "1 C Oak\n2 C Spruce\n") as port:
            def run_job(status, **options):
                job = self.api(port, "POST", "/jobs", {"sets": ["tst"], **options}, expect=202)
                return self.wait_for(port, f"/jobs/{job['id']}", status)["sets"]["tst"]

            for options in ({}, {"min_quality": "normal"}):
                progress = run_job("completed", **options)
                self.assertEqual(progress["cached"], 2)
                self.assertEqual(progress["languages"]["unknown"], 2)
                self.assertEqual(upstream.image_requests, [])

            progress = run_job("partial", min_quality="png")
            self.assertCountEqual(upstream.image_requests, scans)
            self.assertEqual((progress["cached"], progress["downloaded"], progress["below_quality"]), (1, 1, 1))
            self.assertEqual(oak.read_bytes(), high)
            self.assertEqual(spruce.read_bytes(), normal)

            # Once a file meets the floor, asking again does not fetch it.
            scans["spruce.png"] = high
            progress = run_job("completed", min_quality="png")
            self.assertEqual((progress["downloaded"], progress["cached"]), (1, 1))
            self.assertEqual(upstream.image_requests.count("oak.png"), 1)
            self.assertEqual(spruce.read_bytes(), high)

    def test_paused_job_survives_restart_and_keeps_verified_work(self):
        entered, release = threading.Event(), threading.Event()
        png = pixel_png(42)

        def slow_scan(attempt):
            if attempt == 1:
                entered.set()
                if not release.wait(6):
                    raise AssertionError("Test did not release the in-flight request")
            return Response(png)

        upstream = Upstream([printing(i, "en", f"Card {i}", f"{i}.png") for i in range(1, 4)],
                            {"1.png": png, "2.png": slow_scan, "3.png": png})
        rows = "1 C Card 1\n2 C Card 2\n3 C Card 3\n"
        with self.server(upstream, rows) as port:
            job = self.api(port, "POST", "/jobs", {"sets": ["tst"]}, expect=202)
            path = f"/jobs/{job['id']}"
            try:
                self.assertTrue(entered.wait(3))
                self.assertEqual(self.api(port, "POST", path + "/pause", expect=202)["status"], "pausing")
                self.api(port, "POST", path + "/resume", expect=409)
            finally:
                release.set()
            paused = self.wait_for(port, path, "paused")
            self.assertEqual(paused["sets"]["tst"]["processed_images"], 1)
            self.assertEqual(upstream.image_requests, ["1.png", "2.png"])
            self.assertFalse((self.root / "pics/cards/FT/Card 2.full.png").exists())

        with self.server(upstream, rows) as port:
            self.assertEqual(self.api(port, "GET", path), paused)
            self.api(port, "POST", path + "/resume", expect=202)
            job = self.wait_for(port, path, "completed")
            self.assertEqual(job["started_at"], paused["started_at"])
            self.assertEqual((job["sets"]["tst"]["downloaded"], job["sets"]["tst"]["cached"]), (2, 1))
            self.assertEqual(upstream.image_requests, ["1.png", "2.png", "2.png", "3.png"])

    def test_authenticated_idempotent_submission_survives_restart(self):
        upstream = Upstream([printing(1, "en", "Oak", "oak.png")], {"oak.png": pixel_png(80)})
        rows = "1 C Oak\n"
        request = {"sets": ["TST"]}
        headers = {"Authorization": "Bearer private-test-token", "Idempotency-Key": "weekly.tst"}
        with self.server(upstream, rows, token="private-test-token") as port:
            self.assertEqual(self.api(port, "GET", "/health")["status"], "ok")
            self.api(port, "GET", "/jobs", expect=401)
            self.api(port, "POST", "/jobs", request, expect=401)
            job = self.api(port, "POST", "/jobs", request, expect=202, headers=headers)
            job_path = f"/jobs/{job['id']}"
            self.api(port, "POST", job_path + "/pause", expect=401)
            self.wait_for(port, job_path, "completed", headers=headers)
        with self.server(upstream, rows, token="private-test-token") as port:
            replay = self.api(port, "POST", "/jobs", {"sets": ["tst", "TST"]}, expect=202, headers=headers)
            self.assertEqual(replay["id"], job["id"])
            self.api(port, "POST", "/jobs", {**request, "min_quality": "large"}, expect=409, headers=headers)
            self.assertEqual([j["id"] for j in self.api(port, "GET", "/jobs", headers=headers)["jobs"]], [job["id"]])
        self.assertEqual(upstream.image_requests, ["oak.png"])

    def test_v020_checksum_is_preserved_when_database_is_upgraded(self):
        good = pixel_png(80)
        wrong = pixel_png(90)
        folder = self.root / "pics" / "cards" / "FT"
        folder.mkdir(parents=True)
        image = folder / "Oak.full.png"
        image.write_bytes(wrong)
        with closing(sqlite3.connect(self.root / "images.sqlite3")) as db, db:
            db.execute("""CREATE TABLE images (
                       stem TEXT PRIMARY KEY, url TEXT, suffix TEXT NOT NULL,
                       size INTEGER NOT NULL, sha256 TEXT, language TEXT)""")
            db.execute("INSERT INTO images VALUES (?, ?, ?, ?, ?, ?)",
                       ("pics/cards/FT/Oak.full", IMAGE_ORIGIN + "oak.png", ".png",
                        len(good), hashlib.sha256(good).hexdigest(), "en"))
        upstream = Upstream([printing(1, "en", "Oak", "oak.png")], {"oak.png": good})
        with self.server(upstream, "1 C Oak\n") as port:
            job = self.api(port, "POST", "/jobs", {"sets": ["tst"]}, expect=202)
            progress = self.wait_for(port, f"/jobs/{job['id']}", "completed")["sets"]["tst"]
            self.assertEqual((progress["cached"], progress["downloaded"]), (0, 1))
        self.assertEqual(image.read_bytes(), good)

    def test_scan_preview_and_explicit_tidy_then_repair_missing(self):
        card = pixel_png(90)
        upstream = Upstream([printing(1, "en", "Oak", "oak.png"), printing(2, "en", "Pine", "pine.png")],
                            {"pine.png": pixel_png(100)})
        folder = self.root / "pics" / "cards" / "FT"
        folder.mkdir(parents=True)
        legacy = folder / "Oak.fullborder.png"
        canonical = folder / "Oak.full.png"
        legacy.write_bytes(card)
        with self.server(upstream, "1 C Oak\n2 C Pine\n") as port:
            def run(status, **options):
                job = self.api(port, "POST", "/jobs", {"sets": ["tst"], **options}, expect=202)
                return self.wait_for(port, f"/jobs/{job['id']}", status)["sets"]["tst"]

            scan = run("partial", operation="scan", min_quality="large")
            self.assertEqual((scan["missing"], scan["legacy"], scan["below_quality"]), (1, 1, 1))
            self.api(port, "POST", "/jobs", {"sets": ["tst"], "operation": "tidy", "apply": True}, expect=400)
            preview = self.api(port, "POST", "/jobs", {"sets": ["tst"], "operation": "tidy"}, expect=202)
            path = f"/jobs/{preview['id']}"
            progress = self.wait_for(port, path, "partial")["sets"]["tst"]
            self.assertEqual((progress["legacy"], progress["renames"]), (1, 1))
            plan = self.api(port, "GET", path + "/renames")
            self.assertEqual(plan["renames"], [{"set": "tst", "source": "pics/cards/FT/Oak.fullborder.png",
                                               "target": "pics/cards/FT/Oak.full.png"}])
            self.assertEqual(legacy.read_bytes(), card)
            self.assertFalse(canonical.exists())
            self.assertEqual(upstream.image_requests, [])
            applied = self.api(port, "POST", path + "/apply", {}, expect=202)
            tidy = self.wait_for(port, f"/jobs/{applied['id']}", "completed")["sets"]["tst"]
            self.assertEqual(self.api(port, "POST", path + "/apply", {}, expect=202)["id"], applied["id"])
            self.assertEqual(tidy["tidied"], 1)
            self.assertEqual(canonical.read_bytes(), card)
            self.assertFalse(legacy.exists())
            repaired = run("completed")
            self.assertEqual((repaired["cached"], repaired["downloaded"]), (1, 1))
            self.assertEqual(upstream.image_requests, ["pine.png"])

    def test_tidy_plan_pages_and_unreviewed_files_are_not_applied(self):
        card = pixel_png(93)
        before = [f"Card {index:03}" for index in range(101)]
        extra = "Previously absent"
        folder = self.root / "pics" / "cards" / "FT"
        folder.mkdir(parents=True)
        for name in before:
            (folder / (name + ".fullborder.png")).write_bytes(card)
        cards = [printing(i, "en", name, f"{i}.png") for i, name in enumerate([*before, extra], 1)]
        rows = "".join(f"{i} C {name}\n" for i, name in enumerate([*before, extra], 1))
        with self.server(Upstream(cards, {}), rows) as port:
            # Make the first plan span two pages, then replace one source and add a different valid
            # filename after review. Only the remaining reviewed files should move.
            preview = self.api(port, "POST", "/jobs", {"sets": ["tst"], "operation": "tidy"}, expect=202)
            path = f"/jobs/{preview['id']}"
            self.wait_for(port, path, "partial")
            first = self.api(port, "GET", path + "/renames")
            last = self.api(port, "GET", path + f"/renames?offset={first['next_offset']}")
            all_sources = [Path(item["source"]).name for item in first["renames"] + last["renames"]]
            self.assertEqual(all_sources, [name + ".fullborder.png" for name in before])
            self.assertIsNone(last["next_offset"])
            changed = pixel_png(94)
            (folder / (before[0] + ".fullborder.png")).write_bytes(changed)
            (folder / (extra + ".fullborder.png")).write_bytes(card)
            applied = self.api(port, "POST", path + "/apply", {}, expect=202)
            progress = self.wait_for(port, f"/jobs/{applied['id']}", "partial")["sets"]["tst"]
            self.assertEqual((progress["tidied"], progress["failed"]), (100, 1))
        self.assertEqual((folder / (before[0] + ".fullborder.png")).read_bytes(), changed)
        self.assertFalse((folder / (before[0] + ".full.png")).exists())
        self.assertTrue((folder / (extra + ".fullborder.png")).exists())
        self.assertTrue((folder / (before[-1] + ".full.png")).exists())

    def test_saved_sync_list_and_overdue_schedule_survive_restart(self):
        cards = [printing(1, "en", "Oak", "oak.png"),
                 dict(printing(1, "en", "Goblin", "token.png"), set="tsub", set_type="token")]
        sets = [{"code": "tst", "name": "Local Test Set", "set_type": "expansion", "parent_set_code": None,
                 "released_at": "2000-01-01", "digital": False, "card_count": 1},
                {"code": "tsub", "name": "Exact token child", "set_type": "token", "parent_set_code": "tst",
                 "released_at": "2000-01-01", "digital": False, "card_count": 1}]
        upstream = Upstream(cards, {"oak.png": pixel_png(3), "token.png": pixel_png(4)}, sets=sets)
        config = {"sets": ["TST"], "languages": ["zh-CN", "en"], "include_tokens": True, "interval_hours": 1}
        rows = "1 C Oak\n"
        with self.server(upstream, rows) as port:
            self.api(port, "PUT", "/sync-list", config, expect=403, headers={"Origin": "https://another.example"})
            saved = self.api(port, "PUT", "/sync-list", config)
            self.assertIsNotNone(saved["next_run_at"])
            self.assertEqual(saved["next_run_at"], self.api(port, "PUT", "/sync-list", config)["next_run_at"])
            manual = self.api(port, "POST", "/sync-list/run", {}, expect=202, headers={"Idempotency-Key": "test.sync"})
            self.assertEqual(set(manual["sets"]), {"tst", "tsub"})
            self.assertEqual(manual["languages"], ["zhs", "en"])
            self.wait_for(port, f"/jobs/{manual['id']}", "completed")
        with closing(sqlite3.connect(self.root / "images.sqlite3")) as db, db:
            state, = db.execute("SELECT payload FROM sync_list").fetchone()
            db.execute("UPDATE sync_list SET payload = ?", (json.dumps({**json.loads(state), "due": 1}),))
        with self.server(upstream, rows) as port:
            deadline = time.monotonic() + 6
            while True:
                loaded = self.api(port, "GET", "/sync-list")
                if loaded["last_job_id"] != manual["id"] and loaded["last_job_id"] is not None:
                    break
                self.assertLess(time.monotonic(), deadline, loaded)
                time.sleep(0.01)
            automatic = self.wait_for(port, f"/jobs/{loaded['last_job_id']}", "completed")
            self.assertEqual(automatic["source"], "sync_schedule")
            self.assertEqual({name: automatic["sets"][name]["cached"] for name in automatic["sets"]}, {"tst": 1, "tsub": 1})
            self.assertEqual(loaded["sets"], ["tst"])
            self.assertGreater(loaded["next_run_at"], loaded["last_run_at"])
            replay = self.api(port, "POST", "/sync-list/run", {}, expect=202, headers={"Idempotency-Key": "test.sync"})
            self.assertEqual(replay["id"], manual["id"])
            self.assertEqual({job["id"] for job in self.api(port, "GET", "/jobs?summary=1")["jobs"]},
                             {manual["id"], automatic["id"]})
        self.assertCountEqual(upstream.image_requests, ["oak.png", "token.png"])

    def test_retry_submits_only_unfinished_sets(self):
        cards = [printing(1, "en", "Oak", "oak.png"), dict(printing(1, "en", "Pine", "pine.png"), set="other")]
        upstream = Upstream(cards, {"oak.png": pixel_png(5)})
        with self.server(upstream, "1 C Oak\n") as port:
            original = self.api(port, "POST", "/jobs", {"sets": ["tst", "other"]}, expect=202)
            path = f"/jobs/{original['id']}"
            self.wait_for(port, path, "partial")
            upstream.scans["pine.png"] = pixel_png(6)
            retry = self.api(port, "POST", path + "/retry", {}, expect=202)
            fixed = self.wait_for(port, f"/jobs/{retry['id']}", "completed")
            self.assertEqual(list(fixed["sets"]), ["other"])
        self.assertEqual(upstream.image_requests, ["oak.png", "pine.png", "pine.png"])


if __name__ == "__main__":
    unittest.main()
