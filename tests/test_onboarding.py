"""Анкета публичного бота через BotHandlers на фейковых апдейтах. Сети нет."""

from __future__ import annotations

import io
import zipfile
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from freelance_bot.bot import DENIED_TEXT, BotHandlers
from freelance_bot.config import load_settings
from freelance_bot.models import Project
from freelance_bot.onboarding import (
    BUTTON_NO_RESUME,
    EXPERIENCE_ERROR,
    FORGOTTEN_TEXT,
    FREQUENCY_ERROR,
    MATCHES_HINT,
    MATCHES_NONE,
    MAX_ANALYSES_PER_DAY,
    NAME_ERROR,
    NAME_TEXT,
    PUBLIC_HELP,
    RESUME_LIMIT,
    STALE_TEXT,
    address_options,
    first_matches,
    format_profile,
    parse_address,
    parse_full_name,
    parse_years,
    user_settings,
)
from freelance_bot.storage import Storage

ROOT = Path(__file__).resolve().parent.parent
OWNER = 4242
GUEST = 777
NOW = datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
RESUME = (
    "Опыт работы — 3 года 6 месяцев\n"
    "Backend-разработчик на Python. Писал сервисы на FastAPI и Django, REST API, "
    "интеграции с CRM, парсинг сайтов на Scrapy, PostgreSQL, Redis, Docker, Celery.\n"
    "— Делал интеграции с внешними системами и автоматизацию отчётов\n"
    "— Писал парсеры маркетплейсов и выгрузку данных в Google Sheets\n"
    "Навыки: Python, FastAPI, Django, PostgreSQL, Scrapy, Docker, Linux, Git"
)


class Message:
    def __init__(self, text=None, document=None):
        self.text = text
        self.document = document
        self.replies: list[str] = []
        self.markups: list = []

    async def reply_text(self, text, **kwargs):
        self.replies.append(text)
        self.markups.append(kwargs.get("reply_markup"))

    async def reply_chat_action(self, action):
        pass


class Query:
    def __init__(self, data, message):
        self.data = data
        self.message = message
        self.alerts: list[str] = []
        self.markup = "unchanged"

    async def answer(self, text=None, show_alert=False):
        if show_alert:
            self.alerts.append(text)

    async def edit_message_reply_markup(self, reply_markup=None):
        self.markup = reply_markup


class Chat:
    """Диалог одного человека с ботом: все ответы бота по порядку."""

    def __init__(self, handlers: BotHandlers, user_id: int, chat_type: str = "private"):
        self.h = handlers
        self.user_id = user_id
        self.chat_type = chat_type
        self.replies: list[str] = []
        self.last_markup = None
        self.last_query: Query | None = None

    def _update(self, message, query=None):
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=self.user_id),
            effective_chat=SimpleNamespace(type=self.chat_type),
            effective_message=message,
            callback_query=query,
        )

    def _collect(self, message):
        self.replies += message.replies
        if message.markups:
            self.last_markup = message.markups[-1]

    async def command(self, name):
        message = Message(f"/{name}")
        handler = getattr(self.h, name if name != "filters" else "filters_")
        await handler(self._update(message), None)
        self._collect(message)
        return message.replies

    async def say(self, text):
        message = Message(text)
        await self.h.text(self._update(message), None)
        self._collect(message)
        return message.replies

    async def send_file(self, data: bytes, name: str):
        async def download_as_bytearray():
            return bytearray(data)

        async def get_file():
            return SimpleNamespace(download_as_bytearray=download_as_bytearray)

        doc = SimpleNamespace(file_name=name, file_size=len(data), get_file=get_file)
        message = Message(document=doc)
        await self.h.fallback(self._update(message), None)
        self._collect(message)
        return message.replies

    async def press(self, data):
        message = Message()
        query = Query(data, message)
        self.last_query = query
        await self.h.button(self._update(message, query), None)
        self._collect(message)
        return message.replies

    def buttons(self) -> list[tuple[str, str]]:
        markup = self.last_markup
        return [(b.text, b.callback_data) for row in markup.inline_keyboard for b in row]


def _sources_on(settings, off: tuple[str, ...] = ()):
    """Площадки анкеты включены или выключены явно — не по settings.toml."""
    return {
        k: replace(v, enabled=k not in off) if k in settings.public_platforms else v
        for k, v in settings.sources.items()
    }


@pytest.fixture
def env():
    settings = load_settings(ROOT / "settings.toml", ROOT / "no-such.env")
    settings = replace(settings, owner_id=OWNER, public_enabled=True, sources=_sources_on(settings))
    storage = Storage(":memory:", clock=lambda: NOW)
    handlers = BotHandlers(settings, storage, None)
    yield SimpleNamespace(settings=settings, storage=storage, handlers=handlers)
    storage.close()


async def _to_resume_step(chat: Chat) -> None:
    await chat.command("start")
    await chat.press("ob:agree")
    await chat.say("Петров Пётр Петрович")
    await chat.press("ob:addr:1")


