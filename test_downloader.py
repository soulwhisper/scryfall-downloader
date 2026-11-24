from contextlib import closing, contextmanager
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


def pixel_png(red):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes((0, red, 0, 0))))
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
    def __init__(self, cards, scans):
        self.cards = cards
        self.scans = scans
        self.image_requests = []

    def open(self, url, host, _accept):
        if host == "api.scryfall.com":
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
    def server(self, upstream, rows):
        (self.editions / "Test.txt").write_text("[metadata]\nCode=TST\nCode2=FT\n[cards]\n" + rows,
                                               encoding="utf-8")
        jobs = Jobs(self.root, 1, self.editions, None)
        jobs.web.open = upstream.open
        with Server(("127.0.0.1", 0), jobs) as server:
            thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
            jobs.worker.start()
            thread.start()
            try:
                yield server.server_port
            finally:
                server.shutdown()
                thread.join()
                jobs.close()

    def api(self, port, method, path, payload=None, expect=200):
        with closing(HTTPConnection("127.0.0.1", port, timeout=3)) as conn:
            body = None if payload is None else json.dumps(payload).encode()
            headers = {} if body is None else {"Content-Type": "application/json"}
            conn.request(method, path, body, headers)
            with conn.getresponse() as response:
                data = json.load(response)
                self.assertEqual(response.status, expect, data)
                return data

    def wait_for(self, port, path, status):
        deadline = time.monotonic() + 6
        while True:
            job = self.api(port, "GET", path)
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
            job = self.api(port, "POST", "/jobs", {"sets": ["tst"], "languages": ["ja"]}, expect=202)
            progress = self.wait_for(port, f"/jobs/{job['id']}", "partial")["sets"]["tst"]
            self.assertEqual((progress["unavailable"], progress["languages"]), (3, {"ja": 1}))
            self.assertEqual(front.read_bytes(), japanese)
            self.assertEqual((folder / "After.full.png").read_bytes(), english)

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


if __name__ == "__main__":
    unittest.main()
