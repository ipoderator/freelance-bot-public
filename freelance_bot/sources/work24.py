"""Work24: операции API Anything из work24.json, только чтение.

Файл операций снят инструментом goodnight000/api-anything с публичного интерфейса Work24
(без входа и ключа). Бот исполняет его сам, без Node.js и браузера: все операции —
tier 1, то есть обычный GET (docs/platforms.md, §3; ADR 0008).

- listItOrders / listOrders(page) — JSON `orders`, поля по `pick` («новое=старое»): id, slug,
  title, created_at (unix), deadline («Срок сдачи», unix), price (строка «1200» или
  «Предлагайте»), category, group (только listOrders), offersCount, status (0 — открыт);
- getOrder(slug) — HTML карточки, поля по CSS-селекторам: title, description, status
  («Открыт»).

Полное описание есть только в карточке, поэтому карточки открываются лишь для новых
открытых заказов: известные базе ID (`known`) пропускаются ещё до запроса.
"""

from __future__ import annotations

import json
import logging
import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote

from freelance_bot.config import ConfigError
from freelance_bot.http import BlockedError, Fetcher, SourceError
from freelance_bot.models import Platform, Project
from freelance_bot.sources import SourceFormatError

log = logging.getLogger(__name__)

OPERATIONS_PATH = Path(__file__).with_name("work24.json")
CARD_OPERATION = "getOrder"
OPEN_STATUS = "Открыт"
IT_GROUP = "Разработка и IT"  # раздел ленты listItOrders: в её pick нет поля group
MAX_PAGES = 2  # по 20 заказов; в «Разработка и IT» открыто около 60
MAX_DETAILS = 15  # карточек за цикл; остальные новые — в следующем цикле
# Заголовки браузера из записи API Anything не шлём: бот представляется честно (http.py)
_BROWSER_HEADERS = ("user-agent", "sec-", "priority", "upgrade-insecure-requests")
_SLUG_RE = re.compile(r"[0-9a-z][0-9a-z-]*", re.IGNORECASE)
_SIMPLE_RE = re.compile(r"(?P<tag>[a-z][a-z0-9-]*)?(?P<classes>(?:\.[\w-]+)*)", re.IGNORECASE)
_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
     "track", "wbr"}
)  # fmt: skip
_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr",
     "blockquote", "pre"}
)  # fmt: skip


# ── операции API Anything ────────────────────────────────────────────────────


