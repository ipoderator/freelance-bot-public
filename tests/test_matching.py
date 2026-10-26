"""Подбор под анкету по резюме (matching.py, ADR 0014): слова профессии, отсечение того,
что резюме не выполняет, живой поиск SuperJob. Сети нет."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from freelance_bot.config import load_settings
from freelance_bot.matching import (
    PROFESSION_KEY,
    SKILLS_KEY,
    VacancySearch,
    mismatch,
    qualifier_terms,
    rank,
    search_queries,
    title_terms,
    user_settings,
)
from freelance_bot.models import Project
from freelance_bot.resume import ResumeProfile, builtin_profile, extract_text
from freelance_bot.scoring import HARD_EXCLUSIONS, score_project

ROOT = Path(__file__).resolve().parent.parent
RESUMES = ROOT / "tests" / "fixtures" / "resumes"
TODAY = date(2026, 10, 25)
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


@pytest.fixture
def settings():
    return load_settings(ROOT / "settings.toml", ROOT / "no-such.env")


def _data(name: str, settings, **extra) -> dict:
    text = extract_text((RESUMES / f"{name}.txt").read_bytes(), "r.txt")
    profile = builtin_profile(text, settings, TODAY)
    return {"directions": profile.directions, "profile": profile.to_dict(),
            "platforms": ["superjob"], **extra}  # fmt: skip


def _vacancy(title: str, area: str = "Воронеж", description: str = "", vid: str = "1"):
    return Project(
        platform="superjob", external_id=vid, url=f"https://www.superjob.ru/{vid}.html",
        title=title, description=description or f"Обязанности: {title}",
        published_at=NOW - timedelta(hours=3), budget_min=60000, budget_max=None,
        offers_count=None, category="Вакансия SuperJob", extra={"area": area},
    )  # fmt: skip


@pytest.mark.parametrize(
    ("name", "terms", "queries"),
    [
        ("08_cook", ["повар", "повар-универсал"],
         ["повар", "повар горячего цеха", "повар-универсал"]),
        ("02_sales_assistant", ["продав", "продавец-консультант", "кассир"],
         ["продавец-консультант", "кассир"]),
        ("04_courier", ["курьер", "водитель", "водитель-экспедитор"],
         ["курьер", "курьер на личном автомобиле", "водитель-экспедитор"]),
        # «Оператор» сам по себе ничего не говорит — фразой
        ("09_hotel_admin", ["администратор", "оператор бронирования"],
         ["администратор", "администратор гостиницы", "оператор бронирования"]),
        ("05_cleaner", ["уборщ", "уборщик", "уборщик служебных помещений", "горничная"],
         ["уборщик", "уборщик помещений", "горничная"]),
    ],
)  # fmt: skip
def test_profession_words_from_resume(name, terms, queries, settings):
    profile = ResumeProfile.from_dict(_data(name, settings)["profile"])
    assert title_terms(profile) == terms
    assert search_queries(profile) == queries


def test_level_word_is_skipped():
    profile = ResumeProfile([], [], 3.0, False, profession="Старший продавец")
    assert title_terms(profile)[:1] == ["продав"]


def test_not_developer_is_matched_by_profession_and_skills(settings):
    data = _data("07_accountant", settings)
    own = user_settings(data, settings)
    assert set(own.directions) == {PROFESSION_KEY, SKILLS_KEY}
    assert "1С: Бухгалтерия" in own.directions[SKILLS_KEY].keywords
    # исключения владельца (1С, дизайн, учёба…) — чья-то профессия; военная служба и
    # накрутка — нет
    assert tuple(own.exclusions) == HARD_EXCLUSIONS
    assert own.experience_years == 3.8


def test_developer_keeps_owner_rules(settings):
    data = {"directions": ["backend"], "profile": {"experience_years": 1.8}}
    own = user_settings(data, settings)
    assert list(own.directions) == ["backend"] and own.exclusions == settings.exclusions


@pytest.mark.parametrize(
    ("title", "area", "description", "reason"),
    [
        ("Повар", "Воронеж", "", None),
        ("Шеф-повар в столовую", "Воронеж", "Опыт работы от 3 лет", None),  # стаж 6,8
        ("Повар-бригадир", "Воронеж", "Опыт работы от 10 лет", "нужен опыт от 10 лет"),
        ("Повар", "Москва", "", "другой город"),
        ("Повар на дому", "Москва, удалённо", "", None),
        ("Официант", "Воронеж", "", "не по профессии"),
        ("Python-разработчик (повар-бот)", "Воронеж", "", "IT-вакансия"),
    ],
)
def test_mismatch_with_cook_resume(title, area, description, reason, settings):
    data = _data("08_cook", settings)
    own = user_settings(data, settings)
    assert mismatch(_vacancy(title, area, description), data, own) == reason


@pytest.mark.parametrize(
    ("location", "moscow", "remote"),
    [
        (None, "другой город", None),  # анкета до вопроса — как раньше
        ([], "другой город", "удалённая работа"),
        (["other"], None, "удалённая работа"),
        (["other", "remote"], None, None),
    ],
)
def test_other_city_and_remote_follow_signup_choice(location, moscow, remote, settings):
    extra = {} if location is None else {"location": location}
    data = _data("08_cook", settings, **extra)
    own = user_settings(data, settings)
    assert mismatch(_vacancy("Повар", "Москва"), data, own) == moscow
    assert mismatch(_vacancy("Повар", "Воронеж, удалённо"), data, own) == remote
    assert mismatch(_vacancy("Повар", "Воронеж"), data, own) is None


def test_own_city_comes_before_other_cities(settings):
    data = _data("08_cook", settings, location=["other"])
    own = user_settings(data, settings)
    far = _vacancy("Повар горячего цеха", "Москва", vid="far")
    near = _vacancy("Повар", "Воронеж", vid="near")
    items = [(p, score_project(p, own, NOW)) for p in (far, near)]
    assert items[0][1].score >= items[1][1].score  # без города Москва шла бы первой
    assert [p.external_id for p, _ in rank(items, data)] == ["near", "far"]


def test_cafe_admin_does_not_get_system_administrator(settings):
    data = _data("01_cafe_admin", settings)
    own = user_settings(data, settings)
    assert mismatch(_vacancy("Администратор кафе", "Москва"), data, own) is None  # города нет
    assert mismatch(_vacancy("Системный администратор"), data, own) == "IT-вакансия"


def test_developer_vacancy_title_by_directions(settings):
    data = {"directions": ["backend"], "platforms": ["superjob"],
            "profile": ResumeProfile(["backend"], [], 1.8, False, profession="Python-разработчик",
                                     city="Липецк").to_dict()}  # fmt: skip
    own = user_settings(data, settings)
    remote = _vacancy("Backend-разработчик (FastAPI)", "Москва, удалённо")
    assert mismatch(remote, data, own) is None
    assert mismatch(_vacancy("Менеджер по продажам", "Липецк"), data, own) == "не по профессии"
    assert mismatch(_vacancy("Python-разработчик", "Липецк", "Опыт работы от 3 лет"), data,
                    own) == "нужен опыт от 3 лет"  # fmt: skip


class FakeFetcher:
    def __init__(self, rows):
        self.rows, self.urls = rows, []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        pass

    async def get_text(self, url, headers=None):
        self.urls.append(url)
        return json.dumps({"objects": self.rows, "more": False})


def _row(vid: int, title: str, town: str = "Воронеж") -> dict:
    return {"id": vid, "profession": title, "link": f"https://www.superjob.ru/v/{vid}.html",
            "date_published": int((NOW - timedelta(hours=2)).timestamp()),
            "town": {"title": town}, "payment_from": 50000, "currency": "rub"}  # fmt: skip


async def test_search_asks_superjob_by_profession_and_city(settings):
    settings = replace(settings, superjob_api_key="test-key")
    fetcher = FakeFetcher([_row(1, "Повар"), _row(2, "Повар-универсал")])
    search = VacancySearch(settings, fetcher_factory=lambda: fetcher, clock=lambda: NOW)
    found = await search(_data("08_cook", settings, location=[]))
    assert [p.title for p in found] == ["Повар", "Повар-универсал"]
    params = [parse_qs(urlsplit(url).query) for url in fetcher.urls]
    assert [q["keyword"][0] for q in params] == ["повар", "повар горячего цеха",
                                                 "повар-универсал"]  # fmt: skip
    assert all(q["town"] == ["Воронеж"] and "place_of_work" not in q for q in params)
    await search(_data("08_cook", settings, location=[]))
    assert len(fetcher.urls) == 3  # повторно — из кэша


async def test_search_adds_other_cities_and_remote_when_chosen(settings):
    settings = replace(settings, superjob_api_key="test-key")
    fetcher = FakeFetcher([])
    search = VacancySearch(settings, fetcher_factory=lambda: fetcher, clock=lambda: NOW)
    await search(_data("08_cook", settings, location=["other", "remote"]))
    params = [parse_qs(urlsplit(url).query) for url in fetcher.urls]
    places = {(q.get("town", [""])[0], q.get("place_of_work", [""])[0]) for q in params}
    assert places == {("Воронеж", ""), ("", ""), ("", "2")}
    assert len(fetcher.urls) == 9  # три запроса в трёх вариантах


async def test_developer_search_adds_remote_without_city(settings):
    settings = replace(settings, superjob_api_key="test-key")
    fetcher = FakeFetcher([])
    search = VacancySearch(settings, fetcher_factory=lambda: fetcher, clock=lambda: NOW)
    data = {"directions": ["backend"], "platforms": ["superjob"],
            "profile": ResumeProfile(["backend"], [], 1.8, False, profession="Python-разработчик",
                                     city="Липецк").to_dict()}  # fmt: skip
    await search(data)
    params = [parse_qs(urlsplit(url).query) for url in fetcher.urls]
    assert params[0]["town"] == ["Липецк"] and "town" not in params[1]
    assert params[1]["place_of_work"] == ["2"]


async def test_search_needs_key_superjob_and_resume(settings):
    fetcher = FakeFetcher([_row(1, "Повар")])
    search = VacancySearch(settings, fetcher_factory=lambda: fetcher)
    assert await search(_data("08_cook", settings)) == []  # ключа нет
    keyed = VacancySearch(replace(settings, superjob_api_key="k"), fetcher_factory=lambda: fetcher)
    assert await keyed(_data("08_cook", settings, platforms=["fl"])) == []
    assert await keyed({"directions": [], "platforms": ["superjob"]}) == []
    assert fetcher.urls == []


def test_qualifiers_lift_exact_profession_and_repeats_are_dropped(settings):
    data = _data("09_hotel_admin", settings)
    assert qualifier_terms(ResumeProfile.from_dict(data["profile"])) == ["гостин"]
    own = user_settings(data, settings)
    shop = [_vacancy("Администратор магазина", "Липецк", vid=str(i)) for i in range(5)]
    hotel = _vacancy("Администратор гостиницы", "Липецк", vid="h")
    items = [(p, score_project(p, own, p.published_at)) for p in [*shop, hotel]]
    assert [p.external_id for p, _ in rank(items, data)] == ["h", "0"]  # пачка — один раз


class RoutingFetcher(FakeFetcher):
    """SuperJob — строки `rows`; «Работа России» — регион и вакансии из фикстуры или сбой."""

    def __init__(self, rows, trudvsem_ok=True):
        super().__init__(rows)
        self.trudvsem_ok = trudvsem_ok

    async def get_text(self, url, headers=None):
        if "superjob" in url:
            return await super().get_text(url, headers)
        self.urls.append(url)
        if not self.trudvsem_ok:
            raise RuntimeError("Работа России не ответила")
        fixture = json.loads((ROOT / "tests" / "fixtures" / "trudvsem_vacancies.json").read_text())
        return json.dumps(fixture, ensure_ascii=False)


@pytest.fixture
def _no_region_cache():
    from freelance_bot.sources import trudvsem

    trudvsem._regions.clear()
    yield
    trudvsem._regions.clear()


@pytest.mark.usefixtures("_no_region_cache")
async def test_search_covers_trudvsem_in_city_region(settings):
    settings = replace(settings, superjob_api_key="test-key")
    fetcher = RoutingFetcher([_row(1, "Продавец", "Липецк")])
    search = VacancySearch(settings, fetcher_factory=lambda: fetcher,
                           clock=lambda: datetime(2026, 10, 26, tzinfo=UTC))  # fmt: skip
    data = _data("02_sales_assistant", settings, platforms=["superjob", "trudvsem"], location=[])
    found = await search(data)
    assert {p.platform for p in found} == {"superjob", "trudvsem"}
    trud = [urlsplit(u) for u in fetcher.urls if "trudvsem" in u]
    assert parse_qs(trud[0].query)["text"] == ["Липецк"]  # регион города — у самого API
    assert all(u.path.endswith("/region/4800000000000") for u in trud[1:])
    own = user_settings(data, settings)
    kept = [p for p in found if p.platform == "trudvsem" and mismatch(p, data, own) is None]
    assert [p.extra["area"] for p in kept] == ["Липецк", "Липецк"]  # Данков и село — нет


@pytest.mark.usefixtures("_no_region_cache")
async def test_one_platform_failure_keeps_the_other(settings):
    settings = replace(settings, superjob_api_key="test-key")
    fetcher = RoutingFetcher([_row(1, "Продавец", "Липецк")], trudvsem_ok=False)
    search = VacancySearch(settings, fetcher_factory=lambda: fetcher, clock=lambda: NOW)
    data = _data("02_sales_assistant", settings, platforms=["superjob", "trudvsem"], location=[])
    answered = []

    async def on_platform(total):
        answered.append(total)

    found = await search(data, on_platform=on_platform)
    assert [p.platform for p in found] == ["superjob"]
    assert answered == [2, 2]  # прогресс: ответ каждой площадки, и сбойной тоже
    with pytest.raises(RuntimeError):  # сбор одной площадки видит её ошибку
        await search(data, only="trudvsem")


async def test_anketa_source_searches_only_its_platform(settings):
    from freelance_bot.matching import AnketaVacancies

    asked = []

    async def search(data, only=None):
        asked.append(only)
        return []

    anketa = {"platforms": ["superjob"]}
    assert await AnketaVacancies("trudvsem", search, lambda: None).fetch() == []
    await AnketaVacancies("trudvsem", search, lambda: anketa).fetch()
    assert asked == ["trudvsem"]


class ChannelFetcher(FakeFetcher):
    """t.me/s/<канал> — снятая страница канала Перми."""

    async def get_text(self, url, headers=None, expect=None):
        self.urls.append(url)
        return (ROOT / "tests" / "fixtures" / "tgchannels_rabota_perm.html").read_text()


async def test_search_reads_channels_of_own_city(settings):
    """Telegram-каналы (ADR 0016): по словам не ищут — читаются каналы города из резюме."""
    fetcher = ChannelFetcher([])
    search = VacancySearch(settings, fetcher_factory=lambda: fetcher, clock=lambda: NOW)
    data = _data("02_sales_assistant", settings, platforms=["tgchannels"], location=[])
    data["profile"]["city"] = "Пермь"
    found = await search(data)
    assert fetcher.urls == ["https://t.me/s/rabota_perm", "https://t.me/s/vakansii_perm"]
    own = user_settings(data, settings)
    kept = [p.title for p in found if mismatch(p, data, own) is None]
    assert kept == ["Продавец-консультант в зоомагазин"]  # повар и торговый — не по профессии
    await search(data)
    assert len(fetcher.urls) == 2  # повторно — из кэша

    fetcher.urls.clear()
    data["profile"]["city"] = "Воронеж"  # в Воронеже канала нет
    assert await search(data) == [] and fetcher.urls == []
    await search({**data, "location": ["other"]})  # другие города — все каналы
    assert len(fetcher.urls) == len(settings.sources["tgchannels"].channels)
