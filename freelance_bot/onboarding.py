"""Анкета публичного бота (ADR 0011): согласие → ФИО → обращение → резюме → профиль →
направления → площадки → другие города и удалёнка (если выбраны вакансии) → частота.
Состояние — строка users в SQLite (Storage.save_user), поэтому анкета переживает перезапуск.

Шаги (User.step): consent, name, address, address_custom, resume, analyzing, review,
experience_custom, directions, platforms, location, frequency, frequency_custom, done. Частоту
и города можно сменить и после анкеты (кнопки в /start), тогда в data стоит editing. Кнопки анкеты —
callback_data «ob:<действие>[:<значение>]».
Сразу после анкеты присылаются первые заказы и вакансии под неё (first_matches): из уже
собранных и живым поиском SuperJob по профессии и городу из резюме (matching.py, ADR 0014) —
видно, что поиск работает; полный список — кнопка «Заказы по анкете» в /start. Рассылка по
расписанию — следующий этап. Не разработчику IT-направления и биржи фриланса не предлагаются.
"""

from __future__ import annotations

import contextlib
import html
import logging
import re
from collections.abc import Awaitable, Callable, Coroutine, Sequence
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.constants import ChatAction, ParseMode

from freelance_bot.config import Settings
from freelance_bot.matching import (
    OTHER_CITIES,
    REMOTE,
    location_choice,
    mismatch,
    rank,
    resume_profile,
    user_settings,
)
from freelance_bot.models import Project, ScoreResult, User
from freelance_bot.resume import (
    FORMATS_HINT,
    MAX_FILE_BYTES,
    ResumeError,
    ResumeProfile,
    analyze,
    extract_text,
    fetch_link,
    find_link,
    normalize,
)
from freelance_bot.schedule import PRESETS, Schedule, describe, parse_schedule
from freelance_bot.scoring import DIRECTION_LABELS, score_project
from freelance_bot.storage import Storage
from freelance_bot.telegram_ui import (
    MAX_MESSAGE_LEN,
    PLATFORM_NAMES,
    VACANCY_PLATFORMS,
    format_offer_list,
    selection_reason,
)

log = logging.getLogger(__name__)

PREFIX = "ob:"
MAX_ANALYSES_PER_DAY = 5  # каждый разбор — запрос к нейросети с общим лимитом
MIN_PASTED_RESUME_CHARS = 300  # короче — это не резюме, а ответ на вопрос
MAX_FACTS_SHOWN = 6
MAX_JOBS_SHOWN = 4
FIRST_MATCHES_LIMIT = 5  # одно сообщение: показать, что поиск работает, а не завалить
MATCHES_LIMIT = 20  # по кнопке — как /last у владельца
FIRST_MATCHES_WINDOW = timedelta(days=7)  # как /last: заказы обычно открыты несколько дней
# HH в базе — подборки из Telegram владельца (ADR 0009), посторонним их не показываем
PRIVATE_PLATFORMS = frozenset({"hh"})
_NAME_PART_RE = re.compile(r"^[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё'’\-]{0,39}$")
_ADDRESS_RE = re.compile(r"^[A-Za-zА-Яа-яЁё][A-Za-zА-Яа-яЁё'’\-. ]{0,39}$")
_YEARS_INPUT_RE = re.compile(
    r"^\s*(?:(\d{1,2}(?:[.,]\d{1,2})?)\s*(?:год\w*|лет|г\.?)?)?"
    r"\s*(?:(\d{1,2})\s*(?:месяц\w*|мес\.?))?\s*$",
    re.IGNORECASE,
)
MAX_YEARS = 50

BUTTON_AGREE = "✅ Согласен"
BUTTON_DECLINE = "Не согласен"
BUTTON_OWN_ADDRESS = "✏️ Свой вариант"
BUTTON_NO_RESUME = "Без резюме — выберу направления"
BUTTON_OK = "✅ Всё верно"
BUTTON_DIRECTIONS = "✏️ Направления"
BUTTON_REUPLOAD = "📄 Другое резюме"
BUTTON_EXPERIENCE = "✏️ Стаж"
BUTTON_DONE = "Готово ➡️"
BUTTON_ALL = "🌐 Везде"
BUTTON_RESTART = "🔄 Заполнить заново"
BUTTON_FREQUENCY = "⏰ Частота уведомлений"
BUTTON_OWN_SCHEDULE = "✏️ Свой тайминг"
BUTTON_FORGET = "🗑 Удалить мои данные"
BUTTON_MATCHES = "🔎 Заказы по анкете"
BUTTON_LOCATION = "📍 Другие города и удалёнка"
BUTTON_CANCEL = "Отмена"

