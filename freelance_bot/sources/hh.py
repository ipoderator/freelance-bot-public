"""HH.ru: вакансии из сообщений бота hh.ru в Telegram (@hh_rabota_bot, «Хэдди»): подборки по
автопоиску и резюме владельца.

Сайт и API hh.ru бот не трогает: анонимный API отвечает 403, а условия сайта запрещают
автосбор (ADR 0009). Сессия Telegram пользователя (Telethon) читает только чат с ботом HH —
ничего не отправляет, не нажимает кнопки и не отмечает сообщения прочитанными.

Формат подборки (снят 2026-10-24, tests/fixtures/hh_heddy_messages.json): вакансии через
разделитель «-----», в каждой — «N. Название», строка зарплаты (если указана), «📍 Город.
👉 Компания», «Обязанности: …», «Требования: …» (обрезаны многоточием) и «Откликнуться на сайте:
https://my.hh.ru/b/…» — короткая персональная ссылка без ID вакансии. Поэтому ключ вакансии —
хэш названия, компании и города: одна вакансия в разных подборках не придёт дважды.

Разбор (`parse_notice`) не зависит от Telethon: на вход — текст и ссылки (`Notice`).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from freelance_bot.http import SourceError, TemporarySourceError
from freelance_bot.models import Platform, Project
from freelance_bot.sources import SourceFormatError

DEFAULT_CHAT = "hh_rabota_bot"
MESSAGES_LIMIT = 50  # последних сообщений чата за цикл: в подборке 5 вакансий, плюс меню
LOOKBACK = timedelta(days=7)  # старше — уже не новые (как OFFER_WINDOW в пайплайне)
CATEGORY = "Вакансия HH.ru"
DEVICE_MODEL = "freelance-bot (HH)"  # так сессия видна в «Устройствах» Telegram
APPLY_MARK = "Откликнуться на сайте"
PLACE_MARK = "📍"
COMPANY_MARK = "👉"

_VACANCY_PATH_RE = re.compile(r"(?:^|//|\.)hh\.ru/vacancy/(\d+)", re.IGNORECASE)
_VACANCY_PARAM_RE = re.compile(r"[?&]vacancy_?id=(\d+)", re.IGNORECASE)
_SEPARATOR_RE = re.compile(r"^\s*-{5,}\s*$", re.MULTILINE)
_TITLE_RE = re.compile(r"^\s*\d{1,3}\.\s+(\S.*)$")
_APPLY_RE = re.compile(rf"{APPLY_MARK}\s*:?\s*(https?://\S+)", re.IGNORECASE)
_SALARY_RE = re.compile(
    r"^(?:(?:от|до)\s*)?\d[\d \u00a0\u202f]*(?:\s*[–—-]\s*\d[\d \u00a0\u202f]*)?"
    r"\s*(?:₽|руб|rub\b|\$|usd\b|€|eur\b|₸|kzt\b|byn\b|br\b|сум)",
    re.IGNORECASE,
)
_RUB_RE = re.compile(r"₽|руб|\brub\b", re.IGNORECASE)
_NUMBER_RE = re.compile(r"\d[\d \u00a0\u202f]*")


@dataclass(frozen=True)
class Notice:
    """Сообщение бота HH в простом виде. `links` — ссылки из текста (подпись, адрес, позиция
    подписи в `text`), `buttons` — кнопки-ссылки (подпись, адрес)."""

    message_id: int
    sent_at: datetime  # tz-aware UTC
    text: str
    links: tuple[tuple[str, str, int], ...] = ()
    buttons: tuple[tuple[str, str], ...] = ()


def vacancy_id(url: str) -> str | None:
    """ID вакансии из полной ссылки hh.ru (любой поддомен, параметры не важны)."""
    for pattern in (_VACANCY_PATH_RE, _VACANCY_PARAM_RE):
        m = pattern.search(url)
        if m:
            return m.group(1)
    return None


def vacancy_key(title: str, company: str, area: str) -> str:
    """Ключ вакансии без ID: одна и та же в разных подборках даёт один ключ."""
    norm = "|".join(" ".join(x.lower().replace("ё", "е").split()) for x in (title, company, area))
    return "t" + hashlib.sha1(norm.encode()).hexdigest()[:15]


def parse_salary(text: str) -> tuple[int | None, int | None]:
    """«от 150 000 ₽» → (150000, None); «100 000 – 200 000 ₽» → (100000, 200000).
    Только рубли: в другой валюте сумма в баллы не идёт (остаётся текстом в карточке)."""
    if not _RUB_RE.search(text):
        return None, None
    numbers = [int(re.sub(r"\D", "", n)) for n in _NUMBER_RE.findall(text)]
    numbers = [n for n in numbers if n >= 1000]
    if not numbers:
        return None, None
    if len(numbers) >= 2:
        return numbers[0], numbers[1]
    first = text.strip().lower()
    if first.startswith("до"):
        return None, numbers[0]
    if first.startswith("от"):
        return numbers[0], None
    return numbers[0], numbers[0]


def _place(line: str) -> tuple[str, str]:
    """«📍 Москва. 👉 Grand Line» → ("Москва", "Grand Line")."""
    area, _, company = line.replace(PLACE_MARK, "", 1).partition(COMPANY_MARK)
    return " ".join(area.split()).rstrip(". "), " ".join(company.split())


def _vacancy(chunk: str, notice: Notice) -> Project | None:
    """Одна вакансия подборки или None, если в куске её нет (меню, список поисков)."""
    lines = [line.strip() for line in chunk.splitlines()]
    starts = [i for i, line in enumerate(lines) if _TITLE_RE.match(line)]
    if not starts:
        return None
    title = " ".join(_TITLE_RE.match(lines[starts[0]]).group(1).split())  # type: ignore[union-attr]
    area = company = salary = ""
    rest: list[str] = []
    for line in lines[starts[0] + 1 :]:
        if not line or _APPLY_RE.search(line):
            continue
        if line.startswith(PLACE_MARK) and not (area or company):
            area, company = _place(line)
        elif not salary and not rest and _SALARY_RE.match(line):
            salary = " ".join(line.split())
        else:
            rest.append(line)
    apply = _APPLY_RE.search(chunk)
    url = apply.group(1).rstrip(".,)") if apply else ""
    if not url or not (area or company):  # «1. Резюме …», «1. Python junior - 40 новых.»
        return None
    budget_min, budget_max = parse_salary(salary) if salary else (None, None)
    extra: dict[str, Any] = {"kind": "vacancy", "message_id": notice.message_id}
    for key, value in (("company", company), ("salary", salary), ("area", area)):
        if value:
            extra[key] = value
    return Project(
        platform="hh",
        external_id=vacancy_id(url) or vacancy_key(title, company, area),
        url=url,
        title=title,
        description="\n".join(rest),
        published_at=notice.sent_at,
        budget_min=budget_min,
        budget_max=budget_max,
        offers_count=None,
        category=CATEGORY,
        extra=extra,
    )


def parse_notice(notice: Notice) -> list[Project]:
    """Вакансии из одного сообщения бота HH; сообщение без вакансий (меню, счётчики) — []."""
    projects = []
    for chunk in _SEPARATOR_RE.split(notice.text):
        project = _vacancy(chunk, notice)
        if project is not None:
            projects.append(project)
    return projects


def _looks_like_vacancies(notice: Notice) -> bool:
    """Признаки подборки: если из такого сообщения ничего не разобрано — сменился формат."""
    return APPLY_MARK.lower() in notice.text.lower() or (
        PLACE_MARK in notice.text and COMPANY_MARK in notice.text
    )


# ── Telegram ─────────────────────────────────────────────────────────────────


def _urls(node: Any) -> Iterable[str]:
    """Все поля url в объекте Telethon (to_dict): форма кнопок меняется от слоя к слою."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "url" and isinstance(value, str):
                yield value
            else:
                yield from _urls(value)
    elif isinstance(node, list | tuple):
        for item in node:
            yield from _urls(item)


