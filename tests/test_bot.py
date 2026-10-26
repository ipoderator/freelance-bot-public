"""Бот: хэндлеры на фейковых апдейтах, CLI, setup-telegram, launchd-скрипты. Сети нет."""

from __future__ import annotations

import plistlib
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from freelance_bot import __main__ as cli
from freelance_bot.bot import (
    AWAITING_OFFER_FLAG,
    COMMANDS,
    LAST_LIST_FLAG,
    MENU_VERSION_FLAG,
    PUBLIC_COMMANDS,
    BotHandlers,
    TelegramNotifier,
    _make_digest_job,
    _register_commands,
    _register_description,
    build_application,
    next_digest_at,
    parse_offer,
)
from freelance_bot.config import load_settings
from freelance_bot.models import Draft
from freelance_bot.pipeline import Pipeline
from freelance_bot.storage import Storage

ROOT = Path(__file__).resolve().parent.parent
OWNER = 4242
FAKE_TOKEN = "123456:TEST-TOKEN-NOT-REAL"
NOW = datetime(2026, 10, 21, 10, 0, tzinfo=UTC)


class FakeMessage:
    def __init__(self, text=None):
        self.text = text
        self.replies: list[str] = []
        self.markups: list = []
        self.actions: list[str] = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        self.markups.append(kwargs.get("reply_markup"))

    async def reply_chat_action(self, action):
        self.actions.append(action)


class FakeQuery:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.answers: list[str] = []
        self.markup_cleared = False
        self.edited: list[str] = []
        self.edited_markup = None

    async def answer(self, text=None, show_alert=False):
        self.answers.append(text or "")
        self.alert = show_alert

    async def edit_message_reply_markup(self, reply_markup=None):
        self.markup_cleared = reply_markup is None

    async def edit_message_text(self, text, **kwargs):
        self.edited.append(text)
        self.edited_markup = kwargs.get("reply_markup")


def update_from(user_id, callback_data=None, text=None):
    message = FakeMessage(text)
    query = FakeQuery(callback_data, message) if callback_data else None
    return SimpleNamespace(
        effective_user=SimpleNamespace(id=user_id),
        effective_message=message,
        callback_query=query,
    ), message, query


class FakeDrafter:
    async def generate(self, p, r, variant=0):
        return Draft(f"новый вариант {variant}", "builtin", False, False)


class NullNotifier:
    async def send_card(self, text, keyboard): ...

    async def send_text(self, text): ...


@pytest.fixture
def env(make_project):
    from dataclasses import replace

    settings = load_settings(ROOT / "settings.toml", ROOT / "no-such.env")
    settings = replace(settings, owner_id=OWNER, telegram_token=FAKE_TOKEN, digest_enabled=False)
    clock = SimpleNamespace(now=NOW)  # часы базы: тест может их сдвинуть
    storage = Storage(":memory:", clock=lambda: clock.now)
    pipeline = Pipeline(settings, storage, [], FakeDrafter(), NullNotifier(), clock=lambda: NOW)
    p = make_project(external_id="77")
    storage.upsert_project(p)
    from freelance_bot.scoring import score_project

    storage.save_score("fl", "77", score_project(p, settings, NOW))
    yield SimpleNamespace(
        settings=settings,
        storage=storage,
        handlers=BotHandlers(settings, storage, pipeline),
        clock=clock,
    )
    storage.close()


async def test_stranger_gets_refusal_and_no_data(env):
    for handler in (env.handlers.status, env.handlers.filters_, env.handlers.stats,
                    env.handlers.pause, env.handlers.start):
        update, message, _ = update_from(999)
        await handler(update, None)
        assert message.replies == ["Этот бот личный. Доступ закрыт."]
    assert env.storage.get_flag("paused") is None  # чужой /pause ничего не меняет


async def test_stranger_button_does_not_change_status(env):
    update, _, query = update_from(999, "applied:fl:77")
    await env.handlers.button(update, None)
    assert env.storage.get_status("fl", "77") == "new"
    assert query.answers == ["Этот бот личный. Доступ закрыт."]


async def test_owner_in_group_chat_gets_refusal(env):
    update, message, _ = update_from(OWNER)
    update.effective_chat = SimpleNamespace(type="group")
    await env.handlers.last(update, None)
    assert message.replies == ["Этот бот личный. Доступ закрыт."]  # заказы не уходят в группу
    update.effective_chat = SimpleNamespace(type="private")
    await env.handlers.last(update, None)
    assert "Подходящие предложения" in message.replies[1]


async def test_nobody_is_owner_when_owner_id_unset(env):
    from dataclasses import replace

    handlers = BotHandlers(replace(env.settings, owner_id=None), env.storage, None)
    update, message, _ = update_from(OWNER)
    await handlers.status(update, None)
    assert message.replies == ["Этот бот личный. Доступ закрыт."]


async def test_pause_and_resume_persist_in_storage(env, tmp_path):
    update, message, _ = update_from(OWNER)
    await env.handlers.pause(update, None)
    assert env.storage.get_flag("paused") == "1"

    status_update, status_msg, _ = update_from(OWNER)
    await env.handlers.status(status_update, None)
    assert "на паузе" in status_msg.replies[0]

    # перезапуск: новый Storage на том же файле видит паузу
    db = tmp_path / "bot.sqlite3"
    first = Storage(db)
    first.set_flag("paused", "1")
    first.close()
    assert Storage(db).get_flag("paused") == "1"

    await env.handlers.resume(update, None)
    assert env.storage.get_flag("paused") == "0"
    assert "Пауза снята" in message.replies[-1]


async def test_filters_and_stats_handlers_reply(env):
    update, message, _ = update_from(OWNER)
    await env.handlers.filters_(update, None)
    assert "min_score" in message.replies[0]

    env.storage.set_status("fl", "77", "notified")
    await env.handlers.stats(update, None)
    assert "Сегодня" in message.replies[1] and "7 дней" in message.replies[1]
    assert "📨 прислано: <b>1</b> / 1" in message.replies[1]
    assert message.markups[1].inline_keyboard[0][0].callback_data == "stats:reset"