async def test_full_flow_with_pasted_resume(env):
    chat = Chat(env.handlers, GUEST)
    replies = await chat.command("start")
    assert "Согласны на обработку" in replies[0]
    assert "нейросети" not in replies[0]  # ключа нет — текст резюме никуда не уходит
    assert env.storage.get_user(GUEST).consent_at is None  # согласия ещё нет

    assert await chat.press("ob:agree") == [NAME_TEXT]
    assert env.storage.get_user(GUEST).consent_at is not None
    assert await chat.say("Пётр") == [NAME_ERROR]
    replies = await chat.say("Петров  Пётр Петрович")
    assert replies == ["Как к вам обращаться?"]
    assert [t for t, _ in chat.buttons()] == ["Пётр", "Пётр Петрович", "✏️ Свой вариант"]

    replies = await chat.press("ob:addr:1")
    assert replies[0].startswith("Пётр Петрович, пришлите резюме")

    replies = await chat.say(RESUME)
    assert replies[0].startswith("Читаю резюме")
    review = replies[-1]
    assert "Вот что я понял из резюме" in review and "3,5 года" in review
    assert "FastAPI" in review
    user = env.storage.get_user(GUEST)
    assert user.step == "review" and "backend" in user.data["directions"]
    assert user.data["profile"]["generator"] == "builtin"

    replies = await chat.press("ob:ok")
    assert "Где искать?" in replies[0] and "HH.ru — вакансии" in replies[0]
    assert ("✅ HH.ru (вакансии)", "ob:plat:hh") in chat.buttons()  # всё включено сразу
    assert all("kwork" not in data for _, data in chat.buttons())  # Kwork не предлагаем

    await chat.press("ob:plat:hh")  # выключить HH
    assert ("⬜ HH.ru (вакансии)", "ob:plat:hh") in [
        (b.text, b.callback_data) for row in chat.last_query.markup.inline_keyboard for b in row
    ]
    replies = await chat.press("ob:plat_done")  # SuperJob — вакансии: спросить о городах
    assert "но она удалённая" in replies[0]  # города в резюме нет — о других не спрашивать
    assert chat.buttons() == [("✅ Удалённая работа", "ob:loc:remote"), ("Готово ➡️", "ob:loc_done")]
    replies = await chat.press("ob:loc_done")
    assert replies[0].startswith("Как часто присылать") and "МСК" in replies[0]
    assert [d for _, d in chat.buttons()] == [
        "ob:freq:hourly",
        "ob:freq:twice",
        "ob:freq:daily",
        "ob:freq_own",
    ]
    assert env.storage.get_user(GUEST).completed_at is None  # анкета ещё не пройдена
    replies = await chat.press("ob:freq:hourly")
    assert replies[0].startswith("Готово, Пётр Петрович!")
    assert "Площадки: FL.ru, Freelance.ru, Work24, SuperJob" in replies[0]
    assert "⏰ Присылать: раз в час, 9:00–21:00 (МСК)" in replies[0]
    user = env.storage.get_user(GUEST)
    assert user.data["schedule"] == {"every_min": 60, "start_hour": 9, "end_hour": 21}
    assert user.step == "done" and user.completed_at is not None
    assert user.data["full_name"] == "Петров Пётр Петрович"
    assert user.data["platforms"] == [
        "fl", "freelance_ru", "work24", "superjob", "trudvsem", "tgchannels"
    ]
    assert "Петров Пётр Петрович" in env.storage.users()[0].data["full_name"]
    assert len(replies) == 2 and replies[1].startswith(MATCHES_NONE)  # база пуста
    # посторонним рассылки по расписанию ещё нет — так и сказано, со ссылкой на кнопку
    assert "Сам присылать новые раз в час, 9:00–21:00 (МСК) начну, когда включим" in replies[1]
    assert "«🔎 Заказы по анкете» в /start" in replies[1]


def _project(platform, external_id, title, hours_ago, description=""):
    return Project(
        platform=platform,
        external_id=external_id,
        url=f"https://example.com/{platform}/{external_id}",
        title=title,
        description=description or title,
        published_at=datetime.now(UTC) - timedelta(hours=hours_ago),
        budget_min=None,
        budget_max=20000,
        offers_count=2,
        category=None,
    )


async def _finish_signup(chat: Chat, direction: str = "backend") -> list[str]:
    await _to_resume_step(chat)
    await chat.press("ob:noresume")
    await chat.press(f"ob:dir:{direction}")
    await chat.press("ob:dir_done")
    await chat.press("ob:plat_all")
    await chat.press("ob:loc_done")
    return await chat.press("ob:freq:hourly")


async def test_first_matches_sent_right_after_signup(env):
    backend = "Бэкенд на Python: FastAPI, PostgreSQL, Docker"
    for p in (
        _project("fl", "1", backend, 30),
        _project("work24", "2", "REST API на Django и Redis", 2),
        _project("kwork", "3", backend, 1),  # Kwork в анкете не предлагается
        _project("hh", "4", "Python backend-разработчик", 1),  # подборка владельца
        _project("fl", "5", "Спарсить каталог через Scrapy", 1),  # не выбранное направление
        _project("fl", "6", backend, 24 * 9),  # старше недели
    ):
        env.storage.upsert_project(p)
    env.storage.set_status("work24", "2", "applied")  # статус владельца чужим не мешает

    replies = await _finish_signup(Chat(env.handlers, GUEST))
    assert replies[0].startswith("Готово, Пётр Петрович!")
    matches = replies[1]
    assert "Заказы и вакансии под вашу анкету</b> — 2" in matches and MATCHES_HINT in matches
    assert matches.index("REST API на Django") < matches.index(backend)  # свежие сверху
    for external_id in ("3", "4", "5", "6"):
        assert f'/{external_id}"' not in matches
    assert "Выберите кнопкой" not in matches  # готовых откликов посторонним пока нет


