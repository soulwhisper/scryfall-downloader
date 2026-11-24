"""Map Scryfall printings and faces to Forge's local image keys."""

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import unicodedata
from urllib.error import HTTPError
from urllib.parse import quote
from uuid import UUID

from .upstream import Asset, NETWORK_ERRORS, Source, UpstreamError, Web, atomic_write, fresh


EDITIONS_URL = "https://raw.githubusercontent.com/Card-Forge/forge/master/forge-gui/res/editions/"
BAD_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
SHORT_CODE = re.compile(r"[A-Za-z0-9_-]{1,16}\Z")
CARD_SECTIONS = frozenset("""cards|special slot|precon product|borderless|etched|showcase|full art|extended art|
alternate art|retro frame|buy a box|promo|prerelease promo|bundle|box topper|jumpstart|rebalanced|
eternal|conjured|scheme|printsheets""".replace("\n", "").split("|"))
CARD_ROW = re.compile(r"(?:(\S*\d\S*)\s+)?(?:[SCURML]\s+)?([^@$]+?)(?: @[^$]*)?(?: \$\{(.+)\})?$")
TOKEN_ROW = re.compile(r"(\S*\d\S*)\s+([^@]+?)(?: @.*)?$")
VARIANT_PARAMS = re.compile(r'"(flavorname|variant)"\s*:\s*"([^"]+)"', re.I)
FORMATS = (("png", "png", ".png"), ("display", "large", ".webp"), ("large", "large", ".jpg"),
           ("grid", "normal", ".webp"), ("normal", "normal", ".jpg"), ("jpeg", "normal", ".jpg"),
           ("thumb", "small", ".webp"), ("small", "small", ".jpg"))
TWO_SIDES = frozenset({"transform", "modal_dfc", "double_faced_token", "battle", "art_series", "reversible_card"})
TOKEN_LAYOUTS = frozenset({"token", "double_faced_token", "emblem"})


def filename(name: str) -> str:
    # Forge/MWS uses unaccented Oracle names (not localized printed_name).
    name = unicodedata.normalize("NFD", name.translate(str.maketrans("Łł", "Ll")))
    name = "".join(char for char in name if unicodedata.category(char) != "Mn")
    return BAD_FILENAME_CHARS.sub("", name).strip() or "Unnamed"


def collector_key(number: str) -> tuple[str, str]:
    digits = re.sub(r"[^0-9]", "", number)
    remainder = re.sub(r"[0-9]", "", number)
    value = int(digits or "0")
    padded = f"{value:05d}"
    return (padded + remainder if value and number.startswith(digits) else remainder + padded, number)


@dataclass(frozen=True, slots=True)
class EditionCard:
    name: str
    variant: str
    flavor_name: str
    art_suffix: str


@dataclass(slots=True)
class Edition:
    code: str
    folder: str
    scryfall: str
    tokens_code: str
    cards: dict[str, list[EditionCard]]
    tokens: dict[str, list[str]]

    @classmethod
    def parse(cls, text: str):
        metadata = {}
        rows = defaultdict(list)
        tokens = defaultdict(list)
        section = ""
        for line in text.lstrip("\ufeff").splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "//")):
                continue
            if line.startswith("[") and line.endswith("]"):
                section = line[1:-1].casefold()
            elif section == "metadata":
                key, sep, value = line.partition("=")
                if sep:
                    metadata[key.strip().casefold()] = value.strip()
            elif section in CARD_SECTIONS and (match := CARD_ROW.fullmatch(line)):
                number, name, params = match.groups()
                extra = {key.lower(): value for key, value in VARIANT_PARAMS.findall(params or "")}
                rows[name.strip()].append((number or "50000", extra.get("variant", ""), extra.get("flavorname", "")))
            elif section == "tokens" and (match := TOKEN_ROW.fullmatch(line)):
                tokens[match[1]].append(match[2].strip())

        code = metadata.get("code", "")
        folder = metadata.get("code2", code)
        if not SHORT_CODE.fullmatch(code) or not SHORT_CODE.fullmatch(folder):
            return None
        scryfall = metadata.get("scryfallcode", code).lower()
        cards = defaultdict(list)
        for name, prints in rows.items():
            counts = Counter(variant for _, variant, _ in prints)
            for index, (number, variant, flavor_name) in enumerate(
                sorted(prints, key=lambda row: collector_key(row[0]))
            ):
                count = counts[variant] if variant else len(prints)
                suffix = str(index % count + 1) if count > 1 else ""
                cards[number].append(EditionCard(name, variant, flavor_name, suffix))
        return cls(code, folder, scryfall, metadata.get("tokenscode", "t" + scryfall).lower(), cards, tokens)