DECLINED_TEXT = "Хорошо, ничего не сохраняю. Передумаете — /start."
NAME_TEXT = "Как вас зовут? Напишите <b>фамилию, имя и отчество</b> (отчество — если есть)."
NAME_ERROR = "Не похоже на ФИО. Напишите 2–3 слова буквами, например: Иванов Иван Иванович."
ADDRESS_TEXT = "Как к вам обращаться?"
ADDRESS_CUSTOM_TEXT = "Напишите, как к вам обращаться (до 40 символов)."
ADDRESS_ERROR = "Нужно 1–40 символов: буквы, пробел, дефис или точка."
RESUME_TEXT = (
    "{address}, пришлите резюме — я выделю направления, стек и стаж для поиска.\n\n"
    f"• файлом: {FORMATS_HINT}, до {MAX_FILE_BYTES // 1024 // 1024} МБ;\n"
    "• ссылкой на Google Docs, Google Диск или Яндекс Диск;\n"
    "• или вставьте текст резюме сообщением.\n\n"
    "С hh.ru: «Мои резюме» → резюме → «Скачать» → PDF."
)
RESUME_WAIT = "Читаю резюме… Это до минуты."
RESUME_BUSY = "Ещё разбираю резюме — подождите немного."
RESUME_FAILED = "Не получилось разобрать резюме. Попробуйте ещё раз."
RESUME_LIMIT = f"Сегодня уже {MAX_ANALYSES_PER_DAY} разборов резюме. Попробуйте завтра."
RESUME_EXPECTED = "Жду резюме: файл, ссылку или текст. Или кнопка «{button}»."
EXPERIENCE_TEXT = (
    "Сколько у вас коммерческого опыта по профессии? Например: <code>1,5</code> или "
    "<code>1 год 3 месяца</code>. По нему отсеиваю заказы с требованием «опыт от N лет»."
)
EXPERIENCE_ERROR = "Не понял. Напишите число лет (<code>1,5</code>) или «1 год 3 месяца»."
DIRECTIONS_TEXT = "Какие заказы искать? Нажмите, чтобы включить или выключить."
DIRECTIONS_EMPTY = "Выберите хотя бы одно направление"
PLATFORMS_TEXT = "Где искать? Нажмите, чтобы включить или выключить, или «Везде».{hh}"
HH_NOTE = "\n\nHH.ru — вакансии, а не заказы; как его подключить, расскажу отдельно."
PLATFORMS_EMPTY = "Выберите хотя бы одну площадку"
LOCATION_TEXT = (
    "Вакансия подходит по профессии, но она {where} — присылать такие?\n\n"
    "✅ — присылаю, ⬜ — отсекаю. Нажмите, чтобы переключить."
)
LOCATION_SAVED = "📍 Готово, вакансии: {where}."
FREQUENCY_TEXT = (
    "Как часто присылать новые заказы и вакансии? Время — {tz}. Если нового нет, молчу."
)
FREQUENCY_CUSTOM_TEXT = (
    "Напишите своё расписание, время — {tz}. Например:\n"
    "• <code>10:00, 14:00, 19:30</code> — в эти часы;\n"
    "• <code>каждые 30 минут с 8 до 23</code>;\n"
    "• <code>каждые 2 часа</code> — с 9 до 21."
)
FREQUENCY_ERROR = (
    "Не понял расписание. Время — «10:00, 18:00» (до 12 раз в день) или интервал — «каждые "
    "30 минут с 8 до 23» (не чаще раза в 15 минут)."
)
FREQUENCY_SAVED = "⏰ Готово, буду присылать {when} ({tz})."
DONE_TEXT = (
    "Готово, {address}! Анкета сохранена.\n\n{summary}\n\n"
    "/profile — анкета, /start — заполнить заново, /forget — удалить данные."
)
MATCHES_TITLE = "🔎 <b>Заказы и вакансии под вашу анкету</b> — {count}"
MATCHES_HINT = "Поиск работает. Откликнуться — на площадке по ссылке."
MATCHES_NONE = (
    "🔎 Подходящих заказов и вакансий под анкету сейчас нет: ищу по профессии, стажу и "
    "городу из резюме. Можно добавить площадки или прислать другое резюме: /start → "
    "«Заполнить заново»."
)
MATCHES_NONE_CITY = (
    "🔎 В городе {city} на SuperJob сейчас нет вакансий «{profession}» под ваш стаж. "
    "Загляну снова, когда появятся."
)
MATCHES_LATER = "Новые будут приходить {when} ({tz}), когда включим рассылку, — я напишу."
FORGET_TEXT = "Удалить анкету и выжимку из резюме? Это нельзя отменить."
FORGOTTEN_TEXT = "Удалил всё, что знал о вас. Вернуться — /start."
NOTHING_TEXT = "О вас ничего не сохранено. Начать — /start."
PUBLIC_HELP = "Команды: /start — анкета, /profile — ваши данные, /forget — удалить данные."

Reply = Callable[[str, InlineKeyboardMarkup | None], Awaitable[Any]]
Summarize = Callable[[Sequence[Project]], Awaitable[list[str]]]
Search = Callable[[dict[str, Any]], Awaitable[list[Project]]]


def consent_text(settings: Settings) -> str:
    """Что собираем и куда уходит. Нейросеть называется, только если она подключена:
    текст резюме тогда уходит её владельцу (за рубеж)."""
    if settings.groq_api_key:
        llm = "Groq, США"
    elif settings.openai_api_key:
        llm = "OpenAI, США"
    elif settings.anthropic_api_key:
        llm = "Anthropic, США"
    else:
        llm = ""
    sent = f" Текст резюме для разбора отправляется нейросети ({llm})." if llm else ""
    return (
        "👋 Я ищу свежие заказы на фриланс-биржах под ваш профиль.\n\n"
        "Для этого спрошу <b>ФИО</b>, <b>как к вам обращаться</b> и попрошу <b>резюме</b>. "
        "Из резюме выделю направления, стек и стаж; сам файл не храню — только выжимку."
        f"{sent} Удалить всё о себе — команда /forget.\n\n"
        "Согласны на обработку этих данных для подбора заказов?"
    )


def parse_full_name(raw: str) -> str | None:
    words = raw.split()
    if not 2 <= len(words) <= 4 or not all(_NAME_PART_RE.match(w) for w in words):
        return None
    return " ".join(words)