def load_operations(path: Path = OPERATIONS_PATH) -> dict[str, dict[str, Any]]:
    """Операции по имени. Бот выполняет только чтение без браузера — иначе ConfigError."""
    try:
        raw = json.loads(Path(path).read_text("utf-8"))
        items = raw["operations"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ConfigError(
            f"{path}: файл операций Work24 не читается ({type(exc).__name__})"
        ) from None
    operations: dict[str, dict[str, Any]] = {}
    for op in items:
        name = op.get("name")
        request = op.get("request") or {}
        if request.get("method") != "GET" or op.get("readOnly") is not True:
            raise ConfigError(f"{path}: операция {name} не только для чтения — бот её не выполнит")
        if op.get("minTier", 1) != 1 or op.get("volatile"):
            raise ConfigError(f"{path}: операции {name} нужен браузер — бот её не выполнит")
        if (op.get("response") or {}).get("format") not in ("json", "html"):
            raise ConfigError(f"{path}: у операции {name} неизвестный формат ответа")
        operations[name] = op
    return operations


def operation_request(op: dict[str, Any], params: dict[str, object]) -> tuple[str, dict]:
    """Адрес со значениями слотов ({page} в шаблоне записан как %7Bpage%7D) и заголовки."""
    url = op["request"]["url"]
    for name, value in params.items():
        quoted = quote(str(value), safe="")
        url = url.replace(f"%7B{name}%7D", quoted).replace(f"{{{name}}}", quoted)
    if "%7B" in url or "{" in url:
        raise SourceFormatError(f"Work24: в адресе операции {op['name']} не задан слот")
    headers = {
        key: value
        for key, value in (op["request"].get("headers") or {}).items()
        if not key.lower().startswith(_BROWSER_HEADERS)
    }
    return url, headers


def extract_json(op: dict[str, Any], text: str) -> list[dict[str, Any]]:
    """`extract` — ключ со списком, `pick` — поля «имя» или «новое=старое»."""
    spec = op["response"]
    try:
        rows = json.loads(text)[spec["extract"]]
    except (ValueError, KeyError, TypeError) as exc:
        raise SourceFormatError(f"Work24: {op['name']} — ответ не того вида") from exc
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise SourceFormatError(f"Work24: {op['name']} — «{spec['extract']}» не список")
    fields = [field.partition("=") for field in spec["pick"]]
    return [{name: row.get(source or name) for name, _, source in fields} for row in rows]


def extract_html(op: dict[str, Any], text: str) -> dict[str, str | None]:
    """Текст первого элемента под каждым селектором поля; нет элемента — None."""
    spec = op["response"]["html"]
    scope = _parse_selector(spec["items"])
    chains = {name: scope + _parse_selector(sel) for name, sel in spec["fields"].items()}
    parser = _FieldsParser(chains)
    parser.feed(text)
    parser.close()
    return {name: _text(parser.texts[name]) if name in parser.texts else None for name in chains}


# ── CSS-селекторы: «тег», «.класс», «тег.класс» через пробел (потомок) ────────


@dataclass(frozen=True)
class _Simple:
    tag: str | None
    classes: frozenset[str]

    def matches(self, tag: str, classes: frozenset[str]) -> bool:
        return (self.tag is None or self.tag == tag) and self.classes <= classes


def _parse_selector(selector: str) -> tuple[_Simple, ...]:
    chain = []
    for part in selector.split():
        m = _SIMPLE_RE.fullmatch(part)
        if not m or not (m["tag"] or m["classes"]):
            raise SourceFormatError(f"Work24: селектор {selector!r} не поддерживается")
        tag = m["tag"].lower() if m["tag"] else None
        chain.append(_Simple(tag, frozenset(m["classes"].split(".")[1:])))
    if not chain:
        raise SourceFormatError("Work24: пустой селектор")
    return tuple(chain)


def _chain_matches(chain: tuple[_Simple, ...], stack: list[tuple[str, frozenset[str]]]) -> bool:
    """Последний селектор — сам элемент, остальные — его предки в том же порядке."""
    if not chain[-1].matches(*stack[-1]):
        return False
    need = len(chain) - 2
    for tag, classes in reversed(stack[:-1]):
        if need < 0:
            break
        if chain[need].matches(tag, classes):
            need -= 1
    return need < 0


class _FieldsParser(HTMLParser):
    def __init__(self, chains: dict[str, tuple[_Simple, ...]]) -> None:
        super().__init__(convert_charrefs=True)
        self._chains = chains
        self._stack: list[tuple[str, frozenset[str]]] = []
        self._open: dict[str, int] = {}  # поле → глубина его элемента в стеке
        self.texts: dict[str, list[str]] = {}

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self._newline()
        if tag in _VOID_TAGS:
            return
        classes = frozenset(next((v or "" for k, v in attrs if k == "class"), "").split())
        self._stack.append((tag, classes))
        for name, chain in self._chains.items():
            if name not in self.texts and _chain_matches(chain, self._stack):
                self.texts[name] = []
                self._open[name] = len(self._stack)

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self._newline()
        for depth in range(len(self._stack) - 1, -1, -1):
            if self._stack[depth][0] == tag:
                del self._stack[depth:]
                break
        self._open = {name: d for name, d in self._open.items() if d <= len(self._stack)}

    def handle_data(self, data: str) -> None:
        if self._stack and self._stack[-1][0] in ("script", "style"):
            return
        for name in self._open:
            self.texts[name].append(data)

    def _newline(self) -> None:
        for name in self._open:
            self.texts[name].append("\n")


def _text(parts: list[str]) -> str:
    """Абзацы — отдельными строками, пробелы внутри строки схлопнуты."""
    lines = (" ".join(line.split()) for line in "".join(parts).split("\n"))
    return "\n".join(line for line in lines if line)


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
        description = _parse_selector(self._ops[CARD_OPERATION]["response"]["html"]["fields"]
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
