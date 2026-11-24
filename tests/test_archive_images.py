import unittest
from uuid import UUID

from scryfall_downloader.forge import Edition, plan_archive, plan_images


CDN = "https://cards.scryfall.io"


def printing(key, number, name, lang, *, set_code="war", **fields):
    ident = str(UUID(int=key))
    return {
        "id": ident, "set": set_code, "set_type": "expansion", "collector_number": number,
        "name": name, "lang": lang, "layout": "normal", "image_status": "highres_scan",
        "image_uris": {"large": f"{CDN}/large/{ident}.jpg", "display": f"{CDN}/display/{ident}.webp"},
        **fields,
    }


def source_ids(asset):
    return [source.source_id for source in asset.sources]


class ArchiveImagesTest(unittest.TestCase):
    def test_japanese_planeswalker_occupies_its_own_forge_art(self):
        name = "Liliana, Dreadhorde General"
        edition = Edition.parse(f"""[metadata]
Code=WAR
[cards]
97 M {name} @Chris Rallis
[promo]
97★ M {name} @Yoshitaka Amano
""")
        en = printing(1, "97", name, "en")
        zhs = printing(2, "97", name, "zhs")
        ja = printing(3, "97", name, "ja")
        ja_alt = printing(4, "97★", name, "ja", promo_types=["jpwalker"])
        ja_other = printing(5, "97★", name, "ja", promo_types=["jpwalker"], finishes=["foil"])
        cards = [ja_other, en, ja, zhs, ja_alt, ja_alt]
        originals, forge, count, collisions = plan_archive("war", cards, edition, ["zhs", "en"])
        self.assertEqual((len(originals), count, collisions), (5, 5, 0))
        self.assertEqual({asset.stem for asset in originals}, {
            f"pics/printings/WAR/{card['lang']}/{card['collector_number']}_{card['id']}" for card in cards
        })
        assets = {asset.stem: asset for asset in forge}
        regular = assets[f"pics/cards/WAR/{name}1.full"]
        alternate = assets[f"pics/cards/WAR/{name}2.full"]
        self.assertEqual(source_ids(regular), [zhs["id"], en["id"], ja["id"]])
        self.assertEqual(source_ids(alternate), [ja_alt["id"], ja_other["id"]])
        self.assertTrue(regular.legacy_border and alternate.legacy_border)
        self.assertEqual(plan_archive("war", list(reversed(cards)), edition, ["zhs", "en"]),
                         (originals, forge, count, collisions))
        manual, slots, _ = plan_images("war", cards, edition, ["zhs", "en"])
        self.assertEqual(slots, 2)
        self.assertEqual({asset.stem: source_ids(asset) for asset in manual}, {
            regular.stem: [zhs["id"], en["id"]], alternate.stem: [],
        })

    def test_special_priority_beats_explicit_languages_but_manual_is_strict(self):
        name = "Vorinclex, Monstrous Raider"
        edition = Edition.parse(f"[metadata]\nCode=KHM\n[cards]\n333 M {name}\n")
        en = printing(1, "333", name, "en", set_code="khm")
        zhs = printing(2, "333", name, "zhs", set_code="khm")
        ph = printing(3, "333", name, "ph", set_code="khm", promo_types=["boosterfun"])
        cards = [en, zhs, ph]
        originals, forge, _, _ = plan_archive("khm", cards, edition, ["zhs", "en"], webp=True)
        self.assertEqual(source_ids(forge[0]), [ph["id"], ph["id"], zhs["id"], zhs["id"], en["id"], en["id"]])
        by_url = {source.url: source for asset in originals for source in asset.sources}
        self.assertEqual(forge[0].sources, tuple(by_url[source.url] for source in forge[0].sources))
        self.assertEqual(forge[0].languages, ("ph", "zhs", "en"))
        manual, _, _ = plan_images("khm", cards, edition, ["zhs", "en"])
        self.assertEqual(source_ids(manual[0]), [zhs["id"], en["id"]])
        selected_special, _, _ = plan_images("khm", cards, edition, ["zhs", "en", "ph"])
        self.assertEqual(source_ids(selected_special[0]), [ph["id"], zhs["id"], en["id"]])

        # Same-slot alternatives: JP walker is preferred, but ordinary Japanese is not.
        walker = "Liliana, Dreadhorde General"
        regular = printing(4, "97", walker, "en")
        translated = printing(5, "97", walker, "ja")
        promo = printing(6, "97", walker, "ja", promo_types=["jpwalker"])
        variants = [regular, translated, promo]
        _, forge, _, _ = plan_archive("war", variants, None, ["zhs", "en"])
        self.assertEqual(source_ids(forge[0]), [promo["id"], regular["id"], translated["id"]])
        selected_special, _, _ = plan_images("war", variants, None, ["en", "ja"])
        self.assertEqual(source_ids(selected_special[0]), [promo["id"], regular["id"], translated["id"]])

    def test_unknown_languages_digital_faces_and_missing_images_are_archived(self):
        name = "Front // Back"
        faces = [
            {"name": "Front", "image_uris": {"large": f"{CDN}/front.jpg"}},
            {"name": "Back", "image_uris": {"large": f"{CDN}/back.jpg"}},
        ]
        card = printing(1, "7/2", name, "../x", layout="transform", image_uris={}, card_faces=faces,
                        digital=True, set_type="alchemy")
        missing = printing(2, "8", "Missing", "fr", image_status="missing")
        placeholder = printing(3, "9", "Placeholder", "it", image_status="placeholder")
        originals, forge, count, collisions = plan_archive("war", [card, missing, placeholder], None, ["en"])
        self.assertEqual((count, collisions), (3, 0))
        by_stem = {asset.stem: asset for asset in originals}
        base = f"pics/printings/WAR/%2E%2E%2Fx/72_{card['id']}"
        front = by_stem[base + "_1"]
        back = by_stem[base + "_2"]
        self.assertEqual(front.sources[0].url, f"{CDN}/front.jpg")
        self.assertEqual(back.sources[0].url, f"{CDN}/back.jpg")
        canonical = {asset.stem: asset.sources for asset in forge}
        self.assertEqual(canonical["pics/cards/WAR/Front.full"], front.sources)
        self.assertEqual(canonical["pics/cards/WAR/Back.full"], back.sources)
        self.assertEqual(canonical["pics/cards/WAR/Missing.full"], ())
        self.assertEqual(canonical["pics/cards/WAR/Placeholder.full"], ())
        self.assertEqual(by_stem[f"pics/printings/WAR/fr/8_{missing['id']}"].sources, ())
        self.assertEqual(by_stem[f"pics/printings/WAR/it/9_{placeholder['id']}"].sources, ())

    def test_exact_key_collision_keeps_confirmed_art_even_against_special_inference(self):
        edition = Edition.parse("[metadata]\nCode=WAR\n[cards]\n2 C A: B\n")
        inferred = printing(1, "1", "A B", "ph")
        confirmed = printing(2, "2", "A: B", "en")
        originals, forge, count, collisions = plan_archive("war", [inferred, confirmed], edition, ["en"])
        self.assertEqual((len(originals), count, collisions), (2, 2, 1))
        self.assertEqual({asset.stem: source_ids(asset) for asset in forge}, {
            "pics/cards/WAR/A B.full": [confirmed["id"]],
            f"extras/WAR/{inferred['id']}_1": [inferred["id"]],
        })