async def test_first_matches_failure_keeps_signup(env):
    env.storage.upsert_project(_project("fl", "1", "Бэкенд на Python и FastAPI", 1))

    async def broken(projects):
        raise RuntimeError("нейросеть недоступна")

    env.handlers.onboarding.summarize = broken
    replies = await _finish_signup(Chat(env.handlers, GUEST))
    assert len(replies) == 1 and replies[0].startswith("Готово")
    assert env.storage.get_user(GUEST).completed_at is not None


async def test_matches_button_lists_all_and_owner_gets_own_hh(env):
    for i in range(30):
        long_title = f"Бэкенд на Python и FastAPI, PostgreSQL, Docker — сервис №{i} " + "х" * 150
        env.storage.upsert_project(_project("fl", str(i), long_title, i + 1))
    env.storage.upsert_project(_project("hh", "vac", "Python backend-разработчик", 0.5))

    guest = Chat(env.handlers, GUEST)
    await _finish_signup(guest)
    await guest.command("start")
    assert ("🔎 Заказы по анкете", "ob:matches") in guest.buttons()
    pages = await guest.press("ob:matches")
    assert len(pages) > 1 and all(len(p) <= 4096 for p in pages)  # не влезает в одно
    text = "\n".join(pages)
    assert "Заказы и вакансии под вашу анкету</b> — 20" in pages[0] and "<b>20.</b>" in pages[-1]
    assert "/hh/vac" not in text  # подборка HH владельца посторонним не видна
    assert "когда включим рассылку" in pages[-1]
    assert guest.last_query.markup == "unchanged"  # кнопку можно нажать ещё раз

    owner = Chat(env.handlers, OWNER)
    await _finish_signup(owner)
    await owner.command("start")
    pages = await owner.press("ob:matches")
    assert "/hh/vac" in pages[0]  # свою HH владелец видит


async def test_disabled_source_is_not_offered_shown_or_matched(env):
    env.storage.upsert_project(_project("hh", "vac", "Python backend-разработчик", 0.5))
    env.storage.upsert_project(_project("fl", "1", "Бэкенд на Python и FastAPI", 1))
    owner = Chat(env.handlers, OWNER)
    await _finish_signup(owner)  # анкета с HH, пока он был включён
    assert "hh" in env.storage.get_user(OWNER).data["platforms"]

    settings = replace(env.settings, sources=_sources_on(env.settings, off=("hh",)))
    owner = Chat(BotHandlers(settings, env.storage, None), OWNER)
    replies = await owner.command("start")
    assert "🌐 Площадки:" in replies[0] and "HH.ru" not in replies[0]
    pages = await owner.press("ob:matches")
    assert "/fl/1" in pages[0] and "/hh/vac" not in pages[0]  # старые вакансии HH не видны

    await owner.press("ob:restart")
    await owner.say("Петров Пётр Петрович")
    await owner.press("ob:addr:1")
    await owner.press("ob:noresume")
    await owner.press("ob:dir:backend")
    replies = await owner.press("ob:dir_done")
    assert all("hh" not in data for _, data in owner.buttons())  # выключенный не предлагается
    assert "HH.ru — вакансии" in replies[0]  # SuperJob ещё включён — про вакансии пишем


def test_user_settings_take_directions_and_experience_from_signup(env):
    data = {"directions": ["parsing"], "profile": {"experience_years": 3.5}}
    own = user_settings(data, env.settings)
    assert list(own.directions) == ["parsing"] and own.experience_years == 3.5
    assert user_settings({"directions": []}, env.settings).experience_years == 0.0
    assert first_matches({"directions": [], "platforms": ["fl"]}, env.settings, env.storage,
                         datetime.now(UTC)) == []  # fmt: skip


def test_first_matches_use_only_this_users_lessons(env, make_project):
    now = datetime.now(UTC)
    p = make_project(external_id="m1", published_at=now - timedelta(hours=1))
    env.storage.upsert_project(p)
    data = {"directions": ["backend", "telegram"], "platforms": ["fl"]}
    found = first_matches(data, env.settings, env.storage, now, tg_id=GUEST)
    assert [x.external_id for x, _ in found] == ["m1"]
    env.storage.save_lesson(GUEST + 1, "pay", "order", 1_000_000, "чужое")
    assert first_matches(data, env.settings, env.storage, now, tg_id=GUEST) == found
    env.storage.save_lesson(GUEST, "pay", "order", 1_000_000, "своё")
    assert first_matches(data, env.settings, env.storage, now, tg_id=GUEST) == []


async def test_decline_saves_nothing(env):
    chat = Chat(env.handlers, GUEST)
    await chat.command("start")
    replies = await chat.press("ob:decline")
    assert "ничего не сохраняю" in replies[0]
    assert env.storage.get_user(GUEST) is None


async def test_custom_address(env):
    chat = Chat(env.handlers, GUEST)
    await chat.command("start")
    await chat.press("ob:agree")
    await chat.say("Ли Мин")
    assert [t for t, _ in chat.buttons()][:2] == ["Мин", "Ли"]
    await chat.press("ob:addr_own")
    assert await chat.say("<script>") == ["Нужно 1–40 символов: буквы, пробел, дефис или точка."]
    replies = await chat.say("Мин Ли")
    assert replies[0].startswith("Мин Ли, пришлите резюме")


