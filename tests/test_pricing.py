"""Оценка цены и срока: тип работы, сложность, бюджет заказчика (pricing.py)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from freelance_bot.config import load_settings
from freelance_bot.pricing import TIERS, estimate
from freelance_bot.scoring import score_project

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(scope="module")
def settings():
    return load_settings(ROOT / "settings.toml", ROOT / "no-such.env")


def guess(make_project, settings, **kw):
    kw.setdefault("published_at", NOW)
    kw.setdefault("budget_max", None)
    kw.setdefault("description", "")  # у фикстуры по умолчанию — описание с бэкенд-стеком
    p = make_project(**kw)
    return estimate(p, score_project(p, settings, NOW), settings)


@pytest.mark.parametrize(
    ("title", "tier"),
    [
        ("Создать парсер для сайта", "parsing"),
        ("Нужен телеграмм бот", "telegram"),
        ("Создание ИИ агентов для работы с маркетплейсами", "ai_agents"),
        ("Разработка MVP веб-сервиса для логистики", "service"),
        ("Исправить ошибку в боте на aiogram", "fix"),
    ],
)
def test_price_and_days_fall_into_the_tier_without_budget(make_project, settings, title, tier):
    e = guess(make_project, settings, title=title, description="")
    lo, hi = TIERS[tier].price
    assert lo <= e.price_rub <= hi, e
    assert TIERS[tier].days[0] <= e.days <= TIERS[tier].days[1], e
    assert TIERS[tier].label in e.basis and "бюджет не указан" in e.basis


def test_creation_verb_beats_setup_word_in_title(make_project, settings):
    # «подключение» в заголовке не делает создание бота мелкой правкой
    e = guess(
        make_project, settings,
        title="Создание телеграм бота и подключение платежной системы", description="",
    )
    assert TIERS["telegram"].label in e.basis and "оплата" in e.basis


def test_features_make_it_more_expensive_and_simple_cheaper(make_project, settings):
    plain = guess(make_project, settings, title="Нужен телеграмм бот", description="")
    rich = guess(
        make_project, settings, title="Нужен телеграмм бот",
        description="Оплата через ЮKassa, админка, база данных PostgreSQL, рассылки.",
    )
    simple = guess(
        make_project, settings, title="Нужен телеграмм бот", description="Простой бот, 3 кнопки.",
    )
    assert simple.price_rub < plain.price_rub < rich.price_rub
    assert simple.days <= plain.days <= rich.days
    assert "учтено: оплата, админка, база данных, рассылки" in rich.basis


def test_budget_at_or_above_estimate_is_offered_as_is(make_project, settings):
    e = guess(make_project, settings, title="Создать парсер для сайта", budget_max=40000)
    assert e.price_rub == 40000 and "по бюджету заказчика" in e.basis and not e.below_budget
    base = guess(make_project, settings, title="Создать парсер для сайта")
    assert e.days > base.days  # бюджет вчетверо выше оценки — работы больше, чем видно


def test_fl_budget_below_estimate_offers_tier_floor(make_project, settings):
    e = guess(make_project, settings, title="Нужен телеграмм бот", budget_max=2500)
    assert e.price_rub == TIERS["telegram"].price[0]
    assert e.below_budget and "ниже оценки" in e.basis


def test_kwork_never_exceeds_allowed_budget_and_deadline(make_project, settings):
    e = guess(
        make_project, settings, platform="kwork", title="Создать телеграмм БОТА",
        budget_max=1000, deadline_days=3, extra={"possible_price_limit": 3000},
    )
    assert e.price_rub == 3000 and e.days == 3
    assert "допустимый на Kwork — до 3 000 ₽" in e.basis and "срок заказчика 3 дн." in e.basis


def test_kwork_allowed_budget_above_estimate_gives_estimate(make_project, settings):
    e = guess(
        make_project, settings, platform="kwork", title="Создать парсер",
        budget_max=5000, extra={"possible_price_limit": 15000},
    )
    assert 5000 < e.price_rub < 15000 and e.price_rub % 500 == 0


def test_deterministic(make_project, settings):
    a = guess(make_project, settings, title="AI-агент для поддержки с REST API")
    b = guess(make_project, settings, title="AI-агент для поддержки с REST API")
    assert a == b