async def test_stats_reset_asks_first_then_counts_from_reset(env, make_project):
    env.storage.set_status("fl", "77", "notified")
    update, _, query = update_from(OWNER, "stats:reset")
    await env.handlers.button(update, None)
    assert "Сбросить статистику?" in query.edited[-1]
    assert env.storage.get_flag("stats_reset_at") is None  # первое нажатие ничего не сбрасывает

    update, _, query = update_from(OWNER, "stats:keep")
    await env.handlers.button(update, None)
    assert "📨 прислано: <b>1</b> / 1" in query.edited[-1]

    env.clock.now = NOW + timedelta(minutes=5)
    update, _, query = update_from(OWNER, "stats:confirm")
    await env.handlers.button(update, None)
    assert query.answers == ["Статистика сброшена"]
    assert "📨 прислано: <b>0</b> / 0" in query.edited[-1] and "со сброса" in query.edited[-1]
    assert env.storage.get_status("fl", "77") == "notified"  # заказы и история не тронуты

    env.clock.now = NOW + timedelta(minutes=10)
    env.storage.upsert_project(make_project(external_id="78"))
    env.storage.set_status("fl", "78", "notified")  # после сброса счёт идёт заново
    update, message, _ = update_from(OWNER)
    await env.handlers.stats(update, None)
    assert "📨 прислано: <b>1</b> / 1" in message.replies[0]


async def test_help_shows_contents_and_topics_in_place(env):
    update, message, _ = update_from(OWNER)
    await env.handlers.help_(update, None)
    assert "Как пользоваться ботом" in message.replies[0]
    topics = [b.callback_data for row in message.markups[0].inline_keyboard for b in row]
    assert topics == [
        "help:start", "help:menu", "help:orders", "help:vacancies", "help:learning",
        "help:schedule", "help:commands",
    ]

    update, _, query = update_from(OWNER, "help:orders")
    await env.handlers.button(update, None)
    assert "Заказ с биржи" in query.edited[-1] and "💬 Цена и срок" in query.edited[-1]
    pages = [b.callback_data for row in query.edited_markup.inline_keyboard for b in row]
    assert "help:orders" not in pages and pages[-1] == "help:intro"

    update, _, query = update_from(OWNER, "help:commands")
    await env.handlers.button(update, None)
    assert "/stats — Статистика за сегодня и 7 дней" in query.edited[-1]

    update, _, query = update_from(OWNER, "help:schedule")
    await env.handlers.button(update, None)
    assert "присылаю списком раз в час" in query.edited[-1]

    update, _, query = update_from(OWNER, "help:gone")  # кнопка старой справки
    await env.handlers.button(update, None)
    assert "Выберите тему" in query.edited[-1]

    update, message, _ = update_from(9999)  # посторонний при закрытом боте
    await env.handlers.help_(update, None)
    assert message.replies == ["Этот бот личный. Доступ закрыт."]


async def test_register_description_sets_only_changed_text(caplog):
    from freelance_bot import guide

    class Bot:
        def __init__(self, description="", short=""):
            self.description, self.short, self.calls = description, short, []

        async def get_my_description(self):
            return SimpleNamespace(description=self.description)

        async def get_my_short_description(self):
            return SimpleNamespace(short_description=self.short)

        async def set_my_description(self, text):
            self.calls.append("description")
            self.description = text

        async def set_my_short_description(self, text):
            self.calls.append("short")
            self.short = text

    bot = Bot()
    await _register_description(SimpleNamespace(bot=bot))
    assert bot.calls == ["description", "short"]
    assert bot.description.endswith("работает только у владельца.")  # без публичного режима
    assert bot.short == guide.SHORT_DESCRIPTION
    await _register_description(SimpleNamespace(bot=bot))
    assert bot.calls == ["description", "short"]  # тот же текст — Telegram не дёргаем

    settings = SimpleNamespace(public_enabled=True)
    await _register_description(SimpleNamespace(bot=bot), settings)
    assert bot.description == guide.DESCRIPTION

    class BrokenBot:
        async def get_my_description(self):
            raise RuntimeError("сеть")

    await _register_description(SimpleNamespace(bot=BrokenBot()))
    assert "не удалось обновить описание бота" in caplog.text


async def test_status_shows_last_ok(env):
    env.storage.set_flag("last_ok:fl", "2026-10-21T09:00:00+00:00")
    update, message, _ = update_from(OWNER)
    await env.handlers.status(update, None)
    assert "21.10.2026 12:00 МСК" in message.replies[0]


async def test_buttons_change_status_and_history(env):
    update, message, query = update_from(OWNER, "applied:fl:77")
    await env.handlers.button(update, None)
    assert env.storage.get_status("fl", "77") == "applied"
    assert query.markup_cleared
    assert message.replies == [
        "✅ Записал: вы откликнулись на «Telegram-бот на Python» (FL.ru). "
        "Заказ убран из /last, отклик учтён в /stats."
    ]

    update, message, _ = update_from(OWNER, "skip:fl:77")
    await env.handlers.button(update, None)
    assert env.storage.get_status("fl", "77") == "skipped"
    assert message.replies == []  # «Пропустить» — без лишних сообщений
    stats = env.storage.stats(NOW - timedelta(days=1))
    assert stats["applied"] == 1 and stats["skipped"] == 1


async def test_redraft_button_sends_new_draft_and_saves_it(env):
    update, message, query = update_from(OWNER, "redraft:fl:77")
    await env.handlers.button(update, None)
    # нажатие подтверждается до генерации: ответ LLM дольше, чем Telegram ждёт answer
    assert query.answers == ["Готовлю новый вариант…"]
    assert message.actions == ["typing"]
    # тот же вид, что у выбранного заказа: текст блоком для копирования, поля и кнопки
    assert "Вариант 2 отклика" in message.replies[0]
    assert "<pre>новый вариант 1</pre>" in message.replies[0]
    assert "Поля формы" in message.replies[0]
    buttons = [b.callback_data for row in message.markups[0].inline_keyboard for b in row]
    assert "redraft:fl:77" in buttons and "applied:fl:77" in buttons
    assert env.storage.recent_drafts() == ["новый вариант 1"]
    assert env.storage.get_status("fl", "77") == "new"  # статус не трогаем


async def test_redraft_without_score_reports_in_chat(env, make_project):
    env.storage.upsert_project(make_project(external_id="80"))  # балла нет — варианта не будет
    update, message, query = update_from(OWNER, "redraft:fl:80")
    await env.handlers.button(update, None)
    assert query.answers == ["Готовлю новый вариант…"]
    assert message.replies == ["Не удалось собрать новый вариант."]


