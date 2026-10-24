"""Kwork: публичная страница биржи проектов (без входа, только первая страница).

Страница содержит встроенный JSON со списком `"wants":[...]`. Поля проекта:
id, name, description (полный), priceLimit («желаемый бюджет», строка «500.00»),
possiblePriceLimit («допустимый» верхний предел), max_days, kwork_count (число
предложений), date_active (МСК), category_id, user.data.{wants_count, wants_hired_percent}.

Решение по бюджету: budget_max = priceLimit, budget_min = None. possiblePriceLimit —
это потолок, до которого заказчик готов поднять цену (обычно ×3), а не нижняя граница,
поэтому он лежит в extra["possible_price_limit"], а не в budget_min.
"""

from __future__ import annotations

import html
import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from freelance_bot.http import Fetcher
from freelance_bot.models import Platform, Project
from freelance_bot.sources import SourceFormatError

MOSCOW = timezone(timedelta(hours=3))  # без перевода часов с 2014 года
_WANTS_MARKER = '"wants":'


def _to_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(str(value)))
    except ValueError:
        return None


def _find_wants(page: str) -> list[dict]:
    decoder = json.JSONDecoder()
    start = page.find(_WANTS_MARKER)
    if start < 0:
        raise SourceFormatError("Kwork: в странице нет блока wants")
    last_error = "блок wants не массив проектов"
    while start >= 0:
        pos = start + len(_WANTS_MARKER)
        while pos < len(page) and page[pos].isspace():
            pos += 1
        try:
            value, _ = decoder.raw_decode(page, pos)
        except ValueError:
            last_error = "блок wants не разбирается как JSON"
        else:
            if isinstance(value, list) and all(isinstance(x, dict) for x in value):
                return value
        start = page.find(_WANTS_MARKER, start + 1)
    raise SourceFormatError(f"Kwork: {last_error}")


def _parse_want(want: dict) -> Project:
    try:
        external_id = str(want["id"])
        title = html.unescape(str(want["name"])).strip()
        active = datetime.strptime(str(want["date_active"]), "%Y-%m-%d %H:%M:%S")
    except (KeyError, ValueError) as exc:
        raise SourceFormatError(f"Kwork: у проекта нет поля {exc}") from exc
    description = html.unescape(str(want.get("description") or "")).replace("\r\n", "\n").strip()
    user_data = (want.get("user") or {}).get("data") or {}
    extra: dict = {}
    for key, value in (
        ("hired_percent", _to_int(user_data.get("wants_hired_percent"))),
        ("buyer_projects", _to_int(user_data.get("wants_count"))),
        ("possible_price_limit", _to_int(want.get("possiblePriceLimit"))),
    ):
        if value is not None:
            extra[key] = value
    return Project(
        platform="kwork",
        external_id=external_id,
        url=f"https://kwork.ru/projects/{external_id}/view",
        title=title,
        description=description,
        published_at=active.replace(tzinfo=MOSCOW).astimezone(UTC),
        budget_min=None,
        budget_max=_to_int(want.get("priceLimit")),
        offers_count=_to_int(want.get("kwork_count")),
        category=str(want["category_id"]) if want.get("category_id") not in (None, "") else None,
        deadline_days=_to_int(want.get("max_days")),
        extra=extra,
    )


def parse_kwork_projects(html: str) -> list[Project]:  # noqa: A002 - имя из interfaces.md
    wants = _find_wants(html)
    unique: dict[str, Project] = {}
    for want in wants:
        project = _parse_want(want)
        unique.setdefault(project.external_id, project)
    return list(unique.values())


def _first_page_url(url: str) -> str:
    """Пагинацию не используем (robots.txt запрещает ?page=): параметр page убирается."""
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "page"]
    return urlunsplit(parts._replace(query=urlencode(query)))


class KworkSource:
    """Первая страница каждой категории из настроек, без page=."""

    platform: Platform = "kwork"

    def __init__(self, fetcher: Fetcher, urls: Sequence[str]) -> None:
        self._fetcher = fetcher
        self._urls = [_first_page_url(u) for u in urls]

    async def fetch(self) -> list[Project]:
        merged: dict[str, Project] = {}
        for url in self._urls:
            text = await self._fetcher.get_text(url)
            for project in parse_kwork_projects(text):
                merged.setdefault(project.external_id, project)
        return list(merged.values())
