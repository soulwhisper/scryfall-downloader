# Forge image library

## Layout and edition metadata

Paths are relative to the data directory (`--data`):

- `pics/cards/<SHORT_CODE>/` holds regular card images with Forge's names,
  not localized printed names. For example, `MOM/Invasion of Ravnica.full.png`
  and `MOM/Guildpact Paragon.full.png` are the two faces of one card.
- `pics/tokens/<SET_CODE>/` holds tokens. When Forge defines the token, the
  folder uses its edition `Code` (not `Code2`) and the file uses its token
  name. Otherwise, the folder uses the uppercase Scryfall code.
- `pics/printings/<SET_CODE>/<LANG>/<COLLECTOR>_<UUID>[_<FACE>].<EXT>`
  holds the best available source for each printing and face in a full archive.
  Forge does not read this directory.
- `extras/<SET_CODE>/` holds printings whose filenames would collide with a
  Forge key, rather than overwriting another printing.

Forge edition files supply the short folder code (`Code2`, when defined) and
art numbering. The downloader looks for editions on GitHub unless you pass
`--forge-editions /path/to/Forge/res/editions` to use your Forge release. In a
container, mount that directory read-only (for example with
`-v /path/to/Forge/res/editions:/editions:ro`), then append
`--forge-editions /editions` after the image name. When there is no matching
edition, the downloader uses the uppercase Scryfall code and infers names
and numbering. Check the job warning if Forge's image keys do not match.

## Configure Forge

Set `cardPicsDir` in `forge.profile.properties` to the absolute path of
`data/pics/cards/` (or the equivalent in your data directory) and restart
Forge. For tokens, copy the corresponding folders from `pics/tokens/` into
Forge's cache at `pics/tokens/`. Prefer a separate card image directory:
Forge's local lookup order is JPG → PNG → WebP, so an existing JPEG with the
same stem can take precedence over a PNG. Use separate data directories for
independent language collections.

## Full catalogue archive

Use **Library operations → Run full archive** or enable the
[first-start job](operations.md#first-start-archive). This streams Scryfall's
[All Cards export](https://scryfall.com/docs/api/bulk-data) into an on-disk
index. The job includes every set represented in that export, ordered
newest first by the earliest dated printing in each set. Set type, format
legality, paper/digital status, and language do not exclude a printing.
Unreleased entries with real art are included too. Missing scans and
placeholders are reported as unavailable.

For each Forge key, prefer Phyrexian (`ph`), then Japanese alternate-art
planeswalkers (`ja` with Scryfall's `jpwalker` tag), then Simplified Chinese
(`zhs`), English (`en`), and other available languages. Ordinary Japanese
does not receive special priority. Different Forge art slots remain
distinct: e.g. `WAR #97★` is not a rename of `WAR #97`. Distinct UUIDs
and faces are archived even if their Forge keys collide. Finishes
on a single Scryfall printing do not imply separate image files.

Forge copies reuse verified archive bytes when available. The copies and
originals are independent files so editing one tree does not corrupt the
other. A second archive run validates and reuses intact local files rather
than replacing valid copies. Per-set download requests still respect the
languages explicitly selected.

## Scan and repair

Use **Scan library** or submit an [API scan](api.md#job-requests-and-receipts).
It inspects local Forge keys in lookup order without writing images or
fetching CDN artwork. Review `missing`, `unavailable` (no local image or
usable source), `corrupt`, `below_quality`, `legacy`, and sample `issues`
in the job receipt.

Before downloading, the server validates existing files, including manually
copied images and confirmed `.fullborder` aliases. It checks structure and
dimensions, verifies PNG chunk checksums on new or unindexed files, and
records SHA-256 for later checks. Unreadable files surface as permission
errors. Run **Download / repair** with no quality floor to replace missing
or corrupt images. Incomplete transfers and temporary CDN errors retry with
backoff. A valid image is normally kept; an exact-key variant that would
mask a replacement in Forge is removed only after a verified replacement
is available.

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