async def test_handler_error_is_reported_to_owner_only(env):
    context = SimpleNamespace(error=TimeoutError("https://api.telegram.org/bot<secret>/x"))
    update, message, _ = update_from(OWNER, text="2")
    await env.handlers.on_error(update, context)
    assert message.replies == [
        "⚠ Не получилось выполнить действие (TimeoutError). Попробуйте ещё раз."
    ]
    update, message, _ = update_from(999, text="2")
    await env.handlers.on_error(update, context)
    assert message.replies == []
    await env.handlers.on_error(None, context)  # ошибка вне апдейта — только лог


async def test_notifier_waits_retry_after_and_resends(monkeypatch):
    from telegram.error import RetryAfter

    from freelance_bot import bot as bot_module

    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    monkeypatch.setattr(bot_module.asyncio, "sleep", fake_sleep)

    class LimitedBot:
        def __init__(self, failures, retry_after=3):
            self.failures, self.retry_after, self.sent = failures, retry_after, []

        async def send_message(self, **kwargs):
            if self.failures:
                self.failures -= 1
                raise RetryAfter(self.retry_after)
            self.sent.append(kwargs["text"])

    limited = LimitedBot(failures=1)
    await TelegramNotifier(limited, OWNER).send_text("карточка")
    assert limited.sent == ["карточка"] and sleeps == [3.0]

    with pytest.raises(RetryAfter):  # лимит не отпускает — ошибка, проект станет failed
        await TelegramNotifier(LimitedBot(failures=5), OWNER).send_text("карточка")
    with pytest.raises(RetryAfter):  # ждать слишком долго — не ждём
        await TelegramNotifier(LimitedBot(failures=1, retry_after=600), OWNER).send_text("x")


async def test_unknown_callback_is_ignored(env):
    update, _, query = update_from(OWNER, "applied:fl:nope")
    await env.handlers.button(update, None)
    assert "не найден" in query.answers[0]


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("15000, 5", (15000, 5)),
        ("15000 5", (15000, 5)),
        ("15 000 ₽, 5 дней", (15000, 5)),
        ("15 000 15", (15000, 15)),
        ("15к/7", (15000, 7)),
        ("20000 руб\n10 дн", (20000, 10)),
        ("5", None),
        ("привет", None),
        ("15000, 0", None),
        ("15000, 400", None),
        ("0, 5", None),
    ],
)
def test_parse_offer(text, expected):
    assert parse_offer(text) == expected


def test_parse_offer_long_text_is_rejected_fast():
    import time

    started = time.perf_counter()  # раньше 200 пробелов — 1,5 с, 4000 — часы (ReDoS)
    for text in ("1" + " " * 4000 + "x", "1 " * 2000 + "x", "1" + " \n" * 3000 + "x"):
        assert parse_offer(text) is None
    assert parse_offer("15000," + " " * 3000 + "5") == (15000, 5)  # пробелы — не текст
    assert time.perf_counter() - started < 0.5
    assert parse_offer("15\u00a0000   ₽,\n  5   дней") == (15000, 5)  # пробелы схлопываются


async def test_offer_flow_fills_price_and_days_and_gives_link(env):
    env.storage.set_status("fl", "77", "drafted")
    env.storage.add_response(
        "fl", "77", "draft", "Цена [ЦЕНА], срок [СРОК]. [ПРИМЕРЫ РАБОТ — впиши]", "llm", False, None
    )
    update, message, query = update_from(OWNER, "offer:fl:77")
    await env.handlers.button(update, None)
    assert env.storage.get_flag(AWAITING_OFFER_FLAG) == "fl:77"
    assert "через запятую" in message.replies[0]
    assert "📝 Нужен бот с FastAPI-бэкендом." in message.replies[0]
    assert message.markups[0].inline_keyboard[0][0].callback_data == "cancel:fl:77"

    update, message, _ = update_from(OWNER, text="не число")
    await env.handlers.text(update, None)
    assert "Не понял" in message.replies[0]
    assert env.storage.get_flag(AWAITING_OFFER_FLAG) == "fl:77"  # ждём дальше

    update, message, _ = update_from(OWNER, text="15000, 5")
    await env.handlers.text(update, None)
    reply = message.replies[0]
    assert "<pre>Цена 15 000 ₽, срок 5 дней. [ПРИМЕРЫ РАБОТ — впиши]</pre>" in reply
    assert "⚠ Перед отправкой впишите: [ПРИМЕРЫ РАБОТ — впиши]" in reply
    assert "<b>Поля формы</b> (ваши):" in reply
    assert "• Цена: <code>15000</code> ₽" in reply and "• Срок: <code>5</code> (дней)" in reply
    rows = message.markups[0].inline_keyboard
    assert rows[0][0].url == "https://www.fl.ru/projects/1/test/"
    assert rows[2][0].callback_data == "applied:fl:77"
    assert env.storage.get_flag(AWAITING_OFFER_FLAG) == ""
    assert env.storage.latest_response("fl", "77")[0].startswith("Цена 15 000 ₽")
    assert env.storage.get_status("fl", "77") == "drafted"  # отправляет пользователь сам


async def test_pick_sends_ready_response_with_form_fields(env):
    update, message, query = update_from(OWNER, "pick:fl:77")
    await env.handlers.button(update, None)
    assert query.answers == ["Готовлю отклик…"] and message.actions == ["typing"]
    reply = message.replies[0]
    assert reply.startswith("✉️ <b>Отклик для FL.ru:</b> Telegram-бот на Python")
    assert "<pre>новый вариант 0</pre>" in reply
    guess = env.handlers.pipeline.estimate(env.storage.get_project("fl", "77"))
    assert f"• Цена: <code>{guess.price_rub}</code> ₽" in reply
    assert "оценка бота" in reply and "календарных днях" in reply
    rows = message.markups[0].inline_keyboard
    assert rows[0][0].url == "https://www.fl.ru/projects/1/test/"
    assert [b.callback_data for b in rows[1]] == ["offer:fl:77", "redraft:fl:77"]
    assert rows[2][0].callback_data == "applied:fl:77"
    assert env.storage.latest_response("fl", "77")[0] == "новый вариант 0"  # сохранён

    env.storage.set_flag(AWAITING_OFFER_FLAG, "fl:77")
    update, message, _ = update_from(OWNER, text="20000, 7")
    await env.handlers.text(update, None)
    update, message, _ = update_from(OWNER, "pick:fl:77")  # своя цена не теряется
    await env.handlers.button(update, None)
    assert "(ваши)" in message.replies[0] and "<code>20000</code>" in message.replies[0]


