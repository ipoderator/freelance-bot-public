"""Freelance.ru: операции API Anything из freelance-ru.json, только чтение, без входа.

Файл операций снят goodnight000/api-anything с публичных страниц (docs/platforms.md, §5;
ADR 0010); бот исполняет его сам (`api_anything.py`):

- listTasks(page) — HTML ленты /task: 25 заданий всех разделов, около двух суток; поля по
  селекторам: title, url (/task/view/<id>), description (обрезанный фрагмент), category,
  published_at («24.10.2026 19:30», МСК), budget («1 000 ₽ / заказ» или «Обсуждается
  индивидуально»), access («Видно всем» или «★ Только для Премиум»);
- getPublicTask(taskId) — HTML карточки: title, content (шапка: заголовок, полный текст,
  затем «Гонорар», «Срок», «Опыт», «Часовой пояс»), budget, category.

robots.txt закрывает адреса с `?page=`, поэтому читается только первая страница и без
параметра — `/task`. Задание «Только для Премиум» без премиум-доступа закрыто («У вас нет
доступа к этому заданию»): пропускается по пометке в ленте. Лента общая для всех разделов —
карточки открываются только для разделов из настроек (`categories`), один раз и только для
нового публичного задания моложе max_age_hours.

Контакты заказчика («Телеграм: @ник», t.me/…) из текста вырезаются: правила площадки держат их
в профиле (п. 9.5), а слово «Телеграм» из контакта засчитывалось бы как «Telegram-боты» и
попадало бы в черновик.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from freelance_bot.config import ConfigError
from freelance_bot.http import BlockedError, Fetcher, SourceError
from freelance_bot.models import Platform, Project
from freelance_bot.sources import SourceFormatError
from freelance_bot.sources.api_anything import (
    extract_html,
    extract_html_items,
    load_operations,
    operation_request,
)

log = logging.getLogger(__name__)

SITE = "Freelance.ru"
OPERATIONS_PATH = Path(__file__).with_name("freelance-ru.json")
LIST_OPERATION = "listTasks"
CARD_OPERATION = "getPublicTask"
TASK_URL = "https://freelance.ru/task/view/{id}"
PUBLIC_MARK = "Видно всем"
MAX_DETAILS = 10  # карточек за цикл; остальные новые — в следующем цикле
# Разметка, которая есть только в ленте и в карточке: она в ответе — значит, это данные,
# даже если в <title> задания слово «captcha» (http.looks_blocked)
LIST_MARKER = "task-card__title-link"
CARD_MARKER = "tv-hero"
_MSK = ZoneInfo("Europe/Moscow")
_TASK_PATH_RE = re.compile(r"(?:https://freelance\.ru)?/task/view/(\d+)")
_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4}) (\d{2}):(\d{2})")
_MONEY_RE = re.compile(r"(\d[\d\s]*)\s*₽")
_TERM_RE = re.compile(r"(\d+)\s*(дн|день|недел|месяц|мес)", re.IGNORECASE)
_TERM_DAYS = {"дн": 1, "день": 1, "недел": 7, "месяц": 30, "мес": 30}
# Подписи блока под текстом карточки: «Гонорар», затем значение
_META_LABELS = ("Гонорар", "Срок", "Опыт", "Часовой пояс")
_CONTACT_LABEL = (
    r"(?:телеграмм?|telegram|тг|tg|whats ?app|ватсап|вотсап|viber|вайбер|контакты для связи|"
    r"для связи|пишите|писать)\s*[:\-–—]?\s*"
)
_CONTACT_RE = re.compile(
    r"[\w.+-]+@[\w-]+\.[a-z]{2,}"  # e-mail
    rf"|(?:{_CONTACT_LABEL})?https?://(?:t\.me|telegram\.me|wa\.me)/\S+"
    rf"|{_CONTACT_LABEL}@[a-z]\w{{3,}}",  # «@ник» только с подписью: «@BotFather» в ТЗ — не контакт
    re.IGNORECASE,
)


class FreelanceRuSource:
    """Первая страница ленты /task → новые публичные задания с полным текстом из карточки."""

    platform: Platform = "freelance_ru"

    def __init__(
        self,
        fetcher: Fetcher,
        *,
        max_age_hours: float,
        categories: Sequence[str] = (),
        known: Callable[[str], bool] = lambda external_id: False,
        operations: dict[str, dict[str, Any]] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._fetcher = fetcher
        self._categories = frozenset(categories)  # пусто — все разделы
        self._ops = operations if operations is not None else load_operations(
            OPERATIONS_PATH, SITE
        )
        missing = [name for name in (LIST_OPERATION, CARD_OPERATION) if name not in self._ops]
        if missing:
            raise ConfigError(f"{SITE}: в файле операций нет {', '.join(missing)}")
        self._max_age = timedelta(hours=max_age_hours)
        self._known = known
        self._clock = clock
        self._closed: set[str] = set()  # карточка без доступа: не открывать снова
        self._premium: set[str] = set()  # уже посчитанные в журнале «Только для Премиум»

    async def fetch(self) -> list[Project]:
        fresh = await self._fresh_tasks()
        projects: list[Project] = []
        failures: list[SourceError] = []
        for row in fresh[:MAX_DETAILS]:
            try:
                card = await self._card(row["id"])
            except BlockedError:
                raise
            except SourceError as exc:  # эта карточка — в следующем цикле, остальные — сейчас
                log.warning("%s: карточка %s не получена: %s", SITE, row["id"], exc)
                failures.append(exc)
                continue
            if not card.get("title"):  # «У вас нет доступа к этому заданию»
                log.info("%s: задание %s закрыто для просмотра — пропущено", SITE, row["id"])
                self._closed.add(row["id"])
                continue
            projects.append(self._project(row, card))
        if failures and not projects:
            raise failures[-1]
        return projects

    async def _fresh_tasks(self) -> list[dict[str, Any]]:
        """Публичные задания моложе max_age_hours, которых нет в базе; свежие первыми."""
        url, headers = operation_request(self._ops[LIST_OPERATION], {"page": None})
        text = await self._fetcher.get_text(url, headers=headers, expect=LIST_MARKER)
        items = extract_html_items(self._ops[LIST_OPERATION], text)
        rows = [row for row in map(self._row, items) if row is not None]
        if not rows:
            raise SourceFormatError(f"{SITE}: в ленте /task не нашлось заданий — сменился формат")
        cutoff = self._clock() - self._max_age
        fresh, premium = [], []
        for row in rows:
            if row["published"] < cutoff or row["id"] in self._closed or self._known(row["id"]):
                continue
            if self._categories and row.get("category") not in self._categories:
                continue
            if PUBLIC_MARK not in (row.get("access") or ""):
                premium.append(row["id"])
                continue
            fresh.append(row)
        new_premium = [i for i in premium if i not in self._premium]
        if new_premium:
            self._premium.update(new_premium)
            log.info("%s: пропущено «Только для Премиум»: %d", SITE, len(new_premium))
        return sorted(fresh, key=lambda row: row["published"], reverse=True)

    async def _card(self, task_id: str) -> dict[str, str | None]:
        url, headers = operation_request(self._ops[CARD_OPERATION], {"taskId": task_id})
        text = await self._fetcher.get_text(url, headers=headers, expect=CARD_MARKER)
        return extract_html(self._ops[CARD_OPERATION], text)

    @staticmethod
    def _row(raw: dict[str, str | None]) -> dict[str, Any] | None:
        """Запись ленты; без ссылки, заголовка или даты — None (реклама, чужая разметка)."""
        path = _TASK_PATH_RE.fullmatch(raw.get("url") or "")
        published = _moscow_time(raw.get("published_at"))
        if not path or published is None or not raw.get("title"):
            return None
        return dict(raw, id=path.group(1), published=published)

    @staticmethod
    def _project(row: dict[str, Any], card: dict[str, str | None]) -> Project:
        title = " ".join(str(card.get("title") or row["title"]).split())
        body, meta = _split_content(card.get("content") or "", title)
        description = _without_contacts(body or row.get("description") or "")
        experience = meta.get("Опыт", "")
        if experience.lower().startswith("от "):  # «От 3 лет» → scoring поймает требование
            description += f"\nТребуемый опыт {experience.lower()}."
        return Project(
            platform="freelance_ru",
            external_id=row["id"],
            url=TASK_URL.format(id=row["id"]),
            title=title,
            description=description,
            published_at=row["published"],
            budget_min=None,
            budget_max=_budget(card.get("budget") or row.get("budget")),
            offers_count=None,
            category=card.get("category") or row.get("category"),
            deadline_days=_days(meta.get("Срок")),
        )


def _moscow_time(value: str | None) -> datetime | None:
    m = _DATE_RE.fullmatch((value or "").strip())
    if not m:
        return None
    day, month, year, hour, minute = map(int, m.groups())
    try:
        return datetime(year, month, day, hour, minute, tzinfo=_MSK).astimezone(UTC)
    except ValueError:
        return None


def _split_content(content: str, title: str) -> tuple[str, dict[str, str]]:
    """Шапка карточки → текст задания (между заголовком и «Гонорар») и подписи под ним."""
    lines = content.split("\n")
    start = next((i + 1 for i, line in enumerate(lines) if line == title), None)
    if start is None:
        return "", {}
    end = next((i for i in range(start, len(lines)) if lines[i] == _META_LABELS[0]), len(lines))
    meta = {
        lines[i]: lines[i + 1]
        for i in range(end, len(lines) - 1)
        if lines[i] in _META_LABELS
    }
    return "\n".join(lines[start:end]).strip(), meta


def _without_contacts(text: str) -> str:
    """Без «Телеграм: @ник» и ссылок на мессенджеры; опустевшие строки — прочь."""
    lines = (_CONTACT_RE.sub("", line).strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if re.search(r"\w", line))


def _budget(text: str | None) -> int | None:
    """«1 000 ₽ / заказ» → 1000; «Обсуждается индивидуально» и почасовая ставка — None."""
    m = _MONEY_RE.search(text or "")
    if not m or "/ час" in (text or ""):
        return None
    return int(re.sub(r"\s", "", m.group(1)))


def _days(text: str | None) -> int | None:
    """«14 дней», «1 день», «2 недели», «1 месяц» → дни; «Не важен» — None."""
    m = _TERM_RE.search(text or "")
    if not m:
        return None
    word = m.group(2).lower()
    return int(m.group(1)) * next(d for prefix, d in _TERM_DAYS.items() if word.startswith(prefix))
