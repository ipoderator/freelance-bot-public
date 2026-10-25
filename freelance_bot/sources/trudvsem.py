"""«Работа России» (trudvsem.ru): вакансии из открытых данных портала (ADR 0015,
docs/platforms.md, §8).

`GET opendata.trudvsem.ru/api/v1/vacancies[/region/<код>]?text=…&modifiedFrom=…` — без
ключа, только чтение; API опубликован для программ (trudvsem.ru/opendata/api), а robots.txt
хоста закрывает его от поисковых роботов — решение пользователя, ADR 0015. Фильтра по городу
нет — только по региону (код КЛАДР из 13 цифр); регион города бот узнаёт у того же API:
поиск по названию города находит вакансии с ним в адресе (`region_for`).

Страница — до PAGE_SIZE записей, `offset` — номер страницы, свежие (по дате изменения)
сверху. Вакансия — как у SuperJob: карточка без черновика, отклик на сайте. Источник ищет
только под анкету (matching.py): слова профессии, регион города из резюме.
"""

from __future__ import annotations

import html
import json
import re
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from freelance_bot.http import Fetcher
from freelance_bot.models import Platform, Project
from freelance_bot.sources import SourceFormatError

SITE = "Работа России"
API_URL = "https://opendata.trudvsem.ru/api/v1/vacancies"
PAGE_SIZE = 100  # больше API не отдаёт
# Запрос — ~5 с и ~390 КБ (проверено 2026-10-25): за 14 дней продавцов в Липецкой области
# 284, а две страницы — 200 свежих; остальное подберёт сбор под анкету каждые 30 минут
MAX_PAGES = 2
REGION_SAMPLE = 50  # вакансий по названию города, чтобы узнать его регион
CATEGORY = "Вакансия «Работа России»"
REMOTE_EMPLOYMENT = "дистанц"  # employment: «Дистанционная (удаленная) работа»
_TAG_RE = re.compile(r"<[^>]+>")
# «Липецкая область, г Елец, Вермишева улица, 6, …» → «Елец»
_CITY_RE = re.compile(r"(?:^|,\s*)г\.?\s+([А-ЯЁ][а-яё]+(?:[ -][А-ЯЁа-яё][а-яё]+)*)")
_AREA_SKIP_RE = re.compile(r"\b(?:область|край|район|округ|республика|россия)\b", re.I)
_regions: dict[str, str | None] = {}  # город → код региона; регион города не меняется


