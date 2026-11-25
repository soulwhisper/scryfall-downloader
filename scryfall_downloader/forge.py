"""Map Scryfall printings and faces to Forge's local image keys."""

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import unicodedata
from urllib.error import HTTPError
from urllib.parse import quote
from uuid import UUID

from .upstream import Asset, NETWORK_ERRORS, Source, UpstreamError, Web, atomic_write, fresh


EDITIONS_URL = "https://raw.githubusercontent.com/Card-Forge/forge/master/forge-gui/res/editions/"
# Forge's ImageUtil routes these prefixed collectors to separate Scryfall sets.
PLANECHASE_EDITIONS = {
    "ohop": ("hop", "Planechase"),
    "opc2": ("pc2", "Planechase 2012"),
    "opca": ("pca", "Planechase Anthology"),
}
BAD_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
SHORT_CODE = re.compile(r"[A-Za-z0-9_-]{1,16}\Z")
CARD_SECTIONS = frozenset("""cards|special slot|precon product|borderless|etched|showcase|full art|extended art|
alternate art|retro frame|buy a box|promo|prerelease promo|bundle|box topper|jumpstart|rebalanced|
eternal|conjured|scheme|printsheets""".replace("\n", "").split("|"))
CARD_ROW = re.compile(r"(?:(\S*\d\S*)\s+)?(?:([SCURML])\s+)?([^@$]+?)(?: @([^$]*?))?(?: \$\{(.+)\})?$")
RARITY_ORDER = "LCURMS?"
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
    artist: str
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
                number, rarity, name, artist, params = match.groups()
                extra = {key.lower(): value for key, value in VARIANT_PARAMS.findall(params or "")}
                rows[name.strip()].append((number or "50000", rarity or "?", (artist or "").strip(),
                                           extra.get("variant", ""), extra.get("flavorname", "")))
            elif section == "tokens" and (match := TOKEN_ROW.fullmatch(line)):
                tokens[match[1]].append(match[2].strip())

        code = metadata.get("code", "")
        folder = metadata.get("code2", code)
        if not SHORT_CODE.fullmatch(code) or not SHORT_CODE.fullmatch(folder):
            return None
        scryfall = metadata.get("scryfallcode", code).lower()
        cards = defaultdict(list)
        for name, prints in rows.items():
            counts = Counter(row[3] for row in prints)
            ordered = sorted(prints, key=lambda row: (collector_key(row[0])[0], RARITY_ORDER.index(row[1])))
            for index, (number, _rarity, artist, variant, flavor_name) in enumerate(ordered):
                count = counts[variant] if variant else len(prints)
                suffix = str(index % count + 1) if count > 1 else ""
                cards[number].append(EditionCard(name, artist, variant, flavor_name, suffix))
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
        code, set_name = PLANECHASE_EDITIONS.get(code, (code, None))
        if self.local is not None:
            if edition := self.by_code.get(code):
                return edition, None
            return None, "No matching Forge edition file; filenames inferred from Scryfall."

        if set_name is None:
            set_name = card["set_name"]
            if card["set_type"] == "token":
                set_name = set_name.removesuffix(" Tokens")

        def matching_edition(raw):
            edition = Edition.parse(raw.decode("utf-8"))
            return edition if edition and code in (edition.scryfall, edition.tokens_code) else None

        # Forge's resource filenames typically omit punctuation illegal on Windows.
        set_name = BAD_FILENAME_CHARS.sub("", set_name)
        key = hashlib.sha256(set_name.encode()).hexdigest()
        path = self.cache / (key + ".txt")
        try:
            if fresh(path) and (edition := matching_edition(path.read_bytes())):
                return edition, None
            # For example, Forge spells The Brothers' War without the apostrophe.
            alternate = set_name.replace("'", "").replace("’", "")
            for title in dict.fromkeys((set_name, alternate)):
                try:
                    url = EDITIONS_URL + quote(title, safe="") + ".txt"
                    with self.web.open(url, "raw.githubusercontent.com", "text/plain") as reply:
                        raw = reply.read(2 * 1024 * 1024 + 1)
                except HTTPError as exc:
                    if exc.code != 404:
                        raise
                    exc.close()
                    continue
                if len(raw) > 2 * 1024 * 1024:
                    raise UpstreamError("Forge edition file exceeds 2 MiB")
                if edition := matching_edition(raw):
                    atomic_write(path, lambda file: file.write(raw))
                    return edition, None
        except HTTPError as exc:
            with exc:
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


def _ordered_slots(prints):
    return sorted(prints, key=lambda key: (collector_key(key[0]), key[1]))


def _art_indexes(representatives):
    arts = defaultdict(list)
    for (number, _name), card in representatives.items():
        arts[face_names(card)[0]].append(number)
    return {(name, number): str(i) if len(numbers) > 1 else ""
            for name, numbers in arts.items() for i, number in enumerate(numbers, 1)}


def _edition_number(code, number):
    return code.upper() + number if code in PLANECHASE_EDITIONS else number


def _edition_entry(edition, code, card, first_name):
    if not edition:
        return None
    number = _edition_number(code, card["collector_number"])
    names = (card["name"], first_name)
    flavor = card.get("flavor_name")
    rows = [row for row in edition.cards.get(number, ())
            if row.name in names or (flavor and row.flavor_name == flavor)]
    if not rows and re.fullmatch(r"\d+[a-z]?", number):
        # Forge prefixes some Un-set collectors with F, while Scryfall omits it.
        rows = [row for row in edition.cards.get("F" + number, ()) if row.name in names]
    if flavor:
        rows = [row for row in rows if row.flavor_name == flavor] or rows
    if len(rows) > 1 and (artist := card.get("artist")):
        rows = [row for row in rows if filename(row.artist).casefold() == filename(artist).casefold()]
    return rows[0] if len(rows) == 1 else None