async def test_without_resume_directions_are_chosen_by_hand(env):
    chat = Chat(env.handlers, GUEST)
    await _to_resume_step(chat)
    assert await chat.say("привет") == [
        f"Жду резюме: файл, ссылку или текст. Или кнопка «{BUTTON_NO_RESUME}»."
    ]
    replies = await chat.press("ob:noresume")
    assert "Какие заказы искать?" in replies[0]
    await chat.press("ob:dir_done")
    assert chat.last_query.alerts == ["Выберите хотя бы одно направление"]
    await chat.press("ob:dir:parsing")
    await chat.press("ob:dir:nonsense")  # чужое значение ничего не меняет
    assert env.storage.get_user(GUEST).data["directions"] == ["parsing"]
    await chat.press("ob:dir_done")
    await chat.press("ob:plat_all")
    await chat.press("ob:loc_done")
    assert "Напишите своё расписание" in (await chat.press("ob:freq_own"))[0]
    assert await chat.say("иногда") == [FREQUENCY_ERROR]
    assert await chat.say("каждые 5 минут") == [FREQUENCY_ERROR]  # чаще 15 минут нельзя
    replies = await chat.say("каждые 30 минут с 8 до 23")
    assert "Площадки: FL.ru, Freelance.ru, Work24, HH.ru, SuperJob" in replies[0]
    assert "⏰ Присылать: каждые 30 минут, 8:00–23:00 (МСК)" in replies[0]
    user = env.storage.get_user(GUEST)
    assert user.data["profile"] is None and user.completed_at is not None


async def test_platforms_cannot_be_empty(env):
    chat = Chat(env.handlers, GUEST)
    await _to_resume_step(chat)
    await chat.press("ob:noresume")
    await chat.press("ob:dir:backend")
    await chat.press("ob:dir_done")
    for platform in env.settings.public_platforms:
        await chat.press(f"ob:plat:{platform}")
    await chat.press("ob:plat:kwork")  # не из списка анкеты — не включается
    await chat.press("ob:plat_done")
    assert chat.last_query.alerts == ["Выберите хотя бы одну площадку"]
    assert env.storage.get_user(GUEST).step == "platforms"


def _docx(text: str) -> bytes:
    w = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = "".join(f"<w:p><w:r><w:t>{line}</w:t></w:r></w:p>" for line in text.splitlines())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<w:document xmlns:w="{w}"><w:body>{body}</w:body>'
                                        "</w:document>")  # fmt: skip
    return buf.getvalue()


async def test_resume_file_and_bad_file(env):
    chat = Chat(env.handlers, GUEST)
    await _to_resume_step(chat)
    replies = await chat.send_file(b"\xd0\xcf\x11\xe0" * 100, "resume.doc")
    assert "Формат .doc не читаю" in replies[-1]
    assert env.storage.get_user(GUEST).step == "resume"  # можно прислать снова
    replies = await chat.send_file(_docx(RESUME), "resume.docx")
    assert "Вот что я понял из резюме" in replies[-1]
    replies = await chat.press("ob:reupload")
    assert "пришлите резюме" in replies[0]


async def test_experience_can_be_corrected_on_review(env):
    chat = Chat(env.handlers, GUEST)
    await _to_resume_step(chat)
    await chat.say(RESUME)
    assert "3,5 года" in chat.replies[-1]
    assert ("✏️ Стаж", "ob:exp") in chat.buttons()
    assert "коммерческого опыта" in (await chat.press("ob:exp"))[0]
    assert await chat.say("много") == [EXPERIENCE_ERROR]
    replies = await chat.say("1 год 6 месяцев")
    assert "Вот что я понял" in replies[0] and "⏳ Стаж: 1,5 года" in replies[0]
    user = env.storage.get_user(GUEST)
    assert user.step == "review" and user.data["profile"]["experience_years"] == 1.5
    assert "FastAPI" in user.data["profile"]["keywords"]  # остальное из резюме не потеряно


def test_parse_years():
    assert parse_years("1,5") == 1.5
    assert parse_years("1.5 года") == 1.5
    assert parse_years("2 года") == 2.0
    assert parse_years("8 месяцев") == 0.7
    assert parse_years("1 год 3 мес") == 1.2
    assert parse_years("0") == 0.0
    assert parse_years("") is None and parse_years("много") is None
    assert parse_years("99") is None


async def test_hh_link_gets_download_hint(env):
    chat = Chat(env.handlers, GUEST)
    await _to_resume_step(chat)
    replies = await chat.say("вот https://hh.ru/resume/abc123")
    assert "условия hh.ru запрещают" in replies[-1]
    assert env.storage.get_user(GUEST).step == "resume"


async def test_daily_limit_of_resume_analyses(env):
    chat = Chat(env.handlers, GUEST)
    await _to_resume_step(chat)
    for _ in range(MAX_ANALYSES_PER_DAY):
        await chat.say(RESUME)
        await chat.press("ob:reupload")
    assert await chat.say(RESUME) == [RESUME_LIMIT]


async def test_resume_is_parsed_in_background_when_spawn_is_set(env):
    spawned = []
    env.handlers.onboarding.spawn = spawned.append
    chat = Chat(env.handlers, GUEST)
    await _to_resume_step(chat)
    await chat.say(RESUME)
    assert env.storage.get_user(GUEST).step == "analyzing"
    assert await chat.say("ну что?") == ["Ещё разбираю резюме — подождите немного."]
    await spawned[0]
    assert env.storage.get_user(GUEST).step == "review"


async def test_stale_button_repeats_current_question(env):
    chat = Chat(env.handlers, GUEST)
    await chat.command("start")
    await chat.press("ob:agree")
    replies = await chat.press("ob:plat_done")  # кнопка не этого шага
    assert replies == [STALE_TEXT, NAME_TEXT]