async def test_pick_by_number_from_last_list(env, make_project):
    update, message, _ = update_from(OWNER, text="2")
    await env.handlers.text(update, None)
    assert message.replies == ["Нет заказа №2 в списке /last: сначала откройте /last."]

    env.storage.upsert_project(
        make_project(platform="kwork", external_id="5", url="https://kwork.ru/projects/5")
    )
    update, message, _ = update_from(OWNER)
    await env.handlers.last(update, None)
    order = [row[0].callback_data for row in message.markups[0].inline_keyboard]
    assert len(order) == 2

    env.storage.set_flag(AWAITING_OFFER_FLAG, "fl:77")
    second = order[1].removeprefix("pick:")
    update, message, _ = update_from(OWNER, text="№2")
    await env.handlers.text(update, None)
    assert env.storage.get_flag(AWAITING_OFFER_FLAG) == ""  # выбор номера отменяет ожидание
    site = "Kwork" if second.startswith("kwork") else "FL.ru"
    assert message.replies[0].startswith(f"✉️ <b>Отклик для {site}:</b>")
    if site == "Kwork":
        assert "обнуляет коннекты" in message.replies[0]

    update, message, _ = update_from(OWNER, text="3")
    await env.handlers.text(update, None)
    assert message.replies == ["Нет заказа №3 в списке /last: от 1 до 2."]

    env.storage.set_flag(LAST_LIST_FLAG, "fl:gone")
    update, message, _ = update_from(OWNER, text="1")
    await env.handlers.text(update, None)
    assert "не найден" in message.replies[0]


@pytest.mark.parametrize(
    ("now_msk", "interval", "expected_msk"),
    [
        ("10:28", 60, "11:00"),  # сегодня с 11 утра
        ("11:00", 60, "12:00"),  # ровно в слот — следующий, без повтора
        ("11:30", 60, "12:00"),
        ("03:10", 60, "04:00"),  # сетка идёт и ночью
        ("11:30", 120, "13:00"),
        ("23:30", 60, "00:00"),
    ],
)
def test_next_digest_follows_hourly_grid_from_start_hour(now_msk, interval, expected_msk):
    from zoneinfo import ZoneInfo

    msk = ZoneInfo("Europe/Moscow")
    hour, minute = map(int, now_msk.split(":"))
    now = datetime(2026, 10, 24, hour, minute, tzinfo=msk)
    slot = next_digest_at(now.astimezone(UTC), "Europe/Moscow", 11, interval)
    assert slot.tzinfo is not None and slot > now
    assert f"{slot.astimezone(msk):%H:%M}" == expected_msk


async def test_new_sends_queued_projects_once(env, make_project):
    env.storage.upsert_project(make_project(external_id="88", title="Новый бот"))
    env.storage.set_status("fl", "88", "queued")
    update, message, _ = update_from(OWNER, text="🆕 Новые предложения")  # кнопка меню
    await env.handlers.text(update, None)
    reply = message.replies[0]
    assert reply.startswith("🆕 <b>Новые предложения</b> — 1") and "Новый бот" in reply
    assert message.markups[0].inline_keyboard[0][0].callback_data == "pick:fl:88"
    assert env.storage.get_status("fl", "88") == "notified"
    assert env.storage.get_flag(LAST_LIST_FLAG) == "fl:88"  # «1» в чат — этот заказ

    update, message, _ = update_from(OWNER)
    await env.handlers.new(update, None)
    assert message.replies[0].startswith("Новых предложений пока нет")


async def test_menu_buttons_and_menu_command_show_persistent_keyboard(env):
    update, message, _ = update_from(OWNER)
    await env.handlers.menu(update, None)
    assert "👋" in message.replies[0]
    keyboard = message.markups[0].keyboard
    assert [[b.text for b in row] for row in keyboard] == [
        ["🆕 Новые предложения"], ["📋 Все предложения"],
        ["📊 Статистика", "⚙️ Состояние бота"], ["🧠 Чему я научился", "❓ Помощь"],
    ]
    for label, expected in (("📋 Все предложения", "Подходящие предложения"),
                            ("📊 Статистика", "Сегодня"), ("⚙️ Состояние бота", "Состояние бота"),
                            # прежние подписи: клавиатура у владельца ещё старая
                            ("📋 Все заказы", "Подходящие предложения"),
                            ("⚙️ Статус", "Состояние бота"), ("🧠 Обучение", "Чему я научился"),
                            ("❓ Помощь", "Как пользоваться ботом")):
        update, message, _ = update_from(OWNER, text=label)
        await env.handlers.text(update, None)
        assert expected in message.replies[0], label


async def test_menu_is_announced_once_after_update(env):
    class Bot:
        def __init__(self, fail=False):
            self.sent, self.fail = [], fail

        async def send_message(self, **kwargs):
            if self.fail:
                raise TimeoutError
            self.sent.append(kwargs)

    broken = Bot(fail=True)
    await env.handlers.announce_menu(broken)
    assert env.storage.get_flag(MENU_VERSION_FLAG) is None  # не дошло — попробуем при запуске
    bot = Bot()
    await env.handlers.announce_menu(bot)
    await env.handlers.announce_menu(bot)
    assert len(bot.sent) == 1 and bot.sent[0]["chat_id"] == OWNER
    assert bot.sent[0]["reply_markup"].is_persistent


async def test_digest_job_skips_pause_and_sends_via_notifier(env, make_project):
    class Notifier:
        def __init__(self):
            self.cards = []

        async def send_card(self, text, keyboard):
            self.cards.append((text, keyboard))

    notifier = Notifier()
    env.handlers.pipeline.notifier = notifier
    env.storage.upsert_project(make_project(external_id="89"))
    env.storage.set_status("fl", "89", "queued")
    job = _make_digest_job(env.handlers, env.handlers.pipeline)

    env.storage.set_flag("paused", "1")
    await job(None)
    assert notifier.cards == [] and env.storage.get_status("fl", "89") == "queued"

    env.storage.set_flag("paused", "0")
    env.storage.set_flag("digest_last_at", "2000-01-01T00:00:00+00:00")  # слот давно прошёл
    await job(None)
    assert len(notifier.cards) == 1 and notifier.cards[0][1][0][0][1] == "pick:fl:89"
    assert env.storage.get_status("fl", "89") == "notified"


async def test_offer_cancel_and_text_without_choice(env):
    update, _, _ = update_from(OWNER, "offer:fl:77")
    await env.handlers.button(update, None)
    update, _, query = update_from(OWNER, "cancel:fl:77")
    await env.handlers.button(update, None)
    assert env.storage.get_flag(AWAITING_OFFER_FLAG) == ""
    assert query.answers == ["Отменено"] and query.markup_cleared

    update, message, _ = update_from(OWNER, text="15000, 5")
    await env.handlers.text(update, None)
    assert "/last" in message.replies[0]
    assert env.storage.latest_response("fl", "77") is None


