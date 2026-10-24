"""Вежливый HTTP-клиент для публичных страниц площадок.

Только GET, честный User-Agent, не чаще одного запроса к домену раз в 2 секунды,
до 3 попыток при временных ошибках. Блокировку (403, CAPTCHA, анти-бот страница)
не обходим: сразу BlockedError, без повторов.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

log = logging.getLogger(__name__)

DEFAULT_USER_AGENT = "Mozilla/5.0 (compatible; freelance-bot/1.0 (personal use))"
DEFAULT_TIMEOUT_SEC = 20.0
MAX_ATTEMPTS = 3
MIN_INTERVAL_SEC = 2.0
# Retry-After больше этого не ждём внутри запроса: отдаём ошибку, цикл повторит позже.
MAX_RETRY_AFTER_SEC = 60.0
JITTER_SEC = 1.0

# Признаки анти-бот страницы в разметке, которых нет в обычных страницах площадок.
# Не «smartcaptcha»: Kwork подключает скрипт Yandex SmartCaptcha на каждой странице.
_STRUCTURAL_MARKERS = (
    "/cdn-cgi/challenge-platform",
    "cf-chl-",
    "cf_chl_",
)
# Слова в <title> страницы блокировки.
_TITLE_MARKERS = (
    "captcha",
    "капча",
    "робот",
    "robot",
    "just a moment",
    "attention required",
    "access denied",
    "доступ ограничен",
    "доступ запрещ",
    "ddos",
)
# Слова в видимом тексте — только для коротких страниц-заглушек: в полной ленте они
# могут встретиться в тексте проекта («нужен парсер с обходом капчи»). Скрипты и
# атрибуты тегов (src=".../captcha.js") не смотрим.
_BODY_MARKERS = (
    "captcha",
    "капч",
    "не робот",
    "not a robot",
    "checking your browser",
    "проверка браузера",
    "доступ ограничен",
    "ddos-guard",
)
_STUB_TEXT_MAX_CHARS = 2000
_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]*>")
_FEED_PREFIXES = ("<?xml", "<rss", "<feed")
_JSON_PREFIXES = ("{", "[")


class SourceError(Exception):
    """Источник не отдал данные (базовая ошибка)."""


class TemporarySourceError(SourceError):
    """Временная ошибка (таймаут, сеть, 5xx, 429): повторы исчерпаны."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class HttpStatusError(SourceError):
    """Площадка ответила 4xx (кроме 403 и 429): адрес или доступ не тот, повтор не поможет."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


class BlockedError(SourceError):
    """Площадка показала CAPTCHA/403/блок-страницу. Не обходим и не повторяем."""


def looks_blocked(status: int, text: str, expect: str | None = None) -> bool:
    """`expect` — разметка обычной страницы (класс поля с данными): есть в ответе — это
    данные. Нужна, когда в <title> текст заказа: «Парсер с обходом капчи» — не блокировка."""
    if status == 403:
        return True
    if text.lstrip("\ufeff \t\r\n").lower().startswith(_FEED_PREFIXES + _JSON_PREFIXES):
        # RSS/XML-лента или JSON API — данные, а не страница блокировки, какие бы слова в них
        # ни были.
        return False
    if expect is not None and expect in text:
        return False
    lowered = text.lower()
    if any(marker in lowered for marker in _STRUCTURAL_MARKERS):
        return True
    title = _TITLE_RE.search(text)
    if title and any(marker in title.group(1).lower() for marker in _TITLE_MARKERS):
        return True
    visible = _TAG_RE.sub(" ", _SCRIPT_STYLE_RE.sub(" ", _TITLE_RE.sub(" ", text)))
    visible = " ".join(visible.split()).lower()
    if len(visible) <= _STUB_TEXT_MAX_CHARS:
        return any(marker in visible for marker in _BODY_MARKERS)
    return False


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (moment - datetime.now(UTC)).total_seconds())


class Fetcher:
    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        *,
        timeout: float = DEFAULT_TIMEOUT_SEC,
        max_attempts: int = MAX_ATTEMPTS,
        min_interval: float = MIN_INTERVAL_SEC,
        max_retry_after: float = MAX_RETRY_AFTER_SEC,
        user_agent: str = DEFAULT_USER_AGENT,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: Callable[[], float] = random.random,
    ) -> None:
        self._client = httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": user_agent,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.5",
            },
        )
        self._max_attempts = max(1, max_attempts)
        self._min_interval = min_interval
        self._max_retry_after = max_retry_after
        self._sleep = sleep
        self._clock = clock
        self._rng = rng
        self._last_request: dict[str, float] = {}
        self._host_locks: dict[str, asyncio.Lock] = {}

    async def __aenter__(self) -> Fetcher:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_text(
        self, url: str, *, headers: dict[str, str] | None = None, expect: str | None = None
    ) -> str:
        """`headers` — дополнительные заголовки запроса; `expect` — см. looks_blocked."""
        host = httpx.URL(url).host
        for attempt in range(1, self._max_attempts + 1):
            retry_after: float | None = None
            try:
                response = await self._polite_get(host, url, headers)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                problem = f"сетевая ошибка {type(exc).__name__}"
            except (httpx.TooManyRedirects, httpx.DecodingError) as exc:
                raise SourceError(f"{host}: {type(exc).__name__} для {url}") from None
            else:
                status, text = response.status_code, response.text
                if looks_blocked(status, text, expect):
                    raise BlockedError(f"{host}: похоже на блокировку/CAPTCHA (HTTP {status})")
                if status == 429 or status >= 500:
                    problem = f"HTTP {status}"
                    retry_after = _parse_retry_after(response.headers.get("Retry-After"))
                elif status >= 400:
                    raise HttpStatusError(f"{host}: HTTP {status} для {url}", status)
                else:
                    return text

            if retry_after is not None and retry_after > self._max_retry_after:
                raise TemporarySourceError(
                    f"{host}: {problem}, сервер просит подождать {retry_after:.0f} с",
                    retry_after=retry_after,
                )
            if attempt == self._max_attempts:
                raise TemporarySourceError(
                    f"{host}: {problem}, попыток: {attempt}", retry_after=retry_after
                )
            delay = 2.0**attempt + self._rng() * JITTER_SEC
            if retry_after is not None:
                delay = max(delay, retry_after)
            log.warning(
                "%s: %s, попытка %d из %d, повтор через %.1f с",
                host,
                problem,
                attempt,
                self._max_attempts,
                delay,
            )
            await self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    async def _polite_get(
        self, host: str, url: str, headers: dict[str, str] | None = None
    ) -> httpx.Response:
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            last = self._last_request.get(host)
            if last is not None:
                wait = last + self._min_interval - self._clock()
                if wait > 0:
                    await self._sleep(wait)
            try:
                return await self._client.get(url, headers=headers)
            finally:
                self._last_request[host] = self._clock()
