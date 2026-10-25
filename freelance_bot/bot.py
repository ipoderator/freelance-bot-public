"""Telegram-бот (python-telegram-bot 21): команды, кнопки, расписание сбора.

Хэндлеры заказов отвечают только владельцу (TELEGRAM_OWNER_ID). Остальным — анкета
публичного бота (onboarding.py), если [public] enabled, иначе короткий отказ.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from telegram import (
    Bot,
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import RetryAfter
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from freelance_bot.config import Settings
from freelance_bot.drafting import remaining_placeholders
from freelance_bot.models import Project, ScoreResult
from freelance_bot.onboarding import PREFIX as ONBOARDING_PREFIX
from freelance_bot.onboarding import PUBLIC_HELP, Onboarding
from freelance_bot.pipeline import Keyboard, Offer, Pipeline
from freelance_bot.pricing import estimate
from freelance_bot.storage import Storage
from freelance_bot.telegram_ui import (
    BUTTON_APPLIED,
    BUTTON_CANCEL,
    BUTTON_OFFER,
    BUTTON_REDRAFT,
    BUTTON_SKIP,
    DIGEST_TITLE,
    MAX_MESSAGE_LEN,
    MENU_ALL,
    MENU_NEW,
    MENU_ROWS,
    MENU_STATS,
    MENU_STATUS,
    PLATFORM_NAMES,
    VACANCY_NOTE,
    VACANCY_PLATFORMS,
    format_applied,
    format_filters,
    format_offer,
    format_offer_hint,
    format_offer_list,
    format_offer_prompt,
    format_start,
    format_stats,
    format_status,
    format_vacancy_card,
    offer_list_keyboard,
    selection_reason,
)

log = logging.getLogger(__name__)

COMMANDS: list[tuple[str, str]] = [
    ("start", "Анкета: заполнить или посмотреть"),
    ("menu", "Меню: новые и все заказы, статистика, статус"),
    ("status", "Работает ли бот, последний сбор, ошибки"),
    ("pause", "Поставить сбор и уведомления на паузу"),
    ("resume", "Снять паузу"),
    ("filters", "Текущие фильтры и режимы"),
    ("stats", "Статистика за сегодня и 7 дней"),
    ("last", "Подходящие заказы: выбрать и получить готовый отклик"),
    ("new", "Новые заказы, которых ещё не присылал"),
    ("signup", "Заполнить анкету заново"),
]
# Меню посторонних в публичном режиме (ADR 0011)
PUBLIC_COMMANDS: list[tuple[str, str]] = [
    ("start", "Анкета: заполнить или посмотреть"),
    ("profile", "Ваши данные"),
    ("forget", "Удалить мои данные"),
]
DENIED_TEXT = "Этот бот личный. Доступ закрыт."
ERROR_TEXT = "⚠ Не получилось выполнить действие ({name}). Попробуйте ещё раз."
# Только то, что бот обрабатывает: сообщения и нажатия кнопок
ALLOWED_UPDATES = ["message", "callback_query"]
SEND_RETRIES = 2  # 429 от Telegram: ждём retry_after и повторяем, иначе карточка потеряется
MAX_RETRY_AFTER_SEC = 60.0
TYPING_REFRESH_SEC = 4.0  # «печатает…» Telegram показывает около 5 с
LAST_LIMIT = 20  # в несколько сообщений, если не влезает в одно
AWAITING_OFFER_FLAG = "awaiting_offer"  # "<platform>:<id>" — ждём от владельца цену и срок
LAST_LIST_FLAG = "last_list"  # "<platform>:<id>,…" — заказы последнего /last по номерам
MAX_OFFER_DAYS = 365
MAX_OFFER_TEXT = 40  # «15 000 руб., 14 дней» — 21 символ
MENU_VERSION_FLAG = "menu_version"
MENU_VERSION = "1"  # новое меню — новая версия: бот один раз пришлёт его сам после запуска
MENU_MARKUP = ReplyKeyboardMarkup(
    [list(row) for row in MENU_ROWS], resize_keyboard=True, is_persistent=True
)
_PICK_RE = re.compile(r"^\s*№?\s*(\d{1,2})\s*$")  # «2», «№2» — номер заказа из /last

# «15000, 5», «15 000 ₽ 5 дней», «15к/5»: цена (можно «к» = тысячи), разделитель, срок в днях
_OFFER_RE = re.compile(
    r"^\s*(\d[\d \u00a0]*?)\s*(к|k|тыс\.?)?\s*(?:₽|р\.?|руб\w*)?"
    r"(?:\s*[,;/]\s*|\s+)(\d{1,3})\s*(?:дн\w*|д\.?|сут\w*)?\s*$",
    re.IGNORECASE,
)


def parse_offer(text: str) -> tuple[int, int] | None:
    """Цена в рублях и срок в днях из сообщения владельца; не разобрали — None."""
    # _OFFER_RE на длинной строке пробелов перебирает варианты полиномиально (200 пробелов —
    # 1,5 с, 4000 — часы) и вешает весь бот: цена и срок короткие, длинное — не они
    text = " ".join(text.split())
    if len(text) > MAX_OFFER_TEXT:
        return None
    match = _OFFER_RE.match(text)
    if match is None:
        return None
    price = int(re.sub(r"\D", "", match.group(1)))
    if match.group(2):
        price *= 1000
    days = int(match.group(3))
    if price <= 0 or not 0 < days <= MAX_OFFER_DAYS:
        return None
    return price, days


def _markup(keyboard: Keyboard) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(label, callback_data=data) for label, data in row]
            for row in keyboard
        ]
    )


class TelegramNotifier:
    """Шлёт сообщения владельцу. `bot` можно передать позже: build_application привяжет свой."""

    def __init__(self, bot: Bot | None, owner_id: int | None) -> None:
        self.bot = bot
        self.owner_id = owner_id

    def bind(self, bot: Bot) -> None:
        if self.bot is None:
            self.bot = bot

    async def send_card(self, text: str, keyboard: Keyboard) -> None:
        await self._send(text, _markup(keyboard))

    async def send_text(self, text: str) -> None:
        for chunk in _chunks(text):
            await self._send(chunk, None)

    async def _send(self, text: str, markup: InlineKeyboardMarkup | None) -> None:
        if self.bot is None or self.owner_id is None:
            raise RuntimeError("TelegramNotifier не готов: нет бота или TELEGRAM_OWNER_ID")
        for attempt in range(SEND_RETRIES + 1):
            try:
                await self.bot.send_message(
                    chat_id=self.owner_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                    link_preview_options=LinkPreviewOptions(is_disabled=True),
                )
                return
            except RetryAfter as exc:
                wait = _seconds(exc.retry_after)
                if attempt == SEND_RETRIES or wait > MAX_RETRY_AFTER_SEC:
                    raise
                log.warning("Telegram: лимит сообщений, повтор через %.0f с", wait)
                await asyncio.sleep(wait)


def _seconds(value: float | timedelta) -> float:
    return value.total_seconds() if isinstance(value, timedelta) else float(value)


@contextlib.asynccontextmanager
async def _typing(message: Any):
    """«Печатает…» в чате, пока готовится отклик: LLM отвечает до десятков секунд."""

    async def show() -> None:
        with contextlib.suppress(Exception):  # индикатор — удобство, не ошибка
            await message.reply_chat_action(ChatAction.TYPING)

    async def refresh() -> None:
        while True:
            await asyncio.sleep(TYPING_REFRESH_SEC)
            await show()

    await show()
    task = asyncio.create_task(refresh())
    try:
        yield
    finally:
        task.cancel()


def _chunks(text: str, size: int = MAX_MESSAGE_LEN) -> list[str]:
    if len(text) <= size:
        return [text]
    parts, rest = [], text
    while len(rest) > size:
        cut = rest.rfind("\n", 0, size)
        cut = cut if cut > 0 else size
        parts.append(rest[:cut])
        rest = rest[cut:].lstrip("\n")
    return [*parts, rest]


def next_digest_at(now: datetime, tz: str, start_hour: int, interval_min: int) -> datetime:
    """Ближайший слот дайджеста после `now`: сетка от start_hour:00 с шагом interval_min."""
    local = now.astimezone(ZoneInfo(tz))
    anchor = local.replace(hour=start_hour, minute=0, second=0, microsecond=0)
    step = timedelta(minutes=interval_min)
    slot = anchor + ((local - anchor) // step + 1) * step  # первый слот строго позже now
    return slot.astimezone(UTC)


def list_pages(
    items: list[tuple[Project, int | None]],
    summaries: list[str],
    guesses: list[Any],
    tz: str,
    now: datetime,
    title: str | None = None,
    reasons: list[str] | None = None,
) -> list[tuple[str, Keyboard]]:
    """Список заказов по сообщениям: в каждом сколько влезет без обрыва, нумерация сквозная."""
    extra = {"title": title} if title else {}

    def page(lo: int, hi: int) -> str:
        return format_offer_list(
            items[lo:hi], tz, summaries[lo:hi], guesses[lo:hi], lo + 1, now, **extra,
            reasons=reasons[lo:hi] if reasons else None,
        )  # fmt: skip

    pages, lo = [], 0
    while lo < len(items):
        hi = lo + 1
        while hi < len(items) and len(page(lo, hi + 1)) <= MAX_MESSAGE_LEN:
            hi += 1
        pages.append((page(lo, hi), offer_list_keyboard(items[lo:hi], lo + 1)))
        lo = hi
    return pages


class BotHandlers:
    def __init__(self, settings: Settings, storage: Storage, pipeline: Pipeline) -> None:
        self.settings = settings
        self.storage = storage
        self.pipeline = pipeline
        summarize = pipeline.summarizer.summarize_many if pipeline is not None else None
        self.onboarding = Onboarding(settings, storage, summarize=summarize)

    def _is_owner(self, update: Update) -> bool:
        """Владелец и только в личке: в группе ответы (заказы, цены) увидели бы все."""
        user = update.effective_user
        chat = getattr(update, "effective_chat", None)
        return (
            user is not None
            and self.settings.owner_id is not None
            and user.id == self.settings.owner_id
            and (chat is None or chat.type == "private")
        )

    def _is_public(self, update: Update) -> bool:
        """Посторонний в личке при включённом публичном режиме — ему анкета."""
        user = update.effective_user
        chat = getattr(update, "effective_chat", None)
        return (
            self.settings.public_enabled
            and user is not None
            and (chat is None or chat.type == "private")
            and not self._is_owner(update)
        )

    def _owner_in_signup(self, update: Update) -> bool:
        """Владелец проходит анкету для проверки (/signup): его сообщения — в неё."""
        return self._is_owner(update) and self.onboarding.in_progress(update.effective_user.id)

    async def _reply(
        self, update: Update, text: str, markup: InlineKeyboardMarkup | None = None
    ) -> None:
        await update.effective_message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    async def _deny(self, update: Update) -> None:
        text = PUBLIC_HELP if self._is_public(update) else DENIED_TEXT
        if update.callback_query is not None:
            await update.callback_query.answer(text, show_alert=True)
        elif update.effective_message is not None:
            await update.effective_message.reply_text(text)

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/start — сразу анкета, и у владельца (решение пользователя); его меню — /menu."""
        if not (self._is_public(update) or self._is_owner(update)):
            return await self._deny(update)
        await self.onboarding.start(update, context)

    async def menu(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._deny(update)
        await self._reply(update, format_start(self._next_digest()), MENU_MARKUP)

    async def signup(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Анкета глазами нового пользователя; профиль владельца (profile.toml) не меняется."""
        if not self._is_owner(update):
            return await self._deny(update)
        await self.onboarding.start(update, context, restart=True)

    async def profile(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not (self._is_public(update) or self._is_owner(update)):
            return await self._deny(update)
        await self.onboarding.profile(update, context)

    async def forget(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not (self._is_public(update) or self._is_owner(update)):
            return await self._deny(update)
        await self.onboarding.forget(update, context)

    def _next_digest(self) -> str | None:
        """Время ближайшего дайджеста для показа («12:00»); дайджест выключен — None."""
        if not self.settings.digest_enabled:
            return None
        s = self.settings
        slot = next_digest_at(
            datetime.now(UTC), s.timezone, s.digest_start_hour, s.digest_interval_min
        )
        return f"{slot.astimezone(ZoneInfo(s.timezone)):%H:%M}"

    async def status(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._deny(update)
        flags = {}
        for platform in self.settings.sources:
            for kind in ("last_ok", "last_error", "blocked_until"):
                flags[f"{kind}:{platform}"] = self.storage.get_flag(f"{kind}:{platform}")
        paused = self.storage.get_flag("paused") == "1"
        await self._reply(
            update, format_status(self.settings, paused, flags, self._next_digest())
        )

    async def pause(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._deny(update)
        self.storage.set_flag("paused", "1")
        await self._reply(update, "⏸ Пауза: сбор и уведомления остановлены. /resume — продолжить.")

    async def resume(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._deny(update)
        self.storage.set_flag("paused", "0")
        await self._reply(update, "▶ Пауза снята, сбор продолжается.")

    async def filters_(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._deny(update)
        await self._reply(update, format_filters(self.settings))

    async def stats(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._deny(update)
        now = datetime.now(UTC)
        local = now.astimezone(ZoneInfo(self.settings.timezone))
        day_start = local.replace(hour=0, minute=0, second=0, microsecond=0)
        today = self.storage.stats(day_start)
        week = self.storage.stats(now - timedelta(days=7))
        await self._reply(update, format_stats(today, week))

    async def last(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._is_owner(update):
            return await self._deny(update)
        candidates = self.pipeline.offer_candidates(LAST_LIMIT)
        async with _typing(update.effective_message):
            pages = await self._pages(candidates)
        if not pages:
            return await self._reply(update, format_offer_list([], self.settings.timezone))
        for text, keyboard in pages:
            await self._reply(update, text, _markup(keyboard))

    async def new(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Новые заказы сейчас, не дожидаясь дайджеста."""
        if not self._is_owner(update):
            return await self._deny(update)
        async with _typing(update.effective_message):
            sent = await self.send_digest(lambda text, kb: self._reply(update, text, _markup(kb)))
        if not sent:
            await self._reply(
                update,
                f"Новых заказов пока нет — пришлю сам, как появятся. Все подходящие: {MENU_ALL}.",
            )

    async def send_digest(self, send: Any) -> int:
        """Новые (queued) заказы списком через `send(text, keyboard)`; после доставки они
        больше не новые. Возвращает, сколько прислано."""
        candidates = self.pipeline.digest_items()
        if not candidates:
            return 0
        title = DIGEST_TITLE.format(count=len(candidates))
        for text, keyboard in await self._pages(candidates, title):
            await send(text, keyboard)
        self.pipeline.mark_notified([p for p, _ in candidates])
        return len(candidates)

    async def _pages(
        self, candidates: list[tuple[Project, ScoreResult]], title: str | None = None
    ) -> list[tuple[str, Keyboard]]:
        """Сообщения списка; номера запоминаются, чтобы выбрать заказ цифрой в чат."""
        items = [(p, r.score) for p, r in candidates]
        guesses = [estimate(p, r, self.settings) for p, r in candidates]
        summaries = await self.pipeline.summarizer.summarize_many([p for p, _ in items])
        shown = ",".join(f"{p.platform}:{p.external_id}" for p, _ in items)
        self.storage.set_flag(LAST_LIST_FLAG, shown)
        now = datetime.now(UTC)
        reasons = [selection_reason(r) for _, r in candidates]
        return list_pages(
            items, summaries, guesses, self.settings.timezone, now, title, reasons
        )

    async def text(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Обычное сообщение владельца: номер заказа из /last или цена и срок для выбранного.
        Постороннего — ответ на вопрос анкеты."""
        if self._is_public(update):
            return await self.onboarding.text(update, context)
        if not self._is_owner(update):
            return await self._deny(update)
        message_text = update.effective_message.text or ""
        menu = {
            MENU_NEW: self.new,
            MENU_ALL: self.last,
            MENU_STATS: self.stats,
            MENU_STATUS: self.status,
        }.get(message_text.strip())
        if menu is not None:
            return await menu(update, context)
        if self._owner_in_signup(update):  # меню владельца работает и посреди анкеты
            return await self.onboarding.text(update, context)
        pick = _PICK_RE.match(message_text)
        if pick is not None:
            return await self._pick_by_number(update, int(pick.group(1)))
        awaiting = self.storage.get_flag(AWAITING_OFFER_FLAG) or ""
        platform, _, external_id = awaiting.partition(":")
        if not external_id:
            return await self._reply(
                update,
                "Выберите заказ: /last, затем кнопка или номер. "
                "Для цены и срока — кнопка «Цена и срок» под откликом.",
            )
        offer = parse_offer(message_text)
        if offer is None:
            return await self._reply(update, format_offer_hint())
        async with _typing(update.effective_message):
            ready = await self.pipeline.offer(platform, external_id, *offer)
        self.storage.set_flag(AWAITING_OFFER_FLAG, "")
        if ready is None:
            return await self._reply(update, "Заказ не найден в базе. Выберите другой: /last")
        await self._send_offer(update.effective_message, ready)

    async def _pick_by_number(self, update: Update, number: int) -> None:
        shown = [item for item in (self.storage.get_flag(LAST_LIST_FLAG) or "").split(",") if item]
        if not 1 <= number <= len(shown):
            hint = f"от 1 до {len(shown)}" if shown else "сначала откройте /last"
            return await self._reply(update, f"Нет заказа №{number} в списке /last: {hint}.")
        platform, _, external_id = shown[number - 1].partition(":")
        await self._respond(update.effective_message, platform, external_id)

    async def _respond(self, message: Any, platform: str, external_id: str) -> None:
        """Готовый отклик на выбранный заказ — сразу, без вопроса о цене. Вакансия — её
        карточка со ссылкой: отклик пользователь делает на hh.ru сам."""
        self.storage.set_flag(AWAITING_OFFER_FLAG, "")
        if platform in VACANCY_PLATFORMS:
            await self._send_vacancy(message, platform, external_id)
            return
        async with _typing(message):
            ready = await self.pipeline.response(platform, external_id)
        if ready is None:
            await message.reply_text("Заказ не найден в базе. Выберите другой: /last")
            return
        await self._send_offer(message, ready)

    async def _send_vacancy(self, message: Any, platform: str, external_id: str) -> None:
        p = self.storage.get_project(platform, external_id)
        if p is None:
            await message.reply_text("Вакансия не найдена в базе. Выберите другую: /last")
            return
        summary = await self.pipeline.summarizer.summarize(p)
        key = f"{platform}:{external_id}"
        markup = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(f"🔗 Открыть вакансию на {PLATFORM_NAMES[platform]}",
                                      url=p.url)],  # fmt: skip
                [
                    InlineKeyboardButton(BUTTON_APPLIED, callback_data=f"applied:{key}"),
                    InlineKeyboardButton(BUTTON_SKIP, callback_data=f"skip:{key}"),
                ],
            ]
        )
        text = format_vacancy_card(
            p, self.storage.get_score(platform, external_id), self.settings.timezone, summary
        )
        await message.reply_text(
            f"{text}\n\n{VACANCY_NOTE}",
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    async def _send_offer(self, message: Any, ready: Offer) -> None:
        p = ready.project
        site = PLATFORM_NAMES.get(p.platform, p.platform)
        key = f"{p.platform}:{p.external_id}"
        markup = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(f"🔗 Открыть заказ на {site}", url=p.url)],
                [
                    InlineKeyboardButton(BUTTON_OFFER, callback_data=f"offer:{key}"),
                    InlineKeyboardButton(BUTTON_REDRAFT, callback_data=f"redraft:{key}"),
                ],
                [InlineKeyboardButton(BUTTON_APPLIED, callback_data=f"applied:{key}")],
            ]
        )
        leftovers = remaining_placeholders(ready.draft.text)
        await message.reply_text(
            format_offer(
                p, ready.draft, leftovers, ready.price_rub, ready.days, ready.own, ready.variant
            ),
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    async def announce_menu(self, bot: Any) -> None:
        """Один раз после обновления меню присылает его владельцу сам — без /start."""
        shown = self.storage.get_flag(MENU_VERSION_FLAG) == MENU_VERSION
        if self.settings.owner_id is None or shown:
            return
        try:
            await bot.send_message(
                chat_id=self.settings.owner_id,
                text=format_start(self._next_digest()),
                parse_mode=ParseMode.HTML,
                reply_markup=MENU_MARKUP,
            )
        except Exception as exc:  # меню — удобство, запуск из-за него не падает
            log.warning("не удалось прислать меню: %s", type(exc).__name__)
            return
        self.storage.set_flag(MENU_VERSION_FLAG, MENU_VERSION)

    async def on_error(self, update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        # Только тип ошибки: текст сетевых исключений может содержать адрес с токеном.
        name = type(context.error).__name__
        log.error("ошибка обработчика: %s", name)
        if getattr(update, "effective_user", None) is None or not (
            self._is_owner(update) or self._is_public(update)
        ):
            return
        # иначе владелец не узнает, что нажатие или команда не сработали
        if update.effective_message is not None:
            with contextlib.suppress(Exception):
                await update.effective_message.reply_text(ERROR_TEXT.format(name=name))

    async def fallback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Не текст: файл резюме для анкеты; владельцу вне анкеты — ничего."""
        if self._is_public(update) or self._owner_in_signup(update):
            return await self.onboarding.document(update, context)
        if not self._is_owner(update):
            await self._deny(update)

    async def button(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        data = getattr(update.callback_query, "data", None) or ""
        if data.startswith(ONBOARDING_PREFIX) and (
            self._is_public(update) or self._is_owner(update)
        ):
            return await self.onboarding.button(update, context)
        if not self._is_owner(update):
            return await self._deny(update)
        query = update.callback_query
        action, _, rest = (query.data or "").partition(":")
        platform, _, external_id = rest.partition(":")
        if (
            action not in ("applied", "skip", "redraft", "offer", "cancel", "pick")
            or platform not in PLATFORM_NAMES
            or not external_id
        ):
            return await query.answer("Неизвестная кнопка")
        p = self.storage.get_project(platform, external_id)
        if p is None:
            return await query.answer("Проект не найден в базе", show_alert=True)

        if platform in VACANCY_PLATFORMS and action in ("offer", "redraft"):
            return await query.answer(VACANCY_NOTE, show_alert=True)
        if action == "pick":
            await query.answer("Готовлю отклик…" if platform not in VACANCY_PLATFORMS else None)
            await self._respond(query.message, platform, external_id)
            return
        if action == "offer":
            self.storage.set_flag(AWAITING_OFFER_FLAG, f"{platform}:{external_id}")
            await query.answer()
            cancel = _markup([[(BUTTON_CANCEL, f"cancel:{platform}:{external_id}")]])
            summary = await self.pipeline.summarizer.summarize(p)
            await query.message.reply_text(
                format_offer_prompt(p, summary, self.pipeline.estimate(p)),
                parse_mode=ParseMode.HTML,
                reply_markup=cancel,
            )
            return
        if action == "cancel":
            if self.storage.get_flag(AWAITING_OFFER_FLAG) == f"{platform}:{external_id}":
                self.storage.set_flag(AWAITING_OFFER_FLAG, "")
            await query.answer("Отменено")
            await query.edit_message_reply_markup(reply_markup=None)
            return
        if action == "redraft":
            # Ответ на нажатие — сразу: через несколько секунд Telegram его уже не примет,
            # а LLM думает дольше
            await query.answer("Готовлю новый вариант…")
            async with _typing(query.message):
                redrafted = await self.pipeline.redraft(platform, external_id)
            if redrafted is None:
                await query.message.reply_text("Не удалось собрать новый вариант.")
                return
            await self._send_offer(query.message, redrafted)  # тот же вид: текст копируется
            return
        status = "applied" if action == "applied" else "skipped"
        self.storage.set_status(platform, external_id, status, "кнопка в Telegram")
        await query.answer("Отмечено: откликнулся" if status == "applied" else "Пропущено")
        await query.edit_message_reply_markup(reply_markup=None)
        if status == "applied":  # всплывающая подсказка легко теряется — подтверждаем в чате
            await query.message.reply_text(format_applied(p), parse_mode=ParseMode.HTML)


def build_application(settings: Settings, storage: Storage, pipeline: Pipeline) -> Application:
    if not settings.telegram_token:
        raise ValueError("Не задан TELEGRAM_BOT_TOKEN в .env")
    handlers = BotHandlers(settings, storage, pipeline)

    async def post_init(app: Application) -> None:
        await _register_commands(app, settings)
        await handlers.announce_menu(app.bot)

    app = Application.builder().token(settings.telegram_token).post_init(post_init).build()
    app.bot_data["handlers"] = handlers
    for name, callback in (
        ("start", handlers.start),
        ("menu", handlers.menu),
        ("status", handlers.status),
        ("pause", handlers.pause),
        ("resume", handlers.resume),
        ("filters", handlers.filters_),
        ("stats", handlers.stats),
        ("last", handlers.last),
        ("new", handlers.new),
        ("signup", handlers.signup),
        ("profile", handlers.profile),
        ("forget", handlers.forget),
    ):
        app.add_handler(CommandHandler(name, callback))
    app.add_handler(CallbackQueryHandler(handlers.button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handlers.text))
    app.add_handler(MessageHandler(filters.ALL, handlers.fallback))
    app.add_error_handler(handlers.on_error)

    # минута разбора резюме не задерживает остальные апдейты
    handlers.onboarding.spawn = app.create_task
    if isinstance(pipeline.notifier, TelegramNotifier):
        pipeline.notifier.bind(app.bot)

    if app.job_queue is not None:
        for platform, source in settings.sources.items():
            if source.enabled and any(s.platform == platform for s in pipeline.sources):
                app.job_queue.run_repeating(
                    _make_job(pipeline, platform, source.jitter_sec),
                    interval=source.interval_sec,
                    first=5,
                    name=f"collect:{platform}",
                )
        if settings.digest_enabled:
            first = next_digest_at(
                datetime.now(UTC),
                settings.timezone,
                settings.digest_start_hour,
                settings.digest_interval_min,
            )
            app.job_queue.run_repeating(
                _make_digest_job(handlers, pipeline),
                interval=settings.digest_interval_min * 60,
                first=first,
                name="digest",
            )
    return app


def _make_digest_job(handlers: BotHandlers, pipeline: Pipeline):
    async def job(context: ContextTypes.DEFAULT_TYPE) -> None:
        if handlers.storage.get_flag("paused") == "1":
            return  # новые копятся и придут после /resume
        notifier = pipeline.notifier
        sent = await handlers.send_digest(notifier.send_card)
        log.info("дайджест: прислано %d", sent)

    return job


def _make_job(pipeline: Pipeline, platform: Any, jitter_sec: int):
    async def job(context: ContextTypes.DEFAULT_TYPE) -> None:
        if jitter_sec > 0:
            await asyncio.sleep(random.uniform(0, jitter_sec))
        report = await pipeline.run_once(platform)
        log.info(
            "%s: собрано %d, новых %d, прошло %d, прислано %d, в дайджест %d, ошибок %d",
            platform, report.fetched, report.new, report.passed, report.notified,
            report.queued, len(report.errors),
        )

    return job


async def _register_commands(app: Application, settings: Settings | None = None) -> None:
    """Меню команд обновляется при каждом запуске, чтобы новые команды появлялись сами.
    В публичном режиме посторонние видят команды анкеты, владелец в своём чате — все."""
    owner_commands = [BotCommand(name, text) for name, text in COMMANDS]
    public = settings is not None and settings.public_enabled and settings.owner_id is not None
    try:
        if public:
            await app.bot.set_my_commands([BotCommand(n, t) for n, t in PUBLIC_COMMANDS])
            await app.bot.set_my_commands(
                owner_commands, scope=BotCommandScopeChat(settings.owner_id)
            )
            return
        await app.bot.set_my_commands(owner_commands)
        if settings is not None and settings.owner_id is not None:
            # меню владельца из прошлого публичного запуска иначе осталось бы старым
            await app.bot.delete_my_commands(scope=BotCommandScopeChat(settings.owner_id))
    except Exception as exc:  # меню — удобство, запуск из-за него не падает
        log.warning("не удалось обновить меню команд: %s", type(exc).__name__)
