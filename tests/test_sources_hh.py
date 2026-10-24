"""Источник HH: сообщения бота hh.ru в Telegram (снятые 2026-10-24, обезличенные); сети нет."""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from telethon import errors
from telethon.tl.types import MessageEntityTextUrl, MessageEntityUrl

from freelance_bot.config import load_settings
from freelance_bot.http import SourceError, TemporarySourceError
from freelance_bot.models import Draft
from freelance_bot.pipeline import Pipeline
from freelance_bot.sources import SourceFormatError
from freelance_bot.sources.hh import (
    HhSource,
    Notice,
    TelethonReader,
    code_hint,
    collect,
    notice_from_message,
    parse_notice,
    parse_salary,
    vacancy_id,
    vacancy_key,
)
from freelance_bot.storage import Storage

ROOT = Path(__file__).resolve().parent.parent
RAW = json.loads((Path(__file__).parent / "fixtures" / "hh_heddy_messages.json").read_text("utf-8"))
SENT = datetime(2026, 10, 24, 17, 51, 47, tzinfo=UTC)  # подборка 392422


def notice(raw: dict) -> Notice:
    return Notice(
        raw["message_id"],
        datetime.fromisoformat(raw["sent_at"]),
        raw["text"],
        tuple(tuple(x) for x in raw["links"]),
        tuple(tuple(x) for x in raw["buttons"]),
    )


NOTICES = [notice(r) for r in RAW]
LIST = next(n for n in NOTICES if n.message_id == 392422)  # подборка из 5 вакансий
MENU = [n for n in NOTICES if n is not LIST]  # счётчики поисков, выбор резюме, вход в ЛК


def test_heddy_list_gives_five_vacancies_with_all_fields() -> None:
    vacancies = parse_notice(LIST)

    assert [(v.title, v.extra["company"], v.extra["area"]) for v in vacancies] == [
        ("Middle Python/AI Developer — разработчик ядра и интеграций агентов", "Grand Line",
         "Москва"),
        ("Разработчик Python (ИИ-помощник)", "МТС Банк", "Москва"),
        ("AI Engineer (Video Pipeline, Python)", "Emphasoft", "Армения"),
        ("Консультант-исполнитель по внедрению ИИ-решений", "Riverstart (ООО Риверстарт)",
         "Москва"),
        ("Специалист по контент-ферме", "Натури Фарм", "Москва"),
    ]  # fmt: skip
    assert [v.url for v in vacancies] == [
        f"https://my.hh.ru/b/test00{i}" for i in range(1, 6)
    ]
    first, *_, last = vacancies
    assert first.platform == "hh" and first.published_at == SENT
    assert first.description.startswith("Обязанности: Разрабатывать интеграции")
    assert "Требования: Опыт разработки API на FastAPI" in first.description
    assert "salary" not in first.extra and first.budget_min is first.budget_max is None
    assert last.extra["salary"] == "от 120000 ₽ За месяц"
    assert (last.budget_min, last.budget_max) == (120000, None)
    assert last.external_id == vacancy_key("Специалист по контент-ферме", "Натури Фарм", "Москва")


@pytest.mark.parametrize("menu", MENU, ids=lambda n: str(n.message_id))
def test_menus_and_counters_are_not_vacancies(menu: Notice) -> None:
    assert parse_notice(menu) == []


def test_collect_whole_chat_dedups_repeated_vacancy_and_keeps_first_date() -> None:
    later = replace(
        LIST, message_id=392500, sent_at=SENT + timedelta(hours=3),
        text=LIST.text.replace("test00", "again0"),
    )  # fmt: skip

    vacancies = collect([later, *NOTICES])

    assert len(vacancies) == 5
    assert {v.published_at for v in vacancies} == {SENT}  # дата первого уведомления


def test_changed_list_format_is_reported_not_silently_empty() -> None:
    broken = replace(LIST, text=LIST.text.replace("📍", "Город:"))

    with pytest.raises(SourceFormatError, match="сменился формат"):
        collect([broken, *MENU])
    assert collect(MENU) == []  # одни меню — не ошибка