async def test_restart_and_forget(env):
    chat = Chat(env.handlers, GUEST)
    await _to_resume_step(chat)
    await chat.press("ob:noresume")
    await chat.press("ob:dir:backend")
    await chat.press("ob:dir_done")
    await chat.press("ob:plat_all")
    await chat.press("ob:loc_done")
    await chat.press("ob:freq:hourly")
    replies = await chat.command("start")  # пройдена — показывает анкету
    assert "Петров Пётр Петрович" in replies[0]
    assert [d for _, d in chat.buttons()] == [
        "ob:matches",
        "ob:loc_edit",
        "ob:freq_edit",
        "ob:restart",
        "ob:forget",
    ]
    assert await chat.say("что дальше?") == [PUBLIC_HELP]

    # сменить частоту: анкета остаётся пройденной, остальное не спрашивается
    done_at = env.storage.get_user(GUEST).completed_at
    assert "Как часто присылать" in (await chat.press("ob:freq_edit"))[0]
    assert await chat.press("ob:freq:twice") == ["⏰ Готово, буду присылать в 10:00 и 18:00 (МСК)."]
    user = env.storage.get_user(GUEST)
    assert user.step == "done" and user.completed_at == done_at and "editing" not in user.data
    assert user.data["schedule"] == {"times": ["10:00", "18:00"]}

    assert await chat.press("ob:restart") == [NAME_TEXT]
    user = env.storage.get_user(GUEST)
    assert user.completed_at is None and user.consent_at is not None  # согласие не спрашиваем

    await chat.command("forget")
    assert await chat.press("ob:forget_yes") == [FORGOTTEN_TEXT]
    assert env.storage.get_user(GUEST) is None
    assert await chat.command("profile") == ["О вас ничего не сохранено. Начать — /start."]


async def test_owner_commands_are_closed_for_public_users(env):
    chat = Chat(env.handlers, GUEST)
    for command in ("status", "stats", "pause", "last", "filters", "signup"):
        assert await chat.command(command) == [PUBLIC_HELP]
    assert env.storage.get_flag("paused") is None
    await chat.press("applied:fl:1")
    assert chat.last_query.alerts == [PUBLIC_HELP]


async def test_group_chat_and_disabled_public_mode_are_refused(env):
    group = Chat(env.handlers, GUEST, chat_type="group")
    assert await group.command("start") == [DENIED_TEXT]
    closed = BotHandlers(replace(env.settings, public_enabled=False), env.storage, None)
    chat = Chat(closed, GUEST)
    assert await chat.command("start") == [DENIED_TEXT]
    assert await chat.say("Петров Пётр") == [DENIED_TEXT]
    await chat.press("ob:agree")
    assert chat.last_query.alerts == [DENIED_TEXT]
    assert env.storage.get_user(GUEST) is None


async def test_owner_start_opens_signup_and_menu_stays(env):
    closed = BotHandlers(replace(env.settings, public_enabled=False), env.storage, None)
    owner = Chat(closed, OWNER)
    assert "Согласны на обработку" in (await owner.command("start"))[0]  # сразу анкета
    await owner.press("ob:agree")
    assert await owner.say("Сидоров Олег") == ["Как к вам обращаться?"]
    assert await owner.command("start") == ["Как к вам обращаться?"]  # продолжает анкету
    assert "👋" in (await owner.command("menu"))[0]  # меню владельца — /menu
    assert (await owner.command("signup"))[0] == NAME_TEXT  # заново
    await owner.say("Сидоров Олег")
    await owner.press("ob:addr:0")
    assert "пришлите резюме" in owner.replies[-1]
    await owner.command("forget")
    await owner.press("ob:forget_yes")
    assert env.storage.get_user(OWNER) is None


def test_name_and_address_parsing():
    assert parse_full_name("  Иванов   Иван ") == "Иванов Иван"
    assert parse_full_name("Салтыков-Щедрин Михаил Евграфович") is not None
    assert parse_full_name("Иван") is None
    assert parse_full_name("Иванов Иван 123") is None
    assert parse_full_name("a b c d e") is None
    assert address_options("Иванов Иван Иванович") == ["Иван", "Иван Иванович"]
    assert parse_address("  Ваня ") == "Ваня"
    assert parse_address("<b>") is None


def test_profile_text_escapes_resume_words(env):
    data = {
        "full_name": "Иванов Иван",
        "address": "Иван",
        "directions": ["backend"],
        "profile": {"directions": ["backend"], "keywords": ["<b>C++</b>"], "experience_years": 1,
                    "built_bots_or_agents": False, "facts": [{"text": "a & b", "source": "x"}]},
        "platforms": ["fl"],
    }  # fmt: skip
    text = format_profile(data, env.settings)
    assert "&lt;b&gt;C++&lt;/b&gt;" in text and "a &amp; b" in text and "1 год" in text


def test_profile_text_shows_profession_city_jobs_skills_education(env):
    profile = {"directions": [], "keywords": [], "experience_years": 6.8,
               "built_bots_or_agents": False, "profession": "Повар горячего цеха",
               "city": "Воронеж", "skills": ["Горячий цех", "<Санитария>"],
               "education": ["Профессиональное училище · поварское дело · 2020"],
               "jobs": [f"Повар — Кафе {i} (2020–2023)" for i in range(6)]}  # fmt: skip
    text = format_profile({"directions": [], "profile": profile}, env.settings)
    assert "💼 Профессия: Повар горячего цеха" in text and "📍 Город: Воронеж" in text
    assert "6,8 года" in text and "Горячий цех, &lt;Санитария&gt;" in text
    assert "🎓 Образование: Профессиональное училище" in text
    assert text.count("Повар — Кафе") == 4  # мест работы — не больше четырёх


