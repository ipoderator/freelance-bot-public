"""Источник Work-zilla: демо-лента не сохраняется и не присылается; настоящая — по тем же
полям (вид настоящих заданий вживую не проверен, 2026-10-24)."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from freelance_bot.config import ConfigError, load_settings
from freelance_bot.http import Fetcher, SourceError
from freelance_bot.models import Draft
from freelance_bot.pipeline import Pipeline
from freelance_bot.sources import SourceFormatError
from freelance_bot.sources.workzilla import (
    FEED_URL,
    DemoFeedError,
    WorkzillaSource,
    session_from_api_anything,
)
from freelance_bot.storage import Storage

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
# 3 демо-задания из 50, isStaticOrders = true, снято 2026-10-24
DEMO = (FIXTURES / "workzilla_demo.json").read_text("utf-8")
COOKIE = "Bearer=fake-bearer-value-0123; BrowserId=fake-browser-id"
AGENT = "fake-agent-id-0123456789"
NOW = datetime(2026, 10, 24, 19, 0, tzinfo=UTC)


def real_feed(**changes) -> str:
    """Демо-ответ, ставший настоящим: isStaticOrders = false, задания свежие."""
    raw = json.loads(DEMO)
    raw["data"]["isStaticOrders"] = False
    for i, order in enumerate(raw["data"]["other"]):
        order["modified"] = int((NOW - timedelta(hours=i + 1)).timestamp() * 1000)
    raw["data"].update(changes)
    return json.dumps(raw, ensure_ascii=False)


def source(calls, body=DEMO, *, status=200, cookie=COOKIE, agent=AGENT, known=(), max_age=6):
    async def no_sleep(_: float) -> None:
        return None

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(status, text=body)

    fetcher = Fetcher(transport=httpx.MockTransport(handler), sleep=no_sleep)
    return WorkzillaSource(
        fetcher, cookie, agent, max_age_hours=max_age,
        known=lambda external_id: external_id in known, clock=lambda: NOW,
    )  # fmt: skip


async def test_demo_feed_is_refused_after_one_request_with_session_headers():
    calls: list[httpx.Request] = []
    with pytest.raises(DemoFeedError, match="демонстрационная") as err:
        await source(calls).fetch()
    (request,) = calls  # статус и список — один и тот же ответ
    assert str(request.url) == (
        "https://client.work-zilla.com/api/order/v6/list/open?hideInsolvoOrders=false"
    )
    assert request.headers["cookie"] == COOKIE and request.headers["agentid"] == AGENT
    assert "freelance-bot" in request.headers["user-agent"]
    assert "sec-fetch-mode" not in request.headers
    assert COOKIE not in str(err.value) and AGENT not in str(err.value)


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ('{"result": 102}', "result 102"),  # без agentid: 200 и ни статуса, ни заданий
        (json.dumps({"result": 0, "data": {"other": []}}), "нет isStaticOrders"),
        (json.dumps({"result": 0, "data": {"isStaticOrders": None, "other": []}}),
         "нет isStaticOrders"),
        ("<html><title>Вход</title>" + "x" * 3000 + "</html>", "не JSON"),
    ],
)  # fmt: skip
async def test_feed_without_clear_status_is_not_processed(body, match):
    with pytest.raises(SourceFormatError, match=match):
        await source([], body).fetch()


async def test_missing_or_rejected_session():
    calls: list[httpx.Request] = []
    with pytest.raises(SourceError, match="WORKZILLA_COOKIE"):
        await source(calls, cookie=None).fetch()
    with pytest.raises(SourceError, match="WORKZILLA_AGENT_ID"):
        await source(calls, agent="").fetch()
    assert calls == []

    with pytest.raises(SourceError, match="HTTP 401") as err:
        await source(calls, '{"result": 401}', status=401).fetch()
    assert "workzilla-session" in str(err.value) and COOKIE not in str(err.value)


async def test_real_feed_gives_fresh_open_orders():
    projects = await source([], real_feed()).fetch()
    assert [p.external_id for p in projects] == ["27", "469", "301"]  # свежие первыми
    p = projects[0]
    assert p.platform == "workzilla"
    assert p.title == "Привязать сайт на Тильде к домену"
    assert p.description.startswith("Нужно привязать сайт на Тильде к домену")
    assert (p.budget_min, p.budget_max, p.offers_count) == (None, 500, None)
    assert p.published_at == NOW - timedelta(hours=1)
    assert p.url == FEED_URL
    assert p.extra == {"category_id": 0}


async def test_real_feed_skips_archived_old_known_and_merges_interesting():
    raw = json.loads(real_feed())
    other = raw["data"]["other"]
    other[0]["archived"] = True
    other[1]["modified"] = int((NOW - timedelta(hours=7)).timestamp() * 1000)  # старше окна
    interesting = [dict(other[2]), dict(other[2], id=999, subject="Бот для Telegram")]
    raw["data"]["interesting"] = interesting
    projects = await source([], json.dumps(raw), known={"999"}).fetch()
    assert [p.external_id for p in projects] == ["301"]  # дубль из interesting — одна запись

    raw["data"]["interesting"] = {"не": "список"}  # необязательный раздел сломан — other в силе
    assert [p.external_id for p in await source([], json.dumps(raw)).fetch()] == ["301"]


async def test_order_without_id_or_time_is_skipped():
    raw = json.loads(real_feed())
    del raw["data"]["other"][0]["modified"]
    raw["data"]["other"][1]["id"] = None
    assert [p.external_id for p in await source([], json.dumps(raw)).fetch()] == ["301"]


# ── пайплайн: демо-лента не попадает ни в базу, ни в Telegram ────────────────


class Notifier:
    def __init__(self):
        self.cards: list = []
        self.texts: list[str] = []

    async def send_card(self, text, keyboard):
        self.cards.append(text)

    async def send_text(self, text):
        self.texts.append(text)


class Drafter:
    async def generate(self, p, r, variant=0):
        return Draft("Здравствуйте!", "builtin", False, False)


async def test_pipeline_saves_and_sends_nothing_from_demo_feed():
    settings = load_settings(ROOT / "settings.toml", ROOT / "no-such.env")
    wz = replace(settings.sources["workzilla"], enabled=True)
    settings = replace(
        settings, digest_enabled=False, sources={**settings.sources, "workzilla": wz}
    )
    storage = Storage(":memory:", clock=lambda: NOW)
    notifier = Notifier()
    pipeline = Pipeline(
        settings, storage, [source([])], Drafter(), notifier, clock=lambda: NOW
    )
    for _ in range(4):
        report = await pipeline.run_once()
        assert report.fetched == 0 and report.new == 0
    assert notifier.cards == []
    assert len(notifier.texts) == 1 and "демонстрационная" in notifier.texts[0]  # одно на серию
    assert "демонстрационная" in storage.get_flag("last_error:workzilla")
    assert storage.get_flag("bootstrapped:workzilla") is None  # первый настоящий сбор впереди
    assert storage.get_project("workzilla", "27") is None
    storage.close()


# ── сессия из api-anything ───────────────────────────────────────────────────


def write_session(tmp_path: Path, cookies: list[dict], values: dict) -> Path:
    path = tmp_path / "work-zilla.json"
    path.write_text(json.dumps({"cookies": cookies, "values": values}), "utf-8")
    return path


def test_session_takes_live_work_zilla_cookies_and_agent_id(tmp_path):
    cookies = [
        {"name": "Bearer", "value": "b1", "domain": ".work-zilla.com", "expires": 2e9},
        {"name": ".AspNetCore.Session", "value": "s1", "domain": "client.work-zilla.com",
         "expires": -1},
        {"name": "old", "value": "o1", "domain": ".work-zilla.com", "expires": 1.0},
        {"name": "_ga", "value": "g1", "domain": ".example.com", "expires": 2e9},
    ]  # fmt: skip
    values = {"feedStatus/WzAgentId": AGENT, "listVisibleOrders/WzAgentId": AGENT}
    cookie, agent = session_from_api_anything(write_session(tmp_path, cookies, values), now=1e9)
    assert cookie == "Bearer=b1; .AspNetCore.Session=s1"
    assert agent == AGENT


def test_session_without_bearer_or_agent_is_config_error(tmp_path):
    bearer = [{"name": "Bearer", "value": "b1", "domain": ".work-zilla.com", "expires": -1}]
    with pytest.raises(ConfigError, match="Bearer"):
        session_from_api_anything(write_session(tmp_path, [], {"x/WzAgentId": AGENT}))
    with pytest.raises(ConfigError, match="WzAgentId"):
        session_from_api_anything(write_session(tmp_path, bearer, {}))
    with pytest.raises(ConfigError, match="не читается"):
        session_from_api_anything(tmp_path / "нет.json")
