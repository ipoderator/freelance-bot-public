"""«Работа России» (sources/trudvsem.py, ADR 0015): разбор записей открытых данных, регион
города, постраничный поиск. Сети нет — снятый ответ API в fixtures."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from freelance_bot.sources import SourceFormatError, trudvsem
from freelance_bot.sources.trudvsem import TrudvsemSource, parse_vacancy, region_for

FIXTURE = Path(__file__).parent / "fixtures" / "trudvsem_vacancies.json"
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
LIPETSK = "4800000000000"


def _rows() -> list[dict]:
    return [v["vacancy"] for v in json.loads(FIXTURE.read_text())["results"]["vacancies"]]


def _answer(rows: list[dict], total: int | None = None) -> str:
    body = {"status": "200", "meta": {"total": len(rows) if total is None else total}}
    if rows:
        body["results"] = {"vacancies": [{"vacancy": r} for r in rows]}
    return json.dumps(body, ensure_ascii=False)


class FakeFetcher:
    def __init__(self, pages):
        self.pages, self.urls = list(pages), []

    async def get_text(self, url, headers=None):
        self.urls.append(url)
        return self.pages.pop(0)


@pytest.fixture(autouse=True)
def _no_region_cache():
    trudvsem._regions.clear()
    yield
    trudvsem._regions.clear()


def test_vacancy_parsed_without_contacts():
    p = parse_vacancy(_rows()[0])
    assert p is not None and p.platform == "trudvsem" and p.title == "Продавец-кассир"
    assert p.url.startswith("https://trudvsem.ru/vacancy/card/")
    assert p.extra["area"] == "Липецк" and p.extra["company"] == "Пятёрочка"
    assert p.budget_min == 44000 and p.extra["salary"] == "44 000–59 600 ₽"
    assert p.published_at.tzinfo is not None and "Обязанности:" in p.description
    assert "+7" not in p.description


def test_area_is_town_not_region():
    """«Липецкая область, г Данков» — Данков: иначе «липецк» нашёлся бы в названии области."""
    rows = _rows()
    assert parse_vacancy(rows[2]).extra["area"] == "Данков"
    assert parse_vacancy(rows[3]).extra["area"] == "Панино село"


def test_remote_and_experience_are_marked():
    row = {**_rows()[0], "employment": "Дистанционная (удаленная) работа",
           "requirement": {"experience": 3}}  # fmt: skip
    p = parse_vacancy(row)
    assert p.extra["area"] == "Липецк, удалённо"
    assert "Опыт работы от 3 лет" in p.description and p.extra["experience"] == "от 3 лет"


def test_broken_rows_are_skipped():
    row = _rows()[0]
    assert parse_vacancy({**row, "vac_url": "http://trudvsem.ru/x"}) is None
    assert parse_vacancy({**row, "job-name": ""}) is None
    assert parse_vacancy({**row, "date_modify": None, "creation-date": None}) is None


async def test_region_found_by_city_and_cached():
    fetcher = FakeFetcher([_answer(_rows())])
    assert await region_for(fetcher, "Данков") == LIPETSK
    assert await region_for(fetcher, "данков") == LIPETSK  # повторно — без запроса
    assert len(fetcher.urls) == 1
    assert parse_qs(urlsplit(fetcher.urls[0]).query)["text"] == ["Данков"]


async def test_search_in_city_region_pages_until_total():
    rows = _rows()
    first = [{**r, "id": f"a{i}"} for i, r in enumerate(rows)]
    second = [{**r, "id": f"b{i}"} for i, r in enumerate(rows)]
    fetcher = FakeFetcher([_answer(rows), _answer(first, total=200), _answer(second, total=200)])
    source = TrudvsemSource(fetcher, ["продавец"], max_age_hours=24 * 365, city="Липецк",
                            clock=lambda: NOW)  # fmt: skip
    found = await source.fetch()
    assert len(found) == 8
    urls = [urlsplit(u) for u in fetcher.urls[1:]]
    assert all(u.path.endswith(f"/region/{LIPETSK}") for u in urls)
    assert [parse_qs(u.query)["offset"] for u in urls] == [["0"], ["1"]]  # страница — номер
    assert "modifiedFrom" in parse_qs(urls[0].query)


async def test_unknown_city_gives_nothing_and_empty_answer_is_fine():
    fetcher = FakeFetcher([_answer([])])
    source = TrudvsemSource(fetcher, ["продавец"], max_age_hours=72, city="Нигдеград")
    assert await source.fetch() == []
    fetcher = FakeFetcher([_answer([])])
    assert await TrudvsemSource(fetcher, ["продавец"], max_age_hours=72).fetch() == []


async def test_api_error_is_format_error():
    fetcher = FakeFetcher(['{"status": "500", "meta": {"error": "…"}}'])
    with pytest.raises(SourceFormatError):
        await TrudvsemSource(fetcher, ["продавец"], max_age_hours=72).fetch()