def test_full_hh_link_gives_vacancy_id_as_key() -> None:
    text = LIST.text.replace("https://my.hh.ru/b/test001", "https://hh.ru/vacancy/123456?from=tg")

    first = parse_notice(replace(LIST, text=text))[0]

    assert first.external_id == "123456"
    assert vacancy_id("https://spb.hh.ru/applicant/vacancy_response?vacancyId=77") == "77"
    assert vacancy_id("https://my.hh.ru/b/1rxr1bx") is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("от 120000 ₽ За\xa0месяц", (120000, None)),
        ("до 90 000 ₽ на руки", (None, 90000)),
        ("100 000 – 150 000 ₽", (100000, 150000)),
        ("200 000 ₽", (200000, 200000)),
        ("от 3 000 $", (None, None)),  # не рубли — только текстом
    ],
)
def test_parse_salary(text, expected) -> None:
    assert parse_salary(text) == expected


# ── сообщение Telethon ───────────────────────────────────────────────────────


class FakeMarkup:
    def __init__(self, data):
        self.data = data

    def to_dict(self):
        return self.data


def test_notice_from_telethon_message_reads_links_and_url_buttons() -> None:
    text = "1. Python-разработчик\n📍 Москва. 👉 Ромашка\nПодробнее\nhttps://my.hh.ru/b/abc"
    link = MessageEntityUrl(offset=text.index("https"), length=len("https://my.hh.ru/b/abc"))
    named = MessageEntityTextUrl(offset=text.index("Подробнее"), length=9,
                                 url="https://hh.ru/vacancy/5")  # fmt: skip
    markup = FakeMarkup({"_": "ReplyInlineMarkup", "rows": [{"buttons": [
        {"_": "KeyboardInlineButton", "text": "Открыть",
         "type": {"_": "InlineButtonTypeUrl", "url": "https://hh.ru/vacancy/9"}},
        {"_": "KeyboardInlineButton", "text": "Меню", "type": {"_": "InlineButtonTypeCallback"}},
    ]}]})  # fmt: skip
    message = SimpleNamespace(
        id=7, date=SENT, message=text, reply_markup=markup,
        get_entities_text=lambda: [(named, "Подробнее"), (link, "https://my.hh.ru/b/abc")],
    )  # fmt: skip

    n = notice_from_message(message)

    assert n.links == (
        ("Подробнее", "https://hh.ru/vacancy/5", text.index("Подробнее")),
        ("https://my.hh.ru/b/abc", "https://my.hh.ru/b/abc", text.index("https")),
    )
    assert n.buttons == (("Открыть", "https://hh.ru/vacancy/9"),)
    assert n.sent_at == SENT and n.message_id == 7


def test_code_hint_says_where_the_code_went() -> None:
    from telethon.tl.types.auth import SentCodeTypeApp, SentCodeTypeSms

    assert "чат «Telegram»" in code_hint(SimpleNamespace(type=SentCodeTypeApp(length=5)))
    assert "SMS" in code_hint(SimpleNamespace(type=SentCodeTypeSms(length=5)))


# ── чтение чата и ошибки Telegram ────────────────────────────────────────────


class FakeClient:
    def __init__(self, error=None, messages=()):
        self.error = error
        self.messages = list(messages)
        self.disconnected = False

    async def get_input_entity(self, chat):
        self.resolved = getattr(self, "resolved", 0) + 1
        return chat

    async def get_messages(self, peer, limit):
        if self.error:
            raise self.error
        return self.messages

    async def disconnect(self):
        self.disconnected = True
        self.disconnects = getattr(self, "disconnects", 0) + 1


def reader_with(client, clock=lambda: SENT):
    reader = TelethonReader(1, "hash", "session", clock=clock)
    connects = []

    async def connect():  # как настоящий _connect: клиент запоминается до разрыва
        connects.append(1)
        reader._client = client
        return client

    reader._connect = connect  # type: ignore[method-assign]
    return reader, connects


async def test_reader_without_credentials_explains_hh_login() -> None:
    with pytest.raises(SourceError, match="hh-login"):
        await TelethonReader(None, None, None).recent(5)


async def test_flood_wait_pauses_reader_without_new_requests() -> None:
    now = [SENT]
    reader, connects = reader_with(
        FakeClient(errors.FloodWaitError(request=None, capture=120)), clock=lambda: now[0]
    )

    with pytest.raises(TemporarySourceError, match="120"):
        await reader.recent(5)
    now[0] += timedelta(seconds=60)
    with pytest.raises(TemporarySourceError, match="ещё 60"):
        await reader.recent(5)

    assert len(connects) == 1  # во время паузы в Telegram не ходили


async def test_revoked_session_stops_reader_until_new_login() -> None:
    client = FakeClient(errors.AuthKeyUnregisteredError(request=None))
    reader, connects = reader_with(client)

    for _ in range(2):
        with pytest.raises(SourceError, match="hh-login") as info:
            await reader.recent(5)
        assert not isinstance(info.value, TemporarySourceError)

    assert len(connects) == 1 and client.disconnected