async def test_cook_resume_gets_cook_vacancies_in_own_city(env):
    """Не разработчик: IT-направления и биржи не предлагаются, вакансии ищутся по профессии
    и городу из резюме, чужой город, чужая профессия и лишний стаж отсекаются."""
    cook = (ROOT / "tests" / "fixtures" / "resumes" / "08_cook.txt").read_text(encoding="utf-8")
    env.storage.upsert_project(_project("fl", "1", "Бэкенд на Python и FastAPI", 1))

    def vacancy(vid, title, area="Воронеж", description=""):
        p = _project("superjob", vid, title, 2, description or f"Обязанности: {title}")
        return replace(p, extra={"area": area})

    asked = []

    async def search(data, **_):
        asked.append(data["profile"]["profession"])
        return [
            vacancy("v1", "Повар-универсал в столовую"),
            vacancy("v2", "Повар", "Москва"),
            vacancy("v3", "Официант"),
            vacancy("v4", "Шеф-повар", description="Опыт работы от 10 лет"),
            vacancy("v5", "Повар на заготовки", "Воронеж, удалённо"),
        ]

    env.handlers.onboarding.search = search
    chat = Chat(env.handlers, GUEST)
    await _to_resume_step(chat)
    review = (await chat.say(cook))[-1]
    assert "💼 Профессия: Повар горячего цеха" in review and "📍 Город: Воронеж" in review
    assert "🎯 Ищу: вакансии «Повар горячего цеха»" in review
    replies = await chat.press("ob:ok")  # без шага IT-направлений
    assert "Где искать?" in replies[0]
    plats = [d for _, d in chat.buttons()]  # только вакансии: биржи — заказы на разработку
    assert plats == ["ob:plat:hh", "ob:plat:superjob", "ob:plat:trudvsem", "ob:plat:tgchannels",
                     "ob:plat_all", "ob:plat_done"]
    await chat.press("ob:plat_done")
    await chat.press("ob:loc_done")
    replies = await chat.press("ob:freq:hourly")
    assert asked == ["Повар горячего цеха"]
    matches = replies[1]
    assert "под вашу анкету</b> — 2" in matches
    assert "/superjob/v1" in matches and "/superjob/v5" in matches
    for hidden in ("/fl/1", "/superjob/v2", "/superjob/v3", "/superjob/v4"):
        assert hidden not in matches


async def test_search_failure_keeps_collected_matches(env):
    env.storage.upsert_project(_project("fl", "1", "Бэкенд на Python и FastAPI", 1))

    async def broken(data, **_):
        raise RuntimeError("SuperJob не ответил")

    env.handlers.onboarding.search = broken
    replies = await _finish_signup(Chat(env.handlers, GUEST))
    assert "/fl/1" in replies[1]


async def test_no_vacancies_in_city_is_said_plainly(env):
    cleaner = (ROOT / "tests" / "fixtures" / "resumes" / "05_cleaner.txt").read_text(
        encoding="utf-8"
    )

    async def nothing(data, **_):
        return []

    env.handlers.onboarding.search = nothing
    chat = Chat(env.handlers, GUEST)
    await _to_resume_step(chat)
    await chat.say(cleaner)
    await chat.press("ob:ok")
    await chat.press("ob:plat_done")
    await chat.press("ob:loc_done")
    replies = await chat.press("ob:freq:hourly")
    assert "Вакансий «Уборщик помещений» под ваш стаж сейчас нет. Искал: Елец и удалённые." in (
        replies[1]
    )


async def test_signup_asks_about_other_cities_and_remote(env):
    """Вакансия подходит, но в другом городе или удалённая — человек сам решает, отсекать ли;
    решение можно сменить после анкеты, она остаётся пройденной."""
    cook = (ROOT / "tests" / "fixtures" / "resumes" / "08_cook.txt").read_text(encoding="utf-8")

    def vacancy(vid, area):
        return replace(_project("superjob", vid, "Повар", 2, "Обязанности: повар"),
                       extra={"area": area})  # fmt: skip

    async def search(data, **_):
        return [vacancy("near", "Воронеж"), vacancy("far", "Москва"),
                vacancy("home", "Воронеж, удалённо")]  # fmt: skip

    env.handlers.onboarding.search = search
    chat = Chat(env.handlers, GUEST)
    await _to_resume_step(chat)
    await chat.say(cook)
    await chat.press("ob:ok")
    replies = await chat.press("ob:plat_done")
    assert "но она не в городе Воронеж или удалённая — присылать такие?" in replies[0]
    assert chat.buttons() == [
        ("⬜ Другие города, не только Воронеж", "ob:loc:other"),
        ("✅ Удалённая работа", "ob:loc:remote"),
        ("Готово ➡️", "ob:loc_done"),
    ]
    await chat.press("ob:loc:other")
    await chat.press("ob:loc:remote")
    await chat.press("ob:loc:nonsense")  # чужое значение ничего не меняет
    assert env.storage.get_user(GUEST).data["location"] == ["other"]
    await chat.press("ob:loc_done")
    replies = await chat.press("ob:freq:hourly")
    assert "📍 Вакансии: любой город, без удалённых" in replies[0]
    matches = replies[1]
    assert "/superjob/near" in matches and "/superjob/far" in matches
    assert "/superjob/home" not in matches
    assert matches.index("/superjob/near") < matches.index("/superjob/far")  # свой город выше

    done_at = env.storage.get_user(GUEST).completed_at
    await chat.command("start")
    assert ("📍 Другие города и удалёнка", "ob:loc_edit") in chat.buttons()
    await chat.press("ob:loc_edit")
    await chat.press("ob:loc:other")
    await chat.press("ob:loc:remote")
    assert await chat.press("ob:loc_done") == ["📍 Готово, вакансии: Воронеж и удалённые."]
    user = env.storage.get_user(GUEST)
    assert user.step == "done" and user.completed_at == done_at and "editing" not in user.data
    pages = await chat.press("ob:matches")
    assert "/superjob/home" in pages[0] and "/superjob/far" not in pages[0]