class TrudvsemSource:
    """Вакансии по словам `queries` за max_age_hours; `city` — искать в его регионе."""

    platform: Platform = "trudvsem"

    def __init__(
        self,
        fetcher: Fetcher,
        queries: Sequence[str],
        *,
        max_age_hours: float,
        city: str | None = None,
        max_pages: int = MAX_PAGES,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._fetcher = fetcher
        self._queries = list(queries)
        self._city = city
        self._max_pages = max_pages
        self._max_age = timedelta(hours=max_age_hours)
        self._clock = clock

    async def fetch(self) -> list[Project]:
        region = await region_for(self._fetcher, self._city) if self._city else None
        if self._city and region is None:
            return []  # регион не узнали: по всей стране город всё равно отсечётся
        since = self._clock() - self._max_age
        found: dict[str, Project] = {}
        for query in self._queries:
            for page in range(self._max_pages):
                rows, total = await _page(self._fetcher, query, region, since, page)
                for row in rows:
                    p = parse_vacancy(row)
                    if p is not None and p.published_at >= since:
                        found.setdefault(p.external_id, p)
                if not rows or (page + 1) * PAGE_SIZE >= total:
                    break
        return sorted(found.values(), key=lambda p: p.published_at, reverse=True)


async def region_for(fetcher: Fetcher, city: str) -> str | None:
    """Код региона города: самый частый среди вакансий с этим городом в адресе."""
    key = city.casefold()
    if key not in _regions:
        rows, _ = await _page(fetcher, city, None, None, 0, REGION_SAMPLE)
        codes = Counter(
            str(row["region"]["region_code"])
            for row in rows
            if isinstance(row.get("region"), dict) and row["region"].get("region_code")
            and key in _city(row).casefold()
        )  # fmt: skip
        _regions[key] = codes.most_common(1)[0][0] if codes else None
    return _regions[key]


async def _page(
    fetcher: Fetcher,
    text: str,
    region: str | None,
    since: datetime | None,
    page: int,
    limit: int = PAGE_SIZE,
) -> tuple[list[dict[str, Any]], int]:
    params: dict[str, Any] = {"text": text, "offset": page, "limit": limit}
    if since is not None:
        params["modifiedFrom"] = since.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    url = f"{API_URL}/region/{region}" if region else API_URL
    raw = await fetcher.get_text(str(httpx.URL(url, params=params)),
                                 headers={"Accept": "application/json"})  # fmt: skip
    try:
        data = json.loads(raw)
    except ValueError:
        raise SourceFormatError(f"{SITE}: ответ API — не JSON") from None
    if not isinstance(data, dict) or str(data.get("status")) != "200":
        raise SourceFormatError(f"{SITE}: API ответил ошибкой")
    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    try:
        total = int(meta.get("total") or 0)
    except (TypeError, ValueError):
        total = 0
    results = data.get("results")
    if total == 0 or not isinstance(results, dict):  # пустой ответ приходит без results
        return [], total
    vacancies = results.get("vacancies")
    if not isinstance(vacancies, list):
        raise SourceFormatError(f"{SITE}: в ответе API нет списка vacancies")
    rows = [v["vacancy"] for v in vacancies if isinstance(v, dict)
            and isinstance(v.get("vacancy"), dict)]  # fmt: skip
    return rows, total


def parse_vacancy(row: dict[str, Any]) -> Project | None:
    """Вакансия из записи API; без id, названия, ссылки или даты — None. Контакты
    работодателя не берутся: человек откликается на сайте."""
    vacancy_id, title, url = row.get("id"), _text(row.get("job-name")), row.get("vac_url")
    published = _date(row.get("date_modify")) or _date(row.get("creation-date"))
    if not (isinstance(vacancy_id, str) and vacancy_id and title and isinstance(url, str)
            and url.startswith("https://") and published):  # fmt: skip
        return None
    budget_min, budget_max, salary = _salary(row)
    remote = REMOTE_EMPLOYMENT in str(row.get("employment") or "").casefold()
    area = ", ".join(x for x in (_city(row), "удалённо" if remote else "") if x)
    company = row.get("company")
    years = (row.get("requirement") or {}).get("experience")
    extra = {
        k: v
        for k, v in {
            "company": _text(company.get("name")) if isinstance(company, dict) else "",
            "area": area,
            "salary": salary,
            "experience": f"от {years} лет" if isinstance(years, int) and years > 0 else "",
            "schedule": _text(row.get("schedule")),
        }.items()
        if v
    }
    return Project(
        platform="trudvsem",
        external_id=vacancy_id,
        url=url,
        title=title,
        description=_description(row),
        published_at=published,
        budget_min=budget_min,
        budget_max=budget_max,
        offers_count=None,
        category=CATEGORY,
        extra=extra,
    )


def _city(row: dict[str, Any]) -> str:
    """Населённый пункт первого адреса: «г Елец» → «Елец»; село — «Панино село»; без
    адреса — регион."""
    addresses = (row.get("addresses") or {}).get("address")
    location = ""
    if isinstance(addresses, list) and addresses and isinstance(addresses[0], dict):
        location = str(addresses[0].get("location") or "")
    if m := _CITY_RE.search(location):
        return m.group(1)
    for part in location.split(",")[:3]:
        part = part.strip()
        if part and not _AREA_SKIP_RE.search(part):
            return part
    region = row.get("region")
    return str(region.get("name") or "") if isinstance(region, dict) else ""


def _description(row: dict[str, Any]) -> str:
    parts = [
        f"{label}: {text}"
        for label, key in (("Обязанности", "duty"), ("Требования", "requirements"),
                           ("Условия", "conditions"))  # fmt: skip
        if (text := _text(row.get(key)))
    ]
    years = (row.get("requirement") or {}).get("experience")
    # «Опыт работы от 3 лет» — так требование увидят скоринг и обучение (experience.py)
    terms = [
        f"Опыт работы от {years} лет" if isinstance(years, int) and years > 0 else "",
        _text(row.get("schedule")),
        _text(row.get("employment")),
    ]
    if line := "; ".join(t for t in terms if t):
        parts.append(line)
    return "\n\n".join(parts)


def _salary(row: dict[str, Any]) -> tuple[int | None, int | None, str]:
    lo, hi = (v if isinstance(v, int) and v > 0 else None
              for v in (row.get("salary_min"), row.get("salary_max")))  # fmt: skip

    def num(v: int) -> str:
        return f"{v:,}".replace(",", " ")

    if lo and hi and lo != hi:
        text = f"{num(lo)}–{num(hi)} ₽"
    elif lo or hi:
        text = f"от {num(lo)} ₽" if lo else f"до {num(hi)} ₽"  # type: ignore[arg-type]
    else:
        text = _text(row.get("salary"))
    return lo, hi, text


def _date(value: object) -> datetime | None:
    """«2026-10-25T09:56:05+0300» или «2026-10-25» (полночь МСК)."""
    if not isinstance(value, str):
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d"):
        try:
            parsed = datetime.strptime(value, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC) - timedelta(hours=3)
        return parsed.astimezone(UTC)
    return None


def _text(value: object) -> str:
    if not isinstance(value, str):
        return ""
    text = html.unescape(_TAG_RE.sub("\n", value))
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line).strip()
