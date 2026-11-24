import hashlib
import os
from pathlib import Path
import stat
import struct
import tempfile
from threading import Event
import unittest
from unittest.mock import patch
import zlib

from maintenance import maintain
from upstream import Asset, DownloadInterrupted, SavedImage, Source


def pixel_png(red):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes((0, red, 0, 0))))
            + chunk(b"IEND", b""))


class MaintenanceTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.folder = self.root / "pics" / "cards" / "FT"
        self.folder.mkdir(parents=True)
        self.asset = Asset("pics/cards/FT/Oak.full", (), legacy_border=True)
        self.legacy = self.folder / "Oak.fullborder.png"
        self.canonical = self.folder / "Oak.full.png"
        self.image = pixel_png(90)
        self.stop = Event()

    def maintain(self, *, asset=None, previous=None, minimum=None, tidy=False, apply=False):
        return maintain(self.root, asset or self.asset, previous, self.stop, minimum, False,
                        tidy=tidy, apply=apply)

    def test_scan_distinguishes_missing_from_unavailable(self):
        self.assertEqual(self.maintain().outcome, "unavailable")
        source = Source("en", "png", ".png", "https://cards.scryfall.io/oak.png", None, None)
        with_source = Asset(self.asset.stem, (source,), legacy_border=True)
        self.assertEqual(self.maintain(asset=with_source).outcome, "missing")
        self.assertEqual(list(self.folder.iterdir()), [])

    def test_scan_reports_damage_in_lookup_order(self):
        self.canonical.write_bytes(self.image)
        jpeg = self.canonical.with_suffix(".jpg")
        jpeg.write_bytes(b"not a JPEG")
        result = self.maintain()
        self.assertEqual(result.outcome, "corrupt")
        self.assertIn(str(jpeg), result.issue)
        self.assertEqual(jpeg.read_bytes(), b"not a JPEG")
        self.assertEqual(self.canonical.read_bytes(), self.image)

    def test_checksum_mismatch_blocks_tidy(self):
        self.legacy.write_bytes(self.image)
        digest = hashlib.sha256(pixel_png(91)).hexdigest()
        prev = SavedImage(None, ".png", len(self.image), digest, None, self.legacy.name)
        result = self.maintain(previous=prev, tidy=True, apply=True)
        self.assertEqual(result.outcome, "corrupt")
        self.assertTrue(result.legacy)
        self.assertIn("checksum mismatch", result.issue)
        self.assertTrue(self.legacy.exists())
        self.assertFalse(self.canonical.exists())

    def test_preview_is_read_only_and_apply_preserves_inode_and_metadata(self):
        self.legacy.write_bytes(self.image)
        self.legacy.chmod(0o640)
        original = self.legacy.stat()
        prev = SavedImage("https://cards.scryfall.io/oak.png", ".png", original.st_size,
                          hashlib.sha256(self.image).hexdigest(), "en", self.legacy.name,
                          "6a27379a-0e31-4b57-ab8f-5912363bea12", "2026-01-02T00:00:00Z")
        preview = self.maintain(previous=prev, minimum="small", tidy=True)
        self.assertEqual(preview.outcome, "cached")
        self.assertTrue(preview.below_quality)
        self.assertTrue(preview.legacy)
        self.assertIn(str(self.canonical), preview.issue)
        self.assertIsNone(preview.saved)
        self.assertTrue(self.legacy.exists())
        self.assertFalse(self.canonical.exists())

        result = self.maintain(previous=prev, minimum="small", tidy=True, apply=True)
        self.assertEqual(result.outcome, "tidied")
        self.assertTrue(result.below_quality)
        self.assertIsNone(result.issue)
        self.assertEqual(result.saved, SavedImage(prev.url, prev.suffix, prev.size, prev.sha256,
                                                  prev.language, None, prev.source_id, prev.image_updated_at))
        self.assertFalse(self.legacy.exists())
        self.assertEqual(self.canonical.read_bytes(), self.image)
        current = self.canonical.stat()
        self.assertTrue(os.path.samestat(original, current))
        self.assertEqual((stat.S_IMODE(current.st_mode), current.st_uid, current.st_gid, current.st_nlink),
                         (0o640, original.st_uid, original.st_gid, 1))

    def test_tidy_rejects_exact_and_ambiguous_names(self):
        self.legacy.write_bytes(self.image)
        rival = pixel_png(91)
        for name in ("Oak.full.png", "OAK.full.PNG", "Oak.full.webp", "Oak.fullborder.PNG"):
            with self.subTest(name=name):
                path = self.folder / name
                path.write_bytes(rival)
                result = self.maintain(tidy=True, apply=True)
                self.assertEqual(result.outcome, "cached")
                self.assertIn(str(path), result.issue)
                self.assertEqual(self.legacy.read_bytes(), self.image)
                self.assertEqual(path.read_bytes(), rival)
                path.unlink()
        self.assertFalse(self.canonical.exists())

    def test_tidy_leaves_unconfirmed_keys_alone(self):
        self.legacy.write_bytes(self.image)
        result = self.maintain(asset=Asset(self.asset.stem, ()), tidy=True, apply=True)
        self.assertEqual(result.outcome, "unavailable")
        self.assertTrue(self.legacy.exists())
        self.assertFalse(self.canonical.exists())

    def test_tidy_rejects_hardlinked_files(self):
        self.legacy.write_bytes(self.image)
        alias = self.folder / "alias.png"
        os.link(self.legacy, alias)
        result = self.maintain(tidy=True, apply=True)
        self.assertEqual(result.outcome, "cached")
        self.assertIn("hardlinked", result.issue)
        self.assertEqual(alias.read_bytes(), self.image)
        self.assertTrue(self.legacy.exists())
        self.assertFalse(self.canonical.exists())

    def test_unsafe_paths_and_directory_errors_propagate(self):
        self.legacy.symlink_to(self.folder / "missing.png")
        with self.assertRaises(OSError):
            self.maintain(tidy=True, apply=True)
        self.assertTrue(self.legacy.is_symlink())
        self.legacy.unlink()
        self.legacy.write_bytes(self.image)
        with patch("maintenance.os.listdir", side_effect=PermissionError("cannot list directory")):
            with self.assertRaises(PermissionError):
                self.maintain(tidy=True, apply=True)
        self.assertFalse(self.canonical.exists())

    def test_cancellation_before_link_leaves_original_name(self):
        self.legacy.write_bytes(self.image)
        listdir = os.listdir

        def interrupt(fd):
            self.stop.set()
            return listdir(fd)

        with patch("maintenance.os.listdir", side_effect=interrupt), self.assertRaises(DownloadInterrupted):
            self.maintain(tidy=True, apply=True)
        self.assertTrue(self.legacy.exists())
        self.assertFalse(self.canonical.exists())

    def test_racing_destination_is_never_overwritten(self):
        self.legacy.write_bytes(self.image)
        link = os.link
        rival = pixel_png(91)

        def race(*args, **kwargs):
            self.canonical.write_bytes(rival)
            return link(*args, **kwargs)

        with patch("maintenance.os.link", side_effect=race):
            result = self.maintain(tidy=True, apply=True)
        self.assertEqual(result.outcome, "cached")
        self.assertIn(str(self.canonical), result.issue)
        self.assertEqual(self.canonical.read_bytes(), rival)
        self.assertEqual(self.legacy.read_bytes(), self.image)

    def test_source_replaced_after_link_leaves_both_images(self):
        self.legacy.write_bytes(self.image)
        link = os.link
        rival = pixel_png(91)

        def race(*args, **kwargs):
            link(*args, **kwargs)
            self.legacy.unlink()
            self.legacy.write_bytes(rival)

        with patch("maintenance.os.link", side_effect=race):
            result = self.maintain(tidy=True, apply=True)
        self.assertEqual(result.outcome, "cached")
        self.assertIsNone(result.saved)
        self.assertIn("changed during tidy", result.issue)
        self.assertEqual(self.legacy.read_bytes(), rival)
        self.assertEqual(self.canonical.read_bytes(), self.image)
