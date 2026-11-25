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


FORGE_BASE_URL = "https://raw.githubusercontent.com/Card-Forge/forge/master/"
FORGE_TREE_URL = "https://api.github.com/repos/Card-Forge/forge/git/trees/master?recursive=1"
EDITIONS_URL = FORGE_BASE_URL + "forge-gui/res/editions/"
CARDSFOLDER_URL = FORGE_BASE_URL + "forge-gui/res/cardsfolder/"
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


@dataclass(frozen=True, slots=True)
class FaceRule:
    names: tuple[str, ...]
    kind: str


class ForgeEditions:
    def __init__(self, web: Web, cache: Path, local: Path | None):
        self.web = web
        self.cache = cache
        self.local = local
        self.by_code = {}
        self.remote_loaded = False
        self.remote_error = None
        self.rules = {}
        self.rule_by_face = {}
        if local is not None:
            files = sorted(local.glob("*.txt"))
            if not files:
                raise ValueError(f"No Forge edition .txt files in {local}")
            for path in files:
                self._index(Edition.parse(path.read_text(encoding="utf-8")))

    def _index(self, edition):
        if edition is None:
            return
        if edition.cards:
            if edition.scryfall not in self.by_code or edition.code.lower() == edition.scryfall:
                self.by_code[edition.scryfall] = edition
        if edition.tokens:
            self.by_code.setdefault(edition.tokens_code, edition)

    def _load_remote(self):
        if self.remote_loaded or self.remote_error is not None:
            return
        path = self.cache / "editions-index.json"
        try:
            if fresh(path):
                names = json.loads(path.read_bytes())
            else:
                with self.web.open(FORGE_TREE_URL, "api.github.com", "application/vnd.github+json") as reply:
                    raw = reply.read(8 * 1024 * 1024 + 1)
                if len(raw) > 8 * 1024 * 1024:
                    raise UpstreamError("Forge repository index exceeds 8 MiB")
                payload = json.loads(raw)
                if payload.get("truncated"):
                    raise UpstreamError("Forge repository index is truncated")
                prefix = "forge-gui/res/editions/"
                names = [item["path"][len(prefix):] for item in payload.get("tree", [])
                         if item.get("type") == "blob" and item.get("path", "").startswith(prefix)
                         and item["path"].endswith(".txt")]
                if not names:
                    raise UpstreamError("Forge repository index contains no editions")
                atomic_write(path, lambda file: file.write(json.dumps(names).encode()))
            self._remote_names = names
            self.remote_loaded = True
        except (HTTPError, UnicodeError, ValueError, UpstreamError, *NETWORK_ERRORS) as exc:
            self.remote_error = str(exc) or type(exc).__name__

    def _find_by_code(self, code):
        self._load_remote()
        if edition := self.by_code.get(code):
            return edition
        for name in getattr(self, "_remote_names", ()):
            key = hashlib.sha256(name.encode()).hexdigest()
            path = self.cache / (key + ".txt")
            try:
                if fresh(path):
                    raw = path.read_bytes()
                else:
                    with self.web.open(EDITIONS_URL + quote(name), "raw.githubusercontent.com", "text/plain") as reply:
                        raw = reply.read(2 * 1024 * 1024 + 1)
                    if len(raw) > 2 * 1024 * 1024:
                        continue
                    atomic_write(path, lambda file: file.write(raw))
                edition = Edition.parse(raw.decode("utf-8"))
                self._index(edition)
                if edition and code in (edition.scryfall, edition.tokens_code):
                    return edition
            except (HTTPError, UnicodeError, UpstreamError, *NETWORK_ERRORS):
                continue
        return None

    def find(self, code: str, card: dict) -> tuple[Edition | None, str | None]:
        code, set_name = PLANECHASE_EDITIONS.get(code, (code, None))
        if self.local is not None:
            if edition := self.by_code.get(code):
                return edition, None
            return None, "No matching Forge edition file; filenames inferred from Scryfall."

        if edition := self._find_by_code(code):
            return edition, None

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

    def face_rule(self, card):
        name = card.get("name", "")
        if name in self.rules:
            return self.rules[name]
        if name in self.rule_by_face:
            return self.rule_by_face[name]
        # Forge scripts can provide aliases for a Scryfall color printing.
        for related in card.get("all_parts") or []:
            related_name = related.get("name")
            if related_name in self.rule_by_face:
                return self.rule_by_face[related_name]
        if card.get("layout") != "meld" and not any(
                part.get("component") in ("meld_part", "meld_result", "combo_piece")
                or "Specialize" in part.get("type_line", "")
                for part in card.get("all_parts") or []):
            return None
        base = name.split(" // ", 1)[0]
        stem = _forge_script_stem(base)
        if not stem:
            return None
        path = self.cache / "scripts" / f"{stem}.txt"
        try:
            if fresh(path):
                raw = path.read_bytes()
            else:
                url = CARDSFOLDER_URL + stem[0] + "/" + quote(stem) + ".txt"
                with self.web.open(url, "raw.githubusercontent.com", "text/plain") as reply:
                    raw = reply.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise UpstreamError("Forge card script exceeds 2 MiB")
                atomic_write(path, lambda file: file.write(raw))
            rule = _parse_special_script(raw.decode("utf-8"), card)
            if rule:
                self.rules[name] = rule
                for face in rule.names:
                    self.rule_by_face[face] = rule
            return rule
        except (HTTPError, UnicodeError, UpstreamError, *NETWORK_ERRORS):
            return None


