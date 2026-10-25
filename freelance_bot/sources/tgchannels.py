"""Telegram-каналы с вакансиями: веб-превью `t.me/s/<канал>` без аккаунта (ADR 0016,
docs/platforms.md, §9).

Страница — 20 последних постов канала, бот читает только её (без `?before=`). Каналы сети
пишут вакансию в одном формате: `<b>Должность</b>`, строка зарплаты, блоки «Обязанности:»,
«Требования:», «Мы предлагаем:», «Контакты:». Пост без «Обязанности»/«Требования» —
реклама или объявление канала, пропускается. Город вакансии — город канала из настроек.

Условия Telegram запрещают отдавать собранное нейросети: разбор только правилами, суть —
начало описания (summary.BUILTIN_ONLY). Контакты (телефоны частных лиц) не копируются —
в карточке ссылка на пост.
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta

from freelance_bot.http import Fetcher
from freelance_bot.models import Platform, Project
from freelance_bot.sources import SourceFormatError

log = logging.getLogger(__name__)

SITE = "Telegram-каналы"
PAGE_URL = "https://t.me/s/{channel}"
POST_URL = "https://t.me/{channel}/{post}"
CATEGORY = "Вакансия из Telegram-канала"
PAGE_MARK = "tgme_widget_message"  # разметка ленты: есть — страница с постами, не блок
SECTIONS = ("обязанности", "требования")  # без них пост — не вакансия
CONTACTS = "контакты"  # отсюда и до конца поста — телефоны, не копируются
_POST_RE = re.compile(r'data-post="([A-Za-z0-9_]+)/(\d+)"')
_TIME_RE = re.compile(r'<time datetime="([^"]+)"')
_TEXT_RE = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_BR_RE = re.compile(r"<br\s*/?>", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
_URL_RE = re.compile(r"(?:https?://|www\.|t\.me/)\S+", re.I)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_NICK_RE = re.compile(r"(?<![\w@])@[A-Za-z][A-Za-z0-9_]{3,}")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+7|8)[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)")
_MONEY_RE = re.compile(r"\d[\d\s]*\d|\d")
# Латиница вместо кириллицы в русском слове («Пoдгoтoвкa»): поиск по словам её не найдёт
_LATIN = str.maketrans("aceopxyABCEHKMOPTX", "асеорхуАВСЕНКМОРТХ")
_MIXED_RE = re.compile(r"\b(?=\w*[а-яё])(?=\w*[a-z])\w+\b", re.I)


class TgChannelsSource:
    """Вакансии каналов `channels` (имя канала → город) за max_age_hours."""

    platform: Platform = "tgchannels"

    def __init__(
        self,
        fetcher: Fetcher,
        channels: Mapping[str, str],
        *,
        max_age_hours: float,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._fetcher = fetcher
        self._channels = dict(channels)
        self._max_age = timedelta(hours=max_age_hours)
        self._clock = clock

    async def fetch(self) -> list[Project]:
        since = self._clock() - self._max_age
        found: dict[str, Project] = {}
        failed: list[str] = []
        for channel, city in self._channels.items():
            url = PAGE_URL.format(channel=channel)
            page = await self._fetcher.get_text(url, expect=PAGE_MARK)
            if "data-post=" not in page:  # канал закрыт, переименован или сменилась разметка
                failed.append(channel)
                continue
            for p in parse_page(page, channel, city):
                if p.published_at >= since:
                    found.setdefault(p.external_id, p)  # тот же пост в другом канале города
        if failed:
            if len(failed) == len(self._channels):
                raise SourceFormatError(f"{SITE}: ни на одной странице нет постов")
            log.warning("%s: нет постов у %s", SITE, ", ".join(failed))
        return sorted(found.values(), key=lambda p: p.published_at, reverse=True)


def parse_page(page: str, channel: str, city: str) -> list[Project]:
    """Вакансии со страницы `t.me/s/<канал>`; реклама и объявления — пропускаются."""
    projects = []
    for block in page.split('<div class="tgme_widget_message_wrap')[1:]:
        post, when, text = _POST_RE.search(block), _TIME_RE.search(block), _TEXT_RE.search(block)
        if not (post and when and text):
            continue
        try:
            published = datetime.fromisoformat(when.group(1)).astimezone(UTC)
        except ValueError:
            continue
        p = parse_post(_lines(text.group(1)), post.group(1), post.group(2), city, published)
        if p is not None:
            projects.append(p)
    return projects


def parse_post(
    lines: list[str], channel: str, post: str, city: str, published: datetime
) -> Project | None:
    """Пост сети: должность, зарплата, блоки. Не вакансия — None."""
    heads = [_head(line) for line in lines]
    if not any(h in SECTIONS for h in heads) or len(lines) < 2:
        return None
    if CONTACTS in heads:
        lines = lines[: heads.index(CONTACTS)]
    title = _fix_latin(lines[0])
    salary_line = lines[1] if _head(lines[1]) not in SECTIONS else ""
    body = [_without_contacts(_fix_latin(line)) for line in lines[1 + bool(salary_line):]]
    description = "\n".join(line for line in body if re.search(r"\w", line))
    budget_min, budget_max, salary = _salary(salary_line)
    digest = hashlib.sha1(f"{title.casefold()}\n{description.casefold()}".encode()).hexdigest()
    return Project(
        platform="tgchannels",
        external_id=digest[:16],
        url=POST_URL.format(channel=channel, post=post),
        title=title,
        description=description,
        published_at=published,
        budget_min=budget_min,
        budget_max=budget_max,
        offers_count=None,
        category=CATEGORY,
        extra={k: v for k, v in {"area": city, "salary": salary}.items() if v},
    )


def _lines(markup: str) -> list[str]:
    text = html.unescape(_TAG_RE.sub("", _BR_RE.sub("\n", markup)))
    lines = (" ".join(line.split()) for line in text.splitlines())
    return [line for line in lines if line]


def _head(line: str) -> str:
    """«Обязанности:» → «обязанности»; строка с текстом после двоеточия — не заголовок."""
    name, colon, rest = line.partition(":")
    return name.strip().casefold() if colon and not rest.strip() else ""


def _without_contacts(line: str) -> str:
    for pattern in (_URL_RE, _EMAIL_RE, _PHONE_RE, _NICK_RE):
        line = pattern.sub("", line)
    return " ".join(line.split())


def _fix_latin(text: str) -> str:
    return _MIXED_RE.sub(lambda m: m.group(0).translate(_LATIN), text)


def _salary(line: str) -> tuple[int | None, int | None, str]:
    """«96000 ₽» → 96000; «5400 - 6000 ₽» → 5400–6000; «Зарплата по договоренности.» —
    без суммы, текстом."""
    if not line:
        return None, None, ""
    amounts = [int(re.sub(r"\s", "", m)) for m in _MONEY_RE.findall(line)] if "₽" in line else []
    amounts = [a for a in amounts if a >= 1000][:2]
    if not amounts:
        return None, None, line.rstrip(".")
    lo, hi = min(amounts), max(amounts)

    def num(v: int) -> str:
        return f"{v:,}".replace(",", " ")

    return lo, hi, f"{num(lo)}–{num(hi)} ₽" if lo != hi else f"{num(lo)} ₽"

