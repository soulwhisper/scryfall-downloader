"""Values shared by the REST server, sync client, and health probe."""

import os
import re


API_PREFIX = "/api/v1"
DEFAULT_PORT = 8765
SET_CODE = re.compile(r"[a-zA-Z0-9]{1,12}")
BEARER_TOKEN = re.compile(r"[A-Za-z0-9._~+/-]+=*\Z")
DEFAULT_LANGUAGES = ("zhs", "en")
LANGUAGE_CODES = frozenset("en es fr de it pt ja ko ru zhs zht he la grc ar sa ph qya dw tlh".split())
LANGUAGE_ALIASES = {
    "zh-cn": "zhs", "zhcn": "zhs", "zh-hans": "zhs",
    "zh-tw": "zht", "zhtw": "zht", "zh-hant": "zht",
}
TERMINAL = frozenset({"completed", "partial", "failed"})


def default_port() -> int:
    name = "DOWNLOADER_PORT" if "DOWNLOADER_PORT" in os.environ else "DEFAULT_PORT"
    try:
        port = int(os.environ.get(name, DEFAULT_PORT))
    except ValueError:
        port = 0
    if not 1 <= port <= 65535:
        raise ValueError(f"{name} must be an integer between 1 and 65535")
    return port


def downloader_api_token() -> str | None:
    token = os.environ.get("DOWNLOADER_API_TOKEN")
    if token is not None and not BEARER_TOKEN.fullmatch(token):
        raise ValueError("DOWNLOADER_API_TOKEN must be a nonempty Bearer token")
    return token


def normalize_languages(languages) -> list[str]:
    codes = (language.strip().lower().replace("_", "-") for language in languages)
    return list(dict.fromkeys(LANGUAGE_ALIASES.get(code, code) for code in codes))