async def test_network_error_is_temporary_and_reconnects_next_time() -> None:
    reader, connects = reader_with(FakeClient(ConnectionError("reset")))

    for _ in range(2):
        with pytest.raises(TemporarySourceError, match="нет связи"):
            await reader.recent(5)

    assert len(connects) == 2


async def test_reader_returns_only_bot_messages() -> None:
    def msg(id_, text, out):
        return SimpleNamespace(id=id_, date=SENT, message=text, out=out, reply_markup=None,
                               get_entities_text=lambda: [])  # fmt: skip

    reader, _ = reader_with(FakeClient(messages=[
        msg(3, LIST.text, True), msg(2, "", False), msg(1, "Подходящие вакансии", False),
    ]))  # fmt: skip

    assert [n.message_id for n in await reader.recent(5)] == [1]
    assert reader._client is None  # соединение закрыто сразу после чтения


async def test_reader_resolves_chat_once_and_disconnects_after_each_read() -> None:
    client = FakeClient(messages=[])
    reader, connects = reader_with(client)

    for _ in range(3):
        assert await reader.recent(5) == []

    assert len(connects) == 3 and client.disconnects == 3 and client.resolved == 1


async def test_unknown_chat_is_reported() -> None:
    reader, _ = reader_with(FakeClient(errors.UsernameNotOccupiedError(request=None)))

    with pytest.raises(SourceError, match="@hh_rabota_bot не найден"):
        await reader.recent(5)


# ── полный цикл ──────────────────────────────────────────────────────────────


class FakeReader:
    def __init__(self, notices):
        self.notices = list(notices)
        self.closed = False

    async def recent(self, limit):
        return self.notices[:limit]

    async def aclose(self):
        self.closed = True


class FakeNotifier:
    def __init__(self):
        self.cards: list[tuple[str, list]] = []
        self.texts: list[str] = []

    async def send_card(self, text, keyboard):
        self.cards.append((text, keyboard))

    async def send_text(self, text):
        self.texts.append(text)


class NoDrafts:
    async def generate(self, p, r, variant=0) -> Draft:  # pragma: no cover - не должен звать
        raise AssertionError("вакансиям черновик не нужен")


async def test_old_notices_are_skipped_and_reader_is_closed() -> None:
    reader = FakeReader(NOTICES)
    source = HhSource(reader, clock=lambda: SENT + timedelta(days=8))

    assert await source.fetch() == []
    await source.aclose()
    assert reader.closed


async def test_pipeline_sends_vacancy_cards_once_without_drafts() -> None:
    now = SENT + timedelta(minutes=10)
    loaded = load_settings(ROOT / "settings.toml", ROOT / "no-such.env")
    hh = replace(loaded.sources["hh"], enabled=True, mode="notify")
    settings = replace(
        loaded, digest_enabled=False, min_score=25, sources={**loaded.sources, "hh": hh}
    )
    storage = Storage(":memory:", clock=lambda: now)
    notifier = FakeNotifier()
    source = HhSource(FakeReader(NOTICES), clock=lambda: now)
    pipeline = Pipeline(settings, storage, [source], NoDrafts(), notifier, clock=lambda: now)

    first = await pipeline.run_once()
    second = await pipeline.run_once()

    assert (first.fetched, first.new, first.notified, first.errors) == (5, 5, 5, [])
    assert (second.new, second.notified) == (0, 0)  # повторных уведомлений нет
    assert len(notifier.cards) == 5 and notifier.texts == []
    text, keyboard = next(c for c in notifier.cards if "контент-ферме" in c[0])
    assert "🏢 Натури Фарм · 📍 Москва" in text
    assert "💵 зарплата: от 120000 ₽ За месяц" in text
    assert "уведомление от 24.10.2026 20:51 МСК" in text
    assert "Почему подходит:" in text and "claude" in text
    assert 'href="https://my.hh.ru/b/test005"' in text
    assert [b[1] for row in keyboard for b in row] == [
        f"applied:hh:{vacancy_key('Специалист по контент-ферме', 'Натури Фарм', 'Москва')}",
        f"skip:hh:{vacancy_key('Специалист по контент-ферме', 'Натури Фарм', 'Москва')}",
    ]
    grand = next(c[0] for c in notifier.cards if "Grand Line" in c[0])
    assert "💵 зарплата: не указана" in grand
    storage.close()
