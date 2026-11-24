"""Build and read immutable, disk-backed snapshots of Scryfall's all-cards export."""

from contextlib import closing, contextmanager
from datetime import date
import gzip
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
from urllib.error import HTTPError
from uuid import UUID
import weakref
import zlib

from .forge import FORMATS
from .protocol import SET_CODE
from .upstream import NETWORK_ERRORS, UpstreamError, Web, check_interrupted, fresh, origin


_METADATA_URL = "https://api.scryfall.com/bulk-data/all-cards"
_DATA_HOST = "data.scryfall.io"
_MAX_METADATA = 256 * 1024
_MAX_COMPRESSED = 2 * 1024**3
_MAX_EXPANDED = 16 * 1024**3
_MAX_LINE = 1024**2
_SCHEMA_VERSION = 1
_LANGUAGE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")
_RELEASE_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_IMAGE_KEYS = tuple(key for key, _quality, _suffix in FORMATS)
_TEXT_FIELDS = ("flavor_name", "image_status", "image_updated_at")
_FLAG_FIELDS = ("variation", "promo", "full_art", "textless", "oversized", "digital")
_LIST_FIELDS = ("promo_types", "finishes")


def _reject_constant(value):
    raise ValueError(f"invalid JSON constant {value}")


def _decode(raw):
    return json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)


@contextmanager
def _response(web, url, host, accept):
    try:
        with web.open(url, host, accept) as reply:
            if reply.status != 200:
                raise UpstreamError(f"Scryfall bulk request returned HTTP {reply.status}")
            yield reply
    except HTTPError as exc:
        with exc:
            raise UpstreamError(f"Scryfall bulk request returned HTTP {exc.code}") from exc


class _SizedDownload:
    """Limit wire reads and count bytes independently of HTTP framing."""

    def __init__(self, reply, expected, stop):
        self.reply = reply
        self.expected = expected
        self.stop = stop
        self.count = 0

    def read(self, size):
        check_interrupted(self.stop)
        data = self.reply.read(min(size, self.expected - self.count + 1))
        check_interrupted(self.stop)
        self.count += len(data)
        if self.count > self.expected:
            raise ValueError("bulk download exceeds its published compressed size")
        return data


def _text(row, key):
    value = row.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"invalid {key}")
    return value


def _optional_text(source, output, key):
    if (value := source.get(key)) is not None:
        if not isinstance(value, str):
            raise ValueError(f"invalid {key}")
        output[key] = value


def _project_images(source, output):
    if (images := source.get("image_uris")) is not None:
        if not isinstance(images, dict):
            raise ValueError("invalid image_uris")
        selected = {}
        for key in _IMAGE_KEYS:
            _optional_text(images, selected, key)
        output["image_uris"] = selected


def _project(row):
    if not isinstance(row, dict) or row.get("object") != "card":
        raise ValueError("expected a card object")
    ident = UUID(_text(row, "id"))
    code = _text(row, "set")
    if not SET_CODE.fullmatch(code):
        raise ValueError("invalid set code")
    lang = _text(row, "lang")
    if len(lang) > 32 or not _LANGUAGE.fullmatch(lang):
        raise ValueError("invalid language code")
    released = row.get("released_at")
    if released is not None:
        if not isinstance(released, str) or not _RELEASE_DATE.fullmatch(released):
            raise ValueError("invalid released_at")
        date.fromisoformat(released)

    card = {key: _text(row, key) for key in ("set_name", "set_type", "collector_number", "name", "layout")}
    card.update(id=str(ident), set=code.lower(), lang=lang, released_at=released)
    for key in _TEXT_FIELDS:
        _optional_text(row, card, key)
    for key in _FLAG_FIELDS:
        if (value := row.get(key)) is not None:
            if not isinstance(value, bool):
                raise ValueError(f"invalid {key}")
            card[key] = value
    for key in _LIST_FIELDS:
        if (value := row.get(key)) is not None:
            if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
                raise ValueError(f"invalid {key}")
            card[key] = value
    _project_images(row, card)
    if (faces := row.get("card_faces")) is not None:
        if not isinstance(faces, list):
            raise ValueError("invalid card_faces")
        card["card_faces"] = []
        for face in faces:
            if not isinstance(face, dict):
                raise ValueError("invalid card face")
            projected = {"name": _text(face, "name")}
            _optional_text(face, projected, "layout")
            _project_images(face, projected)
            card["card_faces"].append(projected)
    return ident.bytes, card["set"], released, json.dumps(card, ensure_ascii=False, separators=(",", ":"))


def _rows(reply, expected, stop):
    download = _SizedDownload(reply, expected, stop)
    expanded = count = 0
    with gzip.GzipFile(fileobj=download) as archive:
        while True:
            check_interrupted(stop)
            line = archive.readline(min(_MAX_LINE, _MAX_EXPANDED - expanded) + 1)
            if not line:
                break
            expanded += len(line)
            if len(line) > _MAX_LINE:
                raise ValueError("bulk JSONL row exceeds the size limit")
            if expanded > _MAX_EXPANDED:
                raise ValueError("expanded bulk download exceeds the size limit")
            count += 1
            try:
                row = _project(_decode(line))
            except (ValueError, RecursionError) as exc:
                raise ValueError(f"invalid bulk JSONL row {count}: {exc}") from exc
            yield row
    if download.count != expected:
        raise ValueError("bulk download does not match its published compressed size")
    if not count:
        raise ValueError("bulk download contains no cards")


