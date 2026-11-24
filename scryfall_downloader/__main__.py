"""Run the Scryfall downloader or perform offline ownership repair."""

import argparse
import os
from pathlib import Path
import signal
import sqlite3

from .jobs import Jobs
from .ownership import init_state_owner, preflight, repair_ownership, set_identity
from .protocol import DEFAULT_PORT, default_port, downloader_api_token
from .server import Server


def interrupt(_signum, _frame):
    raise KeyboardInterrupt


def posix_id(value: str) -> int:
    try:
        number = int(value)
    except ValueError:
        number = -1
    if not 0 <= number < 2**32 - 1:
        raise argparse.ArgumentTypeError("expected a numeric POSIX UID or GID")
    return number


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", default="127.0.0.1", help="HTTP listen address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, help=f"HTTP port (overrides DEFAULT_PORT; default: {DEFAULT_PORT})")
    parser.add_argument("--data", type=Path, default=Path(__file__).resolve().parents[1] / "data",
                        help="Output and cache directory (default: data/ at the repository root)")
    parser.add_argument("--forge-editions", type=Path, help="Forge's res/editions directory; overrides automatic edition lookup")
    parser.add_argument("--workers", type=int, default=4, choices=range(1, 9), help="Concurrent image downloads (default: 4)")
    parser.add_argument("--proxy", help="Outbound HTTP CONNECT proxy URL (default: use HTTPS_PROXY)")
    parser.add_argument("--uid", type=posix_id, help="Service UID or target UID for ownership repair")
    parser.add_argument("--gid", type=posix_id, help="Service GID or target GID for ownership repair")
    parser.add_argument("--init-state-owner", action="store_true",
                        help="Root launcher: chown only the data directory itself before dropping privileges")
    parser.add_argument("--archive-all", action="store_true", help="Queue one full archive on first start with this data directory")
    parser.add_argument("--repair-ownership", action="store_true", help="Repair image ownership offline, then exit")
    parser.add_argument("--dry-run", action="store_true", help="Preview --repair-ownership without changes")
    args = parser.parse_args()
    if args.port is not None and not 0 <= args.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    if (args.uid is None) != (args.gid is None) or (args.repair_ownership and args.uid is None):
        parser.error("--uid and --gid must be provided together; both are required for ownership repair")
    if args.dry_run and not args.repair_ownership:
        parser.error("--dry-run requires --repair-ownership")
    if args.init_state_owner and (args.uid in (None, 0) or args.repair_ownership):
        parser.error("--init-state-owner requires --uid with a non-root UID and --gid for serving")
    try:
        root = args.data.expanduser().resolve()
        if args.repair_ownership:
            changed, skipped = repair_ownership(root, args.uid, args.gid, dry_run=args.dry_run)
            action = "Would change" if args.dry_run else "Changed"
            print(f"{action} ownership on {changed} entries; skipped {skipped} links or non-regular images")
            return
        port = args.port if args.port is not None else default_port()
        if args.init_state_owner:
            init_state_owner(root, args.uid, args.gid)
        if args.uid is not None:
            set_identity(args.uid, args.gid)
        editions = args.forge_editions.expanduser().resolve() if args.forge_editions else None
        webp = os.environ.get("SCRYFALL_ENABLE_WEBP", "0")
        if webp not in ("0", "1"):
            raise ValueError("SCRYFALL_ENABLE_WEBP must be 0 or 1")
        token = downloader_api_token()
        preflight(root)
        jobs = Jobs(root, args.workers, editions, args.proxy, webp=webp == "1")
        server = Server((args.bind, port), jobs, token)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"Error: {exc}\n")
    signal.signal(signal.SIGTERM, interrupt)
    with server:
        jobs.start(archive_all=args.archive_all)
        try:
            print(f"Listening on http://{args.bind}:{server.server_port}; data: {jobs.root}", flush=True)
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            jobs.close()


if __name__ == "__main__":
    main()
