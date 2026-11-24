from base64 import b64decode
from dataclasses import replace
from io import BytesIO
from pathlib import Path
import struct
from tempfile import TemporaryDirectory
from threading import Event
import unittest
import zlib

from scryfall_downloader.forge import Edition, plan_images
from scryfall_downloader.imagefiles import InvalidImage, image_size
from scryfall_downloader.upstream import Asset, Source, download, local_images


ORIGIN = "https://cards.scryfall.io/"
PRINT_ID = "6da045f8-6278-4c84-9d39-025adf0789c1"
OTHER_ID = "4e7a547f-d1b0-4f4e-9a99-3c44fc89c048"
OLD = "2026-07-13T10:04:21Z"
NEW = "2026-08-13T10:04:21Z"
STEM = "pics/cards/FT/Oak.full"

# Single-color native files: 1x2 lossy, 2x3 lossless, 2x3 extended with alpha, 1x2 JPEG.
LOSSY = b64decode("UklGRjwAAABXRUJQVlA4IDAAAADQAQCdASoBAAIAAgA0JaACdLoB+AADsAD+8MQL/yC5YXXI1/8gP+QH/ID/+PIAAAA=")
LOSSLESS = b64decode("UklGRhwAAABXRUJQVlA4TA8AAAAvAYAAAAcQ0f/+ByKi/wEA")
EXTENDED = b64decode("UklGRl4AAABXRUJQVlA4WAoAAAAQAAAAAQAAAgAAQUxQSAcAAAAAgICAgICAAFZQOCAwAAAA0AEAnQEqAgADAAIANCWg"
                    "AnS6AfgAA7AA/vDEC/8guWF1yNf/ID/kB/yA//jyAAAA")
JPEG = b64decode("/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAMCAgICAgMCAgIDAwMDBAYEBAQEBAgGBgUGCQgKCgkICQkKDA8MCgsO"
                 "CwkJDRENDg8QEBEQCgwSExIQEw8QEBD/2wBDAQMDAwQDBAgEBAgQCwkLEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQ"
                 "EBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBD/wAARCAACAAEDAREAAhEBAxEB/8QAFAABAAAAAAAAAAAAAAAAAAAA"
                 "CP/EABQQAQAAAAAAAAAAAAAAAAAAAAD/xAAVAQEBAAAAAAAAAAAAAAAAAAAHCf/EABQRAQAAAAAAAAAAAAAAAAAA"
                 "AAD/2gAMAwEAAhEDEQA/ADoDFU3/2Q==")


def png(red, width=1, height=2):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    pixels = (b"\x00" + bytes((red, 0, 0)) * width) * height
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


def source(name, *, lang="en", suffix=".png", source_id=PRINT_ID, date=OLD):
    return Source(lang, {".png": "png", ".jpg": "large", ".webp": "large"}[suffix], suffix,
                  ORIGIN + name + suffix, source_id, date)


class Reply(BytesIO):
    status = 200

    def __init__(self, body):
        super().__init__(body)
        self.headers = {"Content-Length": str(len(body))}


class CDN:
    def __init__(self, **responses):
        self.responses = responses
        self.calls = []

    def open(self, url, host, accept):
        self.calls.append((url.removeprefix(ORIGIN), host, accept))
        value = self.responses[self.calls[-1][0]]
        return Reply(value() if callable(value) else value)


class ImmediateRetry(Event):
    def wait(self, _timeout=None):
        return self.is_set()


