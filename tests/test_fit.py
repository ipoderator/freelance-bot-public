"""Смысл должности под резюме (fit.py, ADR 0018) и порядок списка: случаи из проверки
2026-10-25 — кофейня и магазин, горячий и мясной цех, бухгалтер по первичке и главный,
вахта в Москве у вакансии «📍 Воронеж», свежие выше старых, сети и повторы. Сети нет."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from freelance_bot.config import load_settings
from freelance_bot.fit import ResumeFit
from freelance_bot.fit import apply as fit_apply
from freelance_bot.matching import (
    AWAY_REASON,
    VacancySearch,
    drop_repeats,
    group_chains,
    mismatch,
    rank,
    user_settings,
)
from freelance_bot.models import Project
from freelance_bot.resume import builtin_profile, extract_text
from freelance_bot.scoring import score_project
from freelance_bot.telegram_ui import format_offer_list, selection_reason

ROOT = Path(__file__).resolve().parent.parent
RESUMES = ROOT / "tests" / "fixtures" / "resumes"
TODAY = date(2026, 10, 25)
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


@pytest.fixture
def settings():
    return load_settings(ROOT / "settings.toml", ROOT / "no-such.env")


def _data(name: str, settings, city: str | None = None, location=("remote",)) -> dict:
    text = extract_text((RESUMES / f"{name}.txt").read_bytes(), "r.txt")
    profile = builtin_profile(text, settings, TODAY)
    if city is not None:
        profile = replace(profile, city=city)
    return {"directions": [], "profile": profile.to_dict(), "platforms": ["superjob"],
            "location": list(location)}  # fmt: skip


def _vacancy(vid: str, title: str, company: str = "", area: str = "Воронеж",
             description: str = "", hours: float = 2, platform: str = "superjob") -> Project:
    return Project(
        platform=platform, external_id=vid, url=f"https://example.com/{platform}/{vid}",
        title=title, description=description or f"Обязанности: {title}",
        published_at=NOW - timedelta(hours=hours), budget_min=50000, budget_max=60000,
        offers_count=None, category=None, extra={"area": area, "company": company},
    )  # fmt: skip


def _picked(data: dict, settings, vacancies: list[Project]) -> list[str]:
    """Что увидит человек и в каком порядке: балл, отсечение, порядок списка."""
    own = user_settings(data, settings)
    items = []
    for p in vacancies:
        r = score_project(p, own, p.published_at)
        if r.passed and mismatch(p, data, own) is None:
            items.append((p, r))
    return [p.title for p, _ in rank(items, data, NOW)]


def test_cafe_admin_not_shop_admin(settings):
    """Администратору кофейни не присылать администратора «Пятёрочки», хотя слово то же и в
    описании магазина есть «кофе»; кафе — та же сфера, кофейня — точная должность."""
    data = _data("01_cafe_admin", settings, city="Липецк")
    shop = _vacancy("2", "Администратор магазина", "Пятёрочка", "Липецк",
                    "Продажа кофе и чая, контроль кассы и выкладки")  # fmt: skip
    vacancies = [
        _vacancy("1", "Администратор кафе", area="Липецк"),
        shop,
        _vacancy("3", "Администратор", "Пятёрочка", "Липецк"),  # сеть — по компании
        _vacancy("4", "Администратор кофейни", area="Липецк"),
    ]
    assert _picked(data, settings, vacancies) == ["Администратор кофейни", "Администратор кафе"]
    result = score_project(shop, user_settings(data, settings), shop.published_at)
    assert result.excluded_reason == "другая сфера: торговля"
    # «кофе» из описания магазина в объяснение совпадения больше не попадает
    assert "кофе" not in selection_reason(result)


def test_hot_shop_cook_before_meat_shop(settings):
    data = _data("08_cook", settings)
    vacancies = [
        _vacancy("1", "Повар мясного цеха"),
        _vacancy("2", "Повар горячего цеха"),
        _vacancy("3", "Повар"),
        _vacancy("4", "Шеф-повар"),
    ]
    picked = _picked(data, settings, vacancies)
    assert picked[0] == "Повар горячего цеха" and "Шеф-повар" not in picked
    own = user_settings(data, settings)
    meat, hot = (score_project(v, own, v.published_at) for v in vacancies[:2])
    assert meat.score < hot.score and any("другая специализация" in x for x in meat.reasons)


def test_cook_sphere_does_not_matter_but_cafe_admin_does(settings):
    """Повар в пансионате — та же работа повара; штрафа за сферу нет."""
    own = user_settings(_data("08_cook", settings), settings)
    p = _vacancy("1", "Повар-универсал в пансионат")
    assert not any("сфера" in x for x in score_project(p, own, p.published_at).reasons)


def test_primary_docs_accountant_not_chief_or_payroll(settings):
    data = _data("07_accountant", settings, city="Липецк")
    vacancies = [
        _vacancy("1", "Главный бухгалтер", area="Липецк"),
        _vacancy("2", "Ведущий бухгалтер по заработной плате", area="Липецк"),
        _vacancy("3", "Бухгалтер по первичной документации", area="Липецк"),
        _vacancy("4", "Бухгалтер", area="Липецк"),
    ]
    picked = _picked(data, settings, vacancies)
    assert picked[0] == "Бухгалтер по первичной документации"
    assert "Главный бухгалтер" not in picked
    assert "Ведущий бухгалтер по заработной плате" not in picked
    own = user_settings(data, settings)
    chief = score_project(vacancies[0], own, NOW)
    assert chief.excluded_reason == "уровень выше резюме: «главный»"


def test_level_and_specialty_rules():
    fit = ResumeFit.build("Бухгалтер", [], [], ["первичная документация"], [], 1.0)
    assert fit_apply("Старший бухгалтер", "", fit).points < 0  # мало стажа для «старшего»
    senior = replace(fit, experience_years=4.0)
    assert fit_apply("Старший бухгалтер", "", senior).points == 0
    assert fit_apply("Главный бухгалтер", "", senior).excluded  # уровень — не стаж
    chief = ResumeFit.build("Главный бухгалтер", [], [], [], [], 10.0)
    assert fit_apply("Главный бухгалтер", "", chief).excluded is None  # свой уровень
    universal = ResumeFit.build("Повар горячего цеха", [], [], [], ["горяч"], 3.0)
    assert fit_apply("Повар-универсал мясного цеха", "", universal).points == 0
    assert fit_apply("Повар горячего цеха", "", None).points == 0  # без анкеты — ничего


def test_shift_work_in_moscow_is_not_local(settings):
    """«📍 Воронеж», а в тексте — вахта с проживанием в Москве: при «только Воронеж» —
    отсекается, при «другие города» — остаётся."""
    p = _vacancy("1", "Повар-универсал в пансионат", area="Воронеж",
                 description="ВАХТА С ПРОЖИВАНИЕМ В МОСКВЕ И ПОДМОСКОВЬЕ. Готовить блюда")
    only_home = _data("08_cook", settings, location=())
    assert mismatch(p, only_home, user_settings(only_home, settings)) == AWAY_REASON
    anywhere = _data("08_cook", settings, location=("other",))
    assert mismatch(p, anywhere, user_settings(anywhere, settings)) is None
    local = _vacancy("2", "Повар", description="Работа в кафе в центре Воронежа")
    assert mismatch(local, only_home, user_settings(only_home, settings)) is None
    for text in ("Вахтовым методом, 30/30", "Работа вахтой в Сургуте"):
        p = _vacancy("3", "Повар", description=text)
        assert mismatch(p, only_home, user_settings(only_home, settings)) == AWAY_REASON
    watchman = _data("05_cleaner", settings, city="Воронеж", location=())
    p = _vacancy("4", "Уборщик-вахтёр", description="Уборка, дежурство на вахтёрском посту")
    assert mismatch(p, watchman, user_settings(watchman, settings)) is None


def test_fresh_vacancies_above_old_better_ones(settings):
    """Кто откликнется первым, того и позовут: вышедшие за 48 часов — выше, даже если
    старая подходит лучше; внутри свежих — по баллу."""
    data = _data("07_accountant", settings, city="Липецк")
    vacancies = [
        _vacancy("1", "Бухгалтер по первичной документации", "Парус", "Липецк", hours=24 * 9),
        _vacancy("2", "Бухгалтер", "Маяк", "Липецк", hours=5),
        _vacancy("3", "Бухгалтер по первичной документации", "Ромашка", "Липецк", hours=20),
    ]
    picked = _picked(data, settings, vacancies)
    assert picked == [
        "Бухгалтер по первичной документации", "Бухгалтер", "Бухгалтер по первичной документации"
    ]
    own = user_settings(data, settings)
    items = [(v, score_project(v, own, v.published_at)) for v in vacancies]
    assert [p.external_id for p, _ in rank(items, data, NOW)] == ["3", "2", "1"]


def test_same_vacancy_on_two_platforms_once():
    a = _vacancy("1", "Продавец-кассир", "Пятёрочка", "Липецк")
    b = _vacancy("x-1", "Продавец-кассир", "ПЯТЁРОЧКА", "Липецк", platform="trudvsem")
    post = _vacancy("5", "Продавец", "", "Липецк", platform="tgchannels")
    other = _vacancy("6", "Продавец", "", "Липецк")
    r = score_project(a, load_settings(ROOT / "settings.toml", ROOT / "no-such.env"), NOW)
    kept = drop_repeats([(a, r), (b, r), (post, r), (other, r)])
    # без компании (пост канала) работодателя не сравнить — между площадками не склеивается
    assert [p.external_id for p, _ in kept] == ["1", "5", "6"]


def test_chain_in_many_cities_is_one_line(settings):
    r = score_project(_vacancy("0", "x"), settings, NOW)
    towns = ["Липецк", "Курск", "Елец", "Тула", "Орёл", "Тамбов", "Брянск"]
    items = [(_vacancy(str(i), "Продавец-кассир", "Пятёрочка", town), r)
             for i, town in enumerate(towns)]  # fmt: skip
    items.append((_vacancy("99", "Кассир", "Магнит", "Липецк"), r))
    kept, absorbed = group_chains(items)
    assert [p.external_id for p, _ in kept] == ["0", "99"] and len(absorbed) == 6
    assert kept[0][0].extra["also_in"] == towns[1:]
    text = format_offer_list([(p, s.score) for p, s in kept], "Europe/Moscow")
    assert "📍 ещё в 6 городах: Курск, Елец, Тула, Орёл, Тамбов…" in text


class _Fetcher:
    def __init__(self, rows=None, fail=False):
        self.rows, self.fail = rows or [], fail

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def get_text(self, url, headers=None):
        if self.fail:
            raise RuntimeError("нет сети")
        return json.dumps({"objects": self.rows, "more": False})


async def test_search_reports_each_platform(settings):
    """Пустой список объясняет, что искалось: площадка ответила, не ответила, выключена."""
    settings = replace(settings, superjob_api_key="k")
    data = {**_data("08_cook", settings), "platforms": ["superjob", "trudvsem"]}
    trudvsem = settings.sources["trudvsem"]
    off = replace(settings, sources={**settings.sources,
                                     "trudvsem": replace(trudvsem, enabled=False)})  # fmt: skip
    statuses: dict[str, str] = {}
    search = VacancySearch(off, fetcher_factory=lambda: _Fetcher(), clock=lambda: NOW)
    assert await search(data, statuses=statuses) == []
    assert statuses == {"trudvsem": "выключена", "superjob": "найдено 0 до отбора"}
    statuses = {}
    broken = VacancySearch(off, fetcher_factory=lambda: _Fetcher(fail=True), clock=lambda: NOW)
    data = {**data, "platforms": ["superjob", "trudvsem", "tgchannels"]}
    channels = off.sources["tgchannels"]
    broken.settings = replace(off, sources={**off.sources,
                                            "tgchannels": replace(channels, enabled=True)})
    await broken(data, statuses=statuses)
    assert statuses["superjob"] == "не ответила" and statuses["trudvsem"] == "выключена"