# ── проверка 2026-10-25: отметки пользователя, смена резюме, ФИО, профессия ─────────────

COOK = (ROOT / "tests" / "fixtures" / "resumes" / "08_cook.txt").read_text(encoding="utf-8")
ACCOUNTANT = (ROOT / "tests" / "fixtures" / "resumes" / "07_accountant.txt").read_text(
    encoding="utf-8"
)


def _cook_vacancy(vid, title, company="Кафе «Ромашка»"):
    p = _project("superjob", vid, title, 2, f"Обязанности: {title}")
    return replace(p, extra={"area": "Воронеж", "company": company})


async def _cook_signup(env, chat, vacancies):
    async def search(data, **_):
        return list(vacancies)

    env.handlers.onboarding.search = search
    await _to_resume_step(chat)
    await chat.say(COOK)
    await chat.press("ob:ok")
    await chat.press("ob:plat_done")
    await chat.press("ob:loc_done")
    return await chat.press("ob:freq:hourly")


def _press_under(chat, data, markup):
    """Нажатие кнопки под сообщением с этой клавиатурой (у Query — reply_markup сообщения)."""
    message = Message()
    message.reply_markup = markup
    query = Query(data, message)
    query.edited = []

    async def edit_message_text(text, **kwargs):
        query.edited.append(text)

    query.edit_message_text = edit_message_text
    chat.last_query = query
    return message, query


async def test_guest_marks_vacancies_and_teaches_own_matching(env):
    """Обычный пользователь: под списком «✅ Откликнулся» и «👎 Не подходит», отметки и
    правила — его, другим не мешают; отмеченное в список больше не попадает."""
    vacancies = [_cook_vacancy("v1", "Повар горячего цеха"),
                 _cook_vacancy("v2", "Повар", "Столовая «Линия»"),
                 _cook_vacancy("v3", "Повар-универсал", "Столовая «Линия»")]  # fmt: skip
    chat = Chat(env.handlers, GUEST)
    replies = await _cook_signup(env, chat, vacancies)
    assert "✅ — откликнулся, 👎 — не подходит" in replies[1]
    list_markup = chat.last_markup
    buttons = [b.callback_data for row in list_markup.inline_keyboard for b in row]
    assert "ob:ap:superjob:v1" in buttons and "ob:nf:superjob:v2" in buttons

    message, query = _press_under(chat, "ob:ap:superjob:v1", list_markup)
    await env.handlers.button(chat._update(message, query), None)
    assert "✅ Записал: вы откликнулись на «Повар горячего цеха»" in message.replies[0]
    left = [b.callback_data for row in query.markup.inline_keyboard for b in row]
    assert "ob:ap:superjob:v1" not in left and "ob:ap:superjob:v2" in left  # строка убрана

    await chat.press("ob:nf:superjob:v2")
    assert "Чем не подходит «Повар»?" in chat.replies[-1]
    assert ("🚫 Компания", "ob:nr:company:superjob:v2") in chat.buttons()
    await chat.press("ob:nr:company:superjob:v2")
    assert "вакансии компании «столовая «линия»» не присылаю" in chat.replies[-1]
    assert "/learned" in chat.replies[-1]

    scope = "Повар горячего цеха"
    assert [x.kind for x in env.storage.lessons(GUEST, scope)] == ["company"]
    assert env.storage.lessons(OWNER, scope) == [] and env.storage.lessons(GUEST) == []
    learned = (await chat.command("learned"))[-1]
    assert "Для резюме «Повар горячего цеха»" in learned and "столовая" in learned

    await chat.command("start")
    pages = await chat.press("ob:matches")
    text = "\n".join(pages)
    assert "/superjob/v1" not in text  # откликнулся
    assert "/superjob/v2" not in text and "/superjob/v3" not in text  # компания в стоп-листе

    other = Chat(env.handlers, GUEST + 1)  # другой человек — свой список, без чужих правил
    replies = await _cook_signup(env, other, vacancies)
    assert all(f"/superjob/{v}" in replies[1] for v in ("v1", "v2", "v3"))
    assert env.storage.lessons(GUEST + 1, scope) == []
    assert env.storage.closed_marks(GUEST + 1) == set()


async def test_guest_forgets_rule_in_learned(env):
    chat = Chat(env.handlers, GUEST)
    await _cook_signup(env, chat, [_cook_vacancy("v1", "Повар")])
    await chat.press("ob:nf:superjob:v1")
    await chat.press("ob:nr:company:superjob:v1")
    await chat.command("learned")
    [lesson] = env.storage.lessons(GUEST, "Повар горячего цеха")
    assert ("✖ 1", f"ob:ul:{lesson.id}") in chat.buttons()
    message, query = _press_under(chat, f"ob:ul:{lesson.id}", chat.last_markup)
    await env.handlers.button(chat._update(message, query), None)
    assert env.storage.lessons(GUEST, "Повар горячего цеха") == []
    assert "Пока отметок нет" in query.edited[0]


