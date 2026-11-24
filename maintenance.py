"""Inspect Forge's lookup order and optionally migrate unambiguous legacy image names."""

from contextlib import contextmanager
from dataclasses import dataclass, replace
import os
from pathlib import Path
import stat

from imagefiles import IMAGE_SUFFIXES
from upstream import Asset, SavedImage, check_interrupted, local_images


@dataclass(frozen=True, slots=True)
class Inspection:
    outcome: str
    saved: SavedImage | None = None
    below_quality: bool = False
    legacy: bool = False
    issue: str | None = None


@contextmanager
def _directory(root: Path, parent: Path):
    """Pin the actual directory for all mutations, without following directory symlinks."""
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    fd = os.open(root, flags)
    try:
        for part in parent.relative_to(root).parts:
            child = os.open(part, flags, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _stat(fd: int, name: str) -> os.stat_result | None:
    try:
        return os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _same_file(expected: os.stat_result, current: os.stat_result | None, links: int) -> bool:
    return (current is not None and stat.S_ISREG(current.st_mode) and current.st_nlink == links
            and os.path.samestat(expected, current) and current.st_mode == expected.st_mode
            and current.st_uid == expected.st_uid and current.st_gid == expected.st_gid
            and current.st_size == expected.st_size and current.st_mtime_ns == expected.st_mtime_ns)


def _conflict(fd: int, names: set[str], *allowed: str) -> str | None:
    # Include case variants and disabled WebP: neither should be silently shadowed.
    return min((name for name in os.listdir(fd) if name not in allowed and name.casefold() in names),
               default=None)


def maintain(root: Path, asset: Asset, previous: SavedImage | None, stop, minimum: str | None,
             webp: bool, *, tidy: bool = False, apply: bool = False) -> Inspection:
    """Scan the first Forge-visible file; only explicit tidy apply can change names."""
    images = local_images(root, asset, previous, stop, webp)
    check_interrupted(stop)
    if not images:
        if asset.sources:
            return Inspection("missing", issue=f"{asset.stem}: no local image")
        return Inspection("unavailable", issue=f"{asset.stem}: no local image or usable source")

    first = images[0]
    legacy = any(image.legacy for image in images)
    if first.saved is None or first.shape is None:
        return Inspection("corrupt", legacy=legacy,
                          issue=f"{first.path}: invalid image or checksum mismatch")
    below = minimum is not None and not first.shape.meets(minimum)
    issue = f"{first.path}: below {minimum} quality" if below else None
    if issue is None and first.legacy:
        issue = f"{first.path}: legacy Forge filename"
    result = Inspection("cached", below_quality=below, legacy=legacy, issue=issue)
    if not tidy or not asset.legacy_border or not legacy:
        return result

    if len(images) != 1:
        other = images[1] if first.legacy else next(image for image in images if image.legacy)
        return replace(result, issue=f"{first.path}: tidy conflict with {other.path}")
    if first.info.st_nlink != 1:
        return replace(result, issue=f"{first.path}: refusing to tidy a hardlinked image")

    stem = root / asset.stem
    source = first.path
    target = stem.with_name(stem.name + first.saved.suffix)
    legacy_stem = stem.name.removesuffix(".full") + ".fullborder"
    names = {(base + suffix).casefold() for base in (stem.name, legacy_stem) for suffix in IMAGE_SUFFIXES}
    with _directory(root, source.parent) as fd:
        if (other_name := _conflict(fd, names, source.name)) is not None:
            return replace(result, issue=f"{source}: tidy conflict with {source.with_name(other_name)}")
        current = _stat(fd, source.name)
        if (not _same_file(first.info, current, 1)
                or current.st_ctime_ns != first.info.st_ctime_ns):
            return replace(result, issue=f"{source}: image changed during inspection")
        check_interrupted(stop)
        if not apply:
            return replace(result, issue=f"{source}: would rename to {target}")

        # link is the commit point: it creates the exact name without ever overwriting.
        try:
            os.link(source.name, target.name, src_dir_fd=fd, dst_dir_fd=fd, follow_symlinks=False)
        except FileExistsError:
            return replace(result, issue=f"{target}: tidy destination appeared during inspection")
        except FileNotFoundError:
            return replace(result, issue=f"{source}: image disappeared during tidy")

        # Never clean up unfamiliar names. If anything changed, leave all names in
        # place, including the link that may be the last reference to the old bytes.
        if (other_name := _conflict(fd, names, source.name, target.name)) is not None:
            return replace(result, issue=f"{source}: tidy conflict with {source.with_name(other_name)}")
        linked = _stat(fd, target.name)
        current = _stat(fd, source.name)
        if (not _same_file(first.info, linked, 2) or not _same_file(first.info, current, 2)
                or linked.st_ctime_ns != current.st_ctime_ns):
            return replace(result, issue=f"{source}: image or destination changed during tidy")
        try:
            os.unlink(source.name, dir_fd=fd)
        except FileNotFoundError:
            return replace(result, issue=f"{source}: image disappeared during tidy")
    return Inspection("tidied", replace(first.saved, file_name=None), below, legacy=True)