class ImagePipelineTest(unittest.TestCase):
    def setUp(self):
        tmp = TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.stem = self.root / STEM
        self.stem.parent.mkdir(parents=True)
        self.stop = ImmediateRetry()

    def file(self, suffix):
        return self.stem.with_name(self.stem.name + suffix)

    def fetch(self, cdn, sources, previous=None, *, legacy=False, languages=(), **options):
        return download(cdn, self.root, Asset(STEM, tuple(sources), legacy, languages), previous, self.stop,
                        options.pop("minimum", None), **options)

    def test_source_order_gate_and_confirmed_legacy_key(self):
        versions = ("png", "display", "large", "grid", "normal", "jpeg", "thumb", "small")
        en = {"collector_number": "1", "name": "Oak", "lang": "en", "layout": "normal", "id": PRINT_ID,
              "set_type": "expansion", "image_updated_at": OLD, "image_uris": {v: ORIGIN + v for v in versions}}
        ja = dict(en, lang="ja", id=OTHER_ID, image_uris={"thumb": ORIGIN + "ja-thumb"})
        edition = Edition.parse("[metadata]\nCode=TST\nCode2=FT\n[cards]\n1 C Oak\n")

        def plan(webp):
            return plan_images("tst", [en, ja], edition, ["ja", "en"], webp)[0][0]

        off, on = plan(False), plan(True)
        self.assertEqual([s.url for s in off.sources], [ORIGIN + v for v in ("png", "large", "normal", "jpeg", "small")])
        self.assertEqual([s.url for s in on.sources], [ORIGIN + v for v in ("ja-thumb", *versions)])
        self.assertEqual([(s.quality, s.suffix) for s in on.sources], [
            ("small", ".webp"), ("png", ".png"), ("large", ".webp"), ("large", ".jpg"),
            ("normal", ".webp"), ("normal", ".jpg"), ("normal", ".jpg"), ("small", ".webp"), ("small", ".jpg"),
        ])
        self.assertEqual((on.languages, on.legacy_border, on.stem), (("ja", "en"), True, STEM))
        self.assertEqual((on.sources[0].source_id, on.sources[0].image_updated_at), (OTHER_ID, OLD))
        inferred = plan_images("tst", [en], None, ["en"], webp=True)[0][0]
        self.assertFalse(inferred.legacy_border)

    def test_webp_still_containers_and_structural_errors(self):
        for data, dimensions in ((LOSSY, (1, 2)), (LOSSLESS, (2, 3)), (EXTENDED, (2, 3))):
            with self.subTest(dimensions=dimensions):
                shape = image_size(BytesIO(data), ".webp", len(data))
                self.assertEqual((shape.short, shape.long), dimensions)
                self.assertFalse(shape.meets("png"))

        wrong_length = LOSSY[:16] + struct.pack("<I", len(LOSSY)) + LOSSY[20:]
        malformed = (LOSSY[:-2], LOSSY + b"\x00\x00", wrong_length, LOSSLESS[:-1] + b"\x01",
                     EXTENDED[:20] + b"\x12" + EXTENDED[21:], EXTENDED[:24] + b"\x02" + EXTENDED[25:])
        for data in malformed:
            with self.subTest(data=data), self.assertRaises(InvalidImage):
                image_size(BytesIO(data), ".webp", len(data))

    def test_opt_in_retries_corrupt_webp_then_unmasks_verified_file(self):
        image = source("display", suffix=".webp")
        jpeg = self.file(".jpg")
        invalid_png = self.file(".png")
        jpeg.write_bytes(b"broken jpg")
        invalid_png.write_bytes(b"broken png")
        self.file(".webp").write_bytes(b"ignored while the gate is off")
        cdn = CDN()
        self.assertEqual(self.fetch(cdn, [image]), ("unavailable", None, False))
        self.assertEqual(cdn.calls, [])

        def reply():
            self.assertTrue(jpeg.is_file())
            self.assertTrue(invalid_png.is_file())
            return LOSSY[:-2] if len(cdn.calls) == 1 else LOSSY

        cdn.responses["display.webp"] = reply
        outcome, saved, below = self.fetch(cdn, [image], webp=True)
        self.assertEqual((outcome, saved.suffix, below), ("downloaded", ".webp", False))
        self.assertEqual(cdn.calls, [("display.webp", "cards.scryfall.io", "image/webp")] * 2)
        self.assertEqual(self.file(".webp").read_bytes(), LOSSY)
        self.assertFalse(jpeg.exists())
        self.assertFalse(invalid_png.exists())

    def test_same_size_newer_scan_requires_opt_in_and_matching_metadata(self):
        old = source("old")
        cdn = CDN(**{"old.png": png(10)})
        _, previous, _ = self.fetch(cdn, [old])
        cdn.calls.clear()
        newer_same_url = replace(old, image_updated_at=NEW)
        newer = source("new", date=NEW)
        nonqualifying = (source("older", date="2025-01-01T00:00:00Z"), source("equal"),
                         source("unknown-date", date="2026-08-13"), source("other", date=NEW, source_id=OTHER_ID),
                         source("other-lang", lang="ja", date=NEW))
        self.assertEqual(self.fetch(cdn, [newer], previous)[0], "cached")
        self.assertEqual(self.fetch(cdn, [old, *nonqualifying], previous, refresh_scan=True)[0], "cached")
        self.assertEqual(cdn.calls, [])
        cdn.responses["old.png"] = png(20)
        result, saved, _ = self.fetch(cdn, [newer_same_url], previous, refresh_scan=True)
        self.assertEqual((result, saved.source_id, saved.image_updated_at), ("downloaded", PRINT_ID, NEW))
        self.assertEqual(self.file(".png").read_bytes(), png(20))
        self.assertEqual(self.fetch(cdn, [newer], saved, refresh_scan=True)[0], "cached")
        self.assertEqual(cdn.calls, [("old.png", "cards.scryfall.io", "image/png")])

    def test_refresh_language_needs_verified_preference_and_no_quality_loss(self):
        en = source("en")
        ja = source("ja", lang="ja", source_id=OTHER_ID)
        ja_jpg = source("ja", lang="ja", source_id=OTHER_ID, suffix=".jpg")
        cdn = CDN(**{"en.png": png(10), "ja.png": png(20, 1, 1), "ja.jpg": JPEG})
        _, previous, _ = self.fetch(cdn, [en])
        cdn.calls.clear()

        # Exact indexed v0.2.0 receipt can recover metadata; adopting the same bytes cannot.
        legacy_receipt = replace(previous, language=None, source_id=None, image_updated_at=None)
        self.assertEqual(self.fetch(cdn, [en], legacy_receipt)[1], previous)
        self.assertIsNone(self.fetch(cdn, [en], None)[1].language)
        self.assertEqual(self.fetch(cdn, [ja, en], None, refresh_language=True)[0], "cached")
        self.assertEqual(self.fetch(cdn, [ja], previous, languages=("en", "ja"), refresh_language=True)[0], "cached")
        self.assertEqual(self.fetch(cdn, [ja, en], previous)[0], "cached")
        self.assertEqual(cdn.calls, [])

        self.assertEqual(self.fetch(cdn, [ja_jpg, ja], previous, refresh_language=True)[0], "cached")
        self.assertEqual(cdn.calls, [("ja.png", "cards.scryfall.io", "image/png")])
        self.assertEqual(self.file(".png").read_bytes(), png(10))
        cdn.responses["ja.png"] = png(30)
        result, saved, _ = self.fetch(cdn, [ja], previous, languages=("ja",), refresh_language=True)
        self.assertEqual((result, saved.language, self.file(".png").read_bytes()), ("downloaded", "ja", png(30)))

    def test_failed_refresh_keeps_local_and_success_only_retires_maskers(self):
        original = source("old", suffix=".jpg")
        newer = source("new", suffix=".webp", date=NEW)
        cdn = CDN(**{"old.jpg": JPEG, "new.webp": b"not webp"})
        _, previous, _ = self.fetch(cdn, [original])
        legacy = self.file("border.jpg")
        legacy.write_bytes(JPEG)
        self.assertEqual(self.fetch(cdn, [newer], previous, legacy=True, webp=True, refresh_scan=True),
                         ("cached", previous, False))
        self.assertEqual(self.file(".jpg").read_bytes(), JPEG)
        self.assertFalse(self.file(".webp").exists())

        cdn.responses["new.webp"] = LOSSY
        self.assertEqual(self.fetch(cdn, [newer], previous, legacy=True, webp=True, refresh_scan=True)[0], "downloaded")
        self.assertEqual(self.file(".webp").read_bytes(), LOSSY)
        self.assertFalse(self.file(".jpg").exists())
        self.assertEqual(legacy.read_bytes(), JPEG)

    def test_legacy_fallback_records_basename_and_uses_actual_path_for_receipt(self):
        old = source("old")
        cdn = CDN(**{"old.png": png(10)})
        _, canonical_receipt, _ = self.fetch(cdn, [old])
        self.file(".png").unlink()
        self.file(".jpg").write_bytes(b"broken jpg")
        alias = self.file("border.png")
        alias.write_bytes(png(20))
        self.assertEqual(self.fetch(CDN(), [], canonical_receipt), ("unavailable", None, False))
        self.assertTrue(self.file(".jpg").exists())

        result, saved, _ = self.fetch(CDN(), [], canonical_receipt, legacy=True)
        self.assertEqual((result, saved.file_name, saved.language), ("cached", alias.name, None))
        self.assertFalse(self.file(".jpg").exists())
        self.assertEqual(alias.read_bytes(), png(20))
        alias.write_bytes(png(30))
        images = local_images(self.root, Asset(STEM, (), True), saved, self.stop, False)
        self.assertEqual([(image.path, image.saved, image.shape) for image in images], [(alias, None, None)])

    def test_default_does_not_inspect_lower_priority_variants(self):
        self.file(".jpg").write_bytes(JPEG)
        self.file(".png").mkdir()
        self.assertEqual(self.fetch(CDN(), [])[0], "cached")
        with self.assertRaises(OSError):
            local_images(self.root, Asset(STEM, ()), None, self.stop, False)

    def test_concurrent_masker_change_is_not_removed(self):
        jpg = self.file(".jpg")
        jpg.write_bytes(b"old broken jpg")

        def changed():
            jpg.write_bytes(JPEG)
            return LOSSY

        with self.assertRaises(OSError):
            self.fetch(CDN(**{"display.webp": changed}), [source("display", suffix=".webp")], webp=True)
        self.assertEqual(jpg.read_bytes(), JPEG)
        self.assertFalse(self.file(".webp").exists())
