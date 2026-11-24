"""Map Scryfall printings and faces to Forge's local image keys."""

from collections import Counter, defaultdict
from dataclasses import dataclass
import hashlib
from pathlib import Path
import re
import unicodedata
from urllib.error import HTTPError
from urllib.parse import quote

from upstream import Asset, NETWORK_ERRORS, Source, UpstreamError, Web, atomic_write, fresh


EDITIONS_URL = "https://raw.githubusercontent.com/Card-Forge/forge/master/forge-gui/res/editions/"
BAD_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
SHORT_CODE = re.compile(r"[A-Za-z0-9_-]{1,16}\Z")
CARD_SECTIONS = frozenset("""cards|special slot|precon product|borderless|etched|showcase|full art|extended art|
alternate art|retro frame|buy a box|promo|prerelease promo|bundle|box topper|jumpstart|rebalanced|
eternal|conjured|scheme|printsheets""".replace("\n", "").split("|"))
CARD_ROW = re.compile(r"(?:(\S*\d\S*)\s+)?(?:[SCURML]\s+)?([^@$]+?)(?: @[^$]*)?(?: \$\{(.+)\})?$")
TOKEN_ROW = re.compile(r"(\S*\d\S*)\s+([^@]+?)(?: @.*)?$")
VARIANT_PARAMS = re.compile(r'"(flavorname|variant)"\s*:\s*"([^"]+)"', re.I)
FORMATS = (("png", ".png"), ("large", ".jpg"), ("normal", ".jpg"), ("jpeg", ".jpg"), ("small", ".jpg"))
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


def image_sources(prints: dict[str, dict], side: int, languages: list[str]) -> tuple[Source, ...]:
    sources = []
    seen = set()
    for lang in languages:
        card = prints.get(lang)
        if not card or card.get("image_status") in ("missing", "placeholder"):
            continue
        faces = card.get("card_faces") or []
        if card["layout"] in TWO_SIDES and side < len(faces):
            images = faces[side].get("image_uris") or {}
        else:
            images = card.get("image_uris") or {}
        for version, suffix in FORMATS:
            if (url := images.get(version)) and url not in seen:
                seen.add(url)
                sources.append(Source(lang, "normal" if version == "jpeg" else version, suffix, url))
    return tuple(sources)


def plan_images(code: str, cards: list[dict], edition: Edition | None,
                languages: list[str]) -> tuple[list[Asset], int, int]:
    prints = defaultdict(dict)
    for card in cards:
        prints[card["collector_number"], card["name"]][card["lang"]] = card
    ordered = sorted(prints.items(), key=lambda item: (collector_key(item[0][0]), item[0][1]))
    arts = defaultdict(list)
    for (number, _name), langs in ordered:
        card = langs.get("en") or next(iter(langs.values()))
        arts[face_names(card)[0]].append(number)
    indexes = {(name, number): str(i) if len(numbers) > 1 else ""
               for name, numbers in arts.items() for i, number in enumerate(numbers, 1)}

    folder = edition.folder if edition else code.upper()
    desired = []
    for (number, full_name), langs in ordered:
        card = langs.get("en") or next(iter(langs.values()))
        names = face_names(card)
        layout = card["layout"]
        token_set = card["set_type"] == "token"
        faces = card.get("card_faces") or []
        entry = None
        if edition:
            for row in edition.cards.get(number, ()):
                if row.name in (full_name, names[0]) or (row.flavor_name and row.flavor_name == card.get("flavor_name")):
                    entry = row
                    break
        art = entry.art_suffix if entry else indexes[names[0], number]
        for side, name in enumerate(names):
            face_layout = faces[side].get("layout", layout) if layout == "reversible_card" and faces else layout
            if token_set or face_layout in TOKEN_LAYOUTS:
                token_number = number if side == 0 else number + "☇"
                slugs = edition.tokens.get(token_number) if edition else None
                found = bool(slugs)
                if slugs:
                    stem = f"pics/tokens/{edition.code}/{filename(token_number)}_{filename(slugs[0])}"
                else:
                    stem = f"pics/tokens/{code.upper()}/{filename(token_number)}_{filename(name).replace(' ', '_')}"
            else:
                found = entry is not None
                if side == 0 and layout != "split":
                    name = ((entry.flavor_name or (entry.name + ' ' + entry.variant if entry.variant else entry.name))
                            if entry else card.get("flavor_name") or name)
                    if (layout in TWO_SIDES or layout in ("adventure", "prepare")) and " // " in name:
                        name = names[0]
                infix = "" if face_layout == "planar" else ".full"
                stem = f"pics/cards/{folder}/{filename(name)}{art}{infix}"
            desired.append((not found, number, name, side, Asset(stem, image_sources(langs, side, languages))))

    assets = []
    occupied = set()
    collisions = 0
    # Keep confirmed Forge names when an unrecognized printing proposes the same path.
    for _inferred, number, name, side, asset in sorted(desired, key=lambda item: item[0]):
        key = asset.stem.casefold()
        if key in occupied:
            collisions += 1
            asset = Asset(f"extras/{code.upper()}/{filename(number)}_{filename(name)}_{side + 1}_{collisions}", asset.sources)
        occupied.add(asset.stem.casefold())
        assets.append(asset)
    return assets, len(prints), collisions
