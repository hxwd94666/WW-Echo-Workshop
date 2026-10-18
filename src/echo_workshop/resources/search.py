# 提供离线中文拼音和首字母检索。
"""Offline literal/full-pinyin/initials matching shared by all user searches."""

from functools import lru_cache
import json
import unicodedata
from echo_workshop.application.text import simplify_echo_text
from echo_workshop.resources.paths import builtin_path


@lru_cache(maxsize=1)
def _readings():
    return json.loads(builtin_path('pinyin_table.json').read_text(encoding='utf8'))


def _normal(text):
    return unicodedata.normalize('NFKC', str(text)).casefold().strip()


@lru_cache(maxsize=4096)
def search_keys(text):
    original = _normal(text)
    plain = _normal(simplify_echo_text(original))
    readings = _readings()
    # Keep alternatives for names such as 长离; bound heteronym expansion.
    full, initials = [''], ['']
    for char in plain:
        choices = readings.get(char, [char])
        full = list(dict.fromkeys(prefix + part for prefix in full for part in choices))[:32]
        initials = list(
            dict.fromkeys(prefix + part[0] for prefix in initials for part in choices if part)
        )[:32]

    def compact(value):
        return ''.join(c for c in value if c.isalnum())

    return '\n'.join(
        dict.fromkeys(
            [original, plain, *(compact(s) for s in full), *(compact(s) for s in initials)]
        )
    )


def search_matches(text, query):
    query = _normal(query)
    if not query:
        return True
    if query in _normal(text) or query in _normal(simplify_echo_text(text)):
        return True
    # Whitespace-separated pinyin is equivalent to joined pinyin, not a
    # replacement for literal numeric/percentage/UUID search semantics.
    compact = ''.join(c for c in query if c not in " '\t")
    return bool(
        compact and compact.isascii() and compact.isalpha() and compact in search_keys(str(text))
    )
