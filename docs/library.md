# Forge image library

## Layout and edition metadata

Paths are relative to the data directory (`--data`):

- `pics/cards/<SHORT_CODE>/` holds regular card images with Forge's names,
  not localized printed names. For example, `MOM/Invasion of Ravnica.full.png`
  and `MOM/Guildpact Paragon.full.png` are the two faces of one card.
- `pics/tokens/<SET_CODE>/` holds tokens. When Forge defines the token, the
  folder uses its edition `Code` (not `Code2`) and the file uses its token
  name. Otherwise, the folder uses the uppercase Scryfall code.
  A double-faced token with a registered front also saves its back under
  Forge's collector-number and back-name key in that folder, for example
  `MOM/16_phyrexian.png`. Explicit `☇` entries remain available under their
  registered names.

Forge edition files supply the short folder code (`Code2`, when defined) and
art numbering. The downloader resolves editions by `ScryfallCode` and
`TokensCode` first, then falls back to the edition filename. Remote metadata
comes from the cached Forge repository index, so names such as Mercadian
Masques and The Brothers' War do not need exact filename matches. The local
`--forge-editions` directory remains authoritative when supplied. In a
container, mount that directory read-only (for example with
`-v /path/to/Forge/res/editions:/editions:ro`), then append
`--forge-editions /editions` after the image name. When there is no matching
edition, the downloader uses the uppercase Scryfall code and infers names
and numbering. Check the job warning if Forge's image keys do not match.

Request Planechase planes by their Scryfall codes (`ohop`, `opc2`, `opca`).
Forge registers those printings in its parent editions, so their images go
to `cards/PCH`, `cards/PC2`, and `cards/PCA`, respectively. Reversible cards
use Forge's registered `☇` art slots when present; an unregistered extra face
is not written to an invented card key. Meld backs use the related Scryfall
meld result. Specialize uses the Forge card script's primary and five color
face names, with each color's Scryfall printing as its source. These script
and relationship lookups are cached under the data directory.

To fill corrected paths in an existing library, rerun the affected sets.
Old inferred files are not moved or deleted.

## Configure Forge

Set `cardPicsDir` in `forge.profile.properties` to the absolute path of
`data/pics/cards/` (or the equivalent in your data directory) and restart
Forge. For tokens, copy the corresponding folders from `pics/tokens/` into
Forge's cache at `pics/tokens/`. Prefer a separate card image directory:
Forge's local lookup order is JPG → PNG → WebP, so an existing JPEG with the
same stem can take precedence over a PNG. Use separate data directories for
independent language collections.

## Full catalogue sync

Use **Library operations → Sync full catalogue** or enable the
[first-start job](operations.md#first-start-archive). The job streams
[Scryfall's All Cards export](https://scryfall.com/docs/api/bulk-data) into a
local index and visits every set in it, newest first by the earliest dated
printing in each set. It does not filter by set type, legality, paper or
digital status, or language. Missing and placeholder scans are unavailable.

The only image outputs are `pics/cards/` and `pics/tokens/`. A borderless,
extended-art, showcase, or full-art printing gets its own numbered filename
when Forge has a distinct art slot. Two printings can have the same
illustration but different collector numbers and frames; they are not
merged. For example `WAR #97` and `#97★` remain art 1 and art 2.

For sources sharing one Forge key, prefer Phyrexian (`ph`), then
`ja` tagged `jpwalker`, then alternate-art treatments, then `zhs`, `en`,
and other languages. Ordinary Japanese has no language boost. Per-set
requests still honor the selected languages, and valid existing images
are kept unless an explicit replacement policy permits an upgrade.

Forge cannot address a second image for a shared key. A confirmed key
takes precedence over an inferred one. Unmapped variants of a known card
and unresolved key collisions appear in the receipt; they do not overwrite
another art or get a second copy outside Forge's folders.

## Scan and repair

Use **Scan library** or submit an [API scan](api.md#job-requests-and-receipts).
It inspects local Forge keys in lookup order without writing images or
fetching CDN artwork. Review `missing`, `unavailable` (no local image or
usable source), `corrupt`, `below_quality`, `legacy`, and sample `issues`
in the job receipt.

Before downloading, the server validates existing files, including manually
copied images at confirmed Forge keys and confirmed `.fullborder` aliases.
It checks structure and dimensions, verifies PNG chunk checksums on new or
unindexed files, and records SHA-256 for later checks. Unreadable files
surface as permission errors. Run **Download / repair** with no quality floor
to replace missing or corrupt images. Incomplete transfers and temporary CDN
errors retry with backoff. A higher-priority filename stops masking a
replacement only after the replacement is verified.

For card images, the receipt in `images.sqlite3` binds the checksum and
source to the Scryfall printing and face independently of the Forge
filename. If inferred art numbering changes, a valid checksum alone is not
enough: **Scan library** reports the missing correct art, and **Download /
repair** replaces a proven mismatch only after verifying the replacement.
Older receipts can be bound when their source matches the current asset.
Valid files without verifiable provenance at inferred keys are reported and
left untouched. Verify or move them aside before attempting repair there.

## Quality and refresh

For new images, the downloader checks languages in preference order and
skips placeholders. In each language it prefers Scryfall's native full-card
PNG (normally 744 × 1040), then the largest supported fallback. Images are
not resized, cropped, or re-encoded.

| `min_quality` | Minimum dimensions (either orientation) |
| --- | --- |
| `small` | 146 × 204 |
| `normal` | 488 × 680 |
| `large` | 672 × 936 |
| `png` | 744 × 1040, lossless PNG |

A quality upgrade replaces a valid file only with an improvement that meets
the floor. Downloads can still fill missing keys with below-floor images.
`refresh_language` prefers a higher-ranked language when the local language
is known; untracked images are not assumed to be in any language.
`refresh_scan` accepts a later Scryfall `image_updated_at` for the same printing
and language. Refreshes must preserve or improve both dimensions and cannot
replace a PNG with a lossy format.

Card metadata is cached for 24 hours per set and language selection. English
is always included as a naming and coverage reference, not as an image
fallback unless you select it. The refresh options do not bypass this cache.

## WebP opt-in

Set `SCRYFALL_ENABLE_WEBP=1` on the server only if every Forge client reading
the library can load `.webp` (see [Forge #12983](https://github.com/Card-Forge/forge/pull/12983)).
The default is `0`. PNG remains first within a language; when enabled, WebP
`display`, `grid`, and `thumb` precede JPEG at the corresponding quality tiers.
WebP is saved and validated natively. Older clients and a downloader with
WebP disabled do not use WebP as a local image. Disabling the gate does not
convert or delete previously saved WebP files.

## Legacy filename approvals

**Preview renames** proposes `.fullborder` → `.full` only for confirmed Forge
keys. Open the preview's receipt, choose **Load full rename plan**, review
all rows, check **I reviewed the entire rename plan**, then choose **Apply
approved renames**. API clients use the [preview and approval endpoints](api.md#legacy-rename-plans).

Apply uses only the saved plan and rechecks the source checksum and the
destination. It preserves bytes and ownership, never overwrites a destination,
and reports conflicts rather than renaming over them. Preview again if the
approved files have changed. Tidy requires POSIX directory operations and
hard links. Stop other writers to the picture folders before applying.

References: [Scryfall image formats](https://scryfall.com/docs/api/images),
[Forge image keys](https://github.com/Card-Forge/forge/blob/master/forge-core/src/main/java/forge/util/ImageUtil.java),
and [Forge image lookup](https://github.com/Card-Forge/forge/blob/master/forge-core/src/main/java/forge/ImageKeys.java).
