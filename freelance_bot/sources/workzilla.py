"""Work-zilla: операции API Anything из work-zilla.json, сессия аккаунта исполнителя.

feedStatus и listVisibleOrders — один и тот же GET client.work-zilla.com/api/order/v6/list/open
(docs/platforms.md, §6; ADR 0010). Бот делает один запрос за цикл и разбирает ответ обеими
операциями: сначала статус ленты, список — только если `isStaticOrders` равно false.

Пока у аккаунта не пройдены обязательные шаги, Work-zilla отдаёт демонстрационные задания
(`isStaticOrders: true`) — DemoFeedError: ничего не сохраняется и не присылается, в /status
видна причина. Нет поля или ответ без данных (`{"result": 102}` — без заголовка agentid) —
тоже не обрабатываем. Сессия — Cookie (`Bearer`) и agentid из api-anything, в .env:
WORKZILLA_COOKIE, WORKZILLA_AGENT_ID (`python -m freelance_bot workzilla-session`).

Настоящая лента вживую не проверялась (2026-10-24, аккаунт видит только демо): поля заданий
и раздел `interesting` взяты из демо-ответа, ссылки на отдельное задание в ответе нет —
карточка ведёт в ленту исполнителя.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from freelance_bot.config import ConfigError
from freelance_bot.http import Fetcher, HttpStatusError, SourceError
from freelance_bot.models import Platform, Project
from freelance_bot.sources import SourceFormatError
from freelance_bot.sources.api_anything import (
    extract_json,
    extract_json_object,
    load_operations,
    operation_request,
)

log = logging.getLogger(__name__)

SITE = "Work-zilla"
OPERATIONS_PATH = Path(__file__).with_name("work-zilla.json")
STATUS_OPERATION = "feedStatus"
LIST_OPERATION = "listVisibleOrders"
AGENT_SLOT = "WzAgentId"
FEED_URL = "https://client.work-zilla.com/freelancer"
# Кроме «other» (его берёт listVisibleOrders) задания могут прийти в «interesting» — в демо
# он пуст, вид записей предполагается тот же
EXTRA_LISTS = ("data.interesting",)
COOKIE_DOMAIN = "work-zilla.com"
SESSION_HINT = (
    "обновите сессию: войдите в Work-zilla через api-anything и выполните "
    "`python -m freelance_bot workzilla-session --from <state>/sessions/work-zilla.json`"
)


class DemoFeedError(SourceError):
    """Лента демонстрационная (isStaticOrders = true): задания не настоящие."""


class WorkzillaSource:
    """Лента исполнителя Work-zilla → новые задания; демо-лента и лента без статуса — ошибка."""

    platform: Platform = "workzilla"

    def __init__(
        self,
        fetcher: Fetcher,
        cookie: str | None,
        agent_id: str | None,
        *,
        max_age_hours: float,
        known: Callable[[str], bool] = lambda external_id: False,
        operations: dict[str, dict[str, Any]] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._fetcher = fetcher
        self._cookie, self._agent_id = cookie, agent_id
        self._ops = operations if operations is not None else load_operations(
            OPERATIONS_PATH, SITE
        )
        missing = [n for n in (STATUS_OPERATION, LIST_OPERATION) if n not in self._ops]
        if missing:
            raise ConfigError(f"{SITE}: в файле операций нет {', '.join(missing)}")
        self._max_age = timedelta(hours=max_age_hours)
        self._known = known
        self._clock = clock

    async def fetch(self) -> list[Project]:
        if not (self._cookie and self._agent_id):
            raise SourceError(f"{SITE}: не заданы WORKZILLA_COOKIE и WORKZILLA_AGENT_ID — "
                              + SESSION_HINT)  # fmt: skip
        text = await self._feed()
        status = self._feed_status(text)
        if status.get("isStaticOrders") is True:
            raise DemoFeedError(
                f"{SITE}: лента демонстрационная (isStaticOrders = true) — задания не "
                "сохраняются и не присылаются; завершите обязательные шаги в кабинете "
                "исполнителя Work-zilla"
            )
        if status.get("isStaticOrders") is not False:
            raise SourceFormatError(
                f"{SITE}: в ответе нет isStaticOrders — непонятно, настоящие ли задания; "
                "задания не сохранены"
            )
        cutoff = self._clock() - self._max_age
        found: dict[str, Project] = {}
        for row in self._rows(text):
            p = self._project(row)
            if (
                p is None
                or row.get("archived")
                or p.published_at < cutoff
                or self._known(p.external_id)
            ):
                continue
            found.setdefault(p.external_id, p)
        return sorted(found.values(), key=lambda p: p.published_at, reverse=True)

    async def _feed(self) -> str:
        url, headers = operation_request(self._ops[STATUS_OPERATION], {AGENT_SLOT: self._agent_id})
        headers["cookie"] = self._cookie or ""
        try:
            return await self._fetcher.get_text(url, headers=headers)
        except HttpStatusError as exc:
            if exc.status == 401:
                raise SourceError(
                    f"{SITE}: сессия не принята (HTTP 401) — {SESSION_HINT}"
                ) from None
            raise

    def _feed_status(self, text: str) -> dict[str, Any]:
        try:
            raw = json.loads(text)
        except ValueError:
            raise SourceFormatError(
                f"{SITE}: ответ ленты не JSON — похоже, страница входа; {SESSION_HINT}"
            ) from None
        if not isinstance(raw, dict) or not isinstance(raw.get("data"), dict):
            result = raw.get("result") if isinstance(raw, dict) else None
            raise SourceFormatError(
                f"{SITE}: в ответе нет данных ленты (result {result}) — {SESSION_HINT}"
            )
        return extract_json_object(self._ops[STATUS_OPERATION], text)

    def _rows(self, text: str) -> list[dict[str, Any]]:
        op = self._ops[LIST_OPERATION]
        rows = extract_json(op, text)
        for path in EXTRA_LISTS:
            extra = {**op, "response": {**op["response"], "extract": path}}
            try:
                rows += extract_json(extra, text)
            except SourceFormatError as exc:  # необязательный раздел: без него — только other
                log.warning("%s: раздел %s не разобран: %s", SITE, path, exc)
        return rows

    @staticmethod
    def _project(row: dict[str, Any]) -> Project | None:
        try:
            external_id = str(int(row["id"]))
            modified = datetime.fromtimestamp(int(row["modified"]) / 1000, UTC)
        except (KeyError, TypeError, ValueError, OverflowError, OSError):
            log.warning("%s: у задания нет id или времени — пропущено", SITE)
            return None
        title = " ".join(str(row.get("subject") or "").split())
        if not title:
            return None
        try:
            price = int(float(row["price"])) if row.get("price") is not None else None
        except (TypeError, ValueError, OverflowError):
            price = None
        return Project(
            platform="workzilla",
            external_id=external_id,
            url=FEED_URL,
            title=title,
            description=str(row.get("description") or ""),
            published_at=modified,
            budget_min=None,
            budget_max=price or None,
            offers_count=None,
            category=None,  # categoryId без справочника разделов: в операциях его нет
            extra={"category_id": row.get("categoryId")},
        )


def session_from_api_anything(path: Path, now: float | None = None) -> tuple[str, str]:
    """Cookie (действующие, домен work-zilla.com) и agentid из сессии api-anything.

    Значения не печатать: Cookie `Bearer` — вход в аккаунт."""
    try:
        raw = json.loads(Path(path).read_text("utf-8"))
        cookies, values = raw["cookies"], raw.get("values") or {}
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ConfigError(
            f"{path}: сессия api-anything не читается ({type(exc).__name__})"
        ) from None
    moment = time.time() if now is None else now
    pairs = [
        f"{c['name']}={c['value']}"
        for c in cookies
        if isinstance(c, dict)
        and str(c.get("domain", "")).lstrip(".").endswith(COOKIE_DOMAIN)
        and (c.get("expires") is None or c["expires"] <= 0 or c["expires"] > moment)
        and c.get("name") and c.get("value") is not None
    ]
    agent = next(
        (v for k, v in values.items() if k.endswith(f"/{AGENT_SLOT}") and isinstance(v, str)), None
    )
    if not any(pair.startswith("Bearer=") for pair in pairs) or not agent:
        raise ConfigError(
            f"{path}: в сессии нет cookie Bearer или {AGENT_SLOT} — войдите в Work-zilla через "
            "api-anything заново"
        )
    return "; ".join(pairs), agent
