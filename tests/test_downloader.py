from contextlib import closing, contextmanager
from gzip import compress
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
from uuid import UUID
import zlib

from scryfall_downloader.jobs import Jobs
from scryfall_downloader.server import Server


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


class CatalogueUpstream(Upstream):
    data_url = "https://data.scryfall.io/fixture.jsonl.gz"

    def __init__(self, cards, scans):
        super().__init__(cards, scans)
        self.blob = compress(b"\n".join(json.dumps(card).encode() for card in cards))
        self.bulk_requests = []

    def open(self, url, host, accept):
        if url == "https://api.scryfall.com/bulk-data/all-cards":
            self.bulk_requests.append(host)
            return Response(json.dumps({"type": "all_cards", "jsonl_download_uri": self.data_url,
                                        "compressed_size": len(self.blob)}).encode())
        if url == self.data_url:
            self.bulk_requests.append(host)
            return Response(self.blob)
        return super().open(url, host, accept)


class DownloaderTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.editions = self.root / "editions"
        self.editions.mkdir()

    @contextmanager
    def server(self, upstream, rows, *, token=None, archive_all=False):
        (self.editions / "Test.txt").write_text("[metadata]\nCode=TST\nCode2=FT\n[cards]\n" + rows,
                                               encoding="utf-8")
        jobs = Jobs(self.root, 1, self.editions, None)
        jobs.web.open = upstream.open
        with Server(("127.0.0.1", 0), jobs, token) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
            jobs.start(archive_all=archive_all)
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
            conn.request(method, "/api/v1" + path, body, headers)
            with conn.getresponse() as response:
                data = json.load(response)
                self.assertEqual(response.status, expect, data)
                return data

    def test_default_portal_and_versioned_job_location(self):
        upstream = Upstream([printing(1, "en", "Oak", "oak.png")], {"oak.png": pixel_png(16)})
        body = b'{"sets":["tst"]}'
        headers = {"Authorization": "Bearer console-test-token", "Content-Type": "application/json"}
        with self.server(upstream, "1 C Oak\n", token="console-test-token") as port, \
             closing(HTTPConnection("127.0.0.1", port, timeout=3)) as conn:
            conn.request("GET", "/")
            with conn.getresponse() as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(response.getheader("Content-Type"), "text/html; charset=utf-8")
                response.read()
            for method, path, payload in (("GET", "/jobs", None), ("GET", "/health", None),
                                          ("PUT", "/sync-list", body), ("POST", "/jobs", body),
                                          ("GET", "/api/v10/jobs", None)):
                with self.subTest(method=method, path=path):
                    conn.request(method, path, payload, headers)
                    with conn.getresponse() as response:
                        self.assertEqual(response.status, 404, response.read())
            conn.request("POST", "/api/v1/jobs", body, headers)
            with conn.getresponse() as response:
                self.assertEqual(response.status, 202)
                job = json.load(response)
                location = response.getheader("Location")
            self.assertEqual(location, f"/api/v1/jobs/{job['id']}")
            conn.request("GET", location, headers=headers)
            with conn.getresponse() as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(json.load(response)["id"], job["id"])

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

    def test_archive_bootstrap_resumes_with_only_forge_images(self):
        entered, release = threading.Event(), threading.Event()
        ph = pixel_png(60)
        common = pixel_png(10)

        def last_printing(attempt):
            if attempt == 1:
                entered.set()
                if not release.wait(6):
                    raise AssertionError("Test did not release the in-flight request")
            return Response(common)

        cards = []
        for k, code, kind, released, lang, name, scan in (
            (1, "tst", "expansion", "2026-07-01", "en", "Oak", "en.png"),
            (2, "tst", "expansion", "2026-07-01", "zhs", "Oak", "zh.png"),
            (3, "tst", "expansion", "2026-07-01", "ph", "Oak", "ph.png"),
            (4, "tok", "token", "2026-11-01", "en", "Mite", "token.png"),
            (5, "ydg", "alchemy", "2001-01-01", "qya", "Owl", "digital.png"),
            (6, "drf", "draft_innovation", "2000-05-01", "zht", "Pine", "draft.png"),
        ):
            cards.append(dict(printing(1, lang, name, scan), object="card", id=str(UUID(int=k)), set=code,
                              set_type=kind, released_at=released, set_name=code))
        cards.append(dict(cards[0], id=str(UUID(int=7)), collector_number="2", full_art=True,
                          border_color="borderless", image_uris={"png": IMAGE_ORIGIN + "borderless.png"}))
        wide_art = pixel_png(44)
        upstream = CatalogueUpstream(cards, {"token.png": common, "ph.png": ph, "borderless.png": wide_art,
                                             "digital.png": common, "draft.png": last_printing})
        rows = "1 C Oak\n[borderless]\n2 C Oak\n"
        with self.server(upstream, rows, archive_all=True) as port:
            try:
                self.assertTrue(entered.wait(5))
                job, = self.api(port, "GET", "/jobs?summary=1")["jobs"]
                self.assertEqual(job["set_codes"], ["tok", "tst", "ydg", "drf"])
                self.assertEqual(job["sets_finished"], 3)
                self.assertEqual(job["source"], "archive_bootstrap")
                path = f"/jobs/{job['id']}"
                self.api(port, "POST", "/archive/run", {}, expect=409)
                self.api(port, "POST", path + "/pause", expect=202)
            finally:
                release.set()
            paused = self.wait_for(port, path, "paused")

        with self.server(upstream, rows, archive_all=True) as port:
            self.assertEqual(self.api(port, "GET", path), paused)
            self.api(port, "POST", path + "/resume", expect=202)
            completed = self.wait_for(port, path, "completed")
            self.assertEqual(completed["sets"]["tst"]["total_images"], 2)
            pics = self.root / "pics"
            primary = pics / "cards/FT/Oak1.full.png"
            alternate = pics / "cards/FT/Oak2.full.png"
            self.assertEqual(primary.read_bytes(), ph)
            self.assertEqual(alternate.read_bytes(), wide_art)
            self.assertEqual((pics / "tokens/TOK/1_mite.png").read_bytes(), common)
            self.assertEqual({file.relative_to(pics).as_posix() for file in pics.rglob("*.png")}, {
                "cards/FT/Oak1.full.png", "cards/FT/Oak2.full.png", "tokens/TOK/1_mite.png",
                "cards/YDG/Owl.full.png", "cards/DRF/Pine.full.png",
            })
            self.assertEqual({child.name for child in pics.iterdir()}, {"cards", "tokens"})
            self.assertFalse((self.root / "extras").exists())
            self.assertEqual(upstream.bulk_requests, ["api.scryfall.com", "data.scryfall.io"])
            self.assertEqual(upstream.image_requests, ["token.png", "ph.png", "borderless.png",
                                                        "digital.png", "draft.png", "draft.png"])

            primary.write_bytes(common)  # Valid PNG, but wrong checksum for this slot.
            headers = {"Idempotency-Key": "rescan"}
            refresh = self.api(port, "POST", "/archive/run", {}, expect=202, headers=headers)
            repaired = self.wait_for(port, f"/jobs/{refresh['id']}", "completed")
            self.assertEqual(primary.read_bytes(), ph)
            self.assertEqual(alternate.read_bytes(), wide_art)
            self.assertEqual(upstream.image_requests[6:], ["ph.png"])
            self.assertEqual(self.api(port, "POST", "/archive/run", {}, expect=202, headers=headers), repaired)

        with self.server(upstream, rows, archive_all=True) as port:
            self.assertEqual(len(self.api(port, "GET", "/jobs?summary=1")["jobs"]), 2)

    def test_unmapped_variant_is_reported_without_writing_another_image(self):
        cards = [dict(printing(1, "en", "A: B", "correct.png"), object="card", id=str(UUID(int=1))),
                 dict(printing(2, "ph", "A B", "collision.png"), object="card", id=str(UUID(int=2))),
                 dict(printing(3, "en", "A: B", "unmapped.png"), object="card", id=str(UUID(int=3)))]
        artwork = pixel_png(13)
        upstream = CatalogueUpstream(cards, {"correct.png": artwork})
        with self.server(upstream, "1 C A: B\n") as port:
            job = self.api(port, "POST", "/archive/run", {}, expect=202)
            receipt = self.wait_for(port, f"/jobs/{job['id']}", "partial")["sets"]["tst"]
            self.assertEqual(receipt["downloaded"], 1)
            self.assertTrue(any("no unique art slot" in issue for issue in receipt["issues"]))
            self.assertTrue(any("already belongs to" in issue for issue in receipt["issues"]))
            self.assertEqual(upstream.image_requests, ["correct.png"])
            pics = self.root / "pics"
            self.assertEqual({file.relative_to(pics).as_posix(): file.read_bytes() for file in pics.rglob("*.png")},
                             {"cards/FT/A B.full.png": artwork})

    def test_failed_archive_import_has_a_retryable_receipt(self):
        card = dict(printing(1, "en", "Oak", "en.png"), object="card", id=str(UUID(int=1)))
        upstream = CatalogueUpstream([card], {"en.png": pixel_png(12)})
        blob, upstream.blob = upstream.blob, upstream.blob[:-3]
        with self.server(upstream, "1 C Oak\n") as port:
            job = self.api(port, "POST", "/archive/run", {}, expect=202)
            path = f"/jobs/{job['id']}"
            failed = self.wait_for(port, path, "failed")
            self.assertIn("bulk index", failed["error"])
            self.assertEqual(failed["sets"], {})
            upstream.blob = blob
            retry = self.api(port, "POST", path + "/retry", {}, expect=202)
            result = self.wait_for(port, f"/jobs/{retry['id']}", "completed")
            self.assertEqual(list(result["sets"]), ["tst"])

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

    def test_repair_remaps_inferred_art_from_a_v050_database(self):
        scans = {f"en-{number}.png": pixel_png(number * 40) for number in (1, 2, 3)}
        scans.update({f"ja-{number}.png": pixel_png(number * 40 + 1) for number in (1, 2, 3)})
        cards = [dict(printing(n, lang, "Forest", f"{lang}-{n}.png"), id=str(UUID(int=n + offset)))
                 for lang, offset in (("en", 0), ("ja", 10)) for n in (1, 2, 3)]
        upstream = Upstream(cards, scans)
        folder = self.root / "pics" / "cards" / "TST"
        folder.mkdir(parents=True)
        with closing(sqlite3.connect(self.root / "images.sqlite3")) as db, db:
            db.execute("""CREATE TABLE images (
                stem TEXT PRIMARY KEY, url TEXT, suffix TEXT NOT NULL, size INTEGER NOT NULL,
                sha256 TEXT, language TEXT, file_name TEXT, source_id TEXT, image_updated_at TEXT
            )""")
            for index, number in enumerate((2, 3), 1):
                body = scans[f"en-{number}.png"]
                stem = f"Forest{index}.full"
                (folder / f"{stem}.png").write_bytes(body)
                db.execute("INSERT INTO images VALUES (?, ?, ?, ?, ?, ?, NULL, ?, NULL)",
                           (f"pics/cards/TST/{stem}", IMAGE_ORIGIN + f"en-{number}.png",
                            ".png", len(body), hashlib.sha256(body).hexdigest(), "en", str(UUID(int=number))))

        with self.server(upstream, "") as port:
            request = {"sets": ["tst"]}
            scan = self.api(port, "POST", "/jobs", {**request, "operation": "scan"}, expect=202)
            progress = self.wait_for(port, f"/jobs/{scan['id']}", "partial")["sets"]["tst"]
            self.assertEqual((progress["cached"], progress["missing"]), (0, 3))
            self.assertEqual(sum("different printing" in issue for issue in progress["issues"]), 2)
            self.assertEqual((folder / "Forest1.full.png").read_bytes(), scans["en-2.png"])
            self.assertEqual(upstream.image_requests, [])

            job = self.api(port, "POST", "/jobs", request, expect=202)
            progress = self.wait_for(port, f"/jobs/{job['id']}", "completed")["sets"]["tst"]
            self.assertEqual((progress["downloaded"], progress["cached"]), (3, 0))
        for number in (1, 2, 3):
            self.assertEqual((folder / f"Forest{number}.full.png").read_bytes(), scans[f"en-{number}.png"])
        self.assertCountEqual(upstream.image_requests, ["en-1.png", "en-2.png", "en-3.png"])

        # A new language request must not make the correctly bound artwork stale.
        with self.server(upstream, "") as port:
            job = self.api(port, "POST", "/jobs", {**request, "languages": ["ja"]}, expect=202)
            progress = self.wait_for(port, f"/jobs/{job['id']}", "completed")["sets"]["tst"]
            self.assertEqual((progress["downloaded"], progress["cached"], progress["languages"]), (0, 3, {"ja": 0, "en": 3}))
        self.assertCountEqual(upstream.image_requests, ["en-1.png", "en-2.png", "en-3.png"])

    def test_download_leaves_untracked_images_at_inferred_slots_untouched(self):
        original = pixel_png(20)
        image = self.root / "pics" / "cards" / "TST" / "Forest.full.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(original)
        upstream = Upstream([printing(1, "en", "Forest", "forest.png")], {"forest.png": pixel_png(80)})
        with self.server(upstream, "") as port:
            job = self.api(port, "POST", "/jobs", {"sets": ["tst"]}, expect=202)
            progress = self.wait_for(port, f"/jobs/{job['id']}", "failed")["sets"]["tst"]
            self.assertEqual((progress["cached"], progress["downloaded"]), (0, 0))
            self.assertIn("cannot verify the printing for this Forge art slot", progress["errors"][0])
        self.assertEqual(image.read_bytes(), original)
        self.assertEqual(upstream.image_requests, [])

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

    def test_unknown_inferred_legacy_art_is_not_migrated_or_overwritten(self):
        image = self.root / "pics" / "cards" / "TST" / "Forest.full.png"
        image.parent.mkdir(parents=True)
        image.write_bytes(pixel_png(91))
        upstream = Upstream([printing(1, "en", "Forest", "forest.png")], {"forest.png": pixel_png(90)})
        with self.server(upstream, "") as port:
            preview = self.api(port, "POST", "/jobs", {"sets": ["tst"], "operation": "tidy"}, expect=202)
            progress = self.wait_for(port, f"/jobs/{preview['id']}", "partial")["sets"]["tst"]
            self.assertEqual(progress["renames"], 0)
            self.assertTrue(any("left unchanged" in issue for issue in progress["issues"]))
            self.assertEqual(image.read_bytes(), pixel_png(91))

    def test_approved_legacy_rename_is_idempotent_after_restart(self):
        card = pixel_png(90)
        upstream = Upstream([printing(1, "en", "Oak", "oak.png")], {"oak.png": card})
        folder = self.root / "pics" / "cards" / "FT"
        folder.mkdir(parents=True)
        legacy = folder / "Oak.fullborder.png"
        canonical = folder / "Oak.full.png"
        legacy.write_bytes(card)
        with self.server(upstream, "1 C Oak\n") as port:
            preview = self.api(port, "POST", "/jobs", {"sets": ["tst"], "operation": "tidy"}, expect=202)
            path = f"/jobs/{preview['id']}"
            self.wait_for(port, path, "completed")
            applied = self.api(port, "POST", path + "/apply", {}, expect=202)
            self.wait_for(port, f"/jobs/{applied['id']}", "completed")
            self.assertTrue(canonical.exists())
        with self.server(upstream, "1 C Oak\n") as port:
            job = self.api(port, "POST", "/jobs", {"sets": ["tst"], "operation": "tidy"}, expect=202)
            self.wait_for(port, f"/jobs/{job['id']}", "completed")
            self.assertEqual(canonical.read_bytes(), card)
            self.assertFalse(legacy.exists())

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

    def test_token_printing_identity_survives_restart_and_repairs_wrong_art(self):
        token = printing(1, "en", "Mite", "old.png")
        token.update(set_type="token", layout="token", id=str(UUID(int=91)))
        other = dict(token, id=str(UUID(int=92)), collector_number="2")
        scans = {"old.png": pixel_png(15), "new.png": pixel_png(80)}
        upstream = Upstream([dict(token, image_uris={"png": IMAGE_ORIGIN + "old.png"}),
                             dict(other, image_uris={"png": IMAGE_ORIGIN + "new.png"})], scans)
        with self.server(upstream, "") as port:
            job = self.api(port, "POST", "/jobs", {"sets": ["tst"]}, expect=202)
            self.wait_for(port, f"/jobs/{job['id']}", "completed")
        with closing(sqlite3.connect(self.root / "images.sqlite3")) as db:
            row = db.execute("SELECT source_id, printing_key FROM images WHERE stem LIKE 'pics/tokens/%'").fetchone()
            self.assertIsNotNone(row[1])
            self.assertEqual(row[0], token["id"])
            db.execute("UPDATE images SET source_id = ?, printing_key = ? WHERE stem LIKE 'pics/tokens/%'",
                       (other["id"], '["tst","2","Mite",0]'))
        with self.server(upstream, "") as port:
            job = self.api(port, "POST", "/jobs", {"sets": ["tst"]}, expect=202)
            self.wait_for(port, f"/jobs/{job['id']}", "completed")
        self.assertTrue((self.root / "pics/tokens/TST/1_mite.png").exists())

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
