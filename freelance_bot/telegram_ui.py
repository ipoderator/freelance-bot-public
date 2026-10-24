"""Форматирование сообщений бота: карточки, черновики, /status, /filters, /stats.

Только строки (HTML parse_mode, всё экранировано) — без сети и без telegram-библиотеки.
"""

from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from freelance_bot.config import Settings
from freelance_bot.models import PLATFORMS, Draft, ModeDecision, Platform, Project, ScoreResult
from freelance_bot.pricing import Estimate

PLATFORM_NAMES: dict[str, str] = {"fl": "FL.ru", "kwork": "Kwork"}
KWORK_TERMS_URL = "https://kwork.ru/terms_of_service"
KWORK_WARNING = (
    "⚠ Kwork включён на ваш риск: пользовательское соглашение Kwork запрещает "
    f"автоматический сбор данных ({KWORK_TERMS_URL})."
)

FL_FREE_MARK = "🆓 бесплатный отклик (нужна верификация FL)"
FL_PAID_MARK = "💰 отклик платный"

BUTTON_APPLIED = "✅ Откликнулся"
BUTTON_SKIP = "⏭ Пропустить"
BUTTON_REDRAFT = "🔄 Другой вариант"
BUTTON_OFFER = "💬 Цена и срок"
BUTTON_CANCEL = "✖ Отмена"
OFFER_EXAMPLE = "15000, 5"
SUMMARY_MARK = "📝"
LIST_TITLE = "📋 <b>Подходящие заказы</b> — свежие сверху"
DIGEST_TITLE = "🆕 <b>Новые заказы</b> — {count}"
PICK_HINT = "Выберите кнопкой или пришлите номер — пришлю готовый отклик."

# Постоянное меню под полем ввода (ReplyKeyboardMarkup): нажатие присылает этот текст
MENU_NEW = "🆕 Новые"
MENU_ALL = "📋 Все заказы"
MENU_STATS = "📊 Статистика"
MENU_STATUS = "⚙️ Статус"
MENU_ROWS: tuple[tuple[str, ...], ...] = ((MENU_NEW, MENU_ALL), (MENU_STATS, MENU_STATUS))
# Что площадка требует от отклика — подсказки под готовым текстом (docs/platforms.md, БЗ 62056)
PLATFORM_TIPS: dict[str, tuple[str, ...]] = {
    "fl": (
        "срок на FL — в календарных днях, цену указывать необязательно",
        "можно приложить до трёх работ под эту задачу",
    ),
    "kwork": (
        "без контактов и ссылок в тексте: за обмен контактами Kwork обнуляет коннекты",
        "отклик тратит 1 коннект",
    ),
}
ESTIMATE_MARK = "🧮"

_STATS_LABELS: tuple[tuple[str, str, str], ...] = (
    ("collected", "🔎", "собрано"),
    ("filtered_out", "🚫", "отсечено"),
    ("notified", "📨", "прислано"),
    ("drafted", "✍️", "черновиков"),
    ("applied", "✅", "откликнулся"),
    ("skipped", "⏭", "пропущено"),
    ("sent", "🤖", "отправлено авто"),
)


def esc(value: object) -> str:
    return html.escape(str(value), quote=False)


