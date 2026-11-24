import unittest
from uuid import UUID

from scryfall_downloader.forge import Edition, plan_images


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
        forge, count, issues = plan_images("war", cards, edition, ["zhs", "en"], all_languages=True)
        self.assertEqual((len(forge), count, issues), (2, 2, []))
        assets = {asset.stem: asset for asset in forge}
        regular = assets[f"pics/cards/WAR/{name}1.full"]
        alternate = assets[f"pics/cards/WAR/{name}2.full"]
        self.assertEqual(source_ids(regular), [zhs["id"], en["id"], ja["id"]])
        self.assertEqual(source_ids(alternate), [ja_alt["id"], ja_other["id"]])
        self.assertTrue(regular.legacy_border and alternate.legacy_border)
        self.assertEqual(plan_images("war", list(reversed(cards)), edition, ["zhs", "en"], all_languages=True),
                         (forge, count, issues))
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
        forge, _, _ = plan_images("khm", cards, edition, ["zhs", "en"], webp=True, all_languages=True)
        self.assertEqual(source_ids(forge[0]), [ph["id"], ph["id"], zhs["id"], zhs["id"], en["id"], en["id"]])
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
        forge, _, _ = plan_images("war", variants, None, ["zhs", "en"], all_languages=True)
        self.assertEqual(source_ids(forge[0]), [promo["id"], regular["id"], translated["id"]])
        selected_special, _, _ = plan_images("war", variants, None, ["en", "ja"])
        self.assertEqual(source_ids(selected_special[0]), [promo["id"], regular["id"], translated["id"]])

    def test_digital_faces_and_unavailable_images_use_only_forge_keys(self):
        name = "Front // Back"
        faces = [
            {"name": "Front", "image_uris": {"large": f"{CDN}/front.jpg"}},
            {"name": "Back", "image_uris": {"large": f"{CDN}/back.jpg"}},
        ]
        card = printing(1, "7/2", name, "../x", layout="transform", image_uris={}, card_faces=faces,
                        digital=True, set_type="alchemy")
        missing = printing(2, "8", "Missing", "fr", image_status="missing")
        placeholder = printing(3, "9", "Placeholder", "it", image_status="placeholder")
        forge, count, issues = plan_images("war", [card, missing, placeholder], None, ["en"], all_languages=True)
        self.assertEqual((count, issues), (3, []))
        canonical = {asset.stem: asset.sources for asset in forge}
        self.assertEqual(set(canonical), {f"pics/cards/WAR/{name}.full"
                                          for name in ("Front", "Back", "Missing", "Placeholder")})
        self.assertEqual(canonical["pics/cards/WAR/Front.full"][0].url, f"{CDN}/front.jpg")
        self.assertEqual(canonical["pics/cards/WAR/Back.full"][0].url, f"{CDN}/back.jpg")
        self.assertEqual(canonical["pics/cards/WAR/Missing.full"], ())
        self.assertEqual(canonical["pics/cards/WAR/Placeholder.full"], ())

    def test_exact_key_collision_keeps_confirmed_art_even_against_special_inference(self):
        edition = Edition.parse("[metadata]\nCode=WAR\n[cards]\n2 C A: B\n")
        inferred = printing(1, "1", "A B", "ph")
        confirmed = printing(2, "2", "A: B", "en")
        forge, count, issues = plan_images("war", [inferred, confirmed], edition, ["en"], all_languages=True)
        self.assertEqual(count, 2)
        self.assertEqual({asset.stem: source_ids(asset) for asset in forge},
                         {"pics/cards/WAR/A B.full": [confirmed["id"]]})
        self.assertEqual(len(issues), 1)
        self.assertIn("pics/cards/WAR/A B.full", issues[0])

    def test_wide_art_treatments_keep_their_forge_slots_even_with_incomplete_metadata(self):
        # Same artist across ordinary and wide framing is not sufficient to
        # collapse two distinct collector numbers.
        edition = Edition.parse("""[metadata]
Code=X
Code2=XX
[cards]
1 R Oak @A
9 L Island @B
[borderless]
99 R Oak @A
[extended art]
109 R Oak @C
[full art]
10 L Island @B
""")
        regular = printing(1, "1", "Oak", "zhs", artist="A")
        wide = printing(2, "99", "Oak", "en", artist="A", border_color="borderless")
        extended = printing(3, "109", "Oak", "ja", artist="C", frame_effects=["extendedart"])
        full_art = printing(4, "10", "Island", "en", artist="B", full_art=True)
        ordinary_land = printing(5, "9", "Island", "zhs", artist="B")
        unregistered = printing(6, "2", "Oak", "ph", artist="A", variation=True)
        forge, _, issues = plan_images("x", [unregistered, full_art, wide, extended, regular, ordinary_land],
                                       edition, ["zhs", "en"], all_languages=True)
        self.assertEqual({asset.stem: source_ids(asset) for asset in forge}, {
            "pics/cards/XX/Oak1.full": [regular["id"]],
            "pics/cards/XX/Oak2.full": [wide["id"]],
            "pics/cards/XX/Oak3.full": [extended["id"]],
            "pics/cards/XX/Island1.full": [ordinary_land["id"]],
            "pics/cards/XX/Island2.full": [full_art["id"]],
        })
        self.assertEqual(issues, ["2 Oak: no unique art slot in the Forge edition"])
        # The missing lower collector in a later sync must not renumber Oak2.
        found, _, _ = plan_images("x", [wide], edition, ["en"])
        self.assertEqual(found[0].stem, "pics/cards/XX/Oak2.full")

    def test_one_key_prefers_art_treatment_before_language(self):
        name = "Island"
        edition = Edition.parse("[metadata]\nCode=ZEN\n[cards]\n234 L Island\n")
        regular = printing(1, "234", name, "zhs")
        translated = printing(2, "234", name, "ja")
        full_art = printing(3, "234", name, "en", full_art=True)
        assets, _, issues = plan_images("zen", [regular, translated, full_art], edition, ["zhs", "en"],
                                        all_languages=True)
        self.assertEqual(issues, [])
        self.assertEqual({asset.stem: source_ids(asset) for asset in assets}, {
            "pics/cards/ZEN/Island.full": [full_art["id"], regular["id"], translated["id"]],
        })

    def test_forge_artist_disambiguates_reused_collector_numbers(self):
        edition = Edition.parse("[metadata]\nCode=X\n[cards]\n1 R Oak @Second\n1 C Oak @First\n")
        first = printing(1, "1", "Oak", "zhs", artist="First")
        second = printing(2, "1", "Oak", "en", artist="Second")
        assets, _, issues = plan_images("x", [second, first], edition, ["zhs", "en"])
        self.assertEqual(issues, [])
        self.assertEqual({asset.stem: source_ids(asset) for asset in assets}, {
            "pics/cards/X/Oak1.full": [first["id"]],
            "pics/cards/X/Oak2.full": [second["id"]],
        })

    def test_numbered_token_art_and_aliases_use_forge_edition_folder(self):
        edition = Edition.parse("""[metadata]
Code=ONE
Code2=XX
[tokens]
3 r_1_1_phyrexian_goblin
3 r_1_1_phyrexian_goblin_haste
11 c_1_1_a_phyrexian_mite_toxic_noblock
12 c_1_1_a_phyrexian_mite_toxic_noblock
""")
        cards = [printing(n, str(n), name, "en", set_code="tone", layout="token", set_type="token")
                 for n, name in ((3, "Phyrexian Goblin"), (11, "Phyrexian Mite"), (12, "Phyrexian Mite"))]
        assets, _, issues = plan_images("tone", cards, edition, ["en"])
        self.assertEqual(issues, [])
        self.assertEqual({asset.stem: source_ids(asset) for asset in assets}, {
            f"pics/tokens/ONE/3_{slug}": [cards[0]["id"]]
            for slug in ("r_1_1_phyrexian_goblin", "r_1_1_phyrexian_goblin_haste")
        } | {
            f"pics/tokens/ONE/{n}_c_1_1_a_phyrexian_mite_toxic_noblock": [cards[i]["id"]]
            for i, n in enumerate((11, 12), 1)
        })