class ForgeEditions:
    def __init__(self, web: Web, cache: Path, local: Path | None):
        self.web = web
        self.cache = cache
        self.local = local
        self.by_code = {}
        if local is not None:
            files = sorted(local.glob("*.txt"))
            if not files:
                raise ValueError(f"No Forge edition .txt files in {local}")
            for path in files:
                if edition := Edition.parse(path.read_text(encoding="utf-8")):
                    if edition.cards:
                        if edition.scryfall not in self.by_code or edition.code.lower() == edition.scryfall:
                            self.by_code[edition.scryfall] = edition
                    if edition.tokens:
                        self.by_code.setdefault(edition.tokens_code, edition)

    def find(self, code: str, card: dict) -> tuple[Edition | None, str | None]:
        if self.local is not None:
            if edition := self.by_code.get(code):
                return edition, None
            return None, "No matching Forge edition file; filenames inferred from Scryfall."

        set_name = card["set_name"]
        if card["set_type"] == "token":
            set_name = set_name.removesuffix(" Tokens")
        # Forge's resource filenames typically omit punctuation illegal on Windows.
        set_name = BAD_FILENAME_CHARS.sub("", set_name)
        key = hashlib.sha256(set_name.encode()).hexdigest()
        path = self.cache / (key + ".txt")
        try:
            if fresh(path):
                raw = path.read_bytes()
            else:
                with self.web.open(EDITIONS_URL + quote(set_name, safe="") + ".txt", "raw.githubusercontent.com", "text/plain") as reply:
                    raw = reply.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise UpstreamError("Forge edition file exceeds 2 MiB")
                atomic_write(path, lambda file: file.write(raw))
            edition = Edition.parse(raw.decode("utf-8"))
            if edition and code in (edition.scryfall, edition.tokens_code):
                return edition, None
        except HTTPError as exc:
            with exc:
                if exc.code != 404:
                    return None, f"Forge edition lookup returned HTTP {exc.code}; filenames inferred."
        except (UpstreamError, *NETWORK_ERRORS) as exc:
            return None, f"Forge edition lookup failed ({exc}); filenames inferred."
        return None, "No matching Forge edition file; filenames inferred from Scryfall."


def face_names(card: dict) -> list[str]:
    faces = card.get("card_faces") or []
    layout = card["layout"]
    if layout in TWO_SIDES and faces:
        return [face["name"] for face in faces]
    if layout == "split" and faces:
        return ["".join(face["name"] for face in faces)]
    if layout == "flip" and len(faces) == 2:
        return [face["name"] for face in faces]
    if layout in ("adventure", "prepare") and faces:
        return [faces[0]["name"]]
    return [card["name"]]


def _image_slot(card: dict, side: int) -> int | None:
    faces = card.get("card_faces") or []
    if side < len(faces) and (card["layout"] in TWO_SIDES
                             or (not card.get("image_uris") and faces[side].get("image_uris"))):
        return side
    return None


def _printing_sources(card: dict, slot: int | None, webp: bool) -> tuple[Source, ...]:
    if card.get("image_status") in ("missing", "placeholder"):
        return ()
    images = (card if slot is None else card["card_faces"][slot]).get("image_uris") or {}
    ident = card.get("id")
    updated = card.get("image_updated_at")
    try:
        source_id = str(UUID(ident)) if isinstance(ident, str) else None
    except ValueError:
        source_id = None
    sources = []
    seen = set()
    for version, quality, suffix in FORMATS:
        if suffix == ".webp" and not webp:
            continue
        if (url := images.get(version)) and url not in seen:
            seen.add(url)
            sources.append(Source(card["lang"], quality, suffix, url, source_id,
                                  updated if isinstance(updated, str) else None))
    return tuple(sources)


def _unique_sources(groups) -> tuple[Source, ...]:
    by_url = {}
    for group in groups:
        for source in group:
            by_url.setdefault(source.url, source)
    return tuple(by_url.values())


