from gzip import compress
from io import BytesIO
import json
import os
from pathlib import Path
from random import Random
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch
from uuid import UUID

from scryfall_downloader.bulk import BulkCards
from scryfall_downloader.upstream import CACHE_AGE, DownloadInterrupted, UpstreamError


META = "https://api.scryfall.com/bulk-data/all-cards"
DATA = "https://data.scryfall.io/all-cards-20261124000000.jsonl.gz"


def card(number, **fields):
    return {"object": "card", "id": str(UUID(int=number)), "set": "cat", "set_name": "Catalogue",
            "set_type": "expansion", "lang": "en", "released_at": "2026-01-02", "name": "Æther Cat",
            "collector_number": "42★", "layout": "normal", **fields}


def archive(*cards):
    return compress(b"\n".join(json.dumps(row, ensure_ascii=False).encode("utf-8") for row in cards))


class Response(BytesIO):
    status = 200

    def __init__(self, content, on_read=None):
        super().__init__(content)
        self.on_read = on_read

    def read(self, size=-1):
        if size < 0:
            raise AssertionError("bulk reads must be bounded")
        result = super().read(size)
        if self.on_read is not None:
            self.on_read(self.tell())
        return result


class FakeWeb:
    def __init__(self, content, **metadata):
        self.content = content
        self.metadata = {"jsonl_download_uri": DATA, "compressed_size": len(content), **metadata}
        self.requests = []
        self.on_data_read = None
        self.data_status = 200

    def open(self, url, host, accept):
        self.requests.append((url, host, accept))
        if url == META:
            return Response(json.dumps(self.metadata).encode())
        if url == DATA:
            reply = Response(self.content, self.on_data_read)
            reply.status = self.data_status
            return reply
        raise AssertionError(f"Unexpected upstream URL {url}")


class BulkCardsTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name).resolve() / "cache" / "bulk.sqlite3"
        self.stop = Event()

    def seed_stale(self):
        index = BulkCards(FakeWeb(archive(card(1))), self.path)
        index.prepare(self.stop)
        old = self.path.stat().st_mtime - CACHE_AGE - 1
        os.utime(self.path, (old, old))
        return index, self.path.read_bytes()

    def assert_preserved(self, index, original):
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(index.sets(), [("cat", "2026-01-02")])
        self.assertEqual([row["id"] for row in index.cards("cat")], [card(1)["id"]])
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_projects_all_categories_and_keeps_each_print_id(self):
        images = {"normal": "https://cards.scryfall.io/normal/front.jpg",
                  "thumb": "https://cards.scryfall.io/thumb/front.webp",
                  "art_crop": "https://cards.scryfall.io/art_crop/front.jpg"}
        normal = card(8, flavor_name="Dawn Cat", variation=True, promo=True, full_art=False,
                      artist="M. Artist", border_color="borderless", frame_effects=["extendedart"],
                      oversized=True, textless=True, digital=False, promo_types=["starterdeck"],
                      finishes=["foil", "nonfoil"], image_status="highres_scan",
                      image_updated_at="2026-07-02T00:00:00Z", image_uris=images,
                      oracle_text="Omit", legalities={"standard": "not_legal"}, games=["paper"])
        variant = card(7, variation=False, released_at="1993-08-05")
        token = card(4, set="tok", set_type="token", lang="future-lang9", layout="reversible_card",
                     card_faces=[{"name": "前面", "layout": "token", "image_uris": images,
                                  "oracle_text": "Omit face rules"}, {"name": "Back"}])
        digital = card(2, set="arena", set_type="alchemy", lang="en", digital=True, games=["arena"],
                       released_at="2099-07-01")
        image_missing = card(9, set="prm", set_type="promo", released_at=None, lang="grc",
                             image_status="missing", games=[])
        web = FakeWeb(archive(normal, token, digital, image_missing, variant))
        index = BulkCards(web, self.path)

        with self.assertRaisesRegex(RuntimeError, "prepare"):
            index.cards("cat")
        self.assertEqual(web.requests, [])
        index.prepare(self.stop)
        index.prepare(self.stop)
        self.assertEqual(web.requests, [(META, "api.scryfall.com", "application/json"),
                                        (DATA, "data.scryfall.io", "application/gzip")])
        self.assertEqual(index.sets(), [("arena", "2099-07-01"), ("cat", "1993-08-05"),
                                        ("prm", None), ("tok", "2026-01-02")])
        self.assertEqual(index.cards("other"), [])
        self.assertEqual([row["id"] for row in index.cards("cat")], [variant["id"], normal["id"]])
        projected = index.cards("cat")[1]
        expected = {key: value for key, value in normal.items()
                    if key not in ("object", "oracle_text", "legalities", "games")}
        kept_images = {key: images[key] for key in ("normal", "thumb")}
        expected["image_uris"] = kept_images
        self.assertEqual(projected, expected)
        self.assertEqual(index.cards("tok")[0]["lang"], "future-lang9")
        self.assertEqual(index.cards("tok")[0]["card_faces"],
                         [{"name": "前面", "layout": "token", "image_uris": kept_images}, {"name": "Back"}])
        self.assertTrue(index.cards("arena")[0]["digital"])
        self.assertEqual(index.cards("prm")[0]["image_status"], "missing")
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

        offline = FakeWeb(b"")
        restarted = BulkCards(offline, self.path)
        restarted.prepare(self.stop)
        self.assertEqual(restarted.sets(), index.sets())
        self.assertEqual(restarted.cards("cat"), index.cards("cat"))
        self.assertEqual(offline.requests, [])

    def test_successful_refresh_keeps_running_reader_on_its_snapshot(self):
        previous, _ = self.seed_stale()
        replacement = BulkCards(FakeWeb(archive(card(9, set="new"))), self.path)
        replacement.prepare(self.stop)
        previous.prepare(self.stop)
        self.assertEqual(previous.sets(), [("cat", "2026-01-02")])
        self.assertEqual(replacement.sets(), [("new", "2026-01-02")])
        self.assertEqual([row["id"] for row in previous.cards("cat")], [card(1)["id"]])
        self.assertEqual(replacement.cards("cat"), [])

    def test_invalid_fresh_cache_is_replaced_only_by_a_complete_index(self):
        self.path.parent.mkdir()
        self.path.write_bytes(b"not SQLite")
        with self.assertRaises(UpstreamError):
            BulkCards(FakeWeb(compress(b"invalid JSON")), self.path).prepare(self.stop)
        self.assertEqual(self.path.read_bytes(), b"not SQLite")
        index = BulkCards(FakeWeb(archive(card(7, set="ok"))), self.path)
        index.prepare(self.stop)
        self.assertEqual(index.sets(), [("ok", "2026-01-02")])

    def test_invalid_download_does_not_publish_a_partial_index(self):
        previous, original = self.seed_stale()
        data = archive(card(2))
        for description, web, message in (
            ("trailer removed", FakeWeb(data[:-4]), "end-of-stream"),
            ("missing gzip member", FakeWeb(data, compressed_size=len(data) + 12), "compressed size"),
            ("extra bytes", FakeWeb(data + b"\0", compressed_size=len(data)), "compressed size"),
            ("row malformed", FakeWeb(compress(json.dumps(card(2)).encode() + b"\n{")), "row 2"),
            ("unsafe language", FakeWeb(archive(card(2, lang="bad/../code"))), "language code"),
            ("duplicate UUID", FakeWeb(archive(card(2), card(2, set="other"))), "UNIQUE"),
            ("empty archive", FakeWeb(compress(b"")), "contains no cards"),
        ):
            with self.subTest(description=description):
                with self.assertRaisesRegex(UpstreamError, message):
                    BulkCards(web, self.path).prepare(self.stop)
                self.assert_preserved(previous, original)

    def test_archive_bounds_and_origin(self):
        previous, original = self.seed_stale()
        data = archive(card(2))
        for limit, value, message in (("_MAX_LINE", 32, "row exceeds"),
                                      ("_MAX_EXPANDED", 32, "expanded bulk download"),
                                      ("_MAX_COMPRESSED", len(data) - 1, "invalid bulk compressed size")):
            with self.subTest(limit=limit), patch(f"scryfall_downloader.bulk.{limit}", value):
                with self.assertRaisesRegex(UpstreamError, message):
                    BulkCards(FakeWeb(data), self.path).prepare(self.stop)
                self.assert_preserved(previous, original)
        for size in (0, -1, True, str(len(data))):
            with self.subTest(size=size):
                web = FakeWeb(data, compressed_size=size)
                with self.assertRaisesRegex(UpstreamError, "invalid bulk compressed size"):
                    BulkCards(web, self.path).prepare(self.stop)
                self.assertEqual(web.requests, [(META, "api.scryfall.com", "application/json")])
                self.assert_preserved(previous, original)
        for uri in ("http://data.scryfall.io/cards.gz", "https://data.scryfall.io.example/cards.gz",
                    "https://user@data.scryfall.io/cards.gz", "https://data.scryfall.io:444/cards.gz"):
            with self.subTest(uri=uri):
                web = FakeWeb(data, jsonl_download_uri=uri)
                with self.assertRaisesRegex(UpstreamError, "bulk JSONL URL|Upstream URL"):
                    BulkCards(web, self.path).prepare(self.stop)
                self.assertEqual(web.requests, [(META, "api.scryfall.com", "application/json")])
                self.assert_preserved(previous, original)

    def test_cancellation_before_and_during_refresh(self):
        previous, original = self.seed_stale()
        more = Random(37).randbytes(32 * 1024).hex()
        replacement = FakeWeb(archive(card(3), card(4, oracle_text=more)))
        index = BulkCards(replacement, self.path)
        self.stop.set()
        with self.assertRaises(DownloadInterrupted):
            index.prepare(self.stop)
        self.assertEqual(replacement.requests, [])

        self.stop.clear()
        replacement.on_data_read = lambda offset: self.stop.set() if offset > 16 * 1024 else None
        with self.assertRaises(DownloadInterrupted):
            index.prepare(self.stop)
        self.assertEqual([request[0] for request in replacement.requests], [META, DATA])
        self.assert_preserved(previous, original)
        with self.assertRaisesRegex(RuntimeError, "prepare"):
            index.sets()

    def test_partial_http_response_is_rejected(self):
        previous, original = self.seed_stale()
        web = FakeWeb(archive(card(2)))
        web.data_status = 206
        with self.assertRaisesRegex(UpstreamError, "HTTP 206"):
            BulkCards(web, self.path).prepare(self.stop)
        self.assert_preserved(previous, original)

    def test_cache_file_and_directory_symlinks_are_not_followed(self):
        _, original = self.seed_stale()
        other = self.path.parent / "other.sqlite3"
        self.path.rename(other)
        self.path.symlink_to(other)
        web = FakeWeb(archive(card(2)))
        with self.assertRaisesRegex(UpstreamError, "regular file"):
            BulkCards(web, self.path).prepare(self.stop)
        alias = self.path.parent.with_name("alias")
        alias.symlink_to(self.path.parent, target_is_directory=True)
        with self.assertRaisesRegex(UpstreamError, "symlinks"):
            BulkCards(web, alias / other.name).prepare(self.stop)
        self.assertEqual(web.requests, [])
        self.assertEqual(other.read_bytes(), original)
