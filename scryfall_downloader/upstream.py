"""Scryfall transport, on-disk metadata cache, and native image downloads."""

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
import hashlib
import http.client
import json
import os
from pathlib import Path
import ssl
import stat
import tempfile
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, HTTPSHandler, ProxyHandler, Request, build_opener

from .imagefiles import IMAGE_SUFFIXES, QUALITY, ImageSize, InvalidImage, image_size


SEARCH_URL = "https://api.scryfall.com/cards/search"
CACHE_AGE = 24 * 60 * 60
MAX_IMAGE_BYTES = 16 * 1024 * 1024
IMAGE_ATTEMPTS = 4
MAX_RETRY_DELAY = 60
NETWORK_ERRORS = (OSError, http.client.HTTPException, ValueError)
IMAGE_ACCEPT = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}
IMAGE_READ_FLAGS = (os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                    | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))


class UpstreamError(Exception):
    pass


class DownloadInterrupted(Exception):
    pass


class _ImageTransportError(UpstreamError):
    pass


def check_interrupted(stop):
    if stop.is_set():
        raise DownloadInterrupted


def origin(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "https" or parts.username is not None or parts.port not in (None, 443):
        raise UpstreamError("Upstream URL must use HTTPS without credentials or a custom port")
    return parts.hostname or ""


class SameOriginRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if origin(req.full_url) != origin(newurl):
            raise UpstreamError("Upstream redirected to a different host")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Web:
    def __init__(self, proxy: str | None = None, *, timeout: float = 20):
        self.timeout = timeout
        handlers = [SameOriginRedirect(), HTTPSHandler(context=ssl.create_default_context())]
        if proxy is not None:
            try:
                parts = urlsplit(proxy)
                if (parts.scheme != "http" or not parts.hostname or parts.port == 0
                        or parts.path not in ("", "/") or parts.query or parts.fragment):
                    raise ValueError
            except ValueError:
                raise ValueError("--proxy must be an HTTP proxy URL such as http://proxy.example:3128") from None
            handlers.append(ProxyHandler({"https": proxy}))
        # Without an explicit proxy, urllib reads the standard proxy environment variables.
        self.opener = build_opener(*handlers)

    def open(self, url: str, host: str, accept: str):
        if origin(url) != host:
            raise UpstreamError(f"Expected a URL on {host}")
        request = Request(url, headers={"User-Agent": "ForgeScryfallDownloader/1.0", "Accept": accept})
        return self.opener.open(request, timeout=self.timeout)


def fresh(path: Path) -> bool:
    try:
        return time.time() - path.stat().st_mtime < CACHE_AGE
    except FileNotFoundError:
        return False


def atomic_write(path: Path, write):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(dir=path.parent, prefix=".scryfall-", suffix=".part", delete=False)
    try:
        with tmp:
            result = write(tmp)
        os.replace(tmp.name, path)
        return result
    finally:
        Path(tmp.name).unlink(missing_ok=True)


class Scryfall:
    def __init__(self, web: Web, cache: Path, stop):
        self.web = web
        self.cache = cache
        self.stop = stop
        self.next_request = 0.0

    def cards(self, code: str, languages: list[str]) -> list[dict]:
        check_interrupted(self.stop)
        # Include English as a naming/coverage reference, even for a strict non-English job.
        requested = sorted({"en", *languages})
        cache_file = self.cache / f"{code}-{'-'.join(requested)}.json"
        if fresh(cache_file):
            try:
                return json.loads(cache_file.read_bytes())
            except (UnicodeError, json.JSONDecodeError):
                pass  # A damaged cache is not authoritative.

        query = " or ".join(f"lang:{lang}" for lang in requested)
        params = urlencode({
            "q": f"set:{code} ({query})", "unique": "prints", "order": "set",
            "include_extras": "true", "include_multilingual": "true", "include_variations": "true",
        })
        url = f"{SEARCH_URL}?{params}"
        cards = []
        seen = set()
        while url:
            if url in seen or urlsplit(url).path != "/cards/search":
                raise UpstreamError("Invalid or repeated Scryfall pagination URL")
            is_first_page = not seen
            seen.add(url)
            if self.stop.wait(max(0, self.next_request - time.monotonic())):
                raise DownloadInterrupted
            self.next_request = time.monotonic() + 0.55
            try:
                with self.web.open(url, "api.scryfall.com", "application/json") as reply:
                    page = json.load(reply)
            except HTTPError as exc:
                with exc:
                    if exc.code == 404 and is_first_page:
                        break
                    if exc.code == 429:
                        retry_after = exc.headers.get("Retry-After", "")
                        delay = max(30, int(retry_after)) if retry_after.isdecimal() else 30
                        self.next_request = time.monotonic() + delay
                        raise UpstreamError(f"Scryfall rate-limited requests; backing off for {delay}s") from exc
                    raise UpstreamError(f"Scryfall search returned HTTP {exc.code}") from exc
            if page.get("object") != "list" or not isinstance(page.get("data"), list):
                raise UpstreamError("Expected a Scryfall card search result")
            cards.extend(card for card in page["data"] if card["set"] == code and card["lang"] in requested)
            url = page.get("next_page") if page.get("has_more") else None
            if page.get("has_more") and not url:
                raise UpstreamError("Scryfall omitted the next results page")

        check_interrupted(self.stop)
        atomic_write(cache_file, lambda file: file.write(json.dumps(cards, ensure_ascii=False).encode("utf-8")))
        return cards


@dataclass(frozen=True, slots=True)
class Source:
    language: str
    quality: str
    suffix: str
    url: str
    source_id: str | None = None
    image_updated_at: str | None = None


@dataclass(frozen=True, slots=True)
class Asset:
    stem: str
    sources: tuple[Source, ...]
    legacy_border: bool = False
    languages: tuple[str, ...] = ()
    printing_key: str | None = None
    inferred: bool = False


@dataclass(frozen=True, slots=True)
class SavedImage:
    url: str | None
    suffix: str
    size: int
    sha256: str | None
    language: str | None
    file_name: str | None = None
    source_id: str | None = None
    image_updated_at: str | None = None
    printing_key: str | None = None


@dataclass(frozen=True, slots=True)
class LocalImage:
    path: Path
    info: os.stat_result
    saved: SavedImage | None
    shape: ImageSize | None
    legacy: bool
    art_match: bool | None = True

    @property
    def art_issue(self) -> str | None:
        if self.art_match is None:
            return "cannot verify the printing for this Forge art slot; file left unchanged"
        if not self.art_match:
            return "image belongs to a different printing or face"
        return None


class BelowQuality(Exception):
    pass


def _same_image(expected: os.stat_result, actual: os.stat_result) -> bool:
    return (os.path.samestat(expected, actual) and expected.st_mode == actual.st_mode
            and expected.st_size == actual.st_size and expected.st_mtime_ns == actual.st_mtime_ns
            and expected.st_ctime_ns == actual.st_ctime_ns)


def _unchanged(path: Path, expected: os.stat_result | None):
    try:
        current = path.lstat()
    except FileNotFoundError:
        if expected is None:
            return
    else:
        if expected is not None and _same_image(expected, current):
            return
    raise OSError(f"Image changed while checking {path}")


def _read_local(path: Path, info: os.stat_result, previous: SavedImage | None,
                sources: tuple[Source, ...], legacy: bool) -> tuple[SavedImage, ImageSize]:
    suffix = path.suffix
    file_name = path.name if legacy else None
    indexed = (previous is not None and previous.suffix == suffix and previous.sha256 is not None
               and previous.file_name == file_name)
    with os.fdopen(os.open(path, IMAGE_READ_FLAGS), "rb") as file:
        if not _same_image(info, os.fstat(file.fileno())):
            raise OSError(f"Image changed while opening {path}")
        shape = image_size(file, suffix, info.st_size, check_png_chunks=not indexed)
        file.seek(0)
        digest = hashlib.file_digest(file, "sha256").hexdigest()
        if not _same_image(info, os.fstat(file.fileno())):
            raise OSError(f"Image changed while reading {path}")
    if not indexed:
        return SavedImage(None, suffix, info.st_size, digest, None, file_name), shape
    if digest != previous.sha256:
        raise InvalidImage("Image SHA-256 no longer matches")
    language, source_id, updated = previous.language, previous.source_id, previous.image_updated_at
    if previous.url and (language is None or source_id is None or updated is None):
        for source in sources:
            if (source.url == previous.url and source.suffix == suffix
                    and language in (None, source.language) and source_id in (None, source.source_id)):
                language = language or source.language
                source_id = source_id or source.source_id
                updated = updated or source.image_updated_at
                break
    return SavedImage(previous.url, suffix, info.st_size, digest, language, file_name, source_id, updated,
                      previous.printing_key), shape


def _image_path(stem: Path, suffix: str) -> Path:
    return stem.with_name(stem.name + suffix)


def _image_stem(root: Path, asset: Asset) -> Path:
    relative = Path(asset.stem)
    if not relative.name or relative.is_absolute() or ".." in relative.parts:
        raise OSError(f"Unsafe image stem: {asset.stem}")
    directory = root
    for component in (None, *relative.parent.parts):
        if component is not None:
            directory /= component
        try:
            if not stat.S_ISDIR(directory.lstat().st_mode):
                raise OSError(f"Expected a real image directory: {directory}")
        except FileNotFoundError:
            break
    return root / relative


def _check_art(asset: Asset, saved: SavedImage) -> tuple[SavedImage, bool | None]:
    key = asset.printing_key
    if key is None or saved.printing_key == key:
        return saved, True
    if saved.url is None:
        # Manually installed images can use a confirmed Forge key, not a guess.
        return saved, None if asset.inferred else True

    # Backfill pre-upgrade receipts from the exact CDN asset, including its
    # face. Ignore cache-busting query strings, not the format or path.
    old_url = saved.url.partition("?")[0]
    same_language = same_print = False
    for source in asset.sources:
        if (saved.source_id is None or saved.source_id == source.source_id) and source.url.partition("?")[0] == old_url:
            return replace(saved, printing_key=key), True
        if source.source_id is not None:
            same_language |= source.language == saved.language
            same_print |= source.source_id == saved.source_id
    if saved.printing_key is not None or (saved.source_id is not None and same_language and not same_print):
        return saved, False
    # A legacy source filtered out by the current language policy is not
    # evidence of wrong art. Do not adopt it or overwrite it on speculation.
    return saved, None


def _iter_local(stem: Path, asset: Asset, previous: SavedImage | None, stop, webp: bool):
    suffixes = IMAGE_SUFFIXES if webp else IMAGE_SUFFIXES[:-1]
    stems = (stem, _image_path(stem, "border")) if asset.legacy_border else (stem,)
    for key in stems:
        legacy = key != stem
        for suffix in suffixes:
            check_interrupted(stop)
            path = _image_path(key, suffix)
            try:
                info = path.lstat()
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(info.st_mode):
                raise OSError(f"Not a regular file: {path}")
            art_match = True
            try:
                saved, shape = _read_local(path, info, previous, asset.sources, legacy)
            except InvalidImage:
                saved = shape = None
            else:
                saved, art_match = _check_art(asset, saved)
            yield LocalImage(path, info, saved, shape, legacy, art_match)


def local_images(root: Path, asset: Asset, previous: SavedImage | None, stop, webp: bool) -> list[LocalImage]:
    """Inspect every existing Forge lookup candidate in order, including invalid images."""
    return list(_iter_local(_image_stem(root, asset), asset, previous, stop, webp))


def save_image(reply, file, suffix: str, stop) -> tuple[int, str, ImageSize]:
    if reply.status != 200:
        raise InvalidImage(f"Expected a full image; got HTTP {reply.status}")
    raw_length = reply.headers.get("Content-Length")
    try:
        length = int(raw_length) if raw_length is not None else None
    except ValueError:
        raise InvalidImage("Invalid image Content-Length") from None
    if length is not None and length > MAX_IMAGE_BYTES:
        raise UpstreamError("Image exceeds the 16 MiB limit")

    digest = hashlib.sha256()
    size = 0
    while True:
        check_interrupted(stop)
        try:
            block = reply.read(128 * 1024)
        except (OSError, http.client.HTTPException) as exc:
            raise _ImageTransportError(str(exc)) from exc
        if not block:
            break
        size += len(block)
        if size > MAX_IMAGE_BYTES:
            raise UpstreamError("Image exceeds the 16 MiB limit")
        file.write(block)
        digest.update(block)
    if length is not None and size != length:
        raise InvalidImage("Incomplete image body")
    return size, digest.hexdigest(), image_size(file, suffix, size)


def _retry_after(value: str | None) -> float:
    if not value:
        return 0
    if value.isascii() and value.isdecimal():
        return float(value)
    try:
        return parsedate_to_datetime(value).timestamp() - time.time()
    except (ValueError, OverflowError, OSError):
        return 0


def _fetch_image(web: Web, path: Path, source: Source, stop, accepts, before_replace) -> tuple[SavedImage, ImageSize] | None:
    def write(file):
        size, digest, shape = save_image(reply, file, source.suffix, stop)
        if not accepts(shape):
            raise BelowQuality
        check_interrupted(stop)
        # Forge clients do not necessarily share the downloader's UID.
        os.chmod(file.name, 0o644)
        before_replace()
        saved = SavedImage(source.url, source.suffix, size, digest, source.language,
                           source_id=source.source_id, image_updated_at=source.image_updated_at)
        return saved, shape

    for attempt in range(IMAGE_ATTEMPTS):
        check_interrupted(stop)
        delay = 2 ** attempt
        last_attempt = attempt == IMAGE_ATTEMPTS - 1
        try:
            try:
                reply = web.open(source.url, "cards.scryfall.io", IMAGE_ACCEPT[source.suffix])
            except HTTPError:
                raise
            except (OSError, http.client.HTTPException) as exc:
                raise _ImageTransportError(str(exc)) from exc
            with reply:
                before_replace()
                return atomic_write(path, write)
        except BelowQuality:
            return None
        except HTTPError as exc:
            with exc:
                if exc.code in (404, 410):
                    return None
                delay = max(delay, _retry_after(exc.headers.get("Retry-After")))
                if (last_attempt or delay > MAX_RETRY_DELAY
                        or not (exc.code in (408, 429) or 500 <= exc.code < 600)):
                    raise UpstreamError(f"Image CDN returned HTTP {exc.code}") from exc
        except (InvalidImage, _ImageTransportError):
            if last_attempt:
                raise
        if stop.wait(delay):
            raise DownloadInterrupted


def _scan_date(value: str | None) -> datetime | None:
    if value is None:
        return None
    try:
        date = datetime.fromisoformat(value)
    except ValueError:
        return None
    return date if date.utcoffset() == timedelta(0) else None


def _masking_paths(stem: Path, selected: Path, webp: bool):
    for suffix in IMAGE_SUFFIXES if webp else IMAGE_SUFFIXES[:-1]:
        path = _image_path(stem, suffix)
        if path == selected:
            break
        yield path


def _retire_masking(paths: tuple[Path, ...], known: dict[Path, LocalImage]):
    for path in paths:
        # A broken or superseded canonical variant must not mask the chosen image in Forge.
        image = known.get(path)
        if image is not None:
            _unchanged(path, image.info)
            path.unlink()


def download(
    web: Web, root: Path, asset: Asset, previous: SavedImage | None, stop, minimum: str | None,
    webp: bool = False, refresh_language: bool = False, refresh_scan: bool = False,
) -> tuple[str, SavedImage | None, bool]:
    """Reuse a valid image for the planned printing, or commit a verified replacement."""
    stem = _image_stem(root, asset)
    known = {}
    valid = []
    first_valid = selected = None
    stale = None
    legacy_seen = False
    for image in _iter_local(stem, asset, previous, stop, webp):
        known[image.path] = image
        # Do not choose a legacy image masked by another legacy name: never remove aliases.
        selectable = not image.legacy or not legacy_seen
        legacy_seen |= image.legacy
        if image.shape is None:
            continue
        if image.art_match is None:
            raise UpstreamError(f"{image.path}: {image.art_issue}")
        if not image.art_match:
            stale = image
            continue
        if selectable and first_valid is None:
            first_valid = image
        if selectable and selected is None and (minimum is None or (
            image.shape.meets(minimum) and all(image.shape.improves(old.shape) for old in valid)
        )):
            selected = image
            if not (refresh_language or refresh_scan):
                break
        valid.append(image)

    if selected is None:
        selected = first_valid

    def paths_to_retire(path):
        paths = tuple(_masking_paths(stem, path, webp))
        if stale is not None and not stale.legacy and stale.path != path and stale.path not in paths:
            paths += (stale.path,)
        return paths

    def cached():
        check_interrupted(stop)
        masking = paths_to_retire(selected.path)
        if any(path in known for path in masking):
            _unchanged(selected.path, selected.info)
            for path in masking:
                _unchanged(path, known[path].info if path in known else None)
            _retire_masking(masking, known)
        return "cached", selected.saved, minimum is not None and not selected.shape.meets(minimum)

    if selected is not None and not (refresh_language or refresh_scan) and (
        minimum is None or selected.shape.meets(minimum)
    ):
        return cached()

    sources = asset.sources if webp else tuple(source for source in asset.sources if source.suffix != ".webp")
    rank = {}
    if refresh_language:
        languages = asset.languages or dict.fromkeys(source.language for source in sources)
        rank = {language: idx for idx, language in enumerate(languages)}
    old = selected.saved if selected else None
    old_date = _scan_date(old.image_updated_at) if old and refresh_scan else None
    below_quality = minimum is not None and selected is not None and not selected.shape.meets(minimum)
    failure = None
    for source in sources:
        path = _image_path(stem, source.suffix)
        masking = paths_to_retire(path)
        refresh = False
        if selected:
            same_url = any(source.url == image.saved.url for image in valid)
            preferred = (refresh_language and old.language is not None
                         and rank.get(source.language, len(rank)) < rank.get(old.language, len(rank)))
            updated = (_scan_date(source.image_updated_at)
                       if old_date is not None and old.source_id is not None and old.source_id == source.source_id
                       and old.language is not None and old.language == source.language else None)
            newer = updated is not None and updated > old_date
            # Normally reuse an exact URL, except when the API proves its contents changed.
            if same_url and not newer:
                continue
            refresh = preferred or newer
            if not refresh:
                if not below_quality:
                    continue
                nominal = ImageSize(*QUALITY[source.quality], source.suffix)
                if not nominal.meets(minimum) or not all(nominal.improves(image.shape) for image in valid):
                    continue

        shapes = tuple(image.shape for image in valid
                       if image is selected or image.path == path or image.path in masking) if refresh else ()
        if refresh and source.suffix != ".png" and any(shape.suffix == ".png" for shape in shapes):
            continue

        def accepts(shape):
            if selected is None:
                return True
            if minimum is not None and not shape.meets(minimum):
                return False
            if refresh:
                return all(shape.short >= prior.short and shape.long >= prior.long for prior in shapes)
            return all(shape.improves(image.shape) for image in valid)

        def before_replace():
            _image_stem(root, asset)
            for candidate in (*masking, path):
                _unchanged(candidate, known[candidate].info if candidate in known else None)
            if selected is not None and selected.path != path and selected.path not in masking:
                _unchanged(selected.path, selected.info)

        try:
            result = _fetch_image(web, path, source, stop, accepts, before_replace)
        except (InvalidImage, UpstreamError) as exc:
            failure = exc
            continue
        if result is None:
            continue
        saved, shape = result
        _retire_masking(masking, known)
        saved = replace(saved, printing_key=asset.printing_key) if asset.printing_key is not None else saved
        return "downloaded", saved, minimum is not None and not shape.meets(minimum)
    if selected is not None:
        return cached()
    if failure is not None:
        raise failure
    if stale is not None:
        raise UpstreamError(f"{stale.path}: {stale.art_issue}; no usable replacement")
    return "unavailable", None, False
