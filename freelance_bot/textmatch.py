"""Поиск ключевых слов в тексте проекта по границам слов.

Правило (общее для настроек, скоринга и исключений):
- регистр не важен;
- термин начинается на границе слова («ии» не найдётся в «компании»);
- если термин (а у фразы — её последнее слово) не длиннее 3 символов, он должен
  и закончиться на границе слова («ios» не найдётся в «axios»);
- более длинный термин может быть началом слова: «парсинг» ловит «парсинга»;
- слова фразы разделяются любым количеством пробелов; кроме последнего слова,
  каждое должно совпасть целиком («бот для» не найдётся в «ботов для»).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache

EXACT_MAX_LEN = 3


@lru_cache(maxsize=4096)
def _pattern(term: str) -> re.Pattern[str] | None:
    words = term.split()
    if not words:
        return None
    body = r"\s+".join(re.escape(w) for w in words)
    end = r"(?!\w)" if len(words[-1]) <= EXACT_MAX_LEN else ""
    # Внутренние слова фразы — целиком: после каждого обязателен пробел (\s+) по построению.
    return re.compile(rf"(?<!\w){body}{end}", re.IGNORECASE)


def find_terms(text: str, terms: Iterable[str]) -> list[str]:
    """Термины из `terms`, найденные в `text`, в исходном порядке и без повторов."""
    found: list[str] = []
    if not text:
        return found
    for term in dict.fromkeys(terms):
        pattern = _pattern(term)
        if pattern is not None and pattern.search(text):
            found.append(term)
    return found
