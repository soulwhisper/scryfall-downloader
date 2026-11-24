"""Values shared by the REST server, sync client, and health probe."""

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


def normalize_languages(languages) -> list[str]:
    codes = (language.strip().lower().replace("_", "-") for language in languages)
    return list(dict.fromkeys(LANGUAGE_ALIASES.get(code, code) for code in codes))
