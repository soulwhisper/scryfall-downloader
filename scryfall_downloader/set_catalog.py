"""Cache the public Scryfall set catalogue independently of card searches."""

from datetime import date
import json
from pathlib import Path
import threading
import time
from urllib.error import HTTPError

from .protocol import SET_CODE
from .upstream import NETWORK_ERRORS, UpstreamError, Web, atomic_write, fresh


SETS_URL = "https://api.scryfall.com/sets"
MAX_CATALOG_BYTES = 4 * 1024 * 1024
FIELDS = ("code", "name", "set_type", "parent_set_code", "released_at", "digital", "card_count")


def _decode(file):
    raw = file.read(MAX_CATALOG_BYTES + 1)
    if len(raw) > MAX_CATALOG_BYTES:
        raise ValueError("catalogue exceeds the size limit")
    return json.loads(raw)


def _normalize(rows, *, cached=False):
    if not isinstance(rows, list) or not rows:
        raise ValueError("expected a nonempty list of sets")
    result = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict) or (cached and not all(field in row for field in FIELDS)):
            raise ValueError("invalid set record")
        code, name, kind, parent, released, digital, count = (row.get(field) for field in FIELDS)
        if (not isinstance(code, str) or not SET_CODE.fullmatch(code)
                or not isinstance(name, str) or not name.strip()
                or not isinstance(kind, str) or not kind.strip()
                or (parent is not None and (not isinstance(parent, str) or not SET_CODE.fullmatch(parent)))
                or type(digital) is not bool or type(count) is not int or count < 0):
            raise ValueError("invalid set record")
        if released is not None and (not isinstance(released, str) or date.fromisoformat(released).isoformat() != released):
            raise ValueError("invalid set release date")
        # Reject invalid Unicode escapes before the API tries to serialize the result.
        name.encode("utf-8")
        kind.encode("utf-8")
        code = code.lower()
        if code in seen:
            raise ValueError(f"duplicate set code {code}")
        seen.add(code)
        result.append(dict(zip(FIELDS, (code, name, kind, parent.lower() if parent else None, released, digital, count))))
    return result


def expand_sets(rows, explicit, *, include_tokens, new_releases_days, today):
    catalog = {row["code"]: row for row in rows}
    selected = dict.fromkeys(explicit)
    unknown = selected.keys() - catalog.keys()
    if unknown:
        raise ValueError("Unknown Scryfall set code(s): " + ", ".join(map(repr, sorted(unknown))))
    if new_releases_days is not None:
        for code, row in sorted(catalog.items()):
            if row.get("digital") is not False or row.get("set_type") not in ("core", "expansion"):
                continue
            released = row.get("released_at")
            if released is None:
                continue
            try:
                age = (today - date.fromisoformat(released)).days
            except (ValueError, TypeError):
                raise ValueError(f"Invalid release date for {code}") from None
            if 0 <= age <= new_releases_days:
                selected[code] = None
    if include_tokens:
        parents = set(selected)
        for code, row in sorted(catalog.items()):
            if row.get("set_type") == "token" and row.get("parent_set_code") in parents:
                selected[code] = None
    return list(selected)


class SetCatalog:
    def __init__(self, web: Web, path: Path):
        self.web = web
        self.path = path
        self.lock = threading.Lock()
        self.next_request = 0.0
        self.backoff_until = 0.0

    def _cached(self):
        if fresh(self.path):
            try:
                with self.path.open("rb") as file:
                    return _normalize(_decode(file), cached=True)
            except (FileNotFoundError, ValueError, RecursionError):
                pass  # A missing or corrupt cache is not authoritative.
        return None

    def read(self) -> list[dict]:
        if (sets := self._cached()) is not None:
            return sets
        with self.lock:
            if (sets := self._cached()) is not None:
                return sets
            now = time.monotonic()
            if now < self.backoff_until:
                raise UpstreamError("Scryfall rate-limited the set catalogue; try again later")
            if now < self.next_request:
                time.sleep(self.next_request - now)
            self.next_request = time.monotonic() + 0.55
            try:
                with self.web.open(SETS_URL, "api.scryfall.com", "application/json") as reply:
                    if reply.status != 200:
                        raise UpstreamError(f"Scryfall set catalogue returned HTTP {reply.status}")
                    page = _decode(reply)
                if (not isinstance(page, dict) or page.get("object") != "list"
                        or page.get("has_more", False) is not False):
                    raise ValueError("expected a complete Scryfall set list")
                sets = _normalize(page.get("data"))
            except HTTPError as exc:
                with exc:
                    if exc.code == 429:
                        value = exc.headers.get("Retry-After", "") if exc.headers else ""
                        delay = max(30, int(value)) if value.isascii() and value.isdecimal() and len(value) <= 9 else 30
                        self.backoff_until = time.monotonic() + delay
                        raise UpstreamError(f"Scryfall rate-limited the set catalogue; retry after {delay}s") from exc
                    raise UpstreamError(f"Scryfall set catalogue returned HTTP {exc.code}") from exc
            except (ValueError, RecursionError) as exc:
                raise UpstreamError(f"Invalid Scryfall set catalogue: {exc}") from exc
            except NETWORK_ERRORS as exc:
                raise UpstreamError(f"Could not fetch the Scryfall set catalogue: {exc}") from exc

            data = json.dumps(sets, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if len(data) > MAX_CATALOG_BYTES:
                raise UpstreamError("Normalized Scryfall set catalogue exceeds the size limit")
            atomic_write(self.path, lambda file: file.write(data))
            return sets
