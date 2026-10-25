"""SuperJob: разбор ответа API, запросы, ошибки ключа, вакансии в пайплайне. Сети нет."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from freelance_bot.config import ConfigError, load_settings
from freelance_bot.http import Fetcher, HttpStatusError, SourceError
from freelance_bot.models import Draft
from freelance_bot.pipeline import Pipeline
from freelance_bot.sources import SourceFormatError
from freelance_bot.sources.superjob import SuperJobSource, parse_vacancy
from freelance_bot.storage import Storage

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = (ROOT / "tests" / "fixtures" / "superjob_vacancies.json").read_text(encoding="utf-8")
NOW = datetime(2026, 10, 24, 11, 20, tzinfo=UTC)
KEY = "v3.r.TEST-KEY-NOT-REAL"


async def _no_sleep(_: float) -> None:
    return None


def _source(handler, *, key=KEY, queries=("python",), **kwargs) -> SuperJobSource:
    fetcher = Fetcher(transport=httpx.MockTransport(handler), sleep=_no_sleep, min_interval=0)
    return SuperJobSource(
        fetcher, key, queries, max_age_hours=6, clock=lambda: NOW, **kwargs
    )


async def test_fresh_vacancies_from_api():
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=FIXTURE)

    projects = await _source(handler).fetch()

    # старая (4 дня назад), архивная и битая — отброшены; свежие сверху
    assert [p.external_id for p in projects] == ["51000001", "51000002", "51000003"]
    first = projects[0]
    assert first.title == "Python-разработчик (FastAPI)"
    assert first.url == "https://www.superjob.ru/vakansii/python-razrabotchik-51000001.html"
    assert first.published_at == datetime(2026, 10, 24, 11, 5, tzinfo=UTC)
    assert (first.budget_min, first.budget_max) == (150000, 220000)
    assert first.extra == {
        "company": "Облачные Решения",
        "area": "Москва, удалённо",
        "salary": "150 000–220 000 ₽",
        "experience": "от 1 года",
        "schedule": "Полный рабочий день",
    }
    assert first.description.startswith("Обязанности: Разработка REST API на FastAPI")
    assert "<p>" not in first.description
    assert "Опыт работы от 1 года; Полный рабочий день; на дому" in first.description

    request = requests[0]
    assert request.headers["X-Api-App-Id"] == KEY
    assert request.url.path == "/2.0/vacancies/"
    params = request.url.params
    assert params["keyword"] == "python" and params["order_field"] == "date"
    assert params["count"] == "40" and params["page"] == "0"
    assert len(requests) == 1  # «more: false» — вторую страницу не просим
    assert int(params["date_published_from"]) == int((NOW - timedelta(hours=6)).timestamp())
    assert "catalogues" not in params and "place_of_work" not in params



async def test_pages_until_more_is_false():
    rows = json.loads(FIXTURE)["objects"]
    fresh = [r for r in rows if r.get("id") in (51000001, 51000002, 51000003)]
    pages = [
        {"objects": fresh[:2], "total": 3, "more": True},
        {"objects": fresh[2:], "total": 3, "more": False},
    ]
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        seen.append(request.url.params["page"])
        return httpx.Response(200, json=pages[page])

    projects = await _source(handler).fetch()
    assert seen == ["0", "1"]
    assert {p.external_id for p in projects} == {"51000001", "51000002", "51000003"}


async def test_paging_stops_at_max_pages():
    from freelance_bot.sources import superjob

    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(int(request.url.params["page"]))
        row = json.loads(FIXTURE)["objects"][0] | {"id": 52000000 + len(calls)}
        return httpx.Response(200, json={"objects": [row], "more": True})

    projects = await _source(handler).fetch()
    assert calls == list(range(superjob.MAX_PAGES))
    assert len(projects) == superjob.MAX_PAGES

def test_salary_variants():
    rows = json.loads(FIXTURE)["objects"]
    anonymous = parse_vacancy(rows[1])
    assert (anonymous.budget_min, anonymous.budget_max) == (None, None)
    assert anonymous.extra["salary"] == "по договорённости"
    assert "company" not in anonymous.extra  # анонимная вакансия — компания не указана
    assert "Опыт работы от 3 лет" in anonymous.description
    dollars = parse_vacancy(rows[2])
    assert dollars.extra["salary"] == "от 2 000 $"
    assert dollars.budget_min is None  # бюджет — только в рублях
    single = parse_vacancy({**rows[3], "date_published": rows[0]["date_published"]})
    assert single.extra["salary"] == "90 000 ₽"


async def test_each_query_once_and_duplicates_merged():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params["keyword"])
        return httpx.Response(200, text=FIXTURE)

    source = _source(handler, queries=("python", "fastapi"), catalogues=(48, 650),
                     remote_only=True)  # fmt: skip
    projects = await source.fetch()
    assert seen == ["python", "fastapi"]
    assert len(projects) == 3  # одна вакансия из двух запросов — одна


async def test_filters_go_to_api():
    params = {}

    def handler(request: httpx.Request) -> httpx.Response:
        params.update(request.url.params)
        return httpx.Response(200, text='{"objects": []}')

    await _source(handler, catalogues=(48, 650), remote_only=True).fetch()
    assert params["catalogues"] == "48,650" and params["place_of_work"] == "2"


async def test_missing_key_is_explained_without_request():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("без ключа запросов нет")

    with pytest.raises(SourceError, match="SUPERJOB_API_KEY") as err:
        await _source(handler, key=None).fetch()
    assert "api.superjob.ru/register" in str(err.value)


async def test_rejected_key_is_not_reported_as_captcha():
    body = '{"error":{"code":403,"message":"Необходимо передать ключ приложения."}}'
    source = _source(lambda r: httpx.Response(403, text=body))
    with pytest.raises(HttpStatusError, match="ключ SUPERJOB_API_KEY не принят") as err:
        await source.fetch()
    assert err.value.status == 403 and KEY not in str(err.value)


@pytest.mark.parametrize("body", ["<html>oops</html>", '{"error": "x"}', '{"objects": 5}'])
async def test_unexpected_answer_is_format_error(body):
    with pytest.raises(SourceFormatError):
        await _source(lambda r: httpx.Response(200, text=body)).fetch()


def test_settings_section():
    s = load_settings(ROOT / "settings.toml", ROOT / "no-such.env")
    sj = s.sources["superjob"]
    assert sj.mode == "notify" and sj.queries and sj.catalogues == []
    assert "superjob" in s.public_platforms


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ("[sources.superjob]\nqueries = []", "queries"),
        ("[sources.superjob]\ncatalogues = ['IT']", "catalogues"),
        ("[sources.fl]\nqueries = ['python']", "только для sources.superjob"),
    ],
)
def test_bad_settings(tmp_path, section, message):
    path = tmp_path / "settings.toml"
    path.write_text(section, encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_settings(path, tmp_path / "no.env")


def test_key_is_read_from_env(tmp_path):
    (tmp_path / "settings.toml").write_text("", encoding="utf-8")
    (tmp_path / ".env").write_text(f"SUPERJOB_API_KEY={KEY}\n", encoding="utf-8")
    s = load_settings(tmp_path / "settings.toml", tmp_path / ".env")
    assert s.superjob_api_key == KEY and KEY not in repr(s)


class FakeNotifier:
    def __init__(self):
        self.cards: list[tuple[str, list]] = []

    async def send_card(self, text, keyboard):
        self.cards.append((text, keyboard))

    async def send_text(self, text):  # pragma: no cover
        raise AssertionError(text)


class NoDrafts:
    async def generate(self, p, r, variant=0) -> Draft:  # pragma: no cover - не должен звать
        raise AssertionError("вакансиям черновик не нужен")


async def test_pipeline_sends_vacancy_cards_without_drafts():
    loaded = load_settings(ROOT / "settings.toml", ROOT / "no-such.env")
    sj = replace(loaded.sources["superjob"], enabled=True)
    settings = replace(
        loaded, digest_enabled=False, min_score=25, sources={**loaded.sources, "superjob": sj}
    )
    storage = Storage(":memory:", clock=lambda: NOW)
    notifier = FakeNotifier()
    source = _source(lambda r: httpx.Response(200, text=FIXTURE))
    pipeline = Pipeline(settings, storage, [source], NoDrafts(), notifier, clock=lambda: NOW)

    first = await pipeline.run_once()
    second = await pipeline.run_once()

    assert first.errors == [] and first.fetched == 3
    assert second.new == 0 and second.notified == 0
    text, keyboard = next(c for c in notifier.cards if "FastAPI" in c[0])
    assert "🏢 Облачные Решения · 📍 Москва, удалённо" in text
    assert "💵 зарплата: 150 000–220 000 ₽" in text
    assert "🌐 SuperJob · 🕐 опубликована 24.10.2026 14:05 МСК" in text
    assert [b[1] for row in keyboard for b in row] == [
        "applied:superjob:51000001",
        "skip:superjob:51000001",
    ]
    storage.close()