def _forge_script_stem(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _parse_special_script(text: str, card: dict) -> FaceRule | None:
    lines = text.splitlines()
    kind = "normal"
    if any(line.strip().lower() == "alternatemode:specialize" for line in lines):
        kind = "specialize"
    elif any(line.strip().lower() == "alternatemode:meld" for line in lines):
        kind = "meld"
    if kind == "normal":
        return None
    base = card.get("name", "").split(" // ", 1)[0]
    names = [base]
    if kind == "specialize":
        blocks = {}
        current = None
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("SPECIALIZE:"):
                current = stripped.split(":", 1)[1].strip().lower()
            elif stripped.startswith("Name:") and current is not None:
                blocks[current] = stripped.split(":", 1)[1].strip()
                current = None
        if len(blocks) != 5:
            return None
        # The script's primary Name is the Forge base key, not a color face.
        primary = next((line.split(":", 1)[1].strip() for line in lines
                        if line.strip().startswith("Name:")), base)
        return FaceRule((primary, *(blocks[c] for c in ("white", "blue", "black", "red", "green"))), kind)
    else:
        names = [line.split(":", 1)[1].strip() for line in lines if line.startswith("Name:")]
        if not names:
            names = [base]
    return FaceRule(tuple(names), kind)




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


def _special_face_name(card, side, forge):
    if forge is None:
        return None
    rule = forge.face_rule(card)
    if rule is None:
        return None
    if rule.kind == "specialize":
        return rule.names[side] if side < len(rule.names) else None
    if rule.kind == "meld" and side == 1:
        parts = card.get("all_parts") or []
        return next((p.get("name") for p in parts if p.get("component") == "meld_result"), None)
    return rule.names[side] if side < len(rule.names) else None


def _special_source(card, side, cards, forge):
    rule = forge.face_rule(card) if forge is not None else None
    if rule is None:
        return card, _image_slot(card, side)
    if rule.kind == "meld":
        if side == 0:
            return card, None
        part = next((p for p in card.get("all_parts") or [] if p.get("component") == "meld_result"), None)
        if part is not None:
            match = next((c for c in cards if c.get("id") == part.get("id")), None)
            if match is not None:
                return match, None
    elif rule.kind == "specialize":
        target = rule.names[side] if side < len(rule.names) else None
        if target:
            match = next((c for c in cards if c.get("set") == card.get("set") and c.get("name") == target), None)
            if match is not None:
                return match, None
    return card, _image_slot(card, side)


def _forge_keys(code, card, edition, entry, indexes, forge=None):
    number = card["collector_number"]
    names = face_names(card)
    layout = card["layout"]
    token_set = card["set_type"] == "token"
    faces = card.get("card_faces") or []
    rule = forge.face_rule(card) if forge is not None else None
    if layout == "reversible_card":
        # Forge has no general Reversible split type. The edition's ☇ slot
        # identifies a genuinely registered reverse face; otherwise preserve
        # the primary face and let the caller report an unsupported extra face.
        reverse_number = number + "☇"
        if edition is None or reverse_number not in edition.cards:
            names = names[:1]
        else:
            names = names[:2]
            if len(names) == 2:
                names[1] = next((row.name for row in edition.cards[reverse_number]
                                 if row.name == names[1] or row.name == names[0]), names[1])
    if rule and rule.names:
        names = list(rule.names)
        if rule.kind == "specialize":
            layout = "specialize"
            if card.get("name") != names[0]:
                return
            folder = edition.folder if edition else code.upper()
            for side, name in enumerate(names):
                yield side, f"pics/cards/{folder}/{filename(name)}.full", True, True
            return
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
            if side and edition and number in edition.tokens and number + "☇" not in edition.tokens:
                back_name = filename(name.lower().replace(" token", "").replace(" ", "_"))
                yield side, f"pics/tokens/{edition.code}/{filename(number)}_{back_name}", True, False
            elif not slugs:
                # A parent edition can be resolved for a token set without
                # registering this token. In that case Forge's key falls back
                # to the Scryfall token code, not the parent's card code.
                token_folder = code.upper()
                if edition and number in edition.tokens:
                    token_folder = edition.code
                stem = f"pics/tokens/{token_folder}/{filename(token_number)}_{filename(name).replace(' ', '_')}"
                yield side, stem, False, False
        else:
            special = _special_face_name(card, side, forge)
            if special is not None:
                name = special
            if side == 0 and layout != "split":
                if entry:
                    name = entry.flavor_name or (f"{entry.name} {entry.variant}" if entry.variant else entry.name)
                else:
                    name = card.get("flavor_name") or name
                if (layout in TWO_SIDES or layout in ("adventure", "prepare")) and " // " in name:
                    name = names[0]
            infix = "" if face_layout == "planar" else ".full"
            face_art = art
            if layout == "reversible_card" and side == 1 and edition is not None:
                reverse_rows = edition.cards.get(number + "☇", ())
                if reverse_rows:
                    face_art = reverse_rows[0].art_suffix
            stem = f"pics/cards/{folder}/{filename(name)}{face_art}{infix}"
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
    all_languages: bool = False, forge=None,
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
        merged_special = False
        for card in prints[slot]:
            first_name = face_names(card)[0]
            entry = _edition_entry(edition, code, card, first_name)
            token = card["set_type"] == "token" or card["layout"] in TOKEN_LAYOUTS
            if forge is not None and (rule := forge.face_rule(card)) is not None:
                if rule.kind == "specialize" and card["name"] != rule.names[0]:
                    merged_special = True
                    continue
                if rule.kind == "meld" and card["layout"] != "meld":
                    continue
                if rule.kind == "meld" and card.get("collector_number", "").endswith("b"):
                    merged_special = True
                    continue
            if entry is None and not token and (
                name in known_names or first_name in known_names or (edition and code in PLANECHASE_EDITIONS)
            ):
                # The edition defines the card's art count. Guessing a suffix for
                # another collector number can assign it to a different Forge art.
                continue
            for side, stem, found, legacy in _forge_keys(code, card if entry else representative,
                                                         edition, entry, indexes, forge):
                if forge is not None and (rule := forge.face_rule(card)) is not None and rule.kind == "specialize":
                    if side == 0:
                        source_card = card
                    else:
                        target = rule.names[side] if side < len(rule.names) else None
                        source_card = next((c for c in cards if c.get("set") == card.get("set")
                                            and c.get("name") == target), None)
                        if source_card is None:
                            continue
                    source_side = None
                else:
                    source_card, source_side = _special_source(card, side, cards, forge)
                key = side, stem, found, legacy, entry if stem.startswith("pics/cards/") else None
                sources = candidates[key]
                lang = card["lang"]
                if all_languages or lang in language_rank:
                    sources.append((source_card, _printing_sources(source_card, source_side, webp)))
        if not candidates and not (forge is not None and any(
                (rule := forge.face_rule(card)) is not None
                and ((rule.kind == "specialize" and card.get("name") != rule.names[0])
                     or (rule.kind == "meld" and card.get("layout") == "meld"
                         and card.get("collector_number", "").endswith("b")))
                for card in prints[slot])):
            issues.append(f"{number} {name}: no unique art slot in the Forge edition")
        for (side, stem, found, legacy, entry), variants in candidates.items():
            sources = _unique_sources(group for _, group in variants)
            preferred = tuple(dict.fromkeys(
                [card["lang"] for card, _ in variants if _treatment_priority(card) < 3]
                + list(language_rank) + [card["lang"] for card, _ in variants]
            ))
            printing_key = _printing_key(code, number, name, side, edition, entry)
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
