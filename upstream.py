"""Scryfall transport, on-disk metadata cache, and native image downloads."""

from dataclasses import dataclass
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

from imagefiles import IMAGE_SUFFIXES, QUALITY, ImageSize, InvalidImage, image_size


SEARCH_URL = "https://api.scryfall.com/cards/search"
CACHE_AGE = 24 * 60 * 60
MAX_IMAGE_BYTES = 16 * 1024 * 1024
IMAGE_ATTEMPTS = 4
MAX_RETRY_DELAY = 60
NETWORK_ERRORS = (OSError, http.client.HTTPException, ValueError)


class UpstreamError(Exception):
    pass


class DownloadInterrupted(Exception):
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
    def __init__(self, proxy: str | None = None):
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
        return self.opener.open(request, timeout=20)


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


@dataclass(frozen=True, slots=True)
class Asset:
    stem: str
    sources: tuple[Source, ...]


@dataclass(frozen=True, slots=True)
class SavedImage:
    url: str | None
    suffix: str
    size: int
    sha256: str | None
    language: str | None


class BelowQuality(Exception):
    pass


def _read_local(path: Path, info, previous: SavedImage | None, sources: tuple[Source, ...]):
    with path.open("rb") as file:
        if not os.path.samestat(info, os.fstat(file.fileno())):
            raise OSError(f"Image changed while opening {path.name}")
        suffix = path.suffix
        indexed = previous is not None and previous.suffix == suffix and previous.sha256 is not None
        shape = image_size(file, suffix, info.st_size, check_png_chunks=not indexed)
        file.seek(0)
        digest = hashlib.file_digest(file, "sha256").hexdigest()
    if indexed:
        if digest != previous.sha256:
            raise InvalidImage("Image SHA-256 no longer matches")
        language = previous.language or next((s.language for s in sources if s.url == previous.url), None)
        return SavedImage(previous.url, suffix, info.st_size, digest, language), shape
    return SavedImage(None, suffix, info.st_size, digest, None), shape


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
        block = reply.read(128 * 1024)
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


def _fetch_image(web: Web, path: Path, source: Source, stop, minimum: str | None,
                 local_shapes: tuple[ImageSize, ...]) -> tuple[SavedImage, ImageSize] | None:
    def write(file):
        size, digest, shape = save_image(reply, file, source.suffix, stop)
        if minimum is not None and (not shape.meets(minimum) or not all(shape.improves(old) for old in local_shapes)):
            raise BelowQuality
        check_interrupted(stop)
        # Forge clients do not necessarily share the downloader's UID.
        os.chmod(file.name, 0o644)
        return SavedImage(source.url, source.suffix, size, digest, source.language), shape

    for attempt in range(IMAGE_ATTEMPTS):
        check_interrupted(stop)
        delay = 2 ** attempt
        last_attempt = attempt == IMAGE_ATTEMPTS - 1
        try:
            with web.open(source.url, "cards.scryfall.io", "image/png,image/jpeg") as reply:
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
        except (InvalidImage, OSError, http.client.HTTPException):
            if last_attempt:
                raise
        if stop.wait(delay):
            raise DownloadInterrupted


def download(web: Web, root: Path, asset: Asset, previous: SavedImage | None, stop, minimum: str | None):
    """Inspect Forge's exact keys first; only an explicit minimum may replace a valid image."""
    stem = root / asset.stem
    directory = root
    for component in stem.parent.relative_to(root).parts:
        directory /= component
        try:
            if not stat.S_ISDIR(directory.lstat().st_mode):
                raise OSError(f"Expected a real image directory: {directory}")
        except FileNotFoundError:
            break

    jpg = stem.with_name(stem.name + ".jpg")
    jpeg = None

    def retire_jpeg(saved: SavedImage):
        if saved.suffix == ".png" and jpeg is not None:
            current = jpg.lstat()
            if not os.path.samestat(jpeg, current) or jpeg.st_mtime_ns != current.st_mtime_ns:
                raise OSError(f"Image changed while checking {jpg}")
            # A broken or superseded same-key JPEG would mask this PNG in Forge.
            jpg.unlink()

    local = []
    for suffix in IMAGE_SUFFIXES:
        check_interrupted(stop)
        path = stem.with_name(stem.name + suffix)
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(info.st_mode):
            raise OSError(f"Not a regular file: {path}")
        if suffix == ".jpg":
            jpeg = info
        try:
            saved, shape = _read_local(path, info, previous, asset.sources)
        except InvalidImage:
            continue
        if minimum is None or (shape.meets(minimum) and all(shape.improves(old) for _, old in local)):
            check_interrupted(stop)
            retire_jpeg(saved)
            return "cached", saved, False
        local.append((saved, shape))

    local_shapes = tuple(shape for _, shape in local)
    invalid = None
    for source in asset.sources:
        if local:
            nominal = ImageSize(*QUALITY[source.quality], source.suffix)
            if (not nominal.meets(minimum) or not all(nominal.improves(shape) for shape in local_shapes)
                    or any(source.url == saved.url for saved, _ in local)):
                continue
        path = stem.with_name(stem.name + source.suffix)
        try:
            result = _fetch_image(web, path, source, stop, minimum if local else None, local_shapes)
        except InvalidImage as exc:
            invalid = exc
            continue
        if result is None:
            continue
        saved, shape = result
        retire_jpeg(saved)
        return "downloaded", saved, minimum is not None and not shape.meets(minimum)
    if local:
        saved = local[0][0]
        check_interrupted(stop)
        retire_jpeg(saved)
        return "cached", saved, True
    if invalid is not None:
        raise invalid
    return "unavailable", None, False