def fmt_rub(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def fmt_time(moment: datetime, tz: str) -> str:
    local = moment.astimezone(ZoneInfo(tz))
    zone = "МСК" if tz == "Europe/Moscow" else tz
    return f"{local:%d.%m.%Y %H:%M} {zone}"


def fmt_when(moment: datetime, tz: str, now: datetime | None = None) -> str:
    """«сегодня 14:05», «вчера 09:30» относительно `now`, иначе полная дата."""
    if now is not None:
        zone = ZoneInfo(tz)
        local, today = moment.astimezone(zone), now.astimezone(zone).date()
        if local.date() == today:
            return f"сегодня {local:%H:%M}"
        if local.date() == today - timedelta(days=1):
            return f"вчера {local:%H:%M}"
    return fmt_time(moment, tz)


def _budget(p: Project) -> str:
    lo, hi = p.budget_min, p.budget_max
    if lo is not None and hi is not None and lo != hi:
        return f"{fmt_rub(lo)}–{fmt_rub(hi)} ₽"
    value = hi if hi is not None else lo
    return "не указан" if value is None else f"{fmt_rub(value)} ₽"


def _days(days: int) -> str:
    if days % 10 == 1 and days % 100 != 11:
        return f"{days} день"
    if 2 <= days % 10 <= 4 and not 12 <= days % 100 <= 14:
        return f"{days} дня"
    return f"{days} дней"


def format_estimate(e: Estimate) -> str:
    value = f"{fmt_rub(e.price_rub)} ₽ · {_days(e.days)}"
    return f"{ESTIMATE_MARK} Оценка: <b>{value}</b> — {esc(e.basis)}"


def format_card(
    p: Project,
    r: ScoreResult,
    d: ModeDecision,
    tz: str,
    summary: str = "",
    estimate: Estimate | None = None,
) -> str:
    offers = "нет данных" if p.offers_count is None else str(p.offers_count)
    lines = [f"🆕 <b>{esc(p.title)}</b>"]
    if summary:
        lines.append(f"{SUMMARY_MARK} {esc(summary)}")
    site = esc(PLATFORM_NAMES.get(p.platform, p.platform))
    lines.append(f"🌐 {site} · 🕐 {esc(fmt_time(p.published_at, tz))}")
    lines.append(f"💵 бюджет: {esc(_budget(p))} · откликов: {offers}")
    if p.platform == "fl":
        # FL: «для всех» — 0 ₽ после верификации, остальное — за купленные отклики
        lines.append(FL_FREE_MARK if p.extra.get("for_all") else FL_PAID_MARK)
    if estimate is not None:
        lines.append(format_estimate(estimate))
    lines.append(f"⭐ Score: <b>{r.score}</b>/100")
    if r.reasons:
        lines.append("<b>Почему подходит:</b>")
        lines.extend(f"• {esc(reason)}" for reason in r.reasons)
    if d.requested != d.effective and d.reasons:
        lines.append(f"{esc(d.requested)} → {esc(d.effective)}: {esc('; '.join(d.reasons))}")
    lines.append(f'🔗 <a href="{html.escape(p.url, quote=True)}">Открыть проект</a>')
    return "\n".join(lines)


def format_draft(d: Draft, estimated: bool = False) -> str:
    notes = []
    if estimated:
        notes.append(f"цена и срок — оценка бота, свои: «{BUTTON_OFFER}»")
    if d.price_placeholder:
        notes.append("впиши цену вместо [ЦЕНА]")
    if d.deadline_placeholder:
        notes.append("впиши срок вместо [СРОК]")
    head = "<b>Черновик отклика</b>"
    if notes:
        head += f" ({esc(', '.join(notes))})"
    return f"{head}\n\n{esc(d.text)}"


def card_keyboard(platform: Platform | str, external_id: str) -> list[list[tuple[str, str]]]:
    return [
        [
            (BUTTON_APPLIED, f"applied:{platform}:{external_id}"),
            (BUTTON_SKIP, f"skip:{platform}:{external_id}"),
        ],
        [
            (BUTTON_OFFER, f"offer:{platform}:{external_id}"),
            (BUTTON_REDRAFT, f"redraft:{platform}:{external_id}"),
        ],
    ]


def format_offer_list(
    items: Sequence[tuple[Project, int | None]],
    tz: str,
    summaries: Sequence[str] | None = None,
    estimates: Sequence[Estimate] | None = None,
    start: int = 1,
    now: datetime | None = None,
    title: str = LIST_TITLE,
) -> str:
    """Список заказов с номерами от `start`; заголовок — только у первой части."""
    if not items:
        return (
            "За последнюю неделю подходящих заказов, на которые вы ещё не откликнулись, нет. "
            "Новые пришлю сам, как только появятся."
        )
    lines = [title, PICK_HINT, ""] if start == 1 else []
    for index, (p, score) in enumerate(items):
        number = start + index
        url = html.escape(p.url, quote=True)
        lines.append(f'<b>{number}.</b> <a href="{url}">{esc(p.title)}</a>')
        summary = summaries[index] if summaries else ""
        if summary:
            lines.append(f"{SUMMARY_MARK} {esc(summary)}")
        when = [
            f"🕐 {esc(fmt_when(p.published_at, tz, now))}",
            esc(PLATFORM_NAMES.get(p.platform, p.platform)),
        ]
        if score is not None:
            when.append(f"⭐ {score}")
        lines.append(" · ".join(when))
        money = [f"💵 {esc(_budget(p))}"]
        if estimates:
            guess = estimates[index]
            money.append(f"{ESTIMATE_MARK} {fmt_rub(guess.price_rub)} ₽ / {_days(guess.days)}")
        if p.platform == "fl" and not p.extra.get("for_all"):
            money.append("💰 платный отклик")
        lines.append(" · ".join(money))
        lines.append("")
    return "\n".join(lines).rstrip()


def format_start(next_digest: str | None) -> str:
    lines = [
        "👋 <b>Привет!</b> Я ищу заказы на FL.ru и Kwork под ваш профиль "
        "и готовлю отклики.",
        "",
        f"{MENU_NEW} — заказы, которых вы ещё не видели",
        f"{MENU_ALL} — подходящие за неделю, свежие сверху",
        "Выберите заказ кнопкой или номером — пришлю текст отклика и цену со сроком для формы.",
    ]
    if next_digest:
        lines += ["", f"⏰ Новые присылаю сам раз в час, следующий список — {esc(next_digest)}."]
    lines += ["", "Ещё: /filters — фильтры, /pause и /resume — пауза."]
    return "\n".join(lines)


def offer_list_keyboard(
    items: Sequence[tuple[Project, int | None]], start: int = 1
) -> list[list[tuple[str, str]]]:
    return [
        [(f"{number}. {_short(p.title)}", f"pick:{p.platform}:{p.external_id}")]
        for number, (p, _) in enumerate(items, start=start)
    ]


def _short(text: str, size: int = 40) -> str:
    return text if len(text) <= size else text[: size - 1].rstrip() + "…"


def format_offer_prompt(p: Project, summary: str = "", estimate: Estimate | None = None) -> str:
    about = f"\n{SUMMARY_MARK} {esc(summary)}" if summary else ""
    example = OFFER_EXAMPLE
    if estimate is not None:
        about += f"\n{format_estimate(estimate)}"
        example = f"{estimate.price_rub}, {estimate.days}"
    return (
        f"Заказ: <b>{esc(p.title)}</b>{about}\n\n"
        "Напишите цену в рублях и срок в днях через запятую, например: "
        f"<code>{example}</code>"
    )


def format_offer_hint() -> str:
    return (
        "Не понял цену и срок. Напишите два числа через запятую: цена в рублях, срок в днях, "
        f"например <code>{OFFER_EXAMPLE}</code>."
    )


def format_applied(p: Project) -> str:
    site = PLATFORM_NAMES.get(p.platform, p.platform)
    return (
        f"✅ Записал: вы откликнулись на «{esc(p.title)}» ({esc(site)}). "
        "Заказ убран из /last, отклик учтён в /stats."
    )


def format_offer(
    p: Project,
    d: Draft,
    leftovers: Sequence[str],
    price_rub: int,
    days: int,
    own: bool = False,
    variant: int = 0,
) -> str:
    """Готовый отклик: текст для копирования, значения полей формы и правила площадки.

    variant > 0 — ответ на «Другой вариант»: тот же вид, в заголовке номер варианта."""
    site = esc(PLATFORM_NAMES.get(p.platform, p.platform))
    url = html.escape(p.url, quote=True)
    if variant:
        head = f"🔄 <b>Вариант {variant + 1} отклика для {site}:</b>"
    else:
        head = f"✉️ <b>Отклик для {site}:</b>"
    lines = [f"{head} {esc(p.title)}", f'🔗 <a href="{url}">Заказ</a>']
    if leftovers:
        lines += ["", f"⚠ Перед отправкой впишите: {esc(', '.join(leftovers))}"]
    lines += ["", "📋 <b>Текст</b> — нажмите на него, чтобы скопировать:"]
    lines.append(f"<pre>{esc(d.text)}</pre>")
    whose = "ваши" if own else f"оценка бота, свои — «{BUTTON_OFFER}»"
    lines.append(f"🧾 <b>Поля формы</b> ({esc(whose)}):")
    lines.append(f"• Цена: <code>{price_rub}</code> ₽")
    lines.append(f"• Срок: <code>{days}</code> ({esc(_days(days).split()[1])})")
    tips = PLATFORM_TIPS.get(p.platform, ())
    if tips:
        lines += ["", f"💡 <b>{site}:</b>"]
        lines.extend(f"• {esc(tip)}" for tip in tips)
    return "\n".join(lines)


def format_stats(stats_today: Mapping[str, int], stats_week: Mapping[str, int]) -> str:
    lines = ["📊 <b>Статистика</b>", "<i>Сегодня / за 7 дней</i>", ""]
    for key, icon, label in _STATS_LABELS:
        today, week = stats_today.get(key, 0), stats_week.get(key, 0)
        if key == "sent" and not week:
            continue  # автоотправки нет (ADR 0001) — пустая строка только шумит
        lines.append(f"{icon} {label}: <b>{today}</b> / {week}")
    shown, applied = stats_week.get("notified", 0), stats_week.get("applied", 0)
    if shown:
        lines += ["", f"📈 за неделю откликнулись на {applied} из {shown} присланных "
                      f"({round(100 * applied / shown)}%)"]
    return "\n".join(lines)


def format_filters(settings: Settings) -> str:
    lines = ["🎯 <b>Фильтры</b>"]
    lines.append(
        f"min_score: {settings.min_score}; макс. возраст проекта: {settings.max_age_hours:g} ч"
    )
    lines.append("")
    lines.append("🧭 <b>Направления</b>")
    for name, direction in settings.directions.items():
        words = ", ".join(direction.keywords[:8])
        more = "…" if len(direction.keywords) > 8 else ""
        lines.append(f"• {esc(name)} (вес {direction.weight:g}): {esc(words)}{more}")
    if settings.intersections:
        pairs = "; ".join(
            f"{i.directions[0]}+{i.directions[1]} +{i.bonus}" for i in settings.intersections
        )
        lines.append(f"Бонусы пересечений: {esc(pairs)}")
    lines.append("")
    lines.append("🚫 <b>Исключения</b>")
    for name, words in settings.exclusions.items():
        short = ", ".join(words[:6]) + ("…" if len(words) > 6 else "")
        lines.append(f"• {esc(name)}: {esc(short)}")
    lines.append("")
    lines.append("🌐 <b>Площадки</b>")
    for platform in PLATFORMS:
        src = settings.sources[platform]
        state = "включена" if src.enabled else "выключена"
        lines.append(
            f"• {PLATFORM_NAMES[platform]}: {state}, режим {src.mode}, "
            f"интервал {src.interval_sec} с, лимит авто в сутки {src.daily_auto_limit}"
        )
    lines.append(f"Лимит коннектов Kwork в сутки: {settings.kwork_connects_daily_limit}")
    price = f"{settings.offer_price_rub} ₽" if settings.offer_price_rub is not None else "оценка"
    days = f"{settings.offer_days} дн." if settings.offer_days is not None else "оценка"
    lines.append(
        f"Цена/срок в черновике: {esc(price)} / {esc(days)} "
        "(оценка — по типу заказа и бюджету, своё — кнопкой «Цена и срок»)"
    )
    return "\n".join(lines)


def format_status(
    settings: Settings,
    paused: bool,
    flags: Mapping[str, str | None],
    next_digest: str | None = None,
) -> str:
    lines = ["⚙️ <b>Статус</b>", "⏸ на паузе — /resume" if paused else "▶️ работает"]
    if next_digest and not paused:
        lines.append(f"⏰ следующий список новых заказов: {esc(next_digest)}")
    if settings.dry_run:
        lines.append("dry-run: отправка на биржи отключена")
    lines.append("")
    for platform in PLATFORMS:
        src = settings.sources[platform]
        name = PLATFORM_NAMES[platform]
        if not src.enabled:
            lines.append(f"💤 <b>{name}</b> · выключена")
            continue
        last_ok = flags.get(f"last_ok:{platform}")
        when = "ещё не было"
        if last_ok:
            when = fmt_time(datetime.fromisoformat(last_ok), settings.timezone)
        error = flags.get(f"last_error:{platform}")
        blocked = flags.get(f"blocked_until:{platform}")
        icon = "⛔" if blocked else "⚠️" if error else "✅"
        lines.append(f"{icon} <b>{name}</b> · режим {src.mode} · сбор: {esc(when)}")
        if error:
            lines.append(f"   ошибка: {esc(error)}")
        if blocked:
            until = fmt_time(datetime.fromisoformat(blocked), settings.timezone)
            lines.append(f"   пауза из-за блокировки до {esc(until)}")
    if settings.sources["kwork"].enabled:
        lines += ["", esc(KWORK_WARNING)]
    return "\n".join(lines)