async def test_offer_for_deleted_project_and_stranger_text(env):
    env.storage.set_flag(AWAITING_OFFER_FLAG, "fl:gone")
    update, message, _ = update_from(OWNER, text="15000, 5")
    await env.handlers.text(update, None)
    assert "не найден" in message.replies[0]

    env.storage.set_flag(AWAITING_OFFER_FLAG, "fl:77")
    update, message, _ = update_from(999, text="15000, 5")
    await env.handlers.text(update, None)
    assert message.replies == ["Этот бот личный. Доступ закрыт."]
    assert env.storage.latest_response("fl", "77") is None


async def test_last_lists_projects_waiting_for_response(env, make_project):
    from dataclasses import replace

    free_only = replace(env.settings.sources["fl"], only_free_responses=True)
    pipeline = env.handlers.pipeline
    sources = {**pipeline.settings.sources, "fl": free_only}
    pipeline.settings = replace(pipeline.settings, sources=sources)
    update, message, _ = update_from(OWNER)
    await env.handlers.last(update, None)  # у «77» отклик платный — в список не идёт
    assert "подходящих предложений" in message.replies[0] and message.markups[0] is None

    env.storage.upsert_project(
        make_project(external_id="78", title="Бот на Python", extra={"for_all": True})
    )
    await env.handlers.last(update, None)
    assert "Бот на Python" in message.replies[1]
    assert "📝 Нужен бот с FastAPI-бэкендом." in message.replies[1]
    assert message.markups[1].inline_keyboard[0][0].callback_data == "pick:fl:78"

    for number in range(10):  # длинные заголовки и суть: список режется, а не обрывается
        env.storage.upsert_project(
            make_project(
                external_id=f"9{number}", title="Бот на Python " + "очень длинный " * 20,
                description="Сделать бота. " + "x" * 190, extra={"for_all": True},
            )
        )
    update, message, _ = update_from(OWNER)
    await env.handlers.last(update, None)  # 11 заказов не влезают в одно сообщение — несколько
    assert len(message.replies) > 1 and all(len(r) <= 4000 for r in message.replies)
    assert message.replies[0].startswith("📋 <b>Подходящие предложения</b>")
    assert "Подходящие предложения" not in message.replies[1]
    labels = [row[0].text for kb in message.markups for row in kb.inline_keyboard]
    assert [label.split(".")[0] for label in labels] == [str(n) for n in range(1, 12)]
    assert sum(r.count("📝") for r in message.replies) == 11
    assert len(env.storage.get_flag(LAST_LIST_FLAG).split(",")) == 11

    for number in range(15):
        env.storage.upsert_project(
            make_project(external_id=f"5{number:02}", title="Бот", extra={"for_all": True})
        )
    update, message, _ = update_from(OWNER)
    await env.handlers.last(update, None)
    buttons = sum(len(kb.inline_keyboard) for kb in message.markups)
    assert buttons == 20 and len(env.storage.get_flag(LAST_LIST_FLAG).split(",")) == 20

    update, message, _ = update_from(999)
    await env.handlers.last(update, None)
    assert message.replies == ["Этот бот личный. Доступ закрыт."]


async def test_register_commands_sets_menu_and_survives_errors(caplog):
    bot = FakeBot([])
    await _register_commands(SimpleNamespace(bot=bot))
    assert [c for c, _ in bot.commands] == [name for name, _ in COMMANDS]

    class BrokenBot:
        async def set_my_commands(self, commands):
            raise RuntimeError("сеть")

    await _register_commands(SimpleNamespace(bot=BrokenBot()))
    assert "не удалось обновить меню команд" in caplog.text


def test_build_application_registers_commands_and_jobs(env):
    class Src:
        platform = "fl"

        async def fetch(self):
            return []

    notifier = TelegramNotifier(None, OWNER)
    pipeline = Pipeline(env.settings, env.storage, [Src()], FakeDrafter(), notifier)
    app = build_application(env.settings, env.storage, pipeline)
    names = {c for h in app.handlers[0] for c in getattr(h, "commands", [])}
    assert names == {name for name, _ in COMMANDS + PUBLIC_COMMANDS}
    assert [j.name for j in app.job_queue.jobs()] == ["collect:fl"]  # дайджест выключен
    assert notifier.bot is app.bot

    from dataclasses import replace

    app = build_application(replace(env.settings, digest_enabled=True), env.storage, pipeline)
    assert {j.name for j in app.job_queue.jobs()} == {"collect:fl", "digest"}


# ── CLI ──────────────────────────────────────────────────────────────────────


class FakeBot:
    def __init__(self, updates):
        self._updates = updates
        self.commands = None

    async def get_me(self):
        return SimpleNamespace(username="my_freelance_bot")

    async def set_my_commands(self, commands):
        self.commands = [(c.command, c.description) for c in commands]

    async def get_updates(self, offset=None, timeout=0, allowed_updates=None):
        updates, self._updates = self._updates, []
        return updates


def _start_update(user_id):
    return SimpleNamespace(
        update_id=1,
        message=SimpleNamespace(text="/start", from_user=SimpleNamespace(id=user_id)),
    )


async def test_setup_telegram_registers_commands_and_prints_user_id():
    out: list[str] = []
    bot = FakeBot([_start_update(555)])
    code = await cli.setup_telegram(bot, out.append, wait_seconds=1, poll_seconds=0)
    text = "\n".join(out)
    assert code == 0
    assert [c for c, _ in bot.commands] == [
        "start", "menu", "help", "status", "pause", "resume", "filters", "stats", "last", "new",
        "learned", "signup",
    ]
    assert "@my_freelance_bot" in text
    assert "TELEGRAM_OWNER_ID=555" in text


async def test_setup_telegram_reports_timeout_without_start():
    out: list[str] = []
    code = await cli.setup_telegram(FakeBot([]), out.append, wait_seconds=0, poll_seconds=0)
    assert code == 1
    assert "/start" in "\n".join(out)


def test_setup_telegram_without_token_prints_hint(tmp_path, capsys):
    code = cli.main(["--env-file", str(tmp_path / "none.env"),
                     "--settings", str(ROOT / "settings.toml"), "setup-telegram"])
    out = capsys.readouterr().out
    assert code != 0
    assert "TELEGRAM_BOT_TOKEN" in out and "@BotFather" in out
    assert "Traceback" not in out


