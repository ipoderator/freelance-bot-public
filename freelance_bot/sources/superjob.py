"""SuperJob: вакансии через официальный API (api.superjob.ru, ADR 0012, docs/platforms.md, §7).

`GET /2.0/vacancies/` с ключом приложения в заголовке X-Api-App-Id (SUPERJOB_API_KEY в .env;
ключ выдаёт api.superjob.ru/register/ после входа в аккаунт SuperJob). Без авторизации
пользователя API отдаёт вакансии в открытом доступе — этого достаточно, контакты не нужны.

За цикл — по запросу на каждое слово из `queries` (свежие сверху, не старше max_age_hours,
постранично по PAGE_SIZE, до MAX_PAGES страниц); лимит API — 120 запросов в минуту с IP.
Вакансия — как у HH: карточка без черновика, отклик пользователь делает на сайте.
Тот же класс ищет и под анкету пользователя (matching.py): слова профессии, `town` — его город.
"""

from __future__ import annotations

import html
import json
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from freelance_bot.http import BlockedError, Fetcher, HttpStatusError, SourceError
from freelance_bot.models import Platform, Project
from freelance_bot.sources import SourceFormatError

SITE = "SuperJob"
API_URL = "https://api.superjob.ru/2.0/vacancies/"
# API отдаёт не больше 40 за запрос: на count=100 — 40 из 59 и «more: false», остальное
# теряется (проверено 2026-10-25); на 40 «more» честный — листаем, пока он true.
PAGE_SIZE = 40
MAX_PAGES = 5  # до 200 вакансий на слово за цикл
CATEGORY = "Вакансия SuperJob"
REMOTE_PLACE = 2  # place_of_work: «на дому» — удалённая работа
KEY_HINT = (
    "ключ приложения выдаёт https://api.superjob.ru/register/ (вход в аккаунт SuperJob), "
    "впишите его в .env: SUPERJOB_API_KEY"
)
_TAG_RE = re.compile(r"<[^>]+>")
_CURRENCIES = {"rub": "₽", "usd": "$", "eur": "€"}


