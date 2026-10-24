"""Источник Work24: операции API Anything (work24.json) на урезанных настоящих ответах."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from freelance_bot.config import ConfigError
from freelance_bot.http import BlockedError, Fetcher, SourceError, looks_blocked
from freelance_bot.sources import SourceFormatError
from freelance_bot.sources.work24 import (
    OPERATIONS_PATH,
    Work24Source,
    extract_html,
    extract_json,
    load_operations,
    operation_request,
)

FIXTURES = Path(__file__).parent / "fixtures"
LIST = (FIXTURES / "work24_orders.json").read_text("utf-8")  # 35325 и 35225 открыты, 34084 нет
CARD = (FIXTURES / "work24_order.html").read_text("utf-8")
GPU_AT = datetime.fromtimestamp(1792787924, UTC)  # created_at заказа 35325
GPU_SLUG = "35325-ust-i-nast-probrosagpu-passthrough-i-razdelenie-gpu"
OFFSETS_SLUG = "35225-nahojdenie-offsetov-iz-biblioteki"
OPS = load_operations()


def site(calls: list[httpx.Request], cards: dict[str, httpx.Response] | None = None, page=LIST):
    """api.work24.ru отдаёт ленту (вторая страница пуста), work24.ru — карточки."""

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "api.work24.ru":
            first = request.url.params["page"] == "1"
            return httpx.Response(200, text=page if first else '{"orders": []}')
        slug = request.url.path.removeprefix("/order/")
        return (cards or {}).get(slug) or httpx.Response(200, text=CARD)

    return handler


def source(calls, cards=None, *, page=LIST, max_age_hours=48, known=()) -> Work24Source:
    async def no_sleep(_: float) -> None:
        return None

    fetcher = Fetcher(transport=httpx.MockTransport(site(calls, cards, page)), sleep=no_sleep)
    return Work24Source(
        fetcher,
        ["listItOrders"],
        max_age_hours=max_age_hours,
        known=lambda external_id: external_id in known,
        clock=lambda: GPU_AT + timedelta(hours=1),
    )


def card_paths(calls: list[httpx.Request]) -> list[str]:
    return [r.url.path for r in calls if r.url.host == "work24.ru"]


# ── операции ─────────────────────────────────────────────────────────────────


def test_operations_file_has_three_read_only_operations():
    assert set(OPS) == {"listItOrders", "listOrders", "getOrder"}
    assert all(op["readOnly"] and op["minTier"] == 1 for op in OPS.values())


def test_request_fills_slot_and_drops_browser_headers():
    url, headers = operation_request(OPS["listItOrders"], {"page": 2})
    assert url == (
        "https://api.work24.ru/orders?my_disciplines=false&discipline_group_ids[]=2"
        "&page=2&showHiddenOrders=false"
    )
    assert headers["origin"] == "https://work24.ru"
    assert headers["x-client-type"] == "desktop_web"
    assert not any(k.startswith("sec-") or k == "user-agent" for k in headers)

    url, _ = operation_request(OPS["getOrder"], {"slug": "1-a/../b"})
    assert url == "https://work24.ru/order/1-a%2F..%2Fb"  # слот не выходит из пути


def test_request_without_slot_value_is_format_error():
    with pytest.raises(SourceFormatError):
        operation_request(OPS["getOrder"], {})


def test_extract_json_renames_fields_by_pick():
    rows = extract_json(OPS["listItOrders"], LIST)
    assert len(rows) == 3
    assert rows[0] == {
        "id": 35325,
        "slug": GPU_SLUG,
        "title": "Уст. и наст. проброса(GPU Passthrough) и разделение GPU",
        "created_at": 1792787924,
        "deadline": 1792875300,
        "price": "1200",
        "category": "Сервера и хостинги",
        "offersCount": 1,
        "status": 0,
    }


@pytest.mark.parametrize("text", ['{"items": []}', '{"orders": {}}', "<html>", '{"orders": [1]}'])
def test_extract_json_other_shape_is_format_error(text):
    with pytest.raises(SourceFormatError):
        extract_json(OPS["listItOrders"], text)


def test_extract_html_reads_card_fields():
    card = extract_html(OPS["getOrder"], CARD)
    assert card["title"] == "Уст. и наст. проброса(GPU Passthrough) и разделение GPU"
    assert card["status"] == "Открыт"
    assert card["description"].startswith("Установить и настроить Vgpu unlock в проксмос")
    assert card["description"].endswith("стал появлятся код 43 в ВМ")


def test_extract_html_keeps_paragraphs_and_finds_nothing_outside_selector():
    page = (
        "<body><div class='order-text__text'>не то</div><div class='x order__description'>"
        "<div class='order-text__text ql-editor'><p>Первый&nbsp; абзац</p><p>Второй &amp; "
        "<b>жирный</b><br>строка</p><ul><li>пункт</li></ul></div></div></body>"
    )
    card = extract_html(OPS["getOrder"], page)
    assert card["description"] == "Первый абзац\nВторой & жирный\nстрока\nпункт"
    assert card["title"] is None and card["status"] is None


@pytest.mark.parametrize(
    "change",
    [
        {"readOnly": False},
        {"minTier": 2},
        {"volatile": ["csrf"]},
        {"request": {"method": "POST", "url": "https://work24.ru/x"}},
    ],
)
def test_load_operations_refuses_writes_and_browser_operations(tmp_path, change):
    raw = json.loads(OPERATIONS_PATH.read_text("utf-8"))
    raw["operations"][0].update(change)
    path = tmp_path / "work24.json"
    path.write_text(json.dumps(raw), "utf-8")
    with pytest.raises(ConfigError):
        load_operations(path)


def test_unknown_feed_is_config_error():
    with pytest.raises(ConfigError, match="listNothing"):
        Work24Source(Fetcher(), ["listNothing"], max_age_hours=6)


# ── источник ─────────────────────────────────────────────────────────────────


async def test_fetch_returns_fresh_open_orders_with_full_description():
    calls: list[httpx.Request] = []
    projects = {p.external_id: p for p in await source(calls).fetch()}

    assert set(projects) == {"35325", "35225"}  # 34084 не открыт (status 4)
    p = projects["35325"]
    assert p.platform == "work24"
    assert p.url == f"https://work24.ru/order/{GPU_SLUG}"
    assert p.title == "Уст. и наст. проброса(GPU Passthrough) и разделение GPU"
    assert p.description.startswith("Установить и настроить Vgpu unlock")
    assert p.published_at == GPU_AT
    assert (p.budget_min, p.budget_max, p.offers_count) == (None, 1200, 1)
    assert p.category == "Разработка и IT / Сервера и хостинги"
    assert p.deadline_days == 2  # «Срок сдачи» через 24 ч 16 мин после публикации
    assert card_paths(calls) == [f"/order/{GPU_SLUG}", f"/order/{OFFSETS_SLUG}"]
    card = next(r for r in calls if r.url.host == "work24.ru")
    assert "freelance-bot" in card.headers["user-agent"]


async def test_known_and_old_orders_are_not_opened():
    calls: list[httpx.Request] = []
    assert await source(calls, known={"35325"}).fetch() != []
    assert card_paths(calls) == [f"/order/{OFFSETS_SLUG}"]

    calls.clear()
    projects = await source(calls, max_age_hours=6).fetch()  # 35225 опубликован 28 ч назад
    assert [p.external_id for p in projects] == ["35325"]
    assert card_paths(calls) == [f"/order/{GPU_SLUG}"]


async def test_second_page_is_read_until_orders_get_old():
    calls: list[httpx.Request] = []
    await source(calls).fetch()
    pages = [r.url.params["page"] for r in calls if r.url.host == "api.work24.ru"]
    assert pages == ["1", "2"]  # на второй странице пусто — дальше не идём

    calls.clear()
    old = json.loads(LIST)
    old["orders"] = [o for o in old["orders"] if o["id"] == 34084]
    await source(calls, page=json.dumps(old)).fetch()
    assert [r.url.params["page"] for r in calls] == ["1"]  # вся страница старше окна


async def test_closed_card_is_skipped_and_not_reopened():
    calls: list[httpx.Request] = []
    closed = httpx.Response(200, text=CARD.replace("Открыт", "Закрыт"))
    src = source(calls, {OFFSETS_SLUG: closed})
    assert [p.external_id for p in await src.fetch()] == ["35325"]
    calls.clear()
    assert [p.external_id for p in await src.fetch()] == ["35325"]  # known в тесте пуст
    assert card_paths(calls) == [f"/order/{GPU_SLUG}"]


async def test_failed_card_is_skipped_and_all_failed_raise():
    calls: list[httpx.Request] = []
    gone = httpx.Response(404, text="нет")
    projects = await source(calls, {OFFSETS_SLUG: gone}).fetch()
    assert [p.external_id for p in projects] == ["35325"]

    with pytest.raises(SourceError, match="404"):
        await source([], {OFFSETS_SLUG: gone, GPU_SLUG: gone}).fetch()


async def test_card_without_status_is_format_error():
    broken = httpx.Response(200, text=CARD.replace("order-status__text", "status-new"))
    with pytest.raises(SourceFormatError):
        await source([], {OFFSETS_SLUG: broken, GPU_SLUG: broken}).fetch()


async def test_blocked_card_stops_the_cycle():
    with pytest.raises(BlockedError):
        await source([], {GPU_SLUG: httpx.Response(403, text="Forbidden")}).fetch()


async def test_order_about_captcha_is_not_taken_for_a_block():
    page = CARD.replace("<title>Уст. и наст.", "<title>Парсер: обойти captcha. Уст. и наст.")
    assert looks_blocked(200, page)  # без маркера карточки (expect) — пауза на час
    projects = await source([], {GPU_SLUG: httpx.Response(200, text=page)}).fetch()
    assert {p.external_id for p in projects} == {"35325", "35225"}


async def test_budget_word_and_list_format_change():
    data = json.loads(LIST)
    data["orders"][0]["price"] = "Предлагайте"
    projects = {p.external_id: p for p in await source([], page=json.dumps(data)).fetch()}
    assert projects["35325"].budget_max is None

    del data["orders"][0]["created_at"]
    with pytest.raises(SourceFormatError):
        await source([], page=json.dumps(data)).fetch()