class BulkCards:
    def __init__(self, web: Web, path: Path):
        self.web = web
        self.path = Path(os.path.abspath(path))
        self._db: sqlite3.Connection | None = None

    def _regular_cache(self):
        for directory in reversed(self.path.parents):
            try:
                mode = directory.lstat().st_mode
            except FileNotFoundError:
                return False
            if not stat.S_ISDIR(mode):
                raise UpstreamError(f"Bulk cache must use real directories, not symlinks: {directory}")
        try:
            mode = self.path.lstat().st_mode
        except FileNotFoundError:
            return False
        if not stat.S_ISREG(mode):
            raise UpstreamError(f"Bulk cache must be a regular file: {self.path}")
        return True

    def _reader(self, path):
        db = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, check_same_thread=False)
        try:
            db.execute("PRAGMA trusted_schema = OFF")
            if (db.execute("PRAGMA user_version").fetchone() != (_SCHEMA_VERSION,)
                    or db.execute("SELECT id, set_code, released_at, payload FROM cards LIMIT 1").fetchone() is None
                    or db.execute("SELECT code, released_at FROM sets LIMIT 1").fetchone() is None):
                raise sqlite3.DatabaseError("invalid bulk cache schema or empty index")
        except BaseException:
            db.close()
            raise
        return db

    def _build(self, path, stop):
        with _response(self.web, _METADATA_URL, "api.scryfall.com", "application/json") as reply:
            check_interrupted(stop)
            raw = reply.read(_MAX_METADATA + 1)
            check_interrupted(stop)
            if len(raw) > _MAX_METADATA:
                raise ValueError("bulk metadata exceeds the size limit")
            metadata = _decode(raw)
        if not isinstance(metadata, dict):
            raise ValueError("expected a bulk metadata object")
        uri = metadata.get("jsonl_download_uri")
        expected = metadata.get("compressed_size")
        if not isinstance(uri, str) or origin(uri) != _DATA_HOST:
            raise UpstreamError(f"Expected a bulk JSONL URL on {_DATA_HOST}")
        if type(expected) is not int or not 0 < expected <= _MAX_COMPRESSED:
            raise ValueError("invalid bulk compressed size")

        with closing(sqlite3.connect(path)) as db:
            db.set_progress_handler(stop.is_set, 10_000)
            # An unfinished staging database is never published or reused.
            db.execute("PRAGMA journal_mode = OFF")
            db.execute("PRAGMA temp_store = FILE")
            db.execute("PRAGMA cache_size = -8192")
            with db:
                db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")
                db.execute("""CREATE TABLE cards (
                    id BLOB NOT NULL UNIQUE, set_code TEXT NOT NULL, released_at TEXT, payload TEXT NOT NULL,
                    PRIMARY KEY (set_code, id)
                ) WITHOUT ROWID""")
                check_interrupted(stop)
                with _response(self.web, uri, _DATA_HOST, "application/gzip") as reply:
                    db.executemany("INSERT INTO cards VALUES (?, ?, ?, ?)", _rows(reply, expected, stop))
                db.execute("CREATE TABLE sets (code TEXT PRIMARY KEY, released_at TEXT) WITHOUT ROWID")
                db.execute("INSERT INTO sets SELECT set_code, MIN(released_at) FROM cards GROUP BY set_code")

    def prepare(self, stop):
        """Pin a fresh index for this run, downloading a replacement only when needed."""
        check_interrupted(stop)
        if self._db is not None:
            return
        try:
            if self._regular_cache() and fresh(self.path):
                try:
                    self._db = self._reader(self.path)
                except sqlite3.DatabaseError:
                    pass  # A damaged or obsolete cache is not authoritative.
            if self._db is None:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with tempfile.TemporaryDirectory(dir=self.path.parent, prefix=".scryfall-bulk-") as temp:
                    staging = Path(temp) / "cards.sqlite3"
                    self._build(staging, stop)
                    self._reader(staging).close()
                    self._regular_cache()
                    check_interrupted(stop)
                    os.replace(staging, self.path)
                self._regular_cache()
                self._db = self._reader(self.path)
        except (*NETWORK_ERRORS, EOFError, zlib.error, RecursionError, sqlite3.Error) as exc:
            check_interrupted(stop)
            raise UpstreamError(f"Could not prepare Scryfall bulk index: {exc}") from exc
        # The snapshot is owned by the run; closing it must not require a separate public step.
        weakref.finalize(self, self._db.close)

    def _query(self, sql, parameters=()):
        if self._db is None:
            raise RuntimeError("BulkCards.prepare must succeed before reading the index")
        try:
            with closing(self._db.execute(sql, parameters)) as rows:
                yield from rows
        except sqlite3.Error as exc:
            raise UpstreamError(f"Could not read Scryfall bulk index: {exc}") from exc

    def sets(self) -> list[tuple[str, str | None]]:
        return list(self._query("SELECT code, released_at FROM sets ORDER BY code"))

    def cards(self, code: str) -> list[dict]:
        rows = self._query("SELECT payload FROM cards WHERE set_code = ? ORDER BY id", (code,))
        try:
            return [json.loads(payload) for (payload,) in rows]
        except ValueError as exc:
            raise UpstreamError(f"Invalid card in Scryfall bulk index: {exc}") from exc
