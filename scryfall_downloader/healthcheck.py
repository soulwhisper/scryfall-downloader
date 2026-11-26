"""Check the local worker and optionally the Scryfall API."""

from contextlib import closing
from http.client import HTTPConnection, HTTPException
import json
import os
import sys

from .protocol import API_PREFIX, default_port
from .upstream import NETWORK_ERRORS, UpstreamError, Web


def main() -> int:
    try:
        port = default_port()
        upstream = os.environ.get("DOWNLOADER_HEALTH_UPSTREAM", os.environ.get("SCRYFALL_HEALTH_UPSTREAM", "0"))
        if upstream not in ("0", "1"):
            raise ValueError("DOWNLOADER_HEALTH_UPSTREAM must be 0 or 1")
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    try:
        with closing(HTTPConnection("127.0.0.1", port, timeout=3)) as connection:
            connection.request("GET", API_PREFIX + "/health")
            with connection.getresponse() as response:
                if response.status != 200:
                    raise OSError(f"HTTP {response.status}")
    except (OSError, HTTPException) as exc:
        print(f"Downloader API: {exc}", file=sys.stderr)
        return 1
    if upstream == "1":
        try:
            with Web(timeout=4).open("https://api.scryfall.com/bulk-data/all-cards",
                                     "api.scryfall.com", "application/json") as reply:
                info = json.loads(reply.read(65536)) if reply.status == 200 else None
                if not isinstance(info, dict) or info.get("type") != "all_cards":
                    raise UpstreamError("expected bulk metadata")
        except (UpstreamError, *NETWORK_ERRORS) as exc:
            print(f"Scryfall API: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