class SuperJobSource:
    """Свежие вакансии по словам `queries`; одна вакансия из нескольких запросов — одна."""

    platform: Platform = "superjob"

    def __init__(
        self,
        fetcher: Fetcher,
        api_key: str | None,
        queries: Sequence[str],
        *,
        max_age_hours: float,
        catalogues: Sequence[int] = (),
        remote_only: bool = False,
        town: str | None = None,
        max_pages: int = MAX_PAGES,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._fetcher = fetcher
        self._key = api_key
        self._queries = list(queries)
        self._catalogues = list(catalogues)
        self._remote_only = remote_only
        self._town = town  # название города: API принимает его вместо id
        self._max_pages = max_pages
        self._max_age = timedelta(hours=max_age_hours)
        self._clock = clock

    async def fetch(self) -> list[Project]:
        if not self._key:
            raise SourceError(f"{SITE}: не задан SUPERJOB_API_KEY — {KEY_HINT}")
        since = self._clock() - self._max_age
        found: dict[str, Project] = {}
        for query in self._queries:
            for row in await self._search(query, since):
                p = parse_vacancy(row)
                if p is not None and p.published_at >= since and not row.get("is_archive"):
                    found.setdefault(p.external_id, p)
        return sorted(found.values(), key=lambda p: p.published_at, reverse=True)

    async def _search(self, query: str, since: datetime) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for page in range(self._max_pages):
            objects, more = await self._page(query, since, page)
            rows.extend(objects)
            if not more or not objects:
                break
        return rows

    async def _page(
        self, query: str, since: datetime, page: int
    ) -> tuple[list[dict[str, Any]], bool]:
        params: dict[str, Any] = {
            "keyword": query,
            "order_field": "date",
            "order_direction": "desc",
            "date_published_from": int(since.timestamp()),
            "count": PAGE_SIZE,
            "page": page,
        }
        if self._catalogues:
            params["catalogues"] = ",".join(str(c) for c in self._catalogues)
        if self._remote_only:
            params["place_of_work"] = REMOTE_PLACE
        if self._town:
            params["town"] = self._town
        url = str(httpx.URL(API_URL, params=params))
        headers = {"X-Api-App-Id": self._key or "", "Accept": "application/json"}
        try:
            text = await self._fetcher.get_text(url, headers=headers)
        except BlockedError:  # 403: ключ не принят (или доступ закрыт) — не «капча»
            raise HttpStatusError(
                f"{SITE}: HTTP 403 — ключ SUPERJOB_API_KEY не принят; {KEY_HINT}", 403
            ) from None
        try:
            data = json.loads(text)
        except ValueError:
            raise SourceFormatError(f"{SITE}: ответ API — не JSON") from None
        objects = data.get("objects") if isinstance(data, dict) else None
        if not isinstance(objects, list):
            raise SourceFormatError(f"{SITE}: в ответе API нет списка objects")
        return [o for o in objects if isinstance(o, dict)], data.get("more") is True


def parse_vacancy(row: dict[str, Any]) -> Project | None:
    """Вакансия из объекта API; без id, названия, ссылки или даты — None."""
    vacancy_id, title, url = row.get("id"), _text(row.get("profession")), row.get("link")
    published = row.get("date_published")
    if not (isinstance(vacancy_id, int) and title and isinstance(url, str)
            and url.startswith("https://") and isinstance(published, int | float)):  # fmt: skip
        return None
    budget_min, budget_max, salary = _salary(row)
    town = _title(row.get("town"))
    remote = isinstance(row.get("place_of_work"), dict) and row["place_of_work"].get(
        "id"
    ) == REMOTE_PLACE
    area = ", ".join(x for x in (town, "удалённо" if remote else "") if x)
    extra = {
        k: v
        for k, v in {
            "company": _text(row.get("firm_name")),
            "area": area,
            "salary": salary,
            "experience": _title(row.get("experience")),
            "schedule": _title(row.get("type_of_work")),
        }.items()
        if v
    }
    return Project(
        platform="superjob",
        external_id=str(vacancy_id),
        url=url,
        title=title,
        description=_description(row),
        published_at=datetime.fromtimestamp(published, UTC),
        budget_min=budget_min,
        budget_max=budget_max,
        offers_count=None,
        category=CATEGORY,
        extra=extra,
    )


def _description(row: dict[str, Any]) -> str:
    parts = [
        f"{label}: {text}"
        for label, key in (("Обязанности", "work"), ("Требования", "candidat"),
                           ("Условия", "compensation"))  # fmt: skip
        if (text := _text(row.get(key)))
    ]
    # «Опыт работы от 3 лет» — так требование увидят скоринг и обучение (experience.py)
    terms = [
        f"Опыт работы {_title(row.get('experience'))}" if _title(row.get("experience")) else "",
        _title(row.get("type_of_work")),
        _title(row.get("place_of_work")),
    ]
    if line := "; ".join(t for t in terms if t):
        parts.append(line)
    return "\n\n".join(parts)


def _salary(row: dict[str, Any]) -> tuple[int | None, int | None, str]:
    """(бюджет от, до — только в рублях; строка зарплаты для карточки)."""
    lo, hi = (v if isinstance(v, int) and v > 0 else None
              for v in (row.get("payment_from"), row.get("payment_to")))  # fmt: skip
    currency = str(row.get("currency") or "rub").lower()
    sign = _CURRENCIES.get(currency, currency.upper())
    if lo is None and hi is None:
        return None, None, "по договорённости" if row.get("agreement") else ""

    def num(v: int) -> str:
        return f"{v:,}".replace(",", " ")

    if lo and hi and lo != hi:
        text = f"{num(lo)}–{num(hi)} {sign}"
    elif lo and hi:
        text = f"{num(lo)} {sign}"
    else:
        text = f"от {num(lo)} {sign}" if lo else f"до {num(hi)} {sign}"  # type: ignore[arg-type]
    if currency != "rub":
        return None, None, text
    return lo, hi, text


def _title(value: object) -> str:
    return _text(value.get("title")) if isinstance(value, dict) else ""


def _text(value: object) -> str:
    """Текст поля без HTML-тегов и лишних пробелов (в описаниях бывает разметка)."""
    if not isinstance(value, str):
        return ""
    text = html.unescape(_TAG_RE.sub("\n", value))
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line).strip()