async def test_gone_vacancy_button_explains(env):
    chat = Chat(env.handlers, GUEST)
    await _cook_signup(env, chat, [])
    await chat.press("ob:ap:superjob:nope")
    assert "уже нет в вашем списке" in chat.last_query.alerts[0]


async def test_new_resume_has_own_rules_and_says_so(env):
    """Сменил резюме — правила прошлого не действуют и об этом сказано; вернулся — снова."""
    chat = Chat(env.handlers, GUEST)
    await _cook_signup(env, chat, [_cook_vacancy("v1", "Повар")])
    await chat.press("ob:nf:superjob:v1")
    await chat.press("ob:nr:company:superjob:v1")
    await chat.command("start")
    await chat.press("ob:restart")
    await chat.say("Петров Пётр Петрович")
    await chat.press("ob:addr:1")
    review = (await chat.say(ACCOUNTANT))[-1]
    assert "Отметки другого резюме («Повар горячего цеха»: 1) к этому не применяются" in review
    data = env.storage.get_user(GUEST).data
    found = first_matches(data, env.settings, env.storage, datetime.now(UTC), tg_id=GUEST)
    assert found == []  # правил бухгалтера нет, вакансий тоже — но и чужие не мешают
    assert len(env.storage.lessons(GUEST, "Повар горячего цеха")) == 1  # не удалены


async def test_resume_with_other_name_is_flagged(env):
    chat = Chat(env.handlers, GUEST)
    await _to_resume_step(chat)  # ФИО анкеты — Петров Пётр Петрович
    review = (await chat.say(COOK))[-1]  # в резюме — Ирина Белова
    assert "⚠ В резюме ФИО «Ирина Белова», а в анкете — «Петров Пётр Петрович»" in review
    assert env.storage.get_user(GUEST).data["resume_name"] == "Ирина Белова"
    await chat.press("ob:reupload")
    review = (await chat.say("Петров Пётр\n" + COOK.split("\n", 1)[1]))[-1]
    assert "⚠" not in review and "resume_name" not in env.storage.get_user(GUEST).data


async def test_accountant_edits_profession_not_it_directions(env):
    chat = Chat(env.handlers, GUEST)
    await _to_resume_step(chat)
    await chat.say(ACCOUNTANT)
    actions = [d for _, d in chat.buttons()]
    assert "ob:prof" in actions and "ob:dirs" not in actions  # IT-направлений бухгалтеру нет
    assert (await chat.press("ob:prof"))[0].startswith("Какую работу искать?")
    assert await chat.say("1") == ["Нужна должность словами: 2–80 символов, до четырёх через "
                                   "запятую."]  # fmt: skip
    review = (await chat.say("Бухгалтер по первичной документации, помощник бухгалтера"))[-1]
    assert "💼 Профессия: Бухгалтер по первичной документации" in review
    assert "🔁 Смежные должности: помощник бухгалтера" in review
    profile = env.storage.get_user(GUEST).data["profile"]
    assert profile["roles"] == ["помощник бухгалтера"]
    from freelance_bot.matching import search_queries
    from freelance_bot.resume import ResumeProfile

    assert "помощник бухгалтера" in search_queries(ResumeProfile.from_dict(profile))


async def test_stale_button_after_signup_gives_fresh_menu(env):
    chat = Chat(env.handlers, GUEST)
    await _cook_signup(env, chat, [])
    replies = await chat.press("ob:loc:other")  # кнопка шага, который уже пройден
    assert replies[0] == STALE_TEXT and "💼 Профессия: Повар горячего цеха" in replies[1]
    assert ("🔎 Заказы по анкете", "ob:matches") in chat.buttons()


async def test_old_message_edit_error_does_not_break_button(env):
    from telegram.error import BadRequest

    chat = Chat(env.handlers, GUEST)
    await chat.command("start")
    message = Message()
    query = Query("ob:agree", message)

    async def edit_message_reply_markup(reply_markup=None):
        raise BadRequest("Message is not modified")

    query.edit_message_reply_markup = edit_message_reply_markup
    await env.handlers.button(chat._update(message, query), None)
    assert message.replies == [NAME_TEXT]


async def test_empty_result_names_actual_search(env):
    """Пусто — сказано, где искал (любой город и удалённые) и что ответили площадки."""

    async def search(data, statuses=None, **_):
        statuses.update({"superjob": "не ответила", "trudvsem": "найдено 3 до отбора"})
        return []

    env.handlers.onboarding.search = search
    chat = Chat(env.handlers, GUEST)
    await _to_resume_step(chat)
    await chat.say(COOK)
    await chat.press("ob:ok")
    await chat.press("ob:plat_done")
    await chat.press("ob:loc:other")
    await chat.press("ob:loc_done")
    replies = await chat.press("ob:freq:hourly")
    assert "Искал: любой город и удалённые." in replies[1]
    assert "Площадки: SuperJob — не ответила; Работа России — найдено 3 до отбора." in replies[1]
    assert "В городе" not in replies[1]


async def test_owner_mark_under_anketa_list_closes_his_vacancy(env):
    """Владелец отметил «✅» под списком анкеты — и его собранная вакансия закрыта: в «Все
    заказы» и дайджест она больше не попадёт."""
    p = _cook_vacancy("v1", "Повар горячего цеха")
    env.storage.upsert_project(p)
    chat = Chat(env.handlers, OWNER)
    await _cook_signup(env, chat, [])
    await chat.press("ob:ap:superjob:v1")
    assert env.storage.get_status("superjob", "v1") == "applied"
    assert env.storage.closed_marks(OWNER) == {("superjob", "v1")}
