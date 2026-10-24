"""Источник Kwork: разбор блока wants из публичной страницы биржи (урезанная фикстура)."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from freelance_bot.http import BlockedError, Fetcher, looks_blocked
from freelance_bot.sources import SourceFormatError
from freelance_bot.sources.kwork import KworkSource, parse_kwork_projects

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def page() -> str:
    return (FIXTURES / "kwork_projects.html").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def projects(page):
    return {p.external_id: p for p in parse_kwork_projects(page)}


def test_fixture_is_not_treated_as_block_page(page):
    assert looks_blocked(200, page) is False


def test_real_page_with_smartcaptcha_script_is_not_a_block():
    # Обычная страница Kwork подключает скрипт Yandex SmartCaptcha для форм.
    snippet = (FIXTURES / "kwork_smartcaptcha_snippet.html").read_text(encoding="utf-8")
    assert "smartcaptcha" in snippet
    assert looks_blocked(200, snippet) is False


def test_parses_twelve_unique_projects(projects):
    assert len(projects) == 12
    assert all(p.platform == "kwork" for p in projects.values())


def test_core_fields_for_telegram_bot_project(projects):
    p = projects["3268440"]
    assert p.url == "https://kwork.ru/projects/3268440/view"
    assert p.title == "Telegram-бот для мониторинга транспортных заявок"
    assert p.description.startswith(
        "Название: Telegram-бот для мониторинга заявок Wildberries Logistics"
    )
    assert "\r" not in p.description
    assert p.budget_max == 10000 and p.budget_min is None
    assert p.extra["possible_price_limit"] == 30000
    assert p.offers_count == 48
    assert p.deadline_days == 10


def test_zero_offers_is_zero_not_none(projects):
    assert projects["3267861"].offers_count == 0


def test_date_active_moscow_to_utc(projects):
    # «2026-10-21 21:34:41» по Москве = 18:34:41 UTC
    assert projects["3268440"].published_at == datetime(2026, 10, 21, 18, 34, 41, tzinfo=UTC)


def test_buyer_hired_percent_in_extra(projects):
    p = projects["3022438"]
    assert p.extra["hired_percent"] == 57
    assert p.extra["buyer_projects"] == 98


def test_html_entities_in_description_are_decoded(projects):
    assert all("&mdash;" not in p.description for p in projects.values())
    assert any("—" in p.description for p in projects.values())


def test_duplicate_wants_blocks_do_not_duplicate_projects(page):
    doubled = page + page
    assert len(parse_kwork_projects(doubled)) == 12


def test_missing_wants_block_raises_format_error():
    with pytest.raises(SourceFormatError):
        parse_kwork_projects("<html><body>Проекты не найдены</body></html>")


def test_wants_with_unexpected_structure_raises_format_error():
    with pytest.raises(SourceFormatError):
        parse_kwork_projects('<script>x={"wants":[{"foo":1}]}</script>')


def test_wants_that_is_not_valid_json_raises_format_error():
    with pytest.raises(SourceFormatError):
        parse_kwork_projects('<script>x={"wants":[{"id":1,]}</script>')


def _fetcher(handler) -> Fetcher:
    async def no_sleep(_: float) -> None:
        return None

    return Fetcher(httpx.MockTransport(handler), sleep=no_sleep, min_interval=0)


async def test_kworksource_requests_first_page_only(page):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text=page)

    urls = ["https://kwork.ru/projects?c=11", "https://kwork.ru/projects?c=41&page=3"]
    source = KworkSource(_fetcher(handler), urls)
    result = await source.fetch()
    assert source.platform == "kwork"
    assert len(result) == 12  # две категории с пересечением -> без дублей
    assert seen == ["https://kwork.ru/projects?c=11", "https://kwork.ru/projects?c=41"]
    assert not any("page=" in u for u in seen)


async def test_kworksource_propagates_block():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="forbidden")

    with pytest.raises(BlockedError):
        await KworkSource(_fetcher(handler), ["https://kwork.ru/projects?c=11"]).fetch()


def test_fixture_matches_real_json_shape(page):
    # защита от случайной порчи фикстуры: блок wants — валидный JSON-массив
    start = page.index('"wants":') + len('"wants":')
    items, _ = json.JSONDecoder().raw_decode(page[start:])
    assert re.fullmatch(r"\d+\.\d\d", items[0]["priceLimit"])
