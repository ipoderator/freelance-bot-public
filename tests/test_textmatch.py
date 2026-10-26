from __future__ import annotations

from pathlib import Path

import pytest

from freelance_bot import textmatch
from freelance_bot.config import load_settings
from freelance_bot.textmatch import find_terms

INNOCENT_PHRASES = (
    "напишите стоимость",
    "в компании",
    "интеграции",
    "email",
    "description",
    "storage",
    "агентство",
    "axios",
    "scenarios",
)


@pytest.mark.parametrize(
    ("text", "term"),
    [
        ("напишите стоимость", "апи"),
        ("работаем в компании", "ии"),
        ("интеграции", "ии"),
        ("email", "ai"),
        ("description", "script"),
        ("storage", "rag"),
        ("axios", "ios"),
        ("scenarios", "ios"),
        ("агентство недвижимости", "агенты"),
        ("rest apiary", "rest api"),
        ("робот для склада", "бот для"),
    ],
)
def test_term_inside_another_word_is_not_found(text: str, term: str) -> None:
    assert find_terms(text, [term]) == []


@pytest.mark.parametrize(
    ("text", "term"),
    [
        ("Нужен AI-ассистент", "ai"),
        ("Бот с ИИ.", "ии"),
        ("Доступ к API: ключ есть", "api"),
        ("приложение для iOS", "ios"),
        ("RAG по документам", "rag"),
        ("нужна доработка парсинга сайта", "парсинг"),
        ("Python-скрипты для выгрузки", "скрипт"),
        ("написать REST  API для сервиса", "rest api"),
        ("Сделать Бот Для записи", "бот для"),
        ("пишем на make.com", "make.com"),
        ("макет в UI/UX", "ui/ux"),
        ("конфигурация 1С:Предприятие", "1с:предприятие"),
    ],
)
def test_term_at_word_boundary_is_found_case_insensitive(text: str, term: str) -> None:
    assert find_terms(text, [term]) == [term]


def test_returns_matched_terms_in_given_order_without_duplicates() -> None:
    text = "Telegram-бот на Python, python и FastAPI; ещё python"
    terms = ["fastapi", "python", "django", "telegram", "python"]
    assert find_terms(text, terms) == ["fastapi", "python", "telegram"]


def test_empty_inputs() -> None:
    assert find_terms("", ["python"]) == []
    assert find_terms("python", []) == []
    assert find_terms("python", ["", "   "]) == []


def test_no_settings_keyword_matches_innocent_phrases(repo_root: Path, tmp_path: Path) -> None:
    s = load_settings(repo_root / "settings.toml", env_file=tmp_path / "none.env")
    keywords = [kw for d in s.directions.values() for kw in d.keywords]
    keywords += [w for words in s.exclusions.values() for w in words]
    for phrase in INNOCENT_PHRASES:
        assert find_terms(phrase, keywords) == [], phrase


@pytest.mark.parametrize(
    ("text", "term"),
    [
        ("ДЕЛАЮ ПАРСИНГ САЙТОВ", "парсинг"),
        ("KİT для сборки", "kit"),  # «İ»: str.lower() — два символа, регулярка — «i»
        ("ᲀакансия бэкенд", "вакансия"),  # «ᲀ» регулярка с IGNORECASE считает «в»
        ("ſcrapy и python", "scrapy"),  # «ſ» — «s»
        ("ΣΕΡΒΙΣ на python", "σερβισ"),  # конечная сигма после lower() — «ς»
        ("бот  для\nзаписи", "бот для"),  # слова фразы — через любые пробелы
    ],
)
def test_prefilter_keeps_every_regex_match(text: str, term: str) -> None:
    """Быстрый отсев по подстроке не теряет ничего, что нашла бы регулярка."""
    assert textmatch._pattern(term).search(text)
    assert find_terms(text, [term]) == [term]


def test_prefilter_gives_same_result_as_regex_alone(repo_root: Path, tmp_path: Path) -> None:
    s = load_settings(repo_root / "settings.toml", env_file=tmp_path / "none.env")
    terms = [kw for d in s.directions.values() for kw in d.keywords]
    terms += [w for words in s.exclusions.values() for w in words]
    texts = [*INNOCENT_PHRASES, "Telegram-бот на Python и FastAPI, парсинг Wildberries в Kaspi",
             "Нужен AI-ассистент: RAG по документам, REST  API, интеграция с amoCRM"]  # fmt: skip
    for text in texts:
        alone = [t for t in dict.fromkeys(terms) if (p := textmatch._pattern(t)) and p.search(text)]
        assert find_terms(text, terms) == alone, text
