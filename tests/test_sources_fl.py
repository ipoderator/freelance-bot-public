"""Источник FL.ru: разбор официального RSS (фикстура) и FlSource на фейковом транспорте."""

from __future__ import annotations

import html
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from freelance_bot.http import BlockedError, Fetcher, looks_blocked
from freelance_bot.sources import SourceFormatError
from freelance_bot.sources.fl import FlSource, parse_fl_rss

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def rss() -> str:
    return (FIXTURES / "fl_rss.xml").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def projects(rss):
    return {p.external_id: p for p in parse_fl_rss(rss)}


def test_fixture_is_not_treated_as_block_page(rss):
    assert looks_blocked(200, rss) is False


def test_parses_all_items_with_unique_ids(rss):
    items = parse_fl_rss(rss)
    assert len(items) == rss.count("<item>") == 60
    assert len({p.external_id for p in items}) == 60
    assert all(p.platform == "fl" and p.external_id.isdigit() for p in items)


def test_id_url_and_core_fields_from_real_item(projects):
    p = projects["5526235"]
    assert p.url == "https://www.fl.ru/projects/5526235/php-avtorizatsiya-registratsiya-.html"
    assert p.title == "PHP авторизация, регистрация"
    assert p.description == "Рега и QR"
    assert p.category == "Программирование / PHP"
    assert p.offers_count is None


def test_budget_extracted_and_removed_from_title(projects):
    p = projects["5526235"]
    assert (p.budget_min, p.budget_max) == (None, 15000)
    assert "Бюджет" not in p.title and "₽" not in p.title and "&#" not in p.title


def test_nested_parentheses_in_title_survive_budget_removal(projects):
    p = next(p for p in projects.values() if p.title.startswith("Telegram-бот консультант"))
    assert p.title == "Telegram-бот консультант на Python (OpenAI API)"
    assert p.budget_max == 15000


def test_budget_followed_by_for_all_marker(projects):
    p = next(p for p in projects.values() if p.title.startswith("Настройка Kanban-доски"))
    assert p.title == "Настройка Kanban-доски в Miro для управления операционными задачами компании"
    assert p.budget_max == 10000


def test_no_budget_gives_none(projects):
    p = next(p for p in projects.values() if p.title == "Telegram api")
    assert (p.budget_min, p.budget_max) == (None, None)


def test_for_all_marker_without_budget_is_removed_from_title(projects):
    p = next(p for p in projects.values() if p.title.startswith("Senior Python-разработчик"))
    assert "(для всех)" not in p.title
    assert p.budget_max is None


def test_pubdate_converted_to_utc(projects):
    p = projects["5526235"]
    assert p.published_at == datetime(2026, 10, 21, 20, 34, 59, tzinfo=UTC)
    assert p.published_at.utcoffset().total_seconds() == 0


def _rss(items: str) -> str:
    return (
        f'<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>{items}</channel></rss>'
    )


def _item(
    title: str,
    link: str = "https://www.fl.ru/projects/77/x.html",
    date="Wed, 21 Oct 2026 12:00:00 +0300",
):
    return (
        f"<item><title><![CDATA[{title}]]></title><link>{link}</link>"
        f"<description><![CDATA[Текст]]></description><pubDate>{date}</pubDate></item>"
    )


def test_pubdate_with_offset_is_shifted_to_utc():
    (p,) = parse_fl_rss(_rss(_item("Задача")))
    assert p.published_at == datetime(2026, 10, 21, 9, 0, tzinfo=UTC)


def test_budget_by_agreement_is_none():
    (p,) = parse_fl_rss(_rss(_item("Задача (Бюджет: по договоренности)")))
    assert p.budget_max is None and p.title == "Задача"


def test_budget_with_nbsp_thousands_separator():
    (p,) = parse_fl_rss(_rss(_item("Задача (Бюджет: 1 250 000 &#8381;)")))
    assert p.budget_max == 1250000


def test_item_without_project_id_is_skipped():
    items = _item("Хорошая") + _item("Без id", link="https://www.fl.ru/about/")
    assert [p.title for p in parse_fl_rss(_rss(items))] == ["Хорошая"]


def test_empty_channel_is_empty_list():
    assert parse_fl_rss(_rss("")) == []


@pytest.mark.parametrize(
    "bad", ["<rss><channel>", "не xml вообще", "<html><body>hi</body></html>", ""]
)
def test_broken_or_foreign_document_raises_format_error(bad):
    with pytest.raises(SourceFormatError):
        parse_fl_rss(bad)


# --- FlSource ---------------------------------------------------------------


def _fetcher(handler) -> Fetcher:
    async def no_sleep(_: float) -> None:
        return None

    return Fetcher(httpx.MockTransport(handler), sleep=no_sleep, min_interval=0)


async def test_flsource_merges_urls_and_dedupes(rss):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text=rss)

    urls = ["https://www.fl.ru/rss/?subcategory=798&category=5", "https://www.fl.ru/rss/all.xml"]
    source = FlSource(_fetcher(handler), urls)
    result = await source.fetch()
    assert source.platform == "fl"
    assert seen == urls
    assert len(result) == 60  # одна и та же лента дважды -> без дублей


async def test_flsource_propagates_block(rss):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    with pytest.raises(BlockedError):
        await FlSource(_fetcher(handler), ["https://www.fl.ru/rss/?x=1"]).fetch()


@pytest.mark.parametrize(
    ("link", "expected"),
    [
        ("https://www.fl.ru/projects/5/bot.html", "https://www.fl.ru/projects/5/bot.html"),
        ("https://fl.ru/projects/5/", "https://fl.ru/projects/5/"),
        ("https://evil.example/projects/5/", "https://www.fl.ru/projects/5/"),
        ("javascript:alert(1)//www.fl.ru/projects/5", "https://www.fl.ru/projects/5/"),
        ("http://www.fl.ru/projects/5/", "https://www.fl.ru/projects/5/"),
        ("https://www.fl.ru.evil.example/projects/5/", "https://www.fl.ru/projects/5/"),
    ],
)
def test_project_link_must_be_https_fl(link, expected):
    xml = (
        "<rss><channel><item><title>Бот (Бюджет: 1000 ₽)</title>"
        f"<link>{html.escape(link)}</link>"
        "<pubDate>Wed, 21 Oct 2026 09:00:00 GMT</pubDate></item></channel></rss>"
    )
    [project] = parse_fl_rss(xml)
    assert project.url == expected and project.external_id == "5"