def image_sources(prints: dict[str, dict], side: int, languages: list[str],
                  webp: bool = False) -> tuple[Source, ...]:
    return _unique_sources(_printing_sources(card, _image_slot(card, side), webp)
                           for lang in languages if (card := prints.get(lang)))


def _ordered_slots(prints):
    return sorted(prints, key=lambda key: (collector_key(key[0]), key[1]))


def _art_indexes(representatives):
    arts = defaultdict(list)
    for (number, _name), card in representatives.items():
        arts[face_names(card)[0]].append(number)
    return {(name, number): str(i) if len(numbers) > 1 else ""
            for name, numbers in arts.items() for i, number in enumerate(numbers, 1)}


def _edition_entry(edition, number, full_name, first_name, flavor, *, archive):
    if not edition:
        return None
    rows = edition.cards.get(number, ())
    if archive and flavor:
        # A flavor-name row is stronger evidence than the shared Oracle name.
        for row in rows:
            if row.flavor_name == flavor:
                return row
    for row in rows:
        if row.name in (full_name, first_name) or (row.flavor_name and row.flavor_name == flavor):
            return row
    return None


def _forge_keys(code, card, edition, indexes, *, archive=False):
    number = card["collector_number"]
    names = face_names(card)
    layout = card["layout"]
    token_set = card["set_type"] == "token"
    faces = card.get("card_faces") or []
    entry = _edition_entry(edition, number, card["name"], names[0], card.get("flavor_name"), archive=archive)
    art = entry.art_suffix if entry else indexes.get((names[0], number), "")
    folder = edition.folder if edition else code.upper()
    for side, name in enumerate(names):
        face_layout = faces[side].get("layout", layout) if layout == "reversible_card" and faces else layout
        if token_set or face_layout in TOKEN_LAYOUTS:
            token_number = number if side == 0 else number + "☇"
            slugs = edition.tokens.get(token_number) if edition else None
            if slugs:
                stem = f"pics/tokens/{edition.code}/{filename(token_number)}_{filename(slugs[0])}"
            else:
                stem = f"pics/tokens/{code.upper()}/{filename(token_number)}_{filename(name).replace(' ', '_')}"
            yield side, name, stem, bool(slugs), False
        else:
            if side == 0 and layout != "split":
                if entry:
                    name = entry.flavor_name or (f"{entry.name} {entry.variant}" if entry.variant else entry.name)
                else:
                    name = card.get("flavor_name") or name
                if (layout in TWO_SIDES or layout in ("adventure", "prepare")) and " // " in name:
                    name = names[0]
            infix = "" if face_layout == "planar" else ".full"
            stem = f"pics/cards/{folder}/{filename(name)}{art}{infix}"
            yield side, name, stem, entry is not None, entry is not None and bool(infix)


def _special_priority(card):
    lang = card["lang"]
    if lang == "ph":
        return 0
    if lang == "ja" and "jpwalker" in (card.get("promo_types") or ()):
        return 1
    return 2


def _candidate_rank(card, language_rank):
    lang = card["lang"]
    return _special_priority(card), language_rank.get(lang, len(language_rank)), lang, card.get("id") or ""


def plan_images(code: str, cards: list[dict], edition: Edition | None,
                languages: list[str], webp: bool = False) -> tuple[list[Asset], int, int]:
    prints = defaultdict(list)
    for card in cards:
        prints[card["collector_number"], card["name"]].append(card)
    ordered = _ordered_slots(prints)
    representatives = {}
    for slot in ordered:
        variants = prints[slot]
        by_lang = {card["lang"]: card for card in variants}
        representatives[slot] = by_lang.get("en", next(iter(by_lang.values())))
    indexes = _art_indexes(representatives)
    language_rank = {lang: i for i, lang in enumerate(dict.fromkeys(languages))}
    desired = []
    for slot, card in representatives.items():
        variants = sorted((candidate for candidate in prints[slot] if candidate["lang"] in language_rank),
                          key=lambda candidate: _candidate_rank(candidate, language_rank))
        promoted = (candidate["lang"] for candidate in variants if _special_priority(candidate) < 2)
        preferred = tuple(dict.fromkeys((*promoted, *language_rank)))
        for side, name, stem, found, legacy in _forge_keys(code, card, edition, indexes):
            sources = _unique_sources(_printing_sources(candidate, _image_slot(candidate, side), webp)
                                      for candidate in variants)
            desired.append((not found, slot[0], name, side, Asset(stem, sources, legacy, preferred)))

    assets = []
    occupied = set()
    collisions = 0
    # Keep confirmed Forge names when an unrecognized printing proposes the same path.
    for _inferred, number, name, side, asset in sorted(desired, key=lambda item: item[0]):
        key = asset.stem.casefold()
        if key in occupied:
            collisions += 1
            stem = f"extras/{code.upper()}/{filename(number)}_{filename(name)}_{side + 1}_{collisions}"
            asset = Asset(stem, asset.sources, languages=asset.languages)
        occupied.add(asset.stem.casefold())
        assets.append(asset)
    return assets, len(prints), collisions


