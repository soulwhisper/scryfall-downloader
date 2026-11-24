"""Service identity, writable storage preflight, and offline ownership repair."""

from contextlib import closing
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory

from .imagefiles import IMAGE_SUFFIXES


def set_identity(uid: int, gid: int):
    if not hasattr(os, "setuid"):
        raise ValueError("UID/GID selection requires a POSIX system")
    if (os.geteuid(), os.getegid()) == (uid, gid):
        return
    if os.geteuid() != 0:
        raise ValueError("Run as the requested UID:GID, or start as root to drop privileges")
    os.setgroups([])
    os.setgid(gid)
    os.setuid(uid)


def init_state_owner(root: Path, uid: int, gid: int):
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise ValueError("--init-state-owner requires a root launcher")
    root.mkdir(parents=True, exist_ok=True)
    info = root.lstat()
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError(f"Expected a real state directory: {root}")
    if (info.st_uid, info.st_gid) != (uid, gid):
        os.chown(root, uid, gid, follow_symlinks=False)


def preflight(root: Path):
    """Test create, chmod, replace, read and removal as the serving identity."""
    pics, cache = root / "pics", root / "cache"
    root.mkdir(parents=True, exist_ok=True)
    for path in (root, cache, cache / "scryfall", cache / "forge", root / "extras",
                 pics, pics / "cards", pics / "tokens", pics / "printings"):
        try:
            path.mkdir(exist_ok=True)
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise NotADirectoryError("expected a real directory")
            with TemporaryDirectory(prefix=".scryfall-check-", dir=path) as scratch:
                probe = Path(scratch) / "probe"
                probe.write_bytes(b"ok")
                probe.chmod(0o644)
                if probe.replace(probe.with_name("published")).read_bytes() != b"ok":
                    raise OSError("could not read back the published file")
        except OSError as exc:
            raise OSError(f"Storage preflight: {path}: {exc}") from exc


def repair_ownership(root: Path, uid: int, gid: int, *, dry_run: bool) -> tuple[int, int]:
    """Change only the Forge directory tree and image entries, without following links."""
    if not hasattr(os, "fwalk"):
        raise ValueError("Ownership repair requires a POSIX system with dirfd support")
    changed = skipped = 0

    def fail(error):
        raise error

    for top in (root / "pics", root / "extras"):
        try:
            top_mode = top.lstat().st_mode
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(top_mode):
            raise ValueError(f"Expected a real directory: {top}")
        with closing(os.fwalk(top, follow_symlinks=False, onerror=fail)) as walk:
            for path, dirs, files, directory in walk:
                if top.name == "pics" and path == str(top):
                    dirs[:] = [name for name in dirs if name in ("cards", "tokens", "printings")]
                    files = []
                info = os.fstat(directory)
                if (info.st_uid, info.st_gid) != (uid, gid):
                    if dry_run:
                        print(f"{path!r}: {info.st_uid}:{info.st_gid} -> {uid}:{gid}")
                    else:
                        os.fchown(directory, uid, gid)
                    changed += 1
                for name in files:
                    if Path(name).suffix.lower() not in IMAGE_SUFFIXES:
                        continue
                    info = os.stat(name, dir_fd=directory, follow_symlinks=False)
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        skipped += 1
                        continue
                    if (info.st_uid, info.st_gid) == (uid, gid):
                        continue
                    if dry_run:
                        print(f"{os.path.join(path, name)!r}: {info.st_uid}:{info.st_gid} -> {uid}:{gid}")
                    else:
                        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                        try:
                            current = os.fstat(fd)
                            if not os.path.samestat(info, current) or current.st_nlink != 1:
                                raise OSError(f"Image changed during ownership repair: {name}")
                            os.fchown(fd, uid, gid)
                        finally:
                            os.close(fd)
                    changed += 1
    return changed, skipped
