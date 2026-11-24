"""Check the local worker without using an HTTP proxy or API credential."""

from contextlib import closing
from http.client import HTTPConnection
import sys

from .protocol import API_PREFIX, default_port


def main() -> int:
    try:
        port = default_port()
    except ValueError as exc:
        print(exc, file=sys.stderr)
        return 1
    with closing(HTTPConnection("127.0.0.1", port, timeout=3)) as connection:
        connection.request("GET", API_PREFIX + "/health")
        with connection.getresponse() as response:
            return int(response.status != 200)


if __name__ == "__main__":
    raise SystemExit(main())