def test_setup_telegram_failure_does_not_leak_token(tmp_path, capsys, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text(f"TELEGRAM_BOT_TOKEN={FAKE_TOKEN}\n")

    class Boom:
        async def get_me(self):
            raise RuntimeError(f"https://api.telegram.org/bot{FAKE_TOKEN}/getMe failed")

    monkeypatch.setattr(cli, "make_bot", lambda token: Boom())
    code = cli.main(["--env-file", str(env_file), "--settings", str(ROOT / "settings.toml"),
                     "setup-telegram"])
    captured = capsys.readouterr()
    assert code != 0
    assert FAKE_TOKEN not in captured.out + captured.err


def test_once_dry_run_console_without_token(tmp_path, capsys, monkeypatch, make_project):
    class Src:
        platform = "fl"

        async def fetch(self):
            return [make_project(
                external_id="900",
                title="Telegram-бот на Python (FastAPI) с OpenAI",
                description="Нужен telegram бот, backend Python FastAPI PostgreSQL, OpenAI.",
                published_at=datetime.now(UTC) - timedelta(minutes=30),
                offers_count=1,
                extra={"for_all": True},  # бесплатный отклик — платные отсекаются
            )]

    monkeypatch.setattr(cli, "build_sources", lambda *args, **kwargs: [Src()])
    db = tmp_path / "t.sqlite3"
    code = cli.main([
        "--env-file", str(tmp_path / "none.env"), "--settings", str(ROOT / "settings.toml"),
        "--profile", str(ROOT / "profile" / "profile.example.toml"), "--db", str(db),
        "once", "--dry-run", "--console",
    ])
    out = capsys.readouterr().out
    assert code == 0
    assert "Telegram-бот на Python" in out
    assert "Черновик отклика" in out
    assert "собрано 1" in out and "прошло 1" in out
    assert Storage(db).get_status("fl", "900") == "drafted"


def test_disabled_kwork_runs_only_with_include_disabled(capsys):
    from dataclasses import replace

    settings = load_settings(ROOT / "settings.toml", ROOT / "no-such.env")
    on = {name: replace(settings.sources[name], enabled=True) for name in ("fl", "work24")}
    off = {
        name: replace(settings.sources[name], enabled=False)
        for name in ("kwork", "freelance_ru", "workzilla", "hh", "superjob", "trudvsem",
                     "tgchannels")
    }
    settings = replace(settings, sources={**settings.sources, **on, **off})
    assert cli.enabled_platforms(settings, None, include_disabled=False) == ["fl", "work24"]
    assert cli.enabled_platforms(settings, "kwork", include_disabled=True) == ["kwork"]
    assert cli.enabled_platforms(settings, "kwork", include_disabled=False) == []


def test_work24_source_skips_orders_already_in_storage(make_project):
    from dataclasses import replace

    from freelance_bot.sources.work24 import Work24Source

    settings = load_settings(ROOT / "settings.toml", ROOT / "no-such.env")
    w24 = replace(settings.sources["work24"], enabled=True, operations=["listItOrders"])
    settings = replace(settings, sources={**settings.sources, "work24": w24})
    storage = Storage(":memory:")
    storage.upsert_project(make_project(platform="work24", external_id="35325"))
    (source,) = cli.build_sources(settings, "work24", False, storage)
    assert isinstance(source, Work24Source)
    assert source._known("35325") and not source._known("1")
    (fresh,) = cli.build_sources(settings, "work24", False)  # без базы — всё новое
    assert not fresh._known("35325")
    storage.close()


# ── launchd ──────────────────────────────────────────────────────────────────


def test_install_script_renders_valid_plist_without_touching_launchctl():
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "install_launchd.sh"), "--print"],
        capture_output=True, check=True, cwd=ROOT,
    )
    plist = plistlib.loads(result.stdout)
    assert plist["Label"] == "local.freelance-bot"
    assert plist["KeepAlive"] is True and plist["RunAtLoad"] is True
    assert plist["ProgramArguments"][-3:] == ["-m", "freelance_bot", "run"]
    assert plist["ProgramArguments"][0].endswith(".venv/bin/python")
    assert plist["StandardOutPath"].endswith("logs/freelance-bot.log")
    assert plist["WorkingDirectory"] == str(ROOT)


@pytest.mark.parametrize("name", ["install_launchd.sh", "uninstall_launchd.sh"])
def test_launchd_scripts_are_valid_bash(name):
    subprocess.run(["bash", "-n", str(ROOT / "scripts" / name)], check=True)


def _vacancy(make_project, env):
    from freelance_bot.scoring import score_project

    v = make_project(
        platform="hh", external_id="t1", url="https://my.hh.ru/b/test001",
        title="Python-разработчик (FastAPI)", description="Обязанности: backend на FastAPI.",
        budget_max=None, offers_count=None, category="Вакансия HH.ru",
        extra={"kind": "vacancy", "company": "Ромашка", "area": "Москва",
               "salary": "от 150 000 ₽"},
    )  # fmt: skip
    env.storage.upsert_project(v)
    env.storage.save_score("hh", "t1", score_project(v, env.settings, NOW))
    return v


async def test_pick_vacancy_sends_card_with_link_and_no_draft(env, make_project):
    _vacancy(make_project, env)
    update, message, query = update_from(OWNER, "pick:hh:t1")

    await env.handlers.button(update, None)

    reply = message.replies[0]
    assert reply.startswith("💼 <b>Python-разработчик (FastAPI)</b>\n🏢 Ромашка · 📍 Москва")
    assert "💵 зарплата: от 150 000 ₽" in reply and "«✉️ Письмо работодателю»" in reply
    rows = message.markups[0].inline_keyboard
    assert rows[0][0].url == "https://my.hh.ru/b/test001"
    assert [b.callback_data for b in rows[1]] == ["letter:hh:t1"]
    assert [b.callback_data for b in rows[2]] == ["applied:hh:t1", "skip:hh:t1"]
    assert env.storage.latest_response("hh", "t1") is None  # черновик не создавался

    for action in ("offer", "redraft"):
        update, message, query = update_from(OWNER, f"{action}:hh:t1")
        await env.handlers.button(update, None)
        assert "Письмо работодателю" in query.answers[0] and message.replies == []
    assert env.storage.get_flag(AWAITING_OFFER_FLAG) in (None, "")


