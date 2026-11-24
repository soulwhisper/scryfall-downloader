"""Check the local worker without using an HTTP proxy or API credential."""

from contextlib import closing
from http.client import HTTPConnection

from .protocol import API_PREFIX, DEFAULT_PORT


def main() -> int:
    with closing(HTTPConnection("127.0.0.1", DEFAULT_PORT, timeout=3)) as connection:
        connection.request("GET", API_PREFIX + "/health")
        with connection.getresponse() as response:
            return int(response.status != 200)


if __name__ == "__main__":
    raise SystemExit(main())
