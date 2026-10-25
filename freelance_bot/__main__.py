"""Командная строка: init, run, once, setup-telegram, hh-login, workzilla-session."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import sys
import time
import traceback
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path

from freelance_bot.config import ConfigError, Settings, load_settings
from freelance_bot.drafting import make_drafter
from freelance_bot.http import Fetcher
from freelance_bot.matching import AnketaVacancies, VacancySearch, completed_anketa
from freelance_bot.models import PLATFORMS, Platform
from freelance_bot.pipeline import ConsoleNotifier, Pipeline
from freelance_bot.profile import ProfileError, load_profile
from freelance_bot.sources import Source
from freelance_bot.sources.fl import FlSource
from freelance_bot.sources.freelance_ru import FreelanceRuSource
from freelance_bot.sources.hh import HhSource, TelethonReader
from freelance_bot.sources.kwork import KworkSource
from freelance_bot.sources.superjob import SuperJobSource
from freelance_bot.sources.work24 import Work24Source
from freelance_bot.sources.workzilla import WorkzillaSource, session_from_api_anything
from freelance_bot.storage import Storage
from freelance_bot.summary import make_summarizer
from freelance_bot.telegram_ui import KWORK_WARNING, PLATFORM_NAMES

SETUP_WAIT_SEC = 120
CLI = ".venv/bin/python -m freelance_bot"  # как в README: запуск из папки проекта
ONCE_CONSOLE_COMMAND = f"{CLI} once --console"
BOTFATHER_HINT = (
    "Токен бота не задан. Шаги:\n"
    "  1. В Telegram откройте @BotFather и отправьте /newbot.\n"
    "  2. Задайте имя и username бота (username оканчивается на bot).\n"
    "  3. BotFather пришлёт токен. Никому его не показывайте и не публикуйте.\n"
    "  4. Скопируйте .env.example в .env и впишите токен в строку TELEGRAM_BOT_TOKEN=.\n"
    "  5. Запустите команду ещё раз."
)

HH_LOGIN_INTRO = (
    "Вход в ваш аккаунт Telegram для вакансий HH.ru.\n"
    "Бот будет читать только чат с @{chat} (бот hh.ru присылает туда автопоиск) и ничего не "
    "отправлять. Но сессия даёт полный доступ к аккаунту: она записывается в .env (права 600), "
    "не публикуйте файл. Отозвать: Telegram → Настройки → Устройства → «freelance-bot (HH)».\n"
    "api_id и api_hash: https://my.telegram.org → API development tools (создать приложение)."
)
HH_LOGIN_NEXT = (
    "Дальше:\n"
    "  1. В Telegram откройте @{chat}, нажмите «Искать вакансии» и подпишитесь на автопоиск.\n"
    "  2. В settings.toml в [sources.hh] поставьте enabled = true.\n"
    "  3. Используйте сессию на одной машине: одновременный запуск на Mac и на сервере "
    "Telegram считает угоном и отзывает её (AUTH_KEY_DUPLICATED)."
)

WORKZILLA_SESSION_NEXT = (
    "Дальше:\n"
    "  1. Проверьте ленту: .venv/bin/python -m freelance_bot --db data/dry-run.sqlite3 once "
    "--dry-run --console --platform workzilla --include-disabled\n"
    "     Пока Work-zilla отдаёт демонстрационные задания, бот пишет «лента демонстрационная» "
    "и ничего не сохраняет.\n"
    "  2. Когда лента станет настоящей, в settings.toml в [sources.workzilla] поставьте "
    "enabled = true."
)

TELEGRAM_FAILURE_HINT = (
    "Не удалось связаться с Telegram ({name}). Проверьте TELEGRAM_BOT_TOKEN в .env: "
    "если токен отозван, получите новый у @BotFather и запустите setup-telegram."
)
LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"
REDACTED = "***"

_fetchers: list[Fetcher] = []
_readers: list[TelethonReader] = []


class RedactingFormatter(logging.Formatter):
    """Вырезает значения секретов из строки лога, включая текст трейсбека."""

    def __init__(self, fmt: str, secrets: Iterable[str | None]) -> None:
        super().__init__(fmt)
        self.secrets = [s for s in secrets if s]

    def format(self, record: logging.LogRecord) -> str:
        text = super().format(record)
        for secret in self.secrets:
            text = text.replace(secret, REDACTED)
        return text


def _setup_logging(settings: Settings | None) -> None:
    secrets = (
        [
            settings.telegram_token, settings.anthropic_api_key, settings.openai_api_key,
            settings.groq_api_key, settings.hh_tg_api_hash, settings.hh_tg_session,
            settings.workzilla_cookie, settings.workzilla_agent_id, settings.superjob_api_key,
            *_cookie_values(settings.workzilla_cookie),
        ]  # fmt: skip
        if settings
        else []
    )
    handler = logging.StreamHandler()
    handler.setFormatter(RedactingFormatter(LOG_FORMAT, secrets))
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    # httpx на INFO печатает полный адрес запроса — а в нём токен бота.
    for noisy in ("httpx", "httpcore", "telegram", "apscheduler", "telethon"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _cookie_values(cookie: str | None) -> list[str]:
    """Значения из строки Cookie по отдельности: в логе может оказаться одно из них."""
    values = (part.partition("=")[2].strip() for part in (cookie or "").split(";"))
    return [v for v in values if len(v) >= 8]  # короткие («1», «ru») — не секрет и шум


def _is_telegram_failure(exc: BaseException, token: str | None) -> bool:
    """Ошибка Telegram или любая ошибка, в тексте которой (с цепочкой причин) есть токен."""
    from telegram.error import TelegramError

    if isinstance(exc, TelegramError):
        return True
    return bool(token) and token in "".join(traceback.format_exception(exc))


def make_bot(token: str):
    from telegram import Bot

    return Bot(token)


def enabled_platforms(
    settings: Settings, platform: str | None, include_disabled: bool
) -> list[Platform]:
    chosen = [p for p in PLATFORMS if platform in (None, p)]
    return [p for p in chosen if include_disabled or settings.sources[p].enabled]


def build_sources(
    settings: Settings,
    platform: str | None,
    include_disabled: bool,
    storage: Storage | None = None,
) -> list[Source]:
    """`storage` — чтобы Work24 и Freelance.ru не открывали карточки, которые уже есть в базе,
    и чтобы вакансии SuperJob, «Работы России» и Telegram-каналов искались под пройденную
    анкету владельца."""
    fetcher = Fetcher()
    _fetchers.append(fetcher)
    sources: list[Source] = []
    search = VacancySearch(settings)

    def anketa() -> dict | None:
        return completed_anketa(storage, settings.owner_id) if storage is not None else None

    for name in enabled_platforms(settings, platform, include_disabled):
        src = settings.sources[name]
        known = (
            (lambda external_id, name=name: storage.get_project(name, external_id) is not None)
            if storage is not None
            else (lambda external_id: False)
        )
        if name == "hh":
            reader = TelethonReader(
                settings.hh_tg_api_id, settings.hh_tg_api_hash, settings.hh_tg_session, src.chat
            )
            _readers.append(reader)
            sources.append(HhSource(reader))
        elif name == "work24":
            sources.append(
                Work24Source(
                    fetcher, src.operations, max_age_hours=settings.max_age_for(name),
                    known=known,
                )  # fmt: skip
            )
        elif name == "freelance_ru":
            sources.append(
                FreelanceRuSource(
                    fetcher, max_age_hours=settings.max_age_for(name), categories=src.categories,
                    known=known,
                )  # fmt: skip
            )
        elif name == "superjob":
            sources.append(
                SuperJobSource(
                    fetcher, settings.superjob_api_key, src.queries,
                    max_age_hours=settings.max_age_for(name), catalogues=src.catalogues,
                    remote_only=src.remote_only,
                )  # fmt: skip
            )
            sources.append(AnketaVacancies("superjob", search, anketa))
        elif name in ("trudvsem", "tgchannels"):  # своих слов поиска нет — только под анкету
            sources.append(AnketaVacancies(name, search, anketa))
        elif name == "workzilla":
            own = Fetcher()  # cookie сессии не попадают в общий клиент других площадок
            _fetchers.append(own)
            sources.append(
                WorkzillaSource(
                    own, settings.workzilla_cookie, settings.workzilla_agent_id,
                    max_age_hours=settings.max_age_for(name), known=known,
                )  # fmt: skip
            )
        else:
            sources.append(
                FlSource(fetcher, src.urls) if name == "fl" else KworkSource(fetcher, src.urls)
            )
    return sources


async def _close_fetchers() -> None:
    while _fetchers:
        await _fetchers.pop().aclose()
    while _readers:
        await _readers.pop().aclose()


async def setup_telegram(
    bot, out: Callable[[str], None] = print, *, wait_seconds: float = SETUP_WAIT_SEC,
    poll_seconds: float = 2,
) -> int:
    """getMe, setMyCommands, затем ждёт /start и печатает user_id (значение не секретное)."""
    from telegram import BotCommand

    from freelance_bot.bot import COMMANDS

    me = await bot.get_me()
    out(f"Токен принят: бот @{me.username}.")
    await bot.set_my_commands([BotCommand(name, text) for name, text in COMMANDS])
    out("Меню команд зарегистрировано: " + ", ".join(f"/{n}" for n, _ in COMMANDS) + ".")
    out(f"Теперь напишите вашему боту /start в Telegram. Жду до {wait_seconds:g} с…")
    user_id = await wait_for_start(bot, wait_seconds=wait_seconds, poll_seconds=poll_seconds)
    if user_id is not None:
        out(f"Ваш user_id: {user_id}")
        out(f"Впишите в .env строку: TELEGRAM_OWNER_ID={user_id}")
        return 0
    out("Не дождался /start. Напишите боту /start и запустите setup-telegram ещё раз.")
    return 1


async def wait_for_start(
    bot, *, wait_seconds: float = SETUP_WAIT_SEC, poll_seconds: float = 2
) -> int | None:
    """user_id того, кто первым напишет боту /start, или None по истечении ожидания."""
    deadline = time.monotonic() + wait_seconds
    offset = None
    while True:
        remaining = max(0.0, deadline - time.monotonic())
        updates = await bot.get_updates(
            offset=offset, timeout=int(min(10, remaining)), allowed_updates=["message"]
        )
        for update in updates:
            offset = update.update_id + 1
            message = update.message
            if message and (message.text or "").startswith("/start") and message.from_user:
                return message.from_user.id
        if time.monotonic() >= deadline:
            return None
        await asyncio.sleep(poll_seconds)


def _load(args: argparse.Namespace) -> Settings:
    settings = load_settings(Path(args.settings), Path(args.env_file))
    if args.db:
        settings = replace(settings, db_path=Path(args.db))
    if getattr(args, "dry_run", False):
        settings = replace(settings, dry_run=True)
    max_age = getattr(args, "max_age_hours", None)
    if max_age is not None:  # флаг — для всех площадок, и со своим сроком тоже
        sources = {p: replace(s, max_age_hours=None) for p, s in settings.sources.items()}
        settings = replace(settings, max_age_hours=max_age, sources=sources)
    return settings


def _warn_kwork(platforms: list[Platform]) -> None:
    if "kwork" in platforms:
        print(KWORK_WARNING)


async def _cmd_once(args: argparse.Namespace, settings: Settings) -> int:
    platforms = enabled_platforms(settings, args.platform, args.include_disabled)
    if not platforms:
        print("Нет включённых площадок (sources.<площадка>.enabled в settings.toml).")
        return 1
    _warn_kwork(platforms)
    # Разовый прогон показывает находки сразу: дайджест — дело расписания в `run`
    settings = replace(settings, digest_enabled=False)
    profile = load_profile(Path(args.profile))
    storage = Storage(settings.db_path, tz=settings.timezone)
    notifier = ConsoleNotifier()
    bot = None
    if not args.console and settings.telegram_token and settings.owner_id:
        from freelance_bot.bot import TelegramNotifier

        bot = make_bot(settings.telegram_token)
        await bot.initialize()
        notifier = TelegramNotifier(bot, settings.owner_id)
    elif not args.console:
        print("Токена или TELEGRAM_OWNER_ID нет — карточки печатаются в консоль.")
    try:
        drafter = make_drafter(profile, settings, storage)
        summarizer = make_summarizer(settings, storage)
        total_errors = 0
        for platform in platforms:
            sources = build_sources(settings, platform, args.include_disabled, storage)
            pipeline = Pipeline(
                settings, storage, sources, drafter, notifier, summarizer=summarizer
            )
            report = await pipeline.run_once(platform)
            total_errors += len(report.errors)
            print(
                f"{PLATFORM_NAMES[platform]}: собрано {report.fetched}, новых {report.new}, "
                f"прошло {report.passed}, прислано {report.notified}, "
                f"ошибок {len(report.errors)}"
                + (" (dry-run)" if settings.dry_run else "")
            )
            for error in report.errors:
                print(f"  ошибка: {error}")
        return 0 if total_errors == 0 else 1
    finally:
        await _close_fetchers()
        if bot is not None:
            await bot.shutdown()
        storage.close()


def _cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    from freelance_bot.bot import ALLOWED_UPDATES, TelegramNotifier, build_application

    if not settings.telegram_token:
        print(BOTFATHER_HINT)
        print(f"Без токена можно проверить сбор командой: {ONCE_CONSOLE_COMMAND}")
        return 1
    if settings.owner_id is None:
        print(f"Не задан TELEGRAM_OWNER_ID. Запустите: {CLI} setup-telegram")
        return 1
    platforms = enabled_platforms(settings, None, False)
    _warn_kwork(platforms)
    profile = load_profile(Path(args.profile))
    storage = Storage(settings.db_path, tz=settings.timezone)
    pipeline = Pipeline(
        settings,
        storage,
        build_sources(settings, None, False, storage),
        make_drafter(profile, settings, storage),
        TelegramNotifier(None, settings.owner_id),
        summarizer=make_summarizer(settings, storage),
    )
    app = build_application(settings, storage, pipeline)
    print("Бот запущен" + (" (dry-run)" if settings.dry_run else "") + ". Остановка: Ctrl+C.")
    try:
        # Явно: без allowed_updates Telegram берёт список из прошлого getUpdates, а
        # setup-telegram просит только message — и нажатия кнопок не доходили бы.
        app.run_polling(allowed_updates=ALLOWED_UPDATES)
    finally:
        storage.close()
    return 0


def _cmd_setup(settings: Settings) -> int:
    if not settings.telegram_token:
        print(BOTFATHER_HINT)
        return 1

    async def go() -> int:
        bot = make_bot(settings.telegram_token)
        await bot.initialize()
        try:
            return await setup_telegram(bot)
        finally:
            await bot.shutdown()

    try:
        return asyncio.run(go())
    except Exception as exc:  # текст исключения может содержать адрес с токеном — не печатаем
        print(TELEGRAM_FAILURE_HINT.format(name=type(exc).__name__))
        return 1


async def _check_token(token: str) -> str | None:
    """username бота, если Telegram принял токен; иначе None (текст ошибки не печатается)."""
    bot = make_bot(token)
    try:
        await bot.initialize()
        return bot.username
    except Exception:  # noqa: BLE001 - текст ошибки может содержать адрес с токеном
        return None
    finally:
        with contextlib.suppress(Exception):
            await bot.shutdown()


async def _wait_owner(token: str) -> int | None:
    bot = make_bot(token)
    try:
        await bot.initialize()
        return await wait_for_start(bot)
    except Exception:  # noqa: BLE001
        return None
    finally:
        with contextlib.suppress(Exception):
            await bot.shutdown()


def _cmd_init(args: argparse.Namespace) -> int:
    from freelance_bot.setup_wizard import ConsolePrompter, run_wizard

    root = Path(args.settings).resolve().parent
    try:
        return asyncio.run(run_wizard(ConsolePrompter(), root, _check_token, _wait_owner))
    except (KeyboardInterrupt, EOFError):
        print("\nНастройка прервана. Ничего не записано.")
        return 1


def _cmd_hh_login(args: argparse.Namespace, settings: Settings) -> int:
    """Разовый вход пользователем Telegram: сессия → .env, проверка разбора уведомлений HH."""
    from freelance_bot.setup_wizard import ConsolePrompter, update_env_file
    from freelance_bot.sources import SourceFormatError
    from freelance_bot.sources.hh import collect, login

    chat = settings.sources["hh"].chat
    prompter = ConsolePrompter()
    print(HH_LOGIN_INTRO.format(chat=chat))
    api_id = settings.hh_tg_api_id
    while api_id is None:
        raw = prompter.ask("App api_id (число)")
        api_id = int(raw) if raw.isdigit() else None
    api_hash = settings.hh_tg_api_hash or prompter.secret("App api_hash")
    env_path = Path(args.env_file)
    if (api_id, api_hash) != (settings.hh_tg_api_id, settings.hh_tg_api_hash):
        # повторный запуск после неудачного входа не спрашивает их снова
        update_env_file(env_path, {"HH_TG_API_ID": str(api_id), "HH_TG_API_HASH": api_hash})
    try:
        session, notices = asyncio.run(
            login(api_id, api_hash, prompter.ask, prompter.secret, by_code=args.code, chat=chat)
        )
    except (KeyboardInterrupt, EOFError):
        print("\nВход прерван. Ничего не записано.")
        return 1
    except ImportError:
        print("Для QR-кода нужен пакет qrcode: .venv/bin/pip install qrcode. Или войдите "
              "по коду: hh-login --code")
        return 1
    except Exception as exc:  # текст ошибки Telethon может содержать телефон — только тип
        print(f"Не удалось войти в Telegram ({type(exc).__name__}). Ничего не записано.")
        return 1
    backup = update_env_file(env_path, {"HH_TG_SESSION": session})
    saved = f" (прежний — {backup.name})" if backup else ""
    print(f"✓ Сессия записана в {env_path}{saved}.")
    try:
        vacancies = collect(notices)
    except SourceFormatError as exc:
        vacancies = []
        print(f"⚠ {exc}")
    print(f"Сообщений от @{chat}: {len(notices)}, вакансий в них: {len(vacancies)}.")
    for p in vacancies[:5]:
        company = p.extra.get("company", "компания не указана")
        salary = p.extra.get("salary", "зарплата не указана")
        print(f"  • {p.title} — {company} — {salary} — {p.url}")
    print(HH_LOGIN_NEXT.format(chat=chat))
    return 0


def _cmd_workzilla_session(args: argparse.Namespace) -> int:
    """Сессия Work-zilla из api-anything → WORKZILLA_COOKIE и WORKZILLA_AGENT_ID в .env."""
    from freelance_bot.setup_wizard import update_env_file

    cookie, agent_id = session_from_api_anything(Path(args.source))
    env_path = Path(args.env_file)
    backup = update_env_file(
        env_path, {"WORKZILLA_COOKIE": cookie, "WORKZILLA_AGENT_ID": agent_id}
    )
    saved = f" (прежний — {backup.name})" if backup else ""
    print(f"✓ Сессия Work-zilla записана в {env_path}{saved}; значения не показываю.")
    print(WORKZILLA_SESSION_NEXT)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m freelance_bot")
    parser.add_argument("--settings", default="settings.toml", help="путь к settings.toml")
    parser.add_argument("--env-file", default=".env", help="путь к .env")
    parser.add_argument("--profile", default="profile/profile.toml", help="путь к profile.toml")
    parser.add_argument("--db", default=None, help="файл SQLite вместо storage.db_path")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="мастер первичной настройки своей копии бота")

    run = sub.add_parser("run", help="бот + расписание сбора")
    run.add_argument("--dry-run", action="store_true", help="ничего не отправлять на биржи")

    once = sub.add_parser("once", help="один цикл сбора")
    once.add_argument("--dry-run", action="store_true", help="ничего не отправлять на биржи")
    once.add_argument("--platform", choices=PLATFORMS, default=None)
    once.add_argument("--console", action="store_true", help="печатать карточки в консоль")
    once.add_argument(
        "--include-disabled", action="store_true",
        help="опросить площадку, даже если она выключена в settings.toml (разовый запуск)",
    )
    once.add_argument("--max-age-hours", type=float, default=None, help="окно свежести, часов")

    sub.add_parser("setup-telegram", help="проверить токен, зарегистрировать команды")
    hh_login = sub.add_parser(
        "hh-login", help="войти в свой Telegram, чтобы читать уведомления HH.ru о вакансиях"
    )
    hh_login.add_argument(
        "--code", action="store_true", help="вход по коду из Telegram вместо QR-кода"
    )
    wz = sub.add_parser(
        "workzilla-session", help="перенести сессию Work-zilla из api-anything в .env"
    )
    wz.add_argument(
        "--from", dest="source", required=True,
        help="файл сессии api-anything: <API_ANYTHING_HOME>/sessions/work-zilla.json",
    )  # fmt: skip
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":  # настроек ещё может не быть — мастер их создаёт
        return _cmd_init(args)
    try:
        settings = _load(args)
    except ConfigError as exc:
        _setup_logging(None)
        print(f"Ошибка настроек: {exc}")
        return 2
    _setup_logging(settings)
    try:
        if args.command == "setup-telegram":
            return _cmd_setup(settings)
        if args.command == "hh-login":
            return _cmd_hh_login(args, settings)
        if args.command == "workzilla-session":
            return _cmd_workzilla_session(args)
        if args.command == "once":
            return asyncio.run(_cmd_once(args, settings))
        return _cmd_run(args, settings)
    except (ConfigError, ProfileError) as exc:
        print(f"Ошибка настроек: {exc}")
        return 2
    except Exception as exc:
        # InvalidToken и др. несут токен в тексте: печатаем только тип ошибки и подсказку.
        if not _is_telegram_failure(exc, settings.telegram_token):
            raise
        print(TELEGRAM_FAILURE_HINT.format(name=type(exc).__name__))
        return 1


if __name__ == "__main__":
    sys.exit(main())
