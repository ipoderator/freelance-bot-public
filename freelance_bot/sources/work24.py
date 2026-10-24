"""Work24: операции API Anything из work24.json, только чтение.

Файл операций снят инструментом goodnight000/api-anything с публичного интерфейса Work24
(без входа и ключа). Бот исполняет его сам (`api_anything.py`), без Node.js и браузера: все
операции — tier 1, то есть обычный GET (docs/platforms.md, §3; ADR 0008).

- listItOrders / listOrders(page) — JSON `orders`, поля по `pick` («новое=старое»): id, slug,
  title, created_at (unix), deadline («Срок сдачи», unix), price (строка «1200» или
  «Предлагайте»), category, group (только listOrders), offersCount, status (0 — открыт);
- getOrder(slug) — HTML карточки, поля по CSS-селекторам: title, description, status
  («Открыт»).

Полное описание есть только в карточке, поэтому карточки открываются лишь для новых
открытых заказов: известные базе ID (`known`) пропускаются ещё до запроса.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from freelance_bot.config import ConfigError
from freelance_bot.http import BlockedError, Fetcher, SourceError
from freelance_bot.models import Platform, Project
from freelance_bot.sources import SourceFormatError, api_anything
from freelance_bot.sources.api_anything import (
    extract_html,
    extract_json,
    operation_request,
    parse_selector,
)

log = logging.getLogger(__name__)

OPERATIONS_PATH = Path(__file__).with_name("work24.json")
CARD_OPERATION = "getOrder"
OPEN_STATUS = "Открыт"
IT_GROUP = "Разработка и IT"  # раздел ленты listItOrders: в её pick нет поля group
MAX_PAGES = 2  # по 20 заказов; в «Разработка и IT» открыто около 60
MAX_DETAILS = 15  # карточек за цикл; остальные новые — в следующем цикле
_SLUG_RE = re.compile(r"[0-9a-z][0-9a-z-]*", re.IGNORECASE)


def load_operations(path: Path = OPERATIONS_PATH) -> dict[str, dict[str, Any]]:
    """Операции Work24 по имени (исполнитель — sources/api_anything.py)."""
    return api_anything.load_operations(path, "Work24")


# ── источник ─────────────────────────────────────────────────────────────────


def _to_int(value: object) -> int | None:
    try:
        return int(float(str(value).replace(" ", "")))
    except (ValueError, OverflowError):
        return None


class Work24Source:
    """Ленты из настроек (sources.work24.operations) → новые открытые заказы с полным ТЗ."""

    platform: Platform = "work24"

    def __init__(
        self,
        fetcher: Fetcher,
        feeds: Sequence[str],
        *,
        max_age_hours: float,
        known: Callable[[str], bool] = lambda external_id: False,
        operations: dict[str, dict[str, Any]] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._fetcher = fetcher
        self._ops = operations if operations is not None else load_operations()
        missing = [name for name in (*feeds, CARD_OPERATION) if name not in self._ops]
        if missing:
            raise ConfigError(f"Work24: в файле операций нет {', '.join(missing)}")
        self._feeds = list(feeds)
        self._max_age = timedelta(hours=max_age_hours)
        self._known = known
        self._clock = clock
        self._closed: set[str] = set()  # карточка уже не «Открыт»: не открывать снова
        # Класс поля описания: есть в ответе — это карточка заказа, а не страница блокировки
        description = parse_selector(self._ops[CARD_OPERATION]["response"]["html"]["fields"]
                                     ["description"])  # fmt: skip
        self._card_marker = min(description[-1].classes, default=None)

    async def fetch(self) -> list[Project]:
        fresh = await self._fresh_orders()
        projects: list[Project] = []
        failures: list[SourceError] = []
        for row in fresh[:MAX_DETAILS]:
            try:
                card = await self._call(CARD_OPERATION, slug=row["slug"])
            except BlockedError:
                raise
            except SourceError as exc:  # эта карточка — в следующем цикле, остальные — сейчас
                log.warning("Work24: карточка %s не получена: %s", row["id"], exc)
                failures.append(exc)
                continue
            if card["status"] != OPEN_STATUS:
                self._closed.add(row["id"])
                continue
            projects.append(self._project(row, card))
        if failures and not projects:
            raise failures[-1]
        return projects

    async def _fresh_orders(self) -> list[dict[str, Any]]:
        """Открытые заказы моложе max_age_hours, которых нет в базе; свежие первыми."""
        cutoff = self._clock() - self._max_age
        fresh: dict[str, dict[str, Any]] = {}
        for feed in self._feeds:
            for page in range(1, MAX_PAGES + 1):
                rows = [self._row(row) for row in await self._call(feed, page=page)]
                for row in rows:
                    if (
                        row["status"] == 0
                        and row["created"] >= cutoff
                        and row["id"] not in self._closed
                        and not self._known(row["id"])
                    ):
                        row.setdefault("group", IT_GROUP if feed == "listItOrders" else None)
                        fresh.setdefault(row["id"], row)
                if not rows or all(row["created"] < cutoff for row in rows):
                    break
        return sorted(fresh.values(), key=lambda row: row["created"], reverse=True)

    async def _call(self, name: str, **params: object) -> Any:
        op = self._ops[name]
        url, headers = operation_request(op, params)
        if op["response"]["format"] == "json":
            return extract_json(op, await self._fetcher.get_text(url, headers=headers))
        text = await self._fetcher.get_text(url, headers=headers, expect=self._card_marker)
        card = extract_html(op, text)
        if card.get("status") is None:
            raise SourceFormatError(f"Work24: в карточке {params} нет статуса заказа")
        return card

    @staticmethod
    def _row(raw: dict[str, Any]) -> dict[str, Any]:
        external_id, created = _to_int(raw.get("id")), _to_int(raw.get("created_at"))
        slug = str(raw.get("slug") or "")
        if external_id is None or created is None or not raw.get("title"):
            raise SourceFormatError("Work24: у заказа в ленте нет id, title или created_at")
        if not _SLUG_RE.fullmatch(slug) or "status" not in raw:
            raise SourceFormatError(f"Work24: у заказа {external_id} нет адреса или статуса")
        row = dict(raw, id=str(external_id), slug=slug)
        row["created"] = datetime.fromtimestamp(created, UTC)
        return row

    @staticmethod
    def _project(row: dict[str, Any], card: dict[str, str | None]) -> Project:
        created = int(row["created"].timestamp())
        deadline = _to_int(row.get("deadline"))
        days = math.ceil((deadline - created) / 86400) if deadline and deadline > created else None
        group, category = row.get("group"), row.get("category")
        return Project(
            platform="work24",
            external_id=row["id"],
            url=f"https://work24.ru/order/{row['slug']}",
            title=" ".join(str(row["title"]).split()) or card.get("title") or "",
            description=card.get("description") or "",
            published_at=row["created"],
            budget_min=None,
            budget_max=_to_int(row.get("price")),  # «Предлагайте» — бюджета нет
            offers_count=_to_int(row.get("offersCount")),
            category=" / ".join(str(x) for x in (group, category) if x) or None,
            deadline_days=days,
        )
