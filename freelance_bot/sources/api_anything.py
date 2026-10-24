"""Операции goodnight000/api-anything: бот исполняет файл операций сам, без Node.js и браузера.

Файл (`work24.json`, `freelance-ru.json`, `work-zilla.json`) снимает api-anything; бот берёт
из него только операции чтения уровня tier 1 — обычный GET (ADR 0008, 0010):

- адрес — шаблон со слотами `{имя}` (в записи — `%7Bимя%7D`); значение None убирает
  query-параметр слота целиком (robots.txt Freelance.ru закрывает адреса с `?page=`);
- слоты `header:имя` — значение заголовка из параметров (agentid Work-zilla);
- JSON-ответ: `extract` — путь через точку, `pick` — поля «имя» или «новое=старое»;
- HTML-ответ: `items` — селектор записи, `fields` — селекторы полей внутри неё. Селекторы:
  «тег», «.класс», «#id» и их сочетания через пробел (потомок); `@атрибут` в конце поля —
  значение атрибута, а не текст.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import quote

from freelance_bot.config import ConfigError
from freelance_bot.sources import SourceFormatError

# Заголовки браузера из записи API Anything не шлём: бот представляется честно (http.py)
_BROWSER_HEADERS = ("user-agent", "sec-", "priority", "upgrade-insecure-requests")
_SIMPLE_RE = re.compile(r"(?P<tag>[a-z][a-z0-9-]*)?(?P<rest>(?:[.#][\w-]+)*)", re.IGNORECASE)
_VOID_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source",
     "track", "wbr"}
)  # fmt: skip
_BLOCK_TAGS = frozenset(
    {"p", "div", "br", "li", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "tr",
     "blockquote", "pre", "section", "article", "header", "footer", "main", "aside"}
)  # fmt: skip


def load_operations(path: Path, site: str) -> dict[str, dict[str, Any]]:
    """Операции по имени. Бот выполняет только чтение без браузера — иначе ConfigError."""
    try:
        raw = json.loads(Path(path).read_text("utf-8"))
        items = raw["operations"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ConfigError(
            f"{path}: файл операций {site} не читается ({type(exc).__name__})"
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
        operations[name] = {**op, "_site": site}
    return operations


def _site(op: dict[str, Any]) -> str:
    return op.get("_site", "API Anything")


def operation_request(op: dict[str, Any], params: dict[str, object]) -> tuple[str, dict]:
    """Адрес со значениями слотов и заголовки (слоты `header:` — тоже из `params`)."""
    url = op["request"]["url"]
    for name, value in params.items():
        if value is None:
            url = _drop_query_slot(url, name)
            continue
        quoted = quote(str(value), safe="")
        url = url.replace(f"%7B{name}%7D", quoted).replace(f"{{{name}}}", quoted)
    if "%7B" in url or "{" in url:
        raise SourceFormatError(f"{_site(op)}: в адресе операции {op['name']} не задан слот")
    headers = {
        key: value
        for key, value in (op["request"].get("headers") or {}).items()
        if not key.lower().startswith(_BROWSER_HEADERS)
    }
    for slot in op.get("slots") or []:
        name = slot.get("param") or str(slot.get("ref", "")).rsplit("/", 1)[-1]
        for place in slot.get("at") or []:
            if not place.startswith("header:"):
                continue
            if params.get(name) is None:  # имя слота, не значение: в нём нет секрета
                raise SourceFormatError(
                    f"{_site(op)}: для операции {op['name']} не задан заголовок {name}"
                )
            headers[place.removeprefix("header:")] = str(params[name])
    return url, headers


def _drop_query_slot(url: str, name: str) -> str:
    base, sep, query = url.partition("?")
    if not sep:
        return url
    holders = (f"%7B{name}%7D", f"{{{name}}}")
    kept = [part for part in query.split("&") if part.partition("=")[2] not in holders]
    return f"{base}?{'&'.join(kept)}" if kept else base


def _extracted(op: dict[str, Any], text: str) -> Any:
    spec = op["response"]
    try:
        value = json.loads(text)
        for key in spec["extract"].split("."):
            value = value[key]
    except (ValueError, KeyError, TypeError) as exc:
        raise SourceFormatError(f"{_site(op)}: {op['name']} — ответ не того вида") from exc
    return value


def _pick(op: dict[str, Any], row: dict[str, Any]) -> dict[str, Any]:
    fields = [field.partition("=") for field in op["response"]["pick"]]
    return {name: row.get(source or name) for name, _, source in fields}


def extract_json(op: dict[str, Any], text: str) -> list[dict[str, Any]]:
    """Список записей по `extract`, поля по `pick`."""
    rows = _extracted(op, text)
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise SourceFormatError(
            f"{_site(op)}: {op['name']} — «{op['response']['extract']}» не список"
        )
    return [_pick(op, row) for row in rows]


def extract_json_object(op: dict[str, Any], text: str) -> dict[str, Any]:
    """Один объект по `extract` (статус ленты Work-zilla), поля по `pick`."""
    value = _extracted(op, text)
    if not isinstance(value, dict):
        raise SourceFormatError(
            f"{_site(op)}: {op['name']} — «{op['response']['extract']}» не объект"
        )
    return _pick(op, value)


def extract_html_items(op: dict[str, Any], text: str) -> list[dict[str, str | None]]:
    """Записи под селектором `items`: в каждой — первый элемент под селектором поля."""
    spec = op["response"]["html"]
    scope = parse_selector(spec["items"])
    fields = {}
    for name, selector in spec["fields"].items():
        chain, attr = _parse_field(selector)
        fields[name] = (scope + chain, attr)
    parser = _ItemsParser(scope, fields)
    parser.feed(text)
    parser.close()
    return [
        {
            name: (_text(item[name]) if isinstance(item.get(name), list) else item.get(name))
            for name in fields
        }
        for item in parser.items
    ]


def extract_html(op: dict[str, Any], text: str) -> dict[str, str | None]:
    """Поля первой записи (карточка: `items` — body); нет записи или элемента — None."""
    items = extract_html_items(op, text)
    return items[0] if items else dict.fromkeys(op["response"]["html"]["fields"])


# ── CSS-селекторы ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Simple:
    tag: str | None
    classes: frozenset[str]
    id: str | None = None

    def matches(self, el: _Element) -> bool:
        return (
            (self.tag is None or self.tag == el.tag)
            and self.classes <= el.classes
            and (self.id is None or self.id == el.id)
        )


@dataclass(frozen=True)
class _Element:
    tag: str
    classes: frozenset[str]
    id: str | None


def parse_selector(selector: str) -> tuple[_Simple, ...]:
    chain = []
    for part in selector.split():
        m = _SIMPLE_RE.fullmatch(part)
        if not m or not (m["tag"] or m["rest"]):
            raise SourceFormatError(f"API Anything: селектор {selector!r} не поддерживается")
        tokens = re.findall(r"([.#])([\w-]+)", m["rest"])
        ids = [value for kind, value in tokens if kind == "#"]
        if len(ids) > 1:
            raise SourceFormatError(f"API Anything: селектор {selector!r} не поддерживается")
        chain.append(
            _Simple(
                m["tag"].lower() if m["tag"] else None,
                frozenset(value for kind, value in tokens if kind == "."),
                ids[0] if ids else None,
            )
        )
    if not chain:
        raise SourceFormatError("API Anything: пустой селектор")
    return tuple(chain)


def _parse_field(selector: str) -> tuple[tuple[_Simple, ...], str | None]:
    """«.ссылка@href» — атрибут href первого элемента, у которого он есть."""
    css, at, attr = selector.partition("@")
    if at and not re.fullmatch(r"[\w-]+", attr):
        raise SourceFormatError(f"API Anything: селектор {selector!r} не поддерживается")
    return parse_selector(css), (attr.lower() if at else None)


def _chain_matches(chain: tuple[_Simple, ...], stack: list[_Element]) -> bool:
    """Последний селектор — сам элемент, остальные — его предки в том же порядке."""
    if not chain[-1].matches(stack[-1]):
        return False
    need = len(chain) - 2
    for el in reversed(stack[:-1]):
        if need < 0:
            break
        if chain[need].matches(el):
            need -= 1
    return need < 0


class _ItemsParser(HTMLParser):
    def __init__(
        self,
        scope: tuple[_Simple, ...],
        fields: dict[str, tuple[tuple[_Simple, ...], str | None]],
    ) -> None:
        super().__init__(convert_charrefs=True)
        self._scope = scope
        self._fields = fields
        self._stack: list[_Element] = []
        self._item_depth: int | None = None  # глубина элемента открытой записи
        self._open: dict[str, int] = {}  # текстовое поле → глубина его элемента
        self.items: list[dict[str, Any]] = []  # поле → список кусков текста или атрибут

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _BLOCK_TAGS:
            self._newline()
        values = dict(attrs)
        el = _Element(tag, frozenset((values.get("class") or "").split()), values.get("id"))
        self._stack.append(el)
        if self._item_depth is None and _chain_matches(self._scope, self._stack):
            self._item_depth = len(self._stack)
            self.items.append({})
        if self._item_depth is not None:
            self._match_fields(tag in _VOID_TAGS, values)
        if tag in _VOID_TAGS:
            self._stack.pop()

    def _match_fields(self, void: bool, values: dict[str, str | None]) -> None:
        item = self.items[-1]
        for name, (chain, attr) in self._fields.items():
            if name in item or not _chain_matches(chain, self._stack):
                continue
            if attr is not None:
                if values.get(attr) is not None:
                    item[name] = " ".join(str(values[attr]).split())
            elif not void:
                item[name] = []
                self._open[name] = len(self._stack)

    def handle_endtag(self, tag: str) -> None:
        if tag in _BLOCK_TAGS:
            self._newline()
        for depth in range(len(self._stack) - 1, -1, -1):
            if self._stack[depth].tag == tag:
                del self._stack[depth:]
                break
        self._open = {name: d for name, d in self._open.items() if d <= len(self._stack)}
        if self._item_depth is not None and self._item_depth > len(self._stack):
            self._item_depth = None

    def handle_data(self, data: str) -> None:
        if self._stack and self._stack[-1].tag in ("script", "style"):
            return
        for name in self._open:
            self.items[-1][name].append(data)

    def _newline(self) -> None:
        for name in self._open:
            self.items[-1][name].append("\n")


def _text(parts: list[str]) -> str:
    """Абзацы — отдельными строками, пробелы внутри строки схлопнуты."""
    lines = (" ".join(line.split()) for line in "".join(parts).split("\n"))
    return "\n".join(line for line in lines if line)