def _forge_keys(code, card, edition, entry, indexes):
    number = card["collector_number"]
    names = face_names(card)
    layout = card["layout"]
    token_set = card["set_type"] == "token"
    faces = card.get("card_faces") or []
    art = entry.art_suffix if entry else indexes.get((names[0], number), "")
    folder = edition.folder if edition else code.upper()
    for side, name in enumerate(names):
        face_layout = faces[side].get("layout", layout) if layout == "reversible_card" and faces else layout
        if token_set or face_layout in TOKEN_LAYOUTS:
            token_number = number if side == 0 else number + "☇"
            slugs = edition.tokens.get(token_number) if edition else None
            if slugs:
                for slug in dict.fromkeys(slugs):
                    yield side, f"pics/tokens/{edition.code}/{filename(token_number)}_{filename(slug)}", True, False
            if side and edition and number in edition.tokens:
                # PaperToken supplies the back face's name; Forge strips $alt before looking it up.
                back_name = filename(name.lower().replace(" token", "").replace(" ", "_"))
                yield side, f"pics/tokens/{edition.code}/{filename(number)}_{back_name}", True, False
            elif not slugs:
                token_folder = code.upper()
                if edition and len(names) > 1 and number + "☇" in edition.tokens:
                    token_folder = edition.code
                stem = f"pics/tokens/{token_folder}/{filename(token_number)}_{filename(name).replace(' ', '_')}"
                yield side, stem, False, False
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
            yield side, stem, entry is not None, entry is not None and bool(infix)


def _treatment_priority(card):
    lang = card["lang"]
    if lang == "ph":
        return 0
    if lang == "ja" and "jpwalker" in (card.get("promo_types") or ()):
        return 1
    if (card.get("full_art") or card.get("variation") or card.get("border_color") == "borderless"
            or any(effect in ("showcase", "extendedart") for effect in (card.get("frame_effects") or ()))):
        return 2
    return 3


def _candidate_rank(card, language_rank):
    lang = card["lang"]
    return _treatment_priority(card), language_rank.get(lang, len(language_rank)), lang, card.get("id") or ""


def _printing_key(code, number, name, side, edition, entry):
    # An index such as Swamp2 is a destination, not a printing identity.
    qualifier = ()
    if entry is not None:
        rows = edition.cards.get(_edition_number(code, number)) or edition.cards.get("F" + number, ())
        if sum(row.name == entry.name for row in rows) > 1:
            qualifier = (entry.artist, entry.variant, entry.flavor_name)
    return json.dumps((code, number, name, side, *qualifier), ensure_ascii=False, separators=(",", ":"))


def plan_images(
    code: str, cards: list[dict], edition: Edition | None, languages: list[str], webp: bool = False, *,
    all_languages: bool = False,
) -> tuple[list[Asset], int, list[str]]:
    """Build one ordered source list per Forge image key, with no off-tree copies."""
    prints = defaultdict(list)
    for card in cards:
        prints[card["collector_number"], card["name"]].append(card)
    ordered = _ordered_slots(prints)
    language_rank = {lang: i for i, lang in enumerate(dict.fromkeys(languages))}
    representatives = {}
    for slot in ordered:
        variants = prints[slot]
        variants.sort(key=lambda card: _candidate_rank(card, language_rank))
        representatives[slot] = next((card for card in variants if card["lang"] == "en"), variants[0])
    indexes = _art_indexes(representatives)
    known_names = {row.name for rows in edition.cards.values() for row in rows} if edition else set()
    desired = []
    issues = []
    for slot in ordered:
        number, name = slot
        representative = representatives[slot]
        candidates = defaultdict(list)
        for card in prints[slot]:
            first_name = face_names(card)[0]
            entry = _edition_entry(edition, code, card, first_name)
            token = card["set_type"] == "token" or card["layout"] in TOKEN_LAYOUTS
            if entry is None and not token and (
                name in known_names or first_name in known_names or (edition and code in PLANECHASE_EDITIONS)
            ):
                # The edition defines the card's art count. Guessing a suffix for
                # another collector number can assign it to a different Forge art.
                continue
            for side, stem, found, legacy in _forge_keys(code, card if entry else representative,
                                                         edition, entry, indexes):
                key = side, stem, found, legacy, entry if stem.startswith("pics/cards/") else None
                sources = candidates[key]
                lang = card["lang"]
                if all_languages or lang in language_rank:
                    sources.append((card, _printing_sources(card, _image_slot(card, side), webp)))
        if not candidates:
            issues.append(f"{number} {name}: no unique art slot in the Forge edition")
        for (side, stem, found, legacy, entry), variants in candidates.items():
            sources = _unique_sources(group for _, group in variants)
            preferred = tuple(dict.fromkeys(
                [card["lang"] for card, _ in variants if _treatment_priority(card) < 3]
                + list(language_rank) + [card["lang"] for card, _ in variants]
            ))
            printing_key = (_printing_key(code, number, name, side, edition, entry)
                            if stem.startswith("pics/cards/") else None)
            asset = Asset(stem, sources, legacy, preferred, printing_key=printing_key, inferred=not found)
            desired.append((not found, not sources, number, name, asset))

    assets = []
    occupied = {}
    # Confirmed slots reserve their keys even if no eligible scan is available.
    for _inferred, _empty, number, name, asset in sorted(desired, key=lambda item: item[:2]):
        key = asset.stem.casefold()
        if key in occupied:
            issues.append(f"{number} {name}: {asset.stem} already belongs to {occupied[key]}")
            continue
        occupied[key] = f"{number} {name}"
        assets.append(asset)
    return assets, len(prints), issues
