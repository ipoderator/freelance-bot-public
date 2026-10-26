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


def _case_classes() -> dict[int, str]:
    """Буквы, которые re.IGNORECASE считает равными вопреки str.lower() («ᲀ» и «в», «ſ» и
    «s»), → одна буква класса. Нет таблицы в этой версии Python — пусто, и отсева не будет."""
    try:
        from re import _casefix  # type: ignore[attr-defined]
    except ImportError:  # pragma: no cover - другая версия Python
        return {}
    classes: dict[int, str] = {}
    for key, others in _casefix._EXTRA_CASES.items():
        group = (key, *others)
        rep = chr(min(group))
        for code in group:
            classes[code] = rep
    return classes


_CASE_CLASSES = _case_classes()
_RARE_CASE_RE = re.compile(
    "[" + re.escape("".join(chr(c) for c, rep in _CASE_CLASSES.items() if chr(c) != rep)) + "]"
)


@lru_cache(maxsize=1024)
def _fold(text: str) -> str:
    """Текст для быстрого отсева: буквы, которые регулярка с IGNORECASE считает равными,
    здесь одинаковые, по одной на букву — подстрока остаётся подстрокой. «İ» — единственная
    буква, у которой str.lower() даёт два символа, а движок re — «i»."""
    text = text.replace("İ", "i").lower()
    return text.translate(_CASE_CLASSES) if _RARE_CASE_RE.search(text) else text


@lru_cache(maxsize=4096)
def _words(term: str) -> tuple[str, ...]:
    return tuple(_fold(w) for w in term.split())


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
    # Отсев: слова термина, которых нет в тексте даже подстрокой, регулярка тоже не найдёт;
    # без него — миллион поисков регуляркой на подбор и десятки секунд (2026-10-26)
    folded = _fold(text) if _CASE_CLASSES else None
    for term in dict.fromkeys(terms):
        if folded is not None:
            first, *rest = _words(term) or ("",)
            if first not in folded or (rest and not all(w in folded for w in rest)):
                continue
        pattern = _pattern(term)
        if pattern is not None and pattern.search(text):
            found.append(term)
    return found