async def test_last_lists_vacancy_with_company_and_salary(env, make_project):
    _vacancy(make_project, env)
    update, message, _ = update_from(OWNER)

    await env.handlers.last(update, None)

    text = "\n".join(message.replies)
    assert "Python-разработчик (FastAPI)" in text
    assert "🏢 Ромашка · 📍 Москва · 💵 от 150 000 ₽" in text
    update, message, _ = update_from(OWNER, "applied:hh:t1")
    await env.handlers.button(update, None)
    assert "Вакансия убрана из /last" in message.replies[0]


def test_freelance_ru_and_workzilla_sources_check_their_own_platform(make_project):
    from dataclasses import replace

    from freelance_bot.sources.freelance_ru import FreelanceRuSource
    from freelance_bot.sources.workzilla import WorkzillaSource

    settings = load_settings(ROOT / "settings.toml", ROOT / "no-such.env")
    on = {n: replace(settings.sources[n], enabled=True) for n in ("freelance_ru", "workzilla")}
    settings = replace(settings, sources={**settings.sources, **on})
    storage = Storage(":memory:")
    storage.upsert_project(make_project(platform="freelance_ru", external_id="13419"))
    storage.upsert_project(make_project(platform="work24", external_id="27"))
    frl, wz = (cli.build_sources(settings, name, False, storage)[0]
               for name in ("freelance_ru", "workzilla"))  # fmt: skip
    assert isinstance(frl, FreelanceRuSource) and isinstance(wz, WorkzillaSource)
    assert frl._known("13419") and not frl._known("27")
    assert not wz._known("27")  # id другой площадки — не «известное» задание
    storage.close()


def test_max_age_flag_overrides_platform_own_age(tmp_path):
    import argparse

    args = argparse.Namespace(
        settings=str(ROOT / "settings.toml"), env_file=str(tmp_path / "none.env"), db=None,
        dry_run=True, max_age_hours=48.0,
    )  # fmt: skip
    s = cli._load(args)
    assert s.max_age_for("superjob") == s.max_age_for("fl") == 48


async def test_nofit_asks_reason_learns_and_explains(env, make_project):
    from freelance_bot.scoring import score_project

    v = make_project(
        platform="superjob", external_id="s1", url="https://www.superjob.ru/vakansii/s1.html",
        title="Бизнес-аналитик", description="Python, SQL, отчёты.", published_at=NOW,
        budget_min=60000, budget_max=None, offers_count=None,
        extra={"company": "Ромашка", "area": "Москва"},
    )  # fmt: skip
    env.storage.upsert_project(v)
    update, message, query = update_from(OWNER, "nofit:superjob:s1")
    await env.handlers.button(update, None)
    assert env.storage.get_status("superjob", "s1") == "skipped"  # пропуск сразу
    assert query.markup_cleared
    assert message.replies == [
        "Чем не подходит «Бизнес-аналитик»? Выберите причину — я запомню и поменяю отбор."
    ]
    reasons = [b.callback_data for row in message.markups[0].inline_keyboard for b in row]
    assert reasons == [
        "nf-topic:superjob:s1", "nf-skills:superjob:s1", "nf-pay:superjob:s1",
        "nf-office:superjob:s1", "nf-company:superjob:s1", "nf-level:superjob:s1",
    ]  # fmt: skip

    update, message, query = update_from(OWNER, "nf-level:superjob:s1")
    await env.handlers.button(update, None)
    assert query.alert and "нет уровня" in query.answers[0]  # нечего запомнить — подсказка
    assert query.edited == [] and env.storage.lessons(OWNER) == []

    # такая же вакансия ждёт дайджеста — после отметки она в него не попадёт
    twin = replace_project(v, external_id="s2", title="Системный аналитик")
    env.storage.upsert_project(twin)
    env.storage.set_status("superjob", "s2", "queued")
    env.storage.save_score("superjob", "s2", score_project(twin, env.settings, NOW))
    update, message, query = update_from(OWNER, "nf-topic:superjob:s1")
    await env.handlers.button(update, None)
    assert query.answers == ["Запомнил"]
    [text] = query.edited
    assert text.startswith("🧠 <b>Запомнил</b> — 🧭 Не моя тема:")
    assert "• «аналитик» в названии: −15 к баллу" in text
    assert "Из ближайшего списка новых убрал похожих: 1." in text
    assert "/learned" in text
    assert {x.value for x in env.storage.lessons(OWNER)} == {"бизнес", "аналитик"}
    assert env.storage.get_status("superjob", "s2") == "filtered_out"


def replace_project(p, **kw):
    from dataclasses import replace

    return replace(p, **kw)


async def test_learned_lists_lessons_and_forgets_them(env):
    update, message, _ = update_from(OWNER)
    await env.handlers.learned(update, None)
    assert "Пока отметок нет" in message.replies[0]
    assert "👎 Не подходит" in message.replies[0]  # объяснение, как учить бота

    env.storage.save_lesson(OWNER, "topic", "аналитик", -20, "Бизнес-аналитик")
    env.storage.save_lesson(OWNER, "company", "ромашка", 0, "Аналитик")
    update, message, _ = update_from(OWNER, text="🧠 Обучение")  # кнопка меню
    await env.handlers.text(update, None)
    assert "1. вакансии компании «ромашка» не присылаю" in message.replies[0]
    assert "2. «аналитик» в названии: −20 к баллу" in message.replies[0]
    buttons = [b.callback_data for row in message.markups[0].inline_keyboard for b in row]
    first = env.storage.lessons(OWNER)[0].id
    second = env.storage.lessons(OWNER)[1].id
    assert buttons == [f"unlearn:{first}", f"unlearn:{second}", "unlearn:all"]

    update, _, query = update_from(OWNER, f"unlearn:{first}")
    await env.handlers.button(update, None)
    assert query.answers == ["Забыл"] and "ромашка" not in query.edited[0]
    assert [x.value for x in env.storage.lessons(OWNER)] == ["аналитик"]

    update, _, query = update_from(OWNER, "unlearn:all")
    await env.handlers.button(update, None)
    assert env.storage.lessons(OWNER) == [] and "Пока отметок нет" in query.edited[0]

    update, _, query = update_from(999, "unlearn:all")  # чужой — отказ
    await env.handlers.button(update, None)
    assert query.edited == []


async def test_filters_and_menu_tell_how_learning_works(env):
    env.storage.save_lesson(OWNER, "office", "", -15, "Аналитик")
    update, message, _ = update_from(OWNER)
    await env.handlers.filters_(update, None)
    assert "🧠 Правил из ваших отметок: 1" in message.replies[0]
    update, message, _ = update_from(OWNER)
    await env.handlers.menu(update, None)
    assert "👎 Не подходит" in message.replies[0] and "🧠 Чему я научился" in message.replies[0]