def _buttons(markup: Any) -> list[tuple[str, str]]:
    """Кнопки-ссылки (подпись, адрес) из reply_markup любого слоя схемы."""
    found: list[tuple[str, str]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            text = node.get("text")
            if isinstance(text, str):
                found.extend((text, url) for url in _urls(node))
                return
            for value in node.values():
                walk(value)
        elif isinstance(node, list | tuple):
            for item in node:
                walk(item)

    if markup is not None:
        walk(markup.to_dict())
    return found


def notice_from_message(message: Any) -> Notice:
    """Сообщение Telethon → Notice. Позиции ссылок считает по тексту (смещения Telegram — в
    UTF-16, get_entities_text их уже учитывает)."""
    text = message.message or ""
    links: list[tuple[str, str, int]] = []
    cursor = 0
    for entity, shown in message.get_entities_text():
        url = getattr(entity, "url", None) or (shown if type(entity).__name__ ==
                                               "MessageEntityUrl" else None)  # fmt: skip
        if not url:
            continue
        pos = text.find(shown, cursor)
        pos = pos if pos >= 0 else text.find(shown)
        cursor = max(cursor, pos + len(shown)) if pos >= 0 else cursor
        links.append((shown, url, max(pos, 0)))
    sent_at = message.date if message.date.tzinfo else message.date.replace(tzinfo=UTC)
    return Notice(
        message_id=message.id,
        sent_at=sent_at.astimezone(UTC),
        text=text,
        links=tuple(links),
        buttons=tuple(_buttons(message.reply_markup)),
    )


class NoticeReader(Protocol):
    async def recent(self, limit: int) -> list[Notice]: ...

    async def aclose(self) -> None: ...


def _session_error(exc: BaseException) -> str | None:
    """Ошибка, после которой сессия больше не работает: нужен новый вход (hh-login)."""
    from telethon import errors

    fatal = (
        errors.AuthKeyUnregisteredError, errors.SessionRevokedError,
        errors.AuthKeyDuplicatedError, errors.UserDeactivatedBanError,
        errors.UnauthorizedError,
    )  # fmt: skip
    if isinstance(exc, fatal):
        return type(exc).__name__
    return None


class TelethonReader:
    """Читает последние сообщения чата с ботом HH. Соединение — только на время чтения:
    простаивающее Telegram рвёт каждые несколько минут. Адрес чата (peer) хранится между
    циклами: resolveUsername у Telegram строго ограничен по частоте."""

    def __init__(
        self,
        api_id: int | None,
        api_hash: str | None,
        session: str | None,
        chat: str = DEFAULT_CHAT,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._api_id, self._api_hash, self._session = api_id, api_hash, session
        self._chat = chat.lstrip("@")
        self._clock = clock
        self._client: Any = None
        self._peer: Any = None
        self._dead: str | None = None  # сессия отозвана: не стучаться, пока не перезапустят
        self._resume_at: datetime | None = None  # FloodWait: раньше этого времени не звать

    async def recent(self, limit: int) -> list[Notice]:
        if not (self._api_id and self._api_hash and self._session):
            raise SourceError(
                "HH: не заданы HH_TG_API_ID, HH_TG_API_HASH и HH_TG_SESSION — "
                "выполните `python -m freelance_bot hh-login`"
            )
        if self._dead:
            raise SourceError(self._dead)
        now = self._clock()
        if self._resume_at and now < self._resume_at:
            wait = int((self._resume_at - now).total_seconds())
            raise TemporarySourceError(f"HH: Telegram просил подождать, ещё {wait} с", wait)
        from telethon import errors

        try:
            client = await self._connect()
            if self._peer is None:
                self._peer = await client.get_input_entity(self._chat)
            messages = await client.get_messages(self._peer, limit=limit)
            await self._drop()
        except errors.FloodWaitError as exc:
            await self._drop()
            self._resume_at = now + timedelta(seconds=exc.seconds)
            raise TemporarySourceError(
                f"HH: Telegram ограничил частоту запросов, пауза {exc.seconds} с", exc.seconds
            ) from None
        except (errors.UsernameNotOccupiedError, errors.UsernameInvalidError, ValueError):
            await self._drop()
            raise SourceError(f"HH: чат @{self._chat} не найден в Telegram") from None
        except (OSError, ConnectionError, TimeoutError) as exc:
            await self._drop()
            raise TemporarySourceError(f"HH: нет связи с Telegram ({type(exc).__name__})") from None
        except errors.RPCError as exc:
            reason = _session_error(exc)
            await self._drop()
            if reason:
                self._dead = (
                    f"HH: сессия Telegram недействительна ({reason}) — "
                    "выполните `python -m freelance_bot hh-login` и перезапустите бота"
                )
                raise SourceError(self._dead) from None
            raise TemporarySourceError(f"HH: ошибка Telegram {type(exc).__name__}") from None
        # только сообщения бота: свои (номера, «В начало», пересланное) не уведомления HH
        return [
            notice_from_message(m)
            for m in messages
            if getattr(m, "message", None) and not getattr(m, "out", False)
        ]

    async def _connect(self) -> Any:
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        client = TelegramClient(
            StringSession(self._session), self._api_id, self._api_hash,
            receive_updates=False, device_model=DEVICE_MODEL,
        )  # fmt: skip
        await client.connect()
        if not await client.is_user_authorized():
            await client.disconnect()
            self._dead = (
                "HH: сессия Telegram не авторизована — выполните "
                "`python -m freelance_bot hh-login` и перезапустите бота"
            )
            raise SourceError(self._dead)
        self._client = client
        return client

    async def _drop(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001 - разрыв уже случился
                pass

    async def aclose(self) -> None:
        await self._drop()


class HhSource:
    """Вакансии из последних сообщений бота HH, без повторов внутри одного сбора."""

    platform: Platform = "hh"

    def __init__(
        self,
        reader: NoticeReader,
        *,
        limit: int = MESSAGES_LIMIT,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._reader = reader
        self._limit = limit
        self._clock = clock

    async def fetch(self) -> list[Project]:
        cutoff = self._clock() - LOOKBACK
        notices = [n for n in await self._reader.recent(self._limit) if n.sent_at >= cutoff]
        return collect(notices)

    async def aclose(self) -> None:
        await self._reader.aclose()


def collect(notices: Sequence[Notice]) -> list[Project]:
    """Вакансии из сообщений, свежие первыми; одна вакансия в нескольких сообщениях — одна
    запись с датой первого уведомления. Сообщения о вакансиях без единой разобранной —
    признак смены формата."""
    found: dict[str, Project] = {}
    suspicious = 0
    for notice in sorted(notices, key=lambda n: n.sent_at):
        projects = parse_notice(notice)
        if not projects and _looks_like_vacancies(notice):
            suspicious += 1
        for p in projects:
            found.setdefault(p.external_id, p)
    if suspicious and not found:
        raise SourceFormatError(
            f"HH: в {suspicious} сообщ. бота HH о вакансиях не нашлось ссылок на вакансии — "
            "похоже, сменился формат уведомлений"
        )
    return sorted(found.values(), key=lambda p: p.published_at, reverse=True)


# ── вход ─────────────────────────────────────────────────────────────────────

QR_ATTEMPTS = 4  # QR-код живёт ~30 с; столько раз показываем новый
_CODE_HINTS = {
    "SentCodeTypeApp": (
        "Код отправлен в приложение Telegram — в служебный чат «Telegram» (с синей галочкой) "
        "на телефоне или компьютере, где вы уже вошли. SMS не будет."
    ),
    "SentCodeTypeSms": "Код отправлен по SMS.",
    "SentCodeTypeSmsWord": "Код (слово) отправлен по SMS.",
    "SentCodeTypeSmsPhrase": "Код (фраза) отправлен по SMS.",
    "SentCodeTypeFirebaseSms": "Код отправлен по SMS.",
    "SentCodeTypeFragmentSms": "Код отправлен по SMS на номер Fragment.",
    "SentCodeTypeCall": "Telegram позвонит и продиктует код.",
    "SentCodeTypeFlashCall": "Telegram сделает звонок: код — номер, с которого позвонят.",
    "SentCodeTypeMissedCall": "Пропущенный звонок: код — последние цифры номера звонящего.",
    "SentCodeTypeEmailCode": "Код отправлен на почту, привязанную к Telegram.",
    "SentCodeTypeSetUpEmailRequired": (
        "Telegram требует привязать почту, чтобы войти по коду. Проще войти по QR-коду: "
        "запустите hh-login без --code."
    ),
}


def code_hint(sent_code: Any) -> str:
    """Куда Telegram отправил код входа — по типу ответа auth.SentCode."""
    name = type(getattr(sent_code, "type", None)).__name__
    return _CODE_HINTS.get(name, f"Код отправлен ({name}).")


def print_qr(url: str) -> None:
    """QR-код в терминале (пакет qrcode, `pip install qrcode`)."""
    import qrcode

    qr = qrcode.QRCode(border=2)
    qr.add_data(url)
    qr.make(fit=True)
    qr.print_ascii(invert=True)


async def _login_qr(
    client: Any, secret: Callable[[str], str], out: Callable[[str], None],
    show_qr: Callable[[str], None],
) -> None:  # fmt: skip
    from telethon import errors

    qr = await client.qr_login()
    for _ in range(QR_ATTEMPTS):
        show_qr(qr.url)
        out(
            "Отсканируйте телефоном: Telegram → Настройки → Устройства → «Подключить "
            f"устройство». Код действует до {qr.expires.astimezone():%H:%M:%S}."
        )
        try:
            await qr.wait()
            return
        except TimeoutError:
            out("Код истёк — показываю новый.")
            await qr.recreate()
        except errors.SessionPasswordNeededError:
            await client.sign_in(password=secret("Облачный пароль Telegram (двухэтапная проверка)"))
            return
    raise TimeoutError("QR-код не отсканирован")


async def _login_code(
    client: Any, ask: Callable[[str], str], secret: Callable[[str], str],
    out: Callable[[str], None],
) -> None:  # fmt: skip
    from telethon import errors

    phone = ask("Телефон аккаунта Telegram в формате +7…")
    sent = await client.send_code_request(phone)
    out(code_hint(sent))
    code = ask("Код (пусто — отправить другим способом)")
    while not code:
        sent = await client.send_code_request(phone)  # повтор — Telegram шлёт следующим способом
        out(code_hint(sent))
        code = ask("Код")
    try:
        await client.sign_in(phone, code)
    except errors.SessionPasswordNeededError:
        await client.sign_in(password=secret("Облачный пароль Telegram (двухэтапная проверка)"))


async def login(
    api_id: int,
    api_hash: str,
    ask: Callable[[str], str],
    secret: Callable[[str], str],
    out: Callable[[str], None] = print,
    *,
    by_code: bool = False,
    chat: str = DEFAULT_CHAT,
    show_qr: Callable[[str], None] = print_qr,
) -> tuple[str, list[Notice]]:
    """Разовый вход в Telegram пользователем: по QR-коду (по умолчанию) или по коду из
    Telegram. Возвращает строку сессии (секрет) и последние сообщения бота HH для проверки."""
    from telethon import TelegramClient
    from telethon.sessions import StringSession

    client = TelegramClient(
        StringSession(), api_id, api_hash, receive_updates=False, device_model=DEVICE_MODEL
    )
    await client.connect()
    try:
        if by_code:
            await _login_code(client, ask, secret, out)
        else:
            await _login_qr(client, secret, out, show_qr)
        session = client.session.save()
        try:
            messages = await client.get_messages(chat.lstrip("@"), limit=MESSAGES_LIMIT)
        except ValueError:
            messages = []  # с ботом HH ещё не переписывались
        return session, [notice_from_message(m) for m in messages if getattr(m, "message", None)]
    finally:
        await client.disconnect()
