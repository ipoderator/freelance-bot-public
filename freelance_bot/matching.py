"""Подбор заказов и вакансий под анкету по резюме (ADR 0014).

Направления settings.toml — профиль владельца-разработчика; повару или курьеру они не
подходят, а собранные вакансии SuperJob — только по словам владельца. Поэтому под анкету:
- вакансии SuperJob и «Работы России» ищутся живым запросом по профессии и прошлым
  должностям из резюме в городе человека (`VacancySearch`); у владельца с пройденной
  анкетой — ещё и при каждом сборе (`AnketaVacancies`), меню и дайджест — по анкете;
- профессия и навыки из резюме — направления отбора (`user_settings`);
- то, что резюме не выполняет, отсекается (`mismatch`): требуемый стаж больше, чем в
  резюме; вакансия не по профессии; IT-вакансия не разработчику; в другом городе или
  удалённая — если человек не выбрал их в анкете (`location_choice`).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

from freelance_bot.config import Direction, Settings
from freelance_bot.experience import required_years, years_word
from freelance_bot.http import Fetcher
from freelance_bot.models import VACANCY_PLATFORMS, Platform, Project, ScoreResult
from freelance_bot.resume import IT_ROLE_TERMS, ResumeProfile
from freelance_bot.scoring import MILITARY_KEY, WEAK_TERMS, experience_limit
from freelance_bot.sources.superjob import SuperJobSource
from freelance_bot.sources.trudvsem import MAX_PAGES as TRUDVSEM_PAGES
from freelance_bot.sources.trudvsem import TrudvsemSource
from freelance_bot.textmatch import find_terms

log = logging.getLogger(__name__)

PROFESSION_KEY = "profession"
SKILLS_KEY = "skills"
SKILLS_WEIGHT = 0.5  # навыки подтверждают профессию, но сами по себе её не заменяют
VACANCY_WINDOW = timedelta(days=14)  # вакансии открыты дольше заказов
MAX_QUERIES = 3  # запросов к API на анкету: профессия и прошлые должности
SEARCH_PAGES = 1  # SuperJob: по 40 вакансий на запрос — свежие сверху
SEARCH_PLATFORMS: tuple[Platform, ...] = ("superjob", "trudvsem")  # живой поиск под анкету
# «Работа России»: запрос ~5 с, а текстовый поиск широкий («продавец-консультант» находит
# всех продавцов) — хватает двух слов
TRUDVSEM_QUERIES = 2
CACHE_SEC = 600  # повторное нажатие «Заказы по анкете» не ходит в API
REMOTE_AREA = "удалённо"  # так `superjob.parse_vacancy` пишет удалённую работу в area
# Вакансии вне своего города, которые человек согласился получать (шаг анкеты «location»)
REMOTE = "remote"
OTHER_CITIES = "other"
DEFAULT_LOCATION = (REMOTE,)  # анкеты до этого шага: удалённые да, другие города нет
# Первое слово должности, которое само по себе ничего не говорит: ищется вся фраза
# («менеджер по продажам», а не любой «менеджер»)
GENERIC_HEADS = frozenset(
    {"менеджер", "специалист", "оператор", "инженер", "мастер", "помощник", "сотрудник",
     "работник", "консультант", "агент", "эксперт", "manager", "specialist", "engineer"}
)  # fmt: skip
# Уровень перед должностью: «Старший продавец» — продавец
MODIFIERS = frozenset(
    {"старший", "старшая", "младший", "младшая", "главный", "главная", "ведущий", "ведущая",
     "senior", "junior", "middle", "lead"}
)  # fmt: skip
# Вакансии разработки и администрирования систем: «администратор» из резюме кофейни
# не должен находить «Системного администратора»
IT_VACANCY_TERMS = (*IT_ROLE_TERMS, "системный администратор", "сисадмин", "администратор баз",
                    "аналитик данных")  # fmt: skip
# Предлоги в должности: «Курьер на личном автомобиле» — уточняют слова после них
PREPOSITIONS = frozenset(
    {"на", "в", "во", "по", "для", "с", "со", "и", "из", "от", "до", "при", "за"}
)
_WORD_RE = re.compile(r"[\wё]+(?:-[\wё]+)*", re.IGNORECASE)


def resume_profile(data: dict[str, Any]) -> ResumeProfile | None:
    raw = data.get("profile")
    return ResumeProfile.from_dict(raw) if raw else None


def is_developer(data: dict[str, Any]) -> bool:
    """Выбраны направления разработки — отбор как у владельца; иначе — по профессии."""
    return bool(data.get("directions"))


def location_choice(data: dict[str, Any]) -> tuple[str, ...]:
    chosen = data.get("location")
    return DEFAULT_LOCATION if chosen is None else tuple(chosen)


def place_mismatch(p: Project, data: dict[str, Any]) -> str | None:
    """Вакансия удалённая или в другом городе, а человек такие не выбрал."""
    profile = resume_profile(data)
    area = str(p.extra.get("area") or "").casefold()
    city = profile.city.casefold() if profile else ""
    chosen = location_choice(data)
    if REMOTE_AREA in area:
        return None if REMOTE in chosen else "удалённая работа"
    if city and area and city not in area and OTHER_CITIES not in chosen:
        return "другой город"
    return None


def _nearby(p: Project, data: dict[str, Any]) -> bool:
    """Свой город или удалённая: в списке выше других городов, если те выбраны в анкете."""
    return place_mismatch(p, {**data, "location": [REMOTE]}) is None


def _stem(word: str) -> str:
    """«уборщик» → «уборщ» (найдёт и «уборщица»), «продавец» → «продав»."""
    word = word.casefold()
    return word[:-2] if len(word) >= 6 and word.endswith(("ик", "ец")) else word


def _roles(profile: ResumeProfile) -> list[str]:
    """Прошлые должности: «Кассир — Супермаркет (2021 — 2023)» → «Кассир». У hh.ru в
    местах работы стоит компания, а не должность, — у них нет « — », они пропускаются."""
    return [job.split(" — ")[0].strip() for job in profile.jobs if " — " in job]


def _words(title: str) -> list[str]:
    words = _WORD_RE.findall(title)
    while words and words[0].casefold() in MODIFIERS:
        words = words[1:]
    return words


def title_terms(profile: ResumeProfile) -> list[str]:
    """Слова, по которым название вакансии совпадает с профессией из резюме: основное
    слово желаемой должности («повар», «администрат»), однословные прошлые должности
    («кассир», «водитель-экспедитор»), остальные — фразой («оператор бронирования»)."""
    terms: list[str] = []
    for i, title in enumerate([profile.profession, *_roles(profile)]):
        words = _words(title)
        if not words:
            continue
        first = words[0].casefold()
        head = first.split("-")[0]
        if head not in GENERIC_HEADS and (i == 0 or len(words) == 1):
            terms += [_stem(head), first]
        else:
            terms.append(" ".join(words[:3]).casefold())
    return list(dict.fromkeys(t for t in terms if len(t) >= 3))


def qualifier_terms(profile: ResumeProfile) -> list[str]:
    """Уточнения желаемой должности основами: «Администратор гостиницы» → «гостин»,
    «Повар горячего цеха» → «горяч», «цеха». Не обязательны в названии, но поднимают
    «Администратора гостиницы» выше «Администратора магазина»."""
    words = _words(profile.profession)[1:]
    stems = []
    for word in (w.casefold() for w in words if w.casefold() not in PREPOSITIONS):
        if len(word) >= 7:
            stems.append(word[:-3])
        elif len(word) >= 5:
            stems.append(word[:-2])
        elif len(word) >= 3:
            stems.append(word)
    return list(dict.fromkeys(stems))


def search_queries(profile: ResumeProfile) -> list[str]:
    """Слова для поиска SuperJob: полное «Повар горячего цеха» API почти ничего не находит,
    а «повар» — всё; поэтому основное слово, затем полная должность и прошлые должности."""
    queries: list[str] = []
    for i, title in enumerate([profile.profession, *_roles(profile)]):
        words = _words(title)
        if not words:
            continue
        head = words[0].split("-")[0].casefold()
        if head not in GENERIC_HEADS and (i == 0 or len(words) == 1):
            queries.append(words[0].casefold())
        if len(words) > 1 and (i == 0 or head in GENERIC_HEADS):
            queries.append(" ".join(words[:4]).casefold())
    return list(dict.fromkeys(queries))[:MAX_QUERIES]


def user_settings(data: dict[str, Any], settings: Settings) -> Settings:
    """Настройки отбора под анкету. Разработчику — выбранные направления; остальным —
    профессия и навыки из резюме, а исключения владельца (дизайн, 1С, учёба…) не действуют:
    это может быть их профессия; военная служба отсекается у всех. Стаж — из резюме (без
    резюме — 0: стаж владельца из settings.toml чужим не подходит)."""
    chosen = data.get("directions") or []
    profile = resume_profile(data)
    directions = {k: d for k, d in settings.directions.items() if k in chosen}
    exclusions = settings.exclusions
    if profile is not None and not chosen:
        if terms := title_terms(profile):
            directions[PROFESSION_KEY] = Direction(keywords=[*terms, *qualifier_terms(profile)])
        if skills := [s for s in (*profile.skills, *profile.keywords) if len(s) >= 3]:
            directions[SKILLS_KEY] = Direction(keywords=skills, weight=SKILLS_WEIGHT)
        exclusions = {k: v for k, v in settings.exclusions.items() if k == MILITARY_KEY}
    return replace(
        settings,
        directions=directions,
        exclusions=exclusions,
        experience_years=profile.experience_years if profile else 0.0,
    )


def rank(items: list[tuple[Project, ScoreResult]], data: dict[str, Any]
         ) -> list[tuple[Project, ScoreResult]]:  # fmt: skip
    """Порядок списка: разработчику — свежие сверху, как в /last; остальным — свой город
    раньше других городов, затем самые подходящие резюме, затем свежие. Повторы одной
    вакансии (то же название, компания и город — их выкладывают пачками) — один раз."""
    if is_developer(data):
        key = lambda item: (item[0].published_at, item[1].score)  # noqa: E731
    else:
        key = lambda item: (_nearby(item[0], data), item[1].score,  # noqa: E731
                            item[0].published_at)  # fmt: skip
    return drop_repeats(sorted(items, key=key, reverse=True))


def repeat_key(p: Project) -> tuple[str, ...]:
    """Заказы бирж с одним названием — обычно разные заказчики: повторами считаются только
    вакансии."""
    if p.platform not in VACANCY_PLATFORMS:
        return (p.platform, p.external_id)
    return (p.platform, p.title.casefold(), str(p.extra.get("company") or ""),
            str(p.extra.get("area") or ""))  # fmt: skip


def drop_repeats(items: list[tuple[Project, ScoreResult]]) -> list[tuple[Project, ScoreResult]]:
    """Одна вакансия, выложенная пачкой (то же название, компания и город), — один раз;
    порядок сохраняется."""
    seen: set[tuple[str, ...]] = set()
    result = []
    for p, r in items:
        if (key := repeat_key(p)) not in seen:
            seen.add(key)
            result.append((p, r))
    return result


def mismatch(p: Project, data: dict[str, Any], own: Settings) -> str | None:
    """Почему заказ или вакансия не подходит резюме; подходит — None."""
    years = required_years(f"{p.title}\n{p.description}")
    if years is not None and years >= experience_limit(own.experience_years):
        return f"нужен опыт от {years} {years_word(years)}"
    if p.platform in VACANCY_PLATFORMS and (place := place_mismatch(p, data)):
        return place
    profile = resume_profile(data)
    if profile is None:
        return None
    developer = is_developer(data)
    if not developer and find_terms(p.title, IT_VACANCY_TERMS):
        return "IT-вакансия"
    if p.platform in VACANCY_PLATFORMS or not developer:
        terms = title_terms(profile)
        if developer:  # «Backend-разработчик» подходит Python-разработчику
            terms += [
                t
                for d in own.directions.values()
                for t in d.keywords
                if t.casefold() not in WEAK_TERMS
            ]
        if not find_terms(p.title, terms):
            return "не по профессии"
    return None


class VacancySearch:
    """Живой поиск вакансий под анкету: SuperJob и «Работа России»; ответы кэшируются на
    CACHE_SEC. Сбой одной площадки не теряет вакансии другой (кроме поиска `only` — его
    ошибку видит сбор, как у любого источника)."""

    def __init__(
        self,
        settings: Settings,
        fetcher_factory: Callable[[], Fetcher] = Fetcher,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.settings = settings
        self._fetcher_factory = fetcher_factory
        self._clock = clock
        self._cache: dict[tuple[Any, ...], tuple[float, list[Project]]] = {}

    def available(self, platforms: list[Platform]) -> list[Platform]:
        """Площадки поиска из `platforms`, которые включены (SuperJob — ещё и с ключом)."""
        result: list[Platform] = []
        for platform in SEARCH_PLATFORMS:
            source = self.settings.sources.get(platform)
            keyed = platform != "superjob" or bool(self.settings.superjob_api_key)
            if platform in platforms and source is not None and source.enabled and keyed:
                result.append(platform)
        return result

    async def __call__(
        self, data: dict[str, Any], only: Platform | None = None
    ) -> list[Project]:
        profile = resume_profile(data)
        if profile is None:
            return []
        platforms = [p for p in self.available(data.get("platforms") or []) if only in (None, p)]
        queries = search_queries(profile)
        if not platforms or not queries:
            return []
        chosen = location_choice(data)
        answers = await asyncio.gather(  # площадки — параллельно: ждать самую медленную
            *(self._search(p, profile, queries, chosen) for p in platforms),
            return_exceptions=True,
        )
        found: dict[tuple[str, str], Project] = {}
        for platform, projects in zip(platforms, answers, strict=True):
            if isinstance(projects, BaseException):
                if only is not None or not isinstance(projects, Exception):
                    raise projects
                log.warning("вакансии под анкету: %s не ответил: %s", platform,
                            type(projects).__name__)  # fmt: skip
                continue
            for p in projects:
                found.setdefault((p.platform, p.external_id), p)
        result = sorted(found.values(), key=lambda p: p.published_at, reverse=True)
        log.info("вакансии под анкету: запросов %d, найдено %d", len(queries), len(result))
        return result

    async def _search(
        self, platform: Platform, profile: ResumeProfile, queries: list[str],
        chosen: tuple[str, ...],
    ) -> list[Project]:  # fmt: skip
        # свой город; выбраны в анкете — ещё другие города и удалённые по всей стране
        city = profile.city or None
        variants: list[tuple[str | None, bool]] = [(city, False)]
        if city and OTHER_CITIES in chosen:
            variants.append((None, False))
        if REMOTE in chosen and platform == "superjob":
            # у «Работы России» отбора удалённых нет, а поиск по стране ради них — 5 с на
            # слово почти впустую: удалённые оттуда — только вместе с другими городами
            variants.append((None, True))
        if platform == "trudvsem":
            queries = queries[:TRUDVSEM_QUERIES]
        key = (platform, tuple(queries), *variants)
        cached = self._cache.get(key)
        if cached and time.monotonic() - cached[0] < CACHE_SEC:
            return cached[1]
        hours = VACANCY_WINDOW.total_seconds() / 3600
        found: list[Project] = []
        async with self._fetcher_factory() as fetcher:
            for town, remote in variants:
                source: SuperJobSource | TrudvsemSource
                if platform == "superjob":
                    source = SuperJobSource(
                        fetcher, self.settings.superjob_api_key, queries, max_age_hours=hours,
                        remote_only=remote, town=town, max_pages=SEARCH_PAGES, clock=self._clock,
                    )  # fmt: skip
                else:  # по стране — одна страница: там свежие со всей России
                    source = TrudvsemSource(
                        fetcher, queries, max_age_hours=hours, city=town,
                        max_pages=TRUDVSEM_PAGES if town else 1, clock=self._clock,
                    )  # fmt: skip
                found += await source.fetch()
        self._cache[key] = (time.monotonic(), found)
        return found


def completed_anketa(storage: Any, tg_id: int | None) -> dict[str, Any] | None:
    """Пройденная анкета человека (данные users) или None."""
    user = storage.get_user(tg_id) if tg_id else None
    return user.data if user is not None and user.completed_at is not None else None


def outside_anketa(p: Project, data: dict[str, Any], own: Settings) -> str | None:
    """Почему заказ не для анкеты: площадка в ней не выбрана или резюме не подходит."""
    if p.platform not in (data.get("platforms") or []):
        return "площадка не в анкете"
    return mismatch(p, data, own)


class AnketaVacancies:
    """Источник сбора владельца: вакансии площадки `platform` под его пройденную анкету —
    по ним его меню и дайджест (решение пользователя 2026-10-25). Без анкеты — пусто."""

    def __init__(
        self,
        platform: Platform,
        search: VacancySearch,
        anketa: Callable[[], dict[str, Any] | None],
    ) -> None:
        self.platform = platform
        self._search = search
        self._anketa = anketa

    async def fetch(self) -> list[Project]:
        data = self._anketa()
        return await self._search(data, only=self.platform) if data else []