def address_options(full_name: str) -> list[str]:
    """Варианты обращения: «Фамилия Имя Отчество» → Имя, Имя Отчество; из двух слов
    порядок неизвестен — оба слова."""
    words = full_name.split()
    if len(words) >= 3:
        return [words[1], f"{words[1]} {words[2]}"]
    return list(dict.fromkeys([words[1], words[0]])) if len(words) == 2 else words


def parse_years(raw: str) -> float | None:
    """«1,5», «1 год 3 месяца», «8 месяцев», «2 года» → годы с одним знаком."""
    m = _YEARS_INPUT_RE.match(raw)
    if m is None or not (m.group(1) or m.group(2)):
        return None
    years = float((m.group(1) or "0").replace(",", ".")) + int(m.group(2) or 0) / 12
    return round(years, 1) if years <= MAX_YEARS else None


def parse_address(raw: str) -> str | None:
    value = " ".join(raw.split())
    return value if _ADDRESS_RE.match(value) else None


def _button(text: str, action: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text, callback_data=PREFIX + action)


def _markup(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(rows)


def consent_markup() -> InlineKeyboardMarkup:
    return _markup([[_button(BUTTON_AGREE, "agree"), _button(BUTTON_DECLINE, "decline")]])


def address_markup(full_name: str) -> InlineKeyboardMarkup:
    rows = [[_button(o, f"addr:{i}")] for i, o in enumerate(address_options(full_name))]
    return _markup([*rows, [_button(BUTTON_OWN_ADDRESS, "addr_own")]])


def resume_markup() -> InlineKeyboardMarkup:
    return _markup([[_button(BUTTON_NO_RESUME, "noresume")]])


def review_markup() -> InlineKeyboardMarkup:
    return _markup(
        [
            [_button(BUTTON_OK, "ok")],
            [_button(BUTTON_DIRECTIONS, "dirs"), _button(BUTTON_EXPERIENCE, "exp")],
            [_button(BUTTON_REUPLOAD, "reupload")],
        ]
    )


def toggles_markup(
    options: list[tuple[str, str]], chosen: list[str], kind: str, extra: list | None = None
) -> InlineKeyboardMarkup:
    rows = [
        [_button(f"{'✅' if key in chosen else '⬜'} {label}", f"{kind}:{key}")]
        for key, label in options
    ]
    return _markup([*rows, *(extra or []), [_button(BUTTON_DONE, f"{kind}_done")]])


def direction_options(settings: Settings) -> list[tuple[str, str]]:
    return [(name, DIRECTION_LABELS.get(name, name)) for name in settings.directions]


def signup_platforms(data: dict[str, Any], settings: Settings) -> tuple[str, ...]:
    """Площадки анкеты: не разработчику (профессия из резюме без IT-направлений) биржи
    фриланса не нужны — там заказы на разработку; только вакансии, если они включены."""
    live = settings.signup_platforms
    profile = resume_profile(data)
    if profile is not None and profile.profession and not data.get("directions"):
        return tuple(p for p in live if p in VACANCY_PLATFORMS) or live
    return live


def platform_options(
    settings: Settings, data: dict[str, Any] | None = None
) -> list[tuple[str, str]]:
    return [
        (p, PLATFORM_NAMES[p] + (" (вакансии)" if p in VACANCY_PLATFORMS else ""))
        for p in signup_platforms(data or {}, settings)
    ]


def asks_location(data: dict[str, Any], settings: Settings) -> bool:
    """Города и удалёнка — только у вакансий: заказы бирж и так удалённые."""
    live = settings.signup_platforms
    return any(p in VACANCY_PLATFORMS and p in live for p in data.get("platforms") or [])


def location_options(data: dict[str, Any]) -> list[tuple[str, str]]:
    """Без города в резюме другие города не отсекаются — спрашивать о них незачем."""
    resume = resume_profile(data)
    options = [(REMOTE, "Удалённая работа")]
    if resume and resume.city:
        options.insert(0, (OTHER_CITIES, f"Другие города, не только {resume.city}"))
    return options


def describe_location(data: dict[str, Any]) -> str:
    resume = resume_profile(data)
    chosen = location_choice(data)
    city = resume.city if resume and resume.city and OTHER_CITIES not in chosen else ""
    place = city or "любой город"
    return f"{place} и удалённые" if REMOTE in chosen else f"{place}, без удалённых"


def _years(value: float) -> str:
    if not value:
        return "не указан"
    whole = value == int(value)
    shown = f"{int(value)}" if whole else f"{value:.1f}".replace(".", ",")
    if not whole:
        return f"{shown} года"
    n = int(value)
    word = (
        "лет"
        if 11 <= n % 100 <= 14
        else {1: "год", 2: "года", 3: "года", 4: "года"}.get(n % 10, "лет")
    )
    return f"{shown} {word}"


def format_profile(data: dict[str, Any], settings: Settings) -> str:
    """Выжимка анкеты для показа (HTML; всё от пользователя экранировано)."""
    e = html.escape
    lines = []
    if data.get("full_name"):
        lines.append(f"👤 {e(data['full_name'])} — «{e(data.get('address', ''))}»")
    directions = data.get("directions") or []
    labels = ", ".join(DIRECTION_LABELS.get(d, d) for d in directions)
    resume = resume_profile(data)
    if not labels and resume and resume.profession:
        lines.append(f"🎯 Ищу: вакансии «{e(resume.profession)}»")
    else:
        lines.append(f"🎯 Направления: {e(labels) or 'не выбраны'}")
    profile = data.get("profile")
    if profile:
        p = ResumeProfile.from_dict(profile)
        if p.profession:
            lines.append(f"💼 Профессия: {e(p.profession)}")
        if p.city:
            lines.append(f"📍 Город: {e(p.city)}")
        lines.append(f"⏳ Стаж: {_years(p.experience_years)}")
        if p.jobs:
            jobs = "\n".join(f"  – {e(job)}" for job in p.jobs[:MAX_JOBS_SHOWN])
            lines.append(f"🏢 Места работы:\n{jobs}")
        if p.skills:
            lines.append(f"🧩 Навыки: {e(', '.join(p.skills))}")
        if p.education:
            lines.append(f"🎓 Образование: {e('; '.join(p.education))}")
        if p.keywords:
            lines.append(f"🔑 Ключевые слова: {e(', '.join(p.keywords))}")
        if p.facts:
            facts = "\n".join(f"  – {e(f.text)}" for f in p.facts[:MAX_FACTS_SHOWN])
            more = len(p.facts) - MAX_FACTS_SHOWN
            tail = f"\n  …и ещё {more}" if more > 0 else ""
            lines.append(f"📌 Опыт из резюме:\n{facts}{tail}")
    platforms = data.get("platforms")
    if platforms is not None:
        live = settings.signup_platforms  # выбранная, но выключенная площадка не ищется
        names = ", ".join(PLATFORM_NAMES.get(p, p) for p in platforms if p in live)
        lines.append(f"🌐 Площадки: {names or 'не выбраны'}")
        if data.get("location") is not None and asks_location(data, settings):
            lines.append(f"📍 Вакансии: {e(describe_location(data))}")
    if data.get("schedule"):
        when = describe(Schedule.from_dict(data["schedule"]))
        lines.append(f"⏰ Присылать: {when} ({tz_label(settings.timezone)})")
    return "\n".join(lines)


def first_matches(
    data: dict[str, Any],
    settings: Settings,
    storage: Storage,
    now: datetime,
    limit: int = FIRST_MATCHES_LIMIT,
    *,
    include_private: bool = False,
    tg_id: int | None = None,
    extra: Sequence[Project] = (),
) -> list[tuple[Project, ScoreResult]]:
    """Подходящие анкете заказы из собранных за FIRST_MATCHES_WINDOW и вакансии живого
    поиска `extra`: свежие первыми. Балл — на момент публикации, как в /last, чтобы возраст
    не отсекал открытый заказ; что резюме не выполняет (стаж, город, профессия) —
    отсекается (matching.mismatch). Статусы в базе — владельца (откликнулся, пропустил),
    поэтому не учитываются. `include_private` — владельцу его же HH."""
    own = user_settings(data, settings)
    if tg_id is not None:  # свои отметки «не подходит» (ADR 0013)
        own = replace(own, lessons=tuple(storage.lessons(tg_id)))
    platforms = set(data.get("platforms") or []) & set(settings.signup_platforms)
    if not include_private:
        platforms -= PRIVATE_PLATFORMS
    candidates: dict[tuple[str, str], Project] = {}
    for p in [*storage.recent_projects(now - FIRST_MATCHES_WINDOW), *extra]:
        if p.platform in platforms:
            candidates.setdefault((p.platform, p.external_id), p)
    items = []
    for p in candidates.values():
        result = score_project(p, own, p.published_at)
        if result.passed and mismatch(p, data, own) is None:
            items.append((p, result))
    return rank(items, data)[:limit]


def matches_pages(
    items: list[tuple[Project, ScoreResult]],
    summaries: list[str] | None,
    tz: str,
    reserve: int = 0,
) -> list[str]:
    """Список по сообщениям: в каждом сколько влезет (минус `reserve` под приписку),
    нумерация сквозная, заголовок — в первом."""
    title = MATCHES_TITLE.format(count=len(items))

    def page(lo: int, hi: int) -> str:
        return format_offer_list(
            [(p, r.score) for p, r in items[lo:hi]],
            tz,
            summaries[lo:hi] if summaries else None,
            start=lo + 1,
            title=title,
            reasons=[selection_reason(r) for _, r in items[lo:hi]],
            hint=MATCHES_HINT,
        )

    pages, lo = [], 0
    while lo < len(items):
        hi = lo + 1
        while hi < len(items) and len(page(lo, hi + 1)) <= MAX_MESSAGE_LEN - reserve:
            hi += 1
        pages.append(page(lo, hi))
        lo = hi
    return pages


def tz_label(tz: str) -> str:
    return "МСК" if tz == "Europe/Moscow" else tz


def frequency_markup() -> InlineKeyboardMarkup:
    rows = [[_button(label, f"freq:{key}")] for key, (label, _) in PRESETS.items()]
    return _markup([*rows, [_button(BUTTON_OWN_SCHEDULE, "freq_own")]])


class Onboarding:
    """Хэндлеры анкеты. Тяжёлую работу (скачать и разобрать резюме) запускает `spawn` —
    в боте это Application.create_task, чтобы минута разбора не задерживала остальных;
    без него (тесты) — выполняется сразу."""

    def __init__(
        self,
        settings: Settings,
        storage: Storage,
        *,
        llm_client: Any = None,
        spawn: Callable[[Coroutine[Any, Any, Any]], Any] | None = None,
        summarize: Summarize | None = None,
        search: Search | None = None,
    ) -> None:
        self.settings = settings
        self.storage = storage
        self.llm_client = llm_client
        self.spawn = spawn
        self.summarize = summarize  # суть заказа (📝) с кэшем; нет — список без неё
        self.search = search  # живой поиск вакансий под анкету (matching.VacancySearch)

    def in_progress(self, tg_id: int) -> bool:
        """Анкета начата и не закончена — сообщения пользователя идут в неё."""
        user = self.storage.get_user(tg_id)
        return user is not None and user.step != "done"

    # ── команды ──────────────────────────────────────────────────────────────

    async def start(self, update: Any, context: Any = None, *, restart: bool = False) -> None:
        tg_id = update.effective_user.id
        user = self.storage.get_user(tg_id)
        reply = _replier(update.effective_message)
        if user is None or user.consent_at is None:
            self.storage.save_user(tg_id, "consent", {})
            await reply(consent_text(self.settings), consent_markup())
        elif user.step == "done" and not restart:
            rows = [[_button(BUTTON_MATCHES, "matches")]]
            if asks_location(user.data, self.settings):
                rows.append([_button(BUTTON_LOCATION, "loc_edit")])
            rows += [
                [_button(BUTTON_FREQUENCY, "freq_edit")],
                [_button(BUTTON_RESTART, "restart")],
                [_button(BUTTON_FORGET, "forget")],
            ]
            await reply(format_profile(user.data, self.settings), _markup(rows))
        elif restart or user.step in ("consent", "done"):
            self.storage.save_user(tg_id, "name", {}, completed=False)
            await reply(NAME_TEXT, None)
        else:
            await self._prompt(user, reply)

    async def profile(self, update: Any, context: Any = None) -> None:
        user = self.storage.get_user(update.effective_user.id)
        reply = _replier(update.effective_message)
        if user is None or user.consent_at is None:
            return await reply(NOTHING_TEXT, None)
        if user.step != "done":
            return await self._prompt(user, reply)
        await reply(format_profile(user.data, self.settings), None)

    async def forget(self, update: Any, context: Any = None) -> None:
        reply = _replier(update.effective_message)
        if self.storage.get_user(update.effective_user.id) is None:
            return await reply(NOTHING_TEXT, None)
        await reply(
            FORGET_TEXT,
            _markup([[_button(BUTTON_FORGET, "forget_yes"), _button(BUTTON_CANCEL, "forget_no")]]),
        )

    # ── сообщения ────────────────────────────────────────────────────────────

    async def text(self, update: Any, context: Any = None) -> None:
        message = update.effective_message
        reply = _replier(message)
        user = self.storage.get_user(update.effective_user.id)
        raw = message.text or ""
        if user is None:
            return await self.start(update, context)
        if user.step == "name":
            full_name = parse_full_name(raw)
            if full_name is None:
                return await reply(NAME_ERROR, None)
            data = {**user.data, "full_name": full_name}
            user = self.storage.save_user(user.tg_id, "address", data)
            return await self._prompt(user, reply)
        if user.step == "address_custom":
            address = parse_address(raw)
            if address is None:
                return await reply(ADDRESS_ERROR, None)
            return await self._set_address(user, address, reply)
        if user.step == "experience_custom":
            years = parse_years(raw)
            if years is None:
                return await reply(EXPERIENCE_ERROR, None)
            profile = {**(user.data.get("profile") or {}), "experience_years": years}
            user = self.storage.save_user(user.tg_id, "review", {**user.data, "profile": profile})
            return await self._prompt(user, reply)
        if user.step == "frequency_custom":
            schedule = parse_schedule(raw)
            if schedule is None:
                return await reply(FREQUENCY_ERROR, None)
            return await self._set_schedule(user, schedule, reply)
        if user.step == "resume":
            link = find_link(raw)
            if link is not None:
                return await self._take_resume(user, message, lambda: fetch_link(link))
            if len(raw) >= MIN_PASTED_RESUME_CHARS:
                return await self._take_resume(user, message, lambda: _pasted(raw))
            return await reply(RESUME_EXPECTED.format(button=BUTTON_NO_RESUME), resume_markup())
        if user.step == "analyzing":
            return await reply(RESUME_BUSY, None)
        if user.step == "done":
            return await reply(PUBLIC_HELP, None)
        await self._prompt(user, reply)  # ждём кнопку — напоминаем вопрос

    async def document(self, update: Any, context: Any = None) -> None:
        message = update.effective_message
        reply = _replier(message)
        user = self.storage.get_user(update.effective_user.id)
        document = getattr(message, "document", None)
        if user is None:
            return await self.start(update, context)
        if user.step == "analyzing":
            return await reply(RESUME_BUSY, None)
        if user.step != "resume" or document is None:
            return (
                await self._prompt(user, reply)
                if user.step != "done"
                else await reply(PUBLIC_HELP, None)
            )
        if (document.file_size or 0) > MAX_FILE_BYTES:
            return await reply(f"Файл больше {MAX_FILE_BYTES // 1024 // 1024} МБ.", None)

        async def load() -> str:
            file = await document.get_file()
            data = bytes(await file.download_as_bytearray())
            return extract_text(data, document.file_name or "")

        await self._take_resume(user, message, load)

    # ── кнопки ───────────────────────────────────────────────────────────────

    async def button(self, update: Any, context: Any = None) -> None:
        query = update.callback_query
        action, _, value = (query.data or "")[len(PREFIX) :].partition(":")
        reply = _replier(query.message)
        tg_id = update.effective_user.id
        user = self.storage.get_user(tg_id)
        await query.answer()
        if action == "forget_yes":
            await query.edit_message_reply_markup(reply_markup=None)
            deleted = self.storage.delete_user(tg_id)
            return await reply(FORGOTTEN_TEXT if deleted else NOTHING_TEXT, None)
        if action == "forget_no":
            return await query.edit_message_reply_markup(reply_markup=None)
        if user is None:
            return await reply(NOTHING_TEXT, None)
        if action == "forget":
            return await self.forget(update, context)
        if action == "restart":
            await query.edit_message_reply_markup(reply_markup=None)
            return await self.start(update, context, restart=True)
        handler = {
            ("consent", "agree"): self._agree,
            ("consent", "decline"): self._decline,
            ("address", "addr"): self._pick_address,
            ("address", "addr_own"): self._own_address,
            ("resume", "noresume"): self._no_resume,
            ("review", "ok"): self._review_ok,
            ("review", "dirs"): self._open_directions,
            ("review", "reupload"): self._reupload,
            ("review", "exp"): self._own_experience,
            ("directions", "dir"): self._toggle_direction,
            ("directions", "dir_done"): self._directions_done,
            ("platforms", "plat"): self._toggle_platform,
            ("platforms", "plat_all"): self._all_platforms,
            ("platforms", "plat_done"): self._platforms_done,
            ("location", "loc"): self._toggle_location,
            ("location", "loc_done"): self._location_done,
            ("frequency", "freq"): self._pick_frequency,
            ("frequency", "freq_own"): self._own_frequency,
            ("done", "freq_edit"): self._edit_frequency,
            ("done", "loc_edit"): self._edit_location,
            ("done", "matches"): self._matches,
        }.get((user.step, action))
        if handler is None:  # старая кнопка с прошлого шага
            return await self._prompt(user, reply)
        await handler(user, value, query, reply)

    async def _agree(self, user: User, value: str, query: Any, reply: Reply) -> None:
        await query.edit_message_reply_markup(reply_markup=None)
        self.storage.save_user(user.tg_id, "name", {}, consent=True)
        await reply(NAME_TEXT, None)

    async def _decline(self, user: User, value: str, query: Any, reply: Reply) -> None:
        await query.edit_message_reply_markup(reply_markup=None)
        self.storage.delete_user(user.tg_id)
        await reply(DECLINED_TEXT, None)

    async def _pick_address(self, user: User, value: str, query: Any, reply: Reply) -> None:
        options = address_options(user.data.get("full_name", ""))
        if not value.isdigit() or int(value) >= len(options):
            return await self._prompt(user, reply)
        await query.edit_message_reply_markup(reply_markup=None)
        await self._set_address(user, options[int(value)], reply)

    async def _own_address(self, user: User, value: str, query: Any, reply: Reply) -> None:
        await query.edit_message_reply_markup(reply_markup=None)
        self.storage.save_user(user.tg_id, "address_custom", user.data)
        await reply(ADDRESS_CUSTOM_TEXT, None)

    async def _set_address(self, user: User, address: str, reply: Reply) -> None:
        user = self.storage.save_user(user.tg_id, "resume", {**user.data, "address": address})
        await self._prompt(user, reply)

    async def _no_resume(self, user: User, value: str, query: Any, reply: Reply) -> None:
        await query.edit_message_reply_markup(reply_markup=None)
        data = {**user.data, "profile": None, "directions": []}
        user = self.storage.save_user(user.tg_id, "directions", data)
        await self._prompt(user, reply)

    async def _review_ok(self, user: User, value: str, query: Any, reply: Reply) -> None:
        await query.edit_message_reply_markup(reply_markup=None)
        resume = resume_profile(user.data)
        if not user.data.get("directions") and not (resume and resume.profession):
            user = self.storage.save_user(user.tg_id, "directions", user.data)
        else:
            user = self._to_platforms(user)
        await self._prompt(user, reply)

    async def _open_directions(self, user: User, value: str, query: Any, reply: Reply) -> None:
        await query.edit_message_reply_markup(reply_markup=None)
        user = self.storage.save_user(user.tg_id, "directions", user.data)
        await self._prompt(user, reply)

    async def _own_experience(self, user: User, value: str, query: Any, reply: Reply) -> None:
        await query.edit_message_reply_markup(reply_markup=None)
        user = self.storage.save_user(user.tg_id, "experience_custom", user.data)
        await self._prompt(user, reply)

    async def _reupload(self, user: User, value: str, query: Any, reply: Reply) -> None:
        await query.edit_message_reply_markup(reply_markup=None)
        user = self.storage.save_user(user.tg_id, "resume", user.data)
        await self._prompt(user, reply)

    async def _toggle_direction(self, user: User, value: str, query: Any, reply: Reply) -> None:
        if value not in self.settings.directions:
            return
        chosen = _toggled(user.data.get("directions") or [], value)
        user = self.storage.save_user(user.tg_id, "directions", {**user.data, "directions": chosen})
        await query.edit_message_reply_markup(reply_markup=self._directions_markup(user))

    async def _directions_done(self, user: User, value: str, query: Any, reply: Reply) -> None:
        resume = resume_profile(user.data)
        if not user.data.get("directions") and not (resume and resume.profession):
            return await query.answer(DIRECTIONS_EMPTY, show_alert=True)
        await query.edit_message_reply_markup(reply_markup=None)
        await self._prompt(self._to_platforms(user), reply)

    def _to_platforms(self, user: User) -> User:
        offered = signup_platforms(user.data, self.settings)
        platforms = [p for p in user.data.get("platforms") or [] if p in offered]
        if user.data.get("platforms") is None or not platforms:  # впервые — всё включено,
            platforms = list(offered)  # выключить проще, чем искать
        data = {**user.data, "platforms": platforms}
        return self.storage.save_user(user.tg_id, "platforms", data)

    async def _toggle_platform(self, user: User, value: str, query: Any, reply: Reply) -> None:
        if value not in signup_platforms(user.data, self.settings):
            return
        chosen = _toggled(user.data.get("platforms") or [], value)
        user = self.storage.save_user(user.tg_id, "platforms", {**user.data, "platforms": chosen})
        await query.edit_message_reply_markup(reply_markup=self._platforms_markup(user))

    async def _all_platforms(self, user: User, value: str, query: Any, reply: Reply) -> None:
        data = {**user.data, "platforms": list(signup_platforms(user.data, self.settings))}
        await self._platforms_done(self.storage.save_user(user.tg_id, "platforms", data), value,
                                   query, reply)  # fmt: skip

    async def _platforms_done(self, user: User, value: str, query: Any, reply: Reply) -> None:
        live = self.settings.signup_platforms
        if not any(p in live for p in user.data.get("platforms") or []):
            return await query.answer(PLATFORMS_EMPTY, show_alert=True)
        await query.edit_message_reply_markup(reply_markup=None)
        if asks_location(user.data, self.settings):
            data = {**user.data, "location": list(location_choice(user.data))}
            user = self.storage.save_user(user.tg_id, "location", data)
        else:
            user = self.storage.save_user(user.tg_id, "frequency", user.data)
        await self._prompt(user, reply)

    async def _toggle_location(self, user: User, value: str, query: Any, reply: Reply) -> None:
        if value not in dict(location_options(user.data)):
            return
        chosen = _toggled(list(location_choice(user.data)), value)
        user = self.storage.save_user(user.tg_id, "location", {**user.data, "location": chosen})
        await query.edit_message_reply_markup(reply_markup=self._location_markup(user))

    async def _location_done(self, user: User, value: str, query: Any, reply: Reply) -> None:
        await query.edit_message_reply_markup(reply_markup=None)
        data = dict(user.data)
        if data.pop("editing", False):
            self.storage.save_user(user.tg_id, "done", data)
            return await reply(
                LOCATION_SAVED.format(where=html.escape(describe_location(data))), None
            )
        user = self.storage.save_user(user.tg_id, "frequency", data)
        await self._prompt(user, reply)

    async def _edit_location(self, user: User, value: str, query: Any, reply: Reply) -> None:
        """Как смена частоты: анкета остаётся пройденной."""
        await query.edit_message_reply_markup(reply_markup=None)
        data = {**user.data, "location": list(location_choice(user.data)), "editing": True}
        user = self.storage.save_user(user.tg_id, "location", data)
        await self._prompt(user, reply)

    async def _pick_frequency(self, user: User, value: str, query: Any, reply: Reply) -> None:
        if value not in PRESETS:
            return await self._prompt(user, reply)
        await query.edit_message_reply_markup(reply_markup=None)
        await self._set_schedule(user, PRESETS[value][1], reply)

    async def _own_frequency(self, user: User, value: str, query: Any, reply: Reply) -> None:
        await query.edit_message_reply_markup(reply_markup=None)
        user = self.storage.save_user(user.tg_id, "frequency_custom", user.data)
        await self._prompt(user, reply)

    async def _edit_frequency(self, user: User, value: str, query: Any, reply: Reply) -> None:
        """Сменить частоту после анкеты: анкета остаётся пройденной (рассылка не прерывается)."""
        await query.edit_message_reply_markup(reply_markup=None)
        user = self.storage.save_user(user.tg_id, "frequency", {**user.data, "editing": True})
        await self._prompt(user, reply)

    async def _matches(self, user: User, value: str, query: Any, reply: Reply) -> None:
        """Кнопка под анкетой оставлена: список можно запросить ещё раз позже."""
        await self._send_matches(user, reply, MATCHES_LIMIT)

    async def _set_schedule(self, user: User, schedule: Schedule, reply: Reply) -> None:
        data = {**user.data, "schedule": schedule.to_dict()}
        if data.pop("editing", False):
            self.storage.save_user(user.tg_id, "done", data)
            when = describe(schedule)
            return await reply(
                FREQUENCY_SAVED.format(when=when, tz=tz_label(self.settings.timezone)), None
            )
        user = self.storage.save_user(user.tg_id, "done", data, completed=True)
        log.info("анкета пройдена: площадок %d", len(user.data["platforms"]))
        await reply(
            DONE_TEXT.format(
                address=html.escape(user.data.get("address", "")),
                summary=format_profile(user.data, self.settings),
            ),
            None,
        )
        await self._send_matches(user, reply)

    async def _send_matches(
        self, user: User, reply: Reply, limit: int = FIRST_MATCHES_LIMIT
    ) -> None:
        """Заказы под анкету. Сбой здесь не должен портить сохранённую анкету."""
        tz = self.settings.timezone
        later = MATCHES_LATER.format(
            when=describe(Schedule.from_dict(user.data["schedule"])), tz=tz_label(tz)
        )
        live: list[Project] = []
        if self.search is not None:
            try:
                live = await self.search(user.data)
            except Exception as exc:  # SuperJob недоступен — остаются собранные
                log.warning("вакансии под анкету не найдены: %s", type(exc).__name__)
        try:
            items = first_matches(
                user.data, self.settings, self.storage, datetime.now(UTC), limit,
                include_private=user.tg_id == self.settings.owner_id, tg_id=user.tg_id,
                extra=live,
            )  # fmt: skip
            if not items:
                return await reply(f"{self._none_text(user)}\n\n{later}", None)
            projects = [p for p, _ in items]
            summaries = await self.summarize(projects) if self.summarize else None
            pages = matches_pages(items, summaries, tz, reserve=len(later) + 2)
        except Exception as exc:
            log.error("заказы под анкету не собраны: %s", type(exc).__name__)
            return
        log.info("заказы под анкету: %d", len(items))
        pages[-1] = f"{pages[-1]}\n\n{later}"
        for text in pages:
            await reply(text, None)

    def _none_text(self, user: User) -> str:
        """Пусто: не разработчику — что вакансий нет в его городе (SuperJob в небольших
        городах почти пуст), а не совет добавить направления."""
        resume = resume_profile(user.data)
        if resume and resume.profession and resume.city and not user.data.get("directions"):
            return MATCHES_NONE_CITY.format(
                city=html.escape(resume.city), profession=html.escape(resume.profession)
            )
        return MATCHES_NONE

    # ── резюме ───────────────────────────────────────────────────────────────

    async def _take_resume(
        self, user: User, message: Any, load: Callable[[], Awaitable[str]]
    ) -> None:
        reply = _replier(message)
        today = date.today().isoformat()
        count = user.data.get("analyses", {}).get(today, 0)
        if count >= MAX_ANALYSES_PER_DAY:
            return await reply(RESUME_LIMIT, None)
        data = {**user.data, "analyses": {today: count + 1}}
        user = self.storage.save_user(user.tg_id, "analyzing", data)
        await reply(RESUME_WAIT, None)
        work = self._analyze(user, message, load)
        if self.spawn is None:
            await work
        else:
            self.spawn(work)

    async def _analyze(self, user: User, message: Any, load: Callable[[], Awaitable[str]]) -> None:
        reply = _replier(message)
        with contextlib.suppress(Exception):  # индикатор — удобство, разбор важнее
            await message.reply_chat_action(ChatAction.TYPING)
        try:
            text = await load()
            profile = await analyze(text, self.settings, self.llm_client)
        except ResumeError as exc:
            self.storage.save_user(user.tg_id, "resume", user.data)
            return await reply(f"{html.escape(str(exc))}", resume_markup())
        except Exception as exc:  # анкета не должна застрять в «analyzing»
            log.error("резюме не разобрано: %s", type(exc).__name__)
            self.storage.save_user(user.tg_id, "resume", user.data)
            return await reply(RESUME_FAILED, resume_markup())
        data = {**user.data, "profile": profile.to_dict(), "directions": profile.directions}
        user = self.storage.save_user(user.tg_id, "review", data)
        log.info(
            "резюме разобрано (%s): направлений %d, слов %d, фактов %d",
            profile.generator, len(profile.directions), len(profile.keywords), len(profile.facts),
        )  # fmt: skip
        await self._prompt(user, reply)

    # ── вопрос текущего шага ─────────────────────────────────────────────────

    async def _prompt(self, user: User, reply: Reply) -> None:
        step, data = user.step, user.data
        if step == "consent":
            await reply(consent_text(self.settings), consent_markup())
        elif step == "name":
            await reply(NAME_TEXT, None)
        elif step == "address":
            await reply(ADDRESS_TEXT, address_markup(data.get("full_name", "")))
        elif step == "address_custom":
            await reply(ADDRESS_CUSTOM_TEXT, None)
        elif step == "resume":
            address = html.escape(data.get("address", ""))
            await reply(RESUME_TEXT.format(address=address), resume_markup())
        elif step == "analyzing":
            await reply(RESUME_BUSY, None)
        elif step == "experience_custom":
            await reply(EXPERIENCE_TEXT, None)
        elif step == "review":
            await reply(
                "Вот что я понял из резюме:\n\n"
                f"{format_profile({**data, 'full_name': None}, self.settings)}\n\nВсё верно?",
                review_markup(),
            )
        elif step == "directions":
            await reply(DIRECTIONS_TEXT, self._directions_markup(user))
        elif step == "platforms":
            offered = signup_platforms(data, self.settings)
            vacancies = any(p in VACANCY_PLATFORMS for p in offered)
            hh = HH_NOTE if vacancies else ""
            await reply(PLATFORMS_TEXT.format(hh=hh), self._platforms_markup(user))
        elif step == "location":
            resume = resume_profile(data)
            where = (
                f"не в городе {html.escape(resume.city)} или удалённая"
                if resume and resume.city
                else "удалённая"
            )
            await reply(LOCATION_TEXT.format(where=where), self._location_markup(user))
        elif step == "frequency":
            tz = tz_label(self.settings.timezone)
            await reply(FREQUENCY_TEXT.format(tz=tz), frequency_markup())
        elif step == "frequency_custom":
            await reply(FREQUENCY_CUSTOM_TEXT.format(tz=tz_label(self.settings.timezone)), None)
        else:
            await reply(format_profile(data, self.settings), None)

    def _directions_markup(self, user: User) -> InlineKeyboardMarkup:
        return toggles_markup(
            direction_options(self.settings), user.data.get("directions") or [], "dir"
        )

    def _location_markup(self, user: User) -> InlineKeyboardMarkup:
        return toggles_markup(
            location_options(user.data), list(location_choice(user.data)), "loc"
        )

    def _platforms_markup(self, user: User) -> InlineKeyboardMarkup:
        return toggles_markup(
            platform_options(self.settings, user.data),
            user.data.get("platforms") or [],
            "plat",
            [[_button(BUTTON_ALL, "plat_all")]],
        )


async def _pasted(raw: str) -> str:
    text = normalize(raw)
    if len(text) < MIN_PASTED_RESUME_CHARS:
        raise ResumeError("Слишком короткий текст для резюме.")
    return text


def _toggled(chosen: list[str], value: str) -> list[str]:
    return [c for c in chosen if c != value] if value in chosen else [*chosen, value]


def _replier(message: Any) -> Reply:
    async def reply(text: str, markup: InlineKeyboardMarkup | None) -> Any:
        return await message.reply_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    return reply
