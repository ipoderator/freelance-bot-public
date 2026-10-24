from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from freelance_bot.http import (
    BlockedError,
    Fetcher,
    SourceError,
    TemporarySourceError,
    looks_blocked,
)

FEED = "<rss><channel><item><title>Бот</title></item></channel></rss>"
FIXTURES = Path(__file__).parent / "fixtures"
FEED_WITH_CAPTCHA_WORD = (
    '<?xml version="1.0" encoding="utf-8"?><rss version="2.0"><channel>'
    "<title>Заказы на FL.ru</title><item><title>Парсер с обходом капчи (Бюджет: 5 000 ₽)</title>"
    "<description>Сайт показывает captcha, «я не робот» — нужен скрипт.</description>"
    "</item></channel></rss>"
)
BIG_LISTING = (
    "<html><head><title>Биржа проектов</title></head><body><script>window.stateData = "
    '{"wants":[{"id":1,"name":"Парсер","description":"Нужен парсер сайта, там стоит '
    'captcha, «я не робот» и cloudflare — обойти капчу"}]}</script>'
    + "<div class='card'>проект</div>" * 2000
    + "</body></html>"
)


class FakeTime:
    """Управляемые часы: sleep не ждёт, а двигает время и запоминает задержку."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def make_fetcher(
    handler: Callable[[httpx.Request], httpx.Response], ft: FakeTime, **kwargs
) -> Fetcher:
    return Fetcher(
        transport=httpx.MockTransport(handler),
        sleep=ft.sleep,
        clock=ft.clock,
        rng=lambda: 0.0,
        **kwargs,
    )


def scripted(responses: list, calls: list[httpx.Request]):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        item = responses[min(len(calls), len(responses)) - 1]
        if isinstance(item, Exception):
            raise item
        return item

    return handler


async def test_returns_text_with_honest_user_agent() -> None:
    ft, calls = FakeTime(), []
    fetcher = make_fetcher(scripted([httpx.Response(200, text=FEED)], calls), ft)
    assert await fetcher.get_text("https://www.fl.ru/rss/all.xml") == FEED
    ua = calls[0].headers["user-agent"]
    assert "freelance-bot/1.0" in ua
    assert "personal use" in ua
    await fetcher.aclose()


async def test_retries_5xx_with_exponential_backoff() -> None:
    ft, calls = FakeTime(), []
    handler = scripted(
        [httpx.Response(500), httpx.Response(502), httpx.Response(200, text=FEED)], calls
    )
    fetcher = make_fetcher(handler, ft)
    assert await fetcher.get_text("https://kwork.ru/projects?c=11") == FEED
    assert len(calls) == 3
    assert [s for s in ft.sleeps if s > 0] == [2.0, 4.0]


async def test_jitter_is_added_to_backoff() -> None:
    ft, calls = FakeTime(), []
    handler = scripted([httpx.Response(503), httpx.Response(200, text=FEED)], calls)
    fetcher = Fetcher(
        transport=httpx.MockTransport(handler), sleep=ft.sleep, clock=ft.clock, rng=lambda: 0.5
    )
    await fetcher.get_text("https://kwork.ru/projects?c=11")
    assert ft.sleeps[-1] == pytest.approx(2.5)


async def test_gives_up_after_three_attempts_on_5xx() -> None:
    ft, calls = FakeTime(), []
    fetcher = make_fetcher(scripted([httpx.Response(503)], calls), ft)
    with pytest.raises(TemporarySourceError):
        await fetcher.get_text("https://kwork.ru/projects?c=11")
    assert len(calls) == 3


async def test_retries_timeouts_and_network_errors() -> None:
    ft, calls = FakeTime(), []
    request = httpx.Request("GET", "https://www.fl.ru/rss/all.xml")
    handler = scripted(
        [
            httpx.ReadTimeout("timeout", request=request),
            httpx.ConnectError("refused", request=request),
            httpx.Response(200, text=FEED),
        ],
        calls,
    )
    fetcher = make_fetcher(handler, ft)
    assert await fetcher.get_text("https://www.fl.ru/rss/all.xml") == FEED
    assert len(calls) == 3


async def test_timeouts_exhausted_raise_temporary_error() -> None:
    ft, calls = FakeTime(), []
    request = httpx.Request("GET", "https://www.fl.ru/rss/all.xml")
    fetcher = make_fetcher(scripted([httpx.ConnectTimeout("t", request=request)], calls), ft)
    with pytest.raises(TemporarySourceError):
        await fetcher.get_text("https://www.fl.ru/rss/all.xml")
    assert len(calls) == 3


async def test_429_respects_retry_after() -> None:
    ft, calls = FakeTime(), []
    handler = scripted(
        [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, text=FEED)],
        calls,
    )
    fetcher = make_fetcher(handler, ft)
    assert await fetcher.get_text("https://kwork.ru/projects?c=11") == FEED
    assert 7.0 in ft.sleeps
    assert len(calls) == 2


async def test_429_without_retry_after_uses_backoff_and_is_limited() -> None:
    ft, calls = FakeTime(), []
    fetcher = make_fetcher(scripted([httpx.Response(429)], calls), ft)
    with pytest.raises(TemporarySourceError):
        await fetcher.get_text("https://kwork.ru/projects?c=11")
    assert len(calls) == 3


async def test_huge_retry_after_is_not_slept_inside_fetch() -> None:
    ft, calls = FakeTime(), []
    fetcher = make_fetcher(
        scripted([httpx.Response(429, headers={"Retry-After": "3600"})], calls), ft
    )
    with pytest.raises(TemporarySourceError) as exc:
        await fetcher.get_text("https://kwork.ru/projects?c=11")
    assert exc.value.retry_after == 3600
    assert len(calls) == 1
    assert sum(ft.sleeps) < 60


async def test_403_is_blocked_without_retries() -> None:
    ft, calls = FakeTime(), []
    fetcher = make_fetcher(scripted([httpx.Response(403, text="Forbidden")], calls), ft)
    with pytest.raises(BlockedError):
        await fetcher.get_text("https://kwork.ru/projects?c=11")
    assert len(calls) == 1


async def test_captcha_page_is_blocked_without_retries() -> None:
    ft, calls = FakeTime(), []
    page = "<html><title>Ой!</title><body>Подтвердите, что вы не робот. captcha</body></html>"
    fetcher = make_fetcher(scripted([httpx.Response(200, text=page)], calls), ft)
    with pytest.raises(BlockedError):
        await fetcher.get_text("https://kwork.ru/projects?c=11")
    assert len(calls) == 1


async def test_other_4xx_is_source_error_without_retries() -> None:
    ft, calls = FakeTime(), []
    fetcher = make_fetcher(scripted([httpx.Response(404)], calls), ft)
    with pytest.raises(SourceError) as exc:
        await fetcher.get_text("https://kwork.ru/projects?c=999")
    assert not isinstance(exc.value, TemporarySourceError | BlockedError)
    assert len(calls) == 1


async def test_min_two_seconds_between_requests_to_same_domain() -> None:
    ft = FakeTime()
    hits: list[tuple[str, float]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append((request.url.host, ft.now))
        return httpx.Response(200, text=FEED)

    fetcher = make_fetcher(handler, ft)
    await fetcher.get_text("https://kwork.ru/projects?c=11")
    await fetcher.get_text("https://kwork.ru/projects?c=41")
    await fetcher.get_text("https://www.fl.ru/rss/all.xml")
    await fetcher.get_text("https://kwork.ru/projects?c=15")

    kwork_times = [t for host, t in hits if host == "kwork.ru"]
    assert all(b - a >= 2.0 for a, b in zip(kwork_times, kwork_times[1:], strict=False))
    fl_time = next(t for host, t in hits if host == "www.fl.ru")
    assert fl_time == kwork_times[1], "другой домен не ждёт"


@pytest.mark.parametrize(
    ("status", "text", "expected"),
    [
        (403, "", True),
        (403, BIG_LISTING, True),
        (200, FEED, False),
        (200, BIG_LISTING, False),
        (200, "<html><body>Подтвердите, что вы не робот</body></html>", True),
        (200, "<html><body><div>Введите код с картинки (captcha)</div></body></html>", True),
        (200, FEED_WITH_CAPTCHA_WORD, False),
        (200, FEED_WITH_CAPTCHA_WORD.replace("<?xml", "\n  <?xml"), False),
        (
            200,
            "<!DOCTYPE html><html><head><title>Проекты на Kwork</title><script "
            'src="https://smartcaptcha.yandexcloud.net/captcha.js?render=onload"></head>'
            "<body></body></html>",
            False,
        ),
        (
            200,
            "<html><head><title>Проекты</title></head><body><script>window.s="
            '{"isYandexSmartCaptcha":true}</script>Нет проектов</body></html>',
            False,
        ),
        (503, "<html><head><title>Just a moment...</title></head></html>", True),
        (200, '<html><script src="/cdn-cgi/challenge-platform/h/b/orch.js"></script>', True),
        (200, "<html><title>DDoS-Guard</title></html>", True),
        (500, "Internal Server Error", False),
        (429, "Too Many Requests", False),
        (404, "Not found", False),
    ],
)
def test_looks_blocked(status: int, text: str, expected: bool) -> None:
    assert looks_blocked(status, text) is expected


def test_real_kwork_listing_with_smartcaptcha_script_is_not_blocked() -> None:
    page = (FIXTURES / "http_kwork_normal.html").read_text("utf-8")
    assert "smartcaptcha.yandexcloud.net" in page and '"isYandexSmartCaptcha":true' in page
    assert looks_blocked(200, page) is False


async def test_kwork_listing_is_fetched_not_blocked() -> None:
    ft, calls = FakeTime(), []
    page = (FIXTURES / "http_kwork_normal.html").read_text("utf-8")
    fetcher = make_fetcher(scripted([httpx.Response(200, text=page)], calls), ft)
    assert await fetcher.get_text("https://kwork.ru/projects?c=11") == page


@pytest.mark.parametrize(
    "error",
    [
        httpx.TooManyRedirects("loop", request=httpx.Request("GET", "https://kwork.ru/")),
        httpx.DecodingError("bad gzip", request=httpx.Request("GET", "https://kwork.ru/")),
    ],
)
async def test_redirect_loop_and_decoding_error_are_source_errors(error: Exception) -> None:
    ft, calls = FakeTime(), []
    fetcher = make_fetcher(scripted([error], calls), ft)
    with pytest.raises(SourceError) as exc:
        await fetcher.get_text("https://kwork.ru/projects?c=11")
    assert not isinstance(exc.value, TemporarySourceError | BlockedError)
    assert len(calls) == 1
