"""Источник Freelance.ru: операции API Anything (freelance-ru.json) на урезанных настоящих
страницах от 2026-10-24 (имя заказчика вымышленное)."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from freelance_bot.config import load_settings
from freelance_bot.http import BlockedError, Fetcher, SourceError
from freelance_bot.scoring import score_project
from freelance_bot.sources import SourceFormatError
from freelance_bot.sources.freelance_ru import (
    FreelanceRuSource,
    _budget,
    _days,
    _moscow_time,
    _without_contacts,
)

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).parent / "fixtures"
LIST = (FIXTURES / "freelance_ru_tasks.html").read_text("utf-8")  # 13598, 13419; 13600 — Премиум
RAG_CARD = (FIXTURES / "freelance_ru_task.html").read_text("utf-8")  # карточка 13419
NO_ACCESS = (FIXTURES / "freelance_ru_no_access.html").read_text("utf-8")
# Карточка 13598 из карточки 13419: свой заголовок, бюджет и требование к опыту
DESIGN_CARD = (
    RAG_CARD.replace("<h1>RAG LLM </h1>", "<h1>Дизайн для ТГ бота</h1>")
    .replace(
        '<span class="bold cost-not-set">Обсуждается индивидуально</span>',
        '<span class="bold">200 000 &#8381;</span> <span class="cost-unit">/ заказ</span>',
    )
    .replace('<div class="tv-meta-item__val">Не важен</div>',
             '<div class="tv-meta-item__val">От 3 лет</div>')
)  # fmt: skip
NOW = datetime(2026, 10, 24, 19, 0, tzinfo=UTC)  # 22:00 МСК
RAG_AT = datetime(2026, 10, 23, 7, 37, tzinfo=UTC)  # «23.10.2026 10:37» МСК
DESIGN_AT = datetime(2026, 10, 24, 16, 12, tzinfo=UTC)


def site(calls: list[httpx.Request], cards: dict[str, httpx.Response] | None = None, page=LIST):
    defaults = {"13419": RAG_CARD, "13598": DESIGN_CARD, "13600": NO_ACCESS}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path == "/task":
            return httpx.Response(200, text=page)
        task_id = request.url.path.removeprefix("/task/view/")
        return (cards or {}).get(task_id) or httpx.Response(200, text=defaults[task_id])

    return handler


def source(
    calls, cards=None, *, page=LIST, max_age_hours=48, known=(), categories=()
) -> FreelanceRuSource:
    async def no_sleep(_: float) -> None:
        return None

    fetcher = Fetcher(transport=httpx.MockTransport(site(calls, cards, page)), sleep=no_sleep)
    return FreelanceRuSource(
        fetcher,
        max_age_hours=max_age_hours,
        categories=categories,
        known=lambda external_id: external_id in known,
        clock=lambda: NOW,
    )


def card_ids(calls: list[httpx.Request]) -> list[str]:
    return [r.url.path.removeprefix("/task/view/") for r in calls if r.url.path != "/task"]


async def test_fetch_returns_public_tasks_with_full_text_and_skips_premium():
    calls: list[httpx.Request] = []
    projects = {p.external_id: p for p in await source(calls).fetch()}

    assert set(projects) == {"13598", "13419"}
    assert card_ids(calls) == ["13598", "13419"]  # свежие первыми; Премиум 13600 не открыт
    feed = calls[0]
    assert str(feed.url) == "https://freelance.ru/task"  # без ?page= — robots.txt
    assert "freelance-bot" in feed.headers["user-agent"]

    p = projects["13419"]
    assert p.platform == "freelance_ru"
    assert p.url == "https://freelance.ru/task/view/13419"
    assert p.title == "RAG LLM"
    assert p.description.startswith("добрый день. задание и примерные этапы ниже.")
    assert p.description.endswith("этап 3: размещение документов базы знаний онлайн в облаке")
    assert "Гонорар" not in p.description and "Видно всем" not in p.description
    assert p.published_at == RAG_AT
    assert (p.budget_min, p.budget_max, p.offers_count) == (None, None, None)
    assert (p.category, p.deadline_days) == ("ИИ и Нейросети", 14)


async def test_budget_and_experience_requirement_reach_scoring():
    p = {p.external_id: p for p in await source([]).fetch()}["13598"]
    assert (p.title, p.budget_max, p.published_at) == ("Дизайн для ТГ бота", 200_000, DESIGN_AT)
    assert p.description.endswith("Требуемый опыт от 3 лет.")
    settings = load_settings(ROOT / "settings.toml", ROOT / "no-such.env")
    settings = replace(settings, experience_years=1.8)
    assert "⚠ требование: опыт от 3 лет (−15)" in score_project(p, settings, NOW).reasons


async def test_known_and_old_tasks_are_not_opened():
    calls: list[httpx.Request] = []
    assert [p.external_id for p in await source(calls, known={"13598"}).fetch()] == ["13419"]
    assert card_ids(calls) == ["13419"]

    calls.clear()
    assert [p.external_id for p in await source(calls, max_age_hours=6).fetch()] == ["13598"]
    assert card_ids(calls) == ["13598"]


async def test_only_chosen_categories_are_opened():
    calls: list[httpx.Request] = []
    projects = await source(calls, categories=("ИИ и Нейросети", "Веб-разработка и IT")).fetch()
    assert [p.external_id for p in projects] == ["13419"]  # 13598 — «Дизайн и Брендинг»
    assert card_ids(calls) == ["13419"]


async def test_customer_contacts_do_not_reach_scoring_or_draft():
    card = RAG_CARD.replace(
        "этап 3: размещение документов базы знаний онлайн в облаке",
        "этап 3: размещение документов базы знаний онлайн в облаке<br />\n"
        "📩 Контакты для связи: https://t.me/fake_customer<br />\nТелеграм: @fake_customer",
    )
    projects = await source([], {"13419": httpx.Response(200, text=card)}).fetch()
    p = next(p for p in projects if p.external_id == "13419")
    assert p.description.endswith("этап 3: размещение документов базы знаний онлайн в облаке")
    assert "fake_customer" not in p.description


def test_contacts_are_cut_but_technical_mentions_stay():
    assert _without_contacts("Работаем через тг, писать@zds_s. Оплата после") == (
        "Работаем через тг, . Оплата после"
    )
    assert _without_contacts("Бот на aiogram\nПочта: ivan@example.com") == "Бот на aiogram\nПочта:"
    assert _without_contacts("WhatsApp — https://wa.me/79990000000") == ""
    kept = "Интеграция с @BotFather и Telegram API"
    assert _without_contacts(kept) == kept


async def test_card_without_access_is_skipped_and_not_reopened():
    calls: list[httpx.Request] = []
    src = source(calls, {"13419": httpx.Response(200, text=NO_ACCESS)})
    assert [p.external_id for p in await src.fetch()] == ["13598"]
    calls.clear()
    assert [p.external_id for p in await src.fetch()] == ["13598"]  # known в тесте пуст
    assert card_ids(calls) == ["13598"]


async def test_premium_badge_is_skipped_even_without_card_request():
    public = LIST.replace(">Видно всем<", ">★ Только для Премиум<")
    calls: list[httpx.Request] = []
    assert await source(calls, page=public).fetch() == []
    assert card_ids(calls) == []


async def test_failed_card_is_skipped_and_all_failed_raise():
    gone = httpx.Response(404, text="нет")
    assert [p.external_id for p in await source([], {"13419": gone}).fetch()] == ["13598"]
    with pytest.raises(SourceError, match="404"):
        await source([], {"13419": gone, "13598": gone}).fetch()


async def test_feed_without_tasks_is_format_error_and_403_is_block():
    with pytest.raises(SourceFormatError, match="сменился формат"):
        await source([], page="<html><body><div class='task-feed-list'></div></body></html>"
                     + "x" * 3000).fetch()  # fmt: skip

    async def no_sleep(_: float) -> None:
        return None

    blocked = Fetcher(
        transport=httpx.MockTransport(lambda r: httpx.Response(403, text="Forbidden")),
        sleep=no_sleep,
    )
    with pytest.raises(BlockedError):
        await FreelanceRuSource(blocked, max_age_hours=6, clock=lambda: NOW).fetch()


async def test_task_about_captcha_is_not_taken_for_a_block():
    page = RAG_CARD.replace("<title>RAG LLM", "<title>Обойти captcha: RAG LLM")
    projects = await source([], {"13419": httpx.Response(200, text=page)}).fetch()
    assert {p.external_id for p in projects} == {"13598", "13419"}


def test_value_parsers():
    assert _budget("1 000 ₽ / заказ") == 1000
    assert _budget("200 000 ₽ / заказ") == 200_000
    assert _budget("Обсуждается индивидуально") is None
    assert _budget("500 ₽ / час") is None  # почасовая ставка — не бюджет заказа
    assert [_days(t) for t in ("14 дней", "1 день", "3 дня", "2 недели", "1 месяц")] == [
        14, 1, 3, 14, 30,
    ]  # fmt: skip
    assert _days("Не важен") is None and _days(None) is None
    assert _moscow_time("24.10.2026 19:30") == datetime(2026, 10, 24, 16, 30, tzinfo=UTC)
    assert _moscow_time("2 часа назад") is None and _moscow_time("31.02.2026 10:00") is None
    assert _moscow_time("01.01.2026 00:00") - _moscow_time("31.12.2025 21:00") == timedelta(
        hours=3
    )