def _path_segment(value: str) -> str:
    # Percent-encode unknown codes without turning them into path separators,
    # traversal components, or ambiguous aliases of a known language.
    return quote(value, safe="").replace(".", "%2E") or "%empty"


def _archive_id(value: str) -> str:
    try:
        return str(UUID(value))
    except ValueError:
        return hashlib.sha256(value.encode()).hexdigest()


def plan_archive(code: str, cards: list[dict], edition: Edition | None,
                 languages: list[str], webp: bool = False) -> tuple[list[Asset], list[Asset], int, int]:
    """Return (UUID/face originals, Forge projection, distinct UUID count, Forge collisions)."""
    unique = {}
    for card in cards:
        unique.setdefault(_archive_id(card["id"]), card)
    prints = defaultdict(list)
    for ident, card in unique.items():
        prints[card["collector_number"], card["name"]].append((ident, card))
    language_rank = {lang: i for i, lang in enumerate(dict.fromkeys(languages))}
    code = _path_segment(code.upper())
    ordered = _ordered_slots(prints)
    representatives = {}
    for slot in ordered:
        prints[slot].sort(key=lambda item: _candidate_rank(item[1], language_rank))
        representatives[slot] = next((card for _, card in prints[slot] if card["lang"] == "en"), prints[slot][0][1])
    indexes = _art_indexes(representatives)
    originals = []
    forge_slots = defaultdict(list)
    for number, name in ordered:
        for ident, card in prints[number, name]:
            lang = card["lang"]
            stem = f"pics/printings/{code}/{_path_segment(lang)}/{filename(number)}_{ident}"
            faces = card.get("card_faces") or []
            keys = tuple(_forge_keys(code, card, edition, indexes, archive=True))
            face_slots = [i for i, face in enumerate(faces) if card["layout"] in TWO_SIDES or face.get("image_uris")]
            slots = {slot: _printing_sources(card, slot, webp) for slot in face_slots}
            if not slots or card.get("image_uris") or any(_image_slot(card, side) is None for side, *_ in keys):
                slots[None] = _printing_sources(card, None, webp)
            for slot, sources in slots.items():
                suffix = "" if slot is None else f"_{slot + 1}"
                originals.append(Asset(stem + suffix, sources, languages=(lang,)))
            rank = _candidate_rank(card, language_rank)
            for side, _, key, found, legacy in keys:
                image_slot = _image_slot(card, side)
                forge_slots[number, name, side, key, found, legacy].append((rank, lang, ident, slots[image_slot]))

    desired = []
    for (number, name, side, stem, found, legacy), candidates in forge_slots.items():
        rank, _, ident, _ = next((candidate for candidate in candidates if candidate[3]), candidates[0])
        preferred = tuple(dict.fromkeys(lang for _, lang, _, _ in candidates))
        sources = _unique_sources(sources for _, _, _, sources in candidates)
        desired.append((not found, not sources, rank[:2], collector_key(number), name, side, ident,
                        Asset(stem, sources, legacy, preferred)))

    forge_assets = []
    occupied = set()
    collisions = 0
    for *_, side, ident, asset in sorted(desired, key=lambda item: item[:-1]):
        key = asset.stem.casefold()
        if key in occupied:
            collisions += 1
            asset = Asset(f"extras/{code}/{ident}_{side + 1}", asset.sources, languages=asset.languages)
        occupied.add(asset.stem.casefold())
        forge_assets.append(asset)
    return originals, forge_assets, len(unique), collisions