async def test_letter_button_writes_letter_then_another(env, make_project):
    """«✉️ Письмо работодателю»: без анкеты — по profile.toml; повторно — другой вариант."""
    from freelance_bot.profile import load_profile

    _vacancy(make_project, env)
    update, message, query = update_from(OWNER, "letter:hh:t1")
    await env.handlers.button(update, None)  # ни анкеты, ни профиля — писать не из чего
    assert "Нет резюме" in query.answers[0] and message.replies == []

    env.handlers.pipeline.drafter.profile = load_profile(ROOT / "profile/profile.example.toml")
    update, message, query = update_from(OWNER, "letter:hh:t1")
    await env.handlers.button(update, None)
    assert query.answers[0] == "Пишу письмо…"
    first = message.replies[0]
    assert first.startswith("✉️ <b>Письмо:</b> Python-разработчик (FastAPI)")
    assert "<pre>Здравствуйте!" in first and "шаблон по вашему резюме" in first
    rows = message.markups[0].inline_keyboard
    assert rows[0][0].url == "https://my.hh.ru/b/test001"
    assert rows[1][0].text == "🔄 Другое письмо" and rows[1][0].callback_data == "letter:hh:t1"

    update, message, query = update_from(OWNER, "letter:hh:t1")
    await env.handlers.button(update, None)
    second = message.replies[0]
    assert second.startswith("🔄 <b>Письмо, вариант 2:</b>") and second != first


# ── проверка 2026-10-25: расписание и размер списка новых ────────────────────────────


def _owner_anketa(storage, schedule):
    storage.save_user(OWNER, "done", {"full_name": "Иванов Иван", "schedule": schedule,
                                      "platforms": ["fl"], "directions": ["backend"]},
                      consent=True, completed=True)  # fmt: skip


async def test_owner_digest_follows_anketa_schedule(env):
    """В анкете «в 10:00 и 18:00» — список в 21:00 не приходит, приходит в 10:00."""
    _owner_anketa(env.storage, {"times": ["10:00", "18:00"]})
    h = env.handlers
    sent = []

    async def send(text, keyboard):
        sent.append(text)

    msk = lambda hour, day=25: datetime(2026, 10, day, hour - 3, 0, tzinfo=UTC)  # noqa: E731
    assert await h.digest_tick(send, msk(18) + timedelta(minutes=5)) is None  # первый раз
    assert await h.digest_tick(send, msk(21)) is None  # не по расписанию анкеты
    assert await h.digest_tick(send, msk(10, 26)) == 0  # пора: в очереди пусто
    assert await h.digest_tick(send, msk(10, 26) + timedelta(minutes=1)) is None
    assert h.digest_when() == "в 10:00 и 18:00"


async def test_digest_without_anketa_uses_settings_grid(env):
    h = env.handlers
    env.storage.set_flag("digest_last_at", datetime(2026, 10, 25, 8, 5, tzinfo=UTC).isoformat())

    async def send(text, keyboard): ...

    assert await h.digest_tick(send, datetime(2026, 10, 25, 8, 50, tzinfo=UTC)) is None
    assert await h.digest_tick(send, datetime(2026, 10, 25, 9, 0, tzinfo=UTC)) == 0
    assert h.digest_when() == "раз в час"


async def test_digest_is_capped_and_rest_waits(env, make_project):
    from freelance_bot.bot import DIGEST_LIMIT

    for i in range(DIGEST_LIMIT + 5):
        env.storage.upsert_project(make_project(external_id=str(100 + i),
                                                url=f"https://www.fl.ru/projects/{100 + i}/"))
        env.storage.set_status("fl", str(100 + i), "queued")
    pages = []

    async def send(text, keyboard):
        pages.append((text, keyboard))

    assert await env.handlers.send_digest(send) == DIGEST_LIMIT
    text = "\n".join(t for t, _ in pages)
    assert f"Новые предложения</b> — {DIGEST_LIMIT}" in text
    assert "Ещё новых: 5 — пришлю в следующем списке" in pages[-1][0]
    assert len(env.storage.projects_with_status("queued", NOW - timedelta(days=7))) == 5
    assert await env.handlers.send_digest(send) == 5


def test_menu_names_live_sources_and_schedule():
    from freelance_bot.telegram_ui import format_start

    text = format_start("10:00", ["fl", "work24", "superjob", "trudvsem", "tgchannels"],
                        "в 10:00 и 18:00")  # fmt: skip
    assert "заказы на FL.ru и Work24 и вакансии (SuperJob, Работа России и Telegram-каналы)" in text
    assert "Kwork" not in text and "Freelance.ru" not in text
    assert "⏰ Новые присылаю сам в 10:00 и 18:00, следующий список — 10:00." in text
    assert "цену со сроком для формы" not in text


def test_vacancy_list_hint_has_no_price(make_project):
    from freelance_bot.telegram_ui import format_offer_list

    vacancy = make_project(platform="superjob", external_id="1", extra={"area": "Липецк"})
    text = format_offer_list([(vacancy, 50)], "Europe/Moscow")
    assert "письмом работодателю" in text and "готовый отклик" not in text
    mixed = format_offer_list([(vacancy, 50), (make_project(), 50)], "Europe/Moscow")
    assert "к вакансии — письмо работодателю" in mixed


async def test_owner_lessons_belong_to_his_resume(env, make_project):
    """Отметка владельца при пройденной анкете — для её резюме; без анкеты — profile.toml."""
    env.storage.save_user(OWNER, "done", {
        "full_name": "Иванов Иван", "directions": [], "platforms": ["superjob"],
        "profile": {"profession": "Повар", "city": "Воронеж", "experience_years": 3},
    }, consent=True, completed=True)  # fmt: skip
    p = make_project(platform="superjob", external_id="9", title="Повар",
                     extra={"area": "Воронеж", "company": "Кафе"})  # fmt: skip
    env.storage.upsert_project(p)
    update, message, query = update_from(OWNER, callback_data="nf-company:superjob:9")
    await env.handlers.button(update, None)
    assert [x.value for x in env.storage.lessons(OWNER, "Повар")] == ["кафе"]
    assert env.storage.lessons(OWNER) == []
    update, message, _ = update_from(OWNER, text="/learned")
    await env.handlers.learned(update, None)
    assert "Для резюме «Повар»" in message.replies[0] and "кафе" in message.replies[0]
