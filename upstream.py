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


SEARCH_URL = "https://api.scryfall.com/cards/search"
CACHE_AGE = 24 * 60 * 60
MAX_IMAGE_BYTES = 16 * 1024 * 1024
IMAGE_ATTEMPTS = 4
MAX_RETRY_DELAY = 60
NETWORK_ERRORS = (OSError, http.client.HTTPException, ValueError)
SIGNATURES = {
    ".png": (b"\x89PNG\r\n\x1a\n", b"\x00\x00\x00\x00IEND\xaeB`\x82"),
    ".jpg": (b"\xff\xd8\xff", b"\xff\xd9"),
}


class UpstreamError(Exception):
    pass


class InvalidImage(UpstreamError):
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
    suffix: str
    url: str


@dataclass(frozen=True, slots=True)
class Asset:
    stem: str
    sources: tuple[Source, ...]


@dataclass(frozen=True, slots=True)
class SavedImage:
    url: str
    suffix: str
    size: int
    sha256: str | None

    def matches(self, path: Path) -> bool:
        if self.sha256 is None:  # Receipts from the old size/mtime index must be downloaded again.
            return False
        try:
            info = path.stat()
            if info.st_size != self.size or not stat.S_ISREG(info.st_mode):
                return False
            with path.open("rb") as file:
                return hashlib.file_digest(file, "sha256").hexdigest() == self.sha256
        except FileNotFoundError:
            return False


def save_image(reply, file, suffix: str, stop) -> tuple[int, str]:
    start, end = SIGNATURES[suffix]
    if reply.status != 200:
        raise InvalidImage(f"Expected a full image; got HTTP {reply.status}")
    raw_length = reply.headers.get("Content-Length")
    try:
        length = int(raw_length) if raw_length is not None else None
    except ValueError:
        raise InvalidImage("Invalid image Content-Length") from None
    if length is not None and length > MAX_IMAGE_BYTES:
        raise UpstreamError("Image exceeds the 16 MiB limit")
    head = reply.read(len(start))
    if head != start:
        raise InvalidImage("Image content does not match its advertised format")
    file.write(head)
    digest = hashlib.sha256(head)
    size = len(head)
    tail = head
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
        tail = (tail + block)[-len(end):] if len(block) < len(end) else block[-len(end):]
    if tail != end or (length is not None and size != length):
        raise InvalidImage("Incomplete image body")
    return size, digest.hexdigest()


def _retry_after(value: str | None) -> float:
    if not value:
        return 0
    if value.isascii() and value.isdecimal():
        return float(value)
    try:
        return parsedate_to_datetime(value).timestamp() - time.time()
    except (ValueError, OverflowError, OSError):
        return 0


def _fetch_image(web: Web, path: Path, source: Source, stop) -> SavedImage | None:
    for attempt in range(IMAGE_ATTEMPTS):
        check_interrupted(stop)
        delay = 2 ** attempt
        last_attempt = attempt == IMAGE_ATTEMPTS - 1
        try:
            with web.open(source.url, "cards.scryfall.io", "image/png,image/jpeg") as reply:
                size, digest = atomic_write(path, lambda file: save_image(reply, file, source.suffix, stop))
            return SavedImage(source.url, source.suffix, size, digest)
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


def download(web: Web, root: Path, asset: Asset, previous: SavedImage | None, stop):
    """Prefer the best valid image; never publish a partial response."""
    invalid = None
    for source in asset.sources:
        check_interrupted(stop)
        path = root / (asset.stem + source.suffix)
        if previous and previous.url == source.url and previous.suffix == source.suffix and previous.matches(path):
            return "cached", source, previous
        try:
            saved = _fetch_image(web, path, source, stop)
        except InvalidImage as exc:
            invalid = exc
            continue
        if saved is None:
            continue

        if previous and previous.suffix != source.suffix:
            # Forge may choose .jpg ahead of .png. Remove only our superseded file.
            (root / (asset.stem + previous.suffix)).unlink(missing_ok=True)
        return "downloaded", source, saved
    if invalid is not None:
        raise invalid
    return "unavailable", None, None
