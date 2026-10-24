"""CLI: ошибка Telegram в once/run не выводит токен ни в stdout, ни в stderr, ни в лог."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from telegram.error import InvalidToken

from freelance_bot import __main__ as cli
from freelance_bot import bot as bot_module

ROOT = Path(__file__).resolve().parent.parent
FAKE_TOKEN = "123456:ABCdefFAKEtokenNOTreal_0123456789"
REJECTED = f"The token `{FAKE_TOKEN}` was rejected by the server."
NOW_HH = datetime(2026, 10, 24, 18, 0, tzinfo=UTC)


@pytest.fixture
def env_file(tmp_path: Path) -> Path:
    path = tmp_path / ".env"
    path.write_text(f"TELEGRAM_BOT_TOKEN={FAKE_TOKEN}\nTELEGRAM_OWNER_ID=42\n")
    return path


@contextmanager
def bare_root_logger() -> Iterator[None]:
    """Корневой логгер без обработчиков pytest — как при настоящем запуске: main сам
    настраивает вывод логов в stderr (его launchd пишет в logs/freelance-bot.log)."""
    root = logging.getLogger()
    saved = root.handlers, root.level
    root.handlers = []
    try:
        yield
    finally:
        root.handlers, root.level = saved


def cli_args(tmp_path: Path, env_file: Path, *command: str) -> list[str]:
    return [
        "--env-file", str(env_file), "--settings", str(ROOT / "settings.toml"),
        "--profile", str(ROOT / "profile" / "profile.example.toml"),
        "--db", str(tmp_path / "t.sqlite3"),
        *command,
    ]  # fmt: skip


def assert_token_hidden(code: int, captured: pytest.CaptureResult) -> None:
    output = captured.out + captured.err
    assert code != 0
    assert FAKE_TOKEN not in output
    assert "123456:" not in output
    assert "InvalidToken" in captured.out  # тип ошибки и подсказка — есть
    assert "TELEGRAM_BOT_TOKEN" in captured.out


class RejectingBot:
    """Как Bot из PTB при отозванном токене: initialize бросает InvalidToken с токеном."""

    async def initialize(self) -> None:
        raise InvalidToken(REJECTED)

    async def shutdown(self) -> None:
        pass


def test_once_with_rejected_token_does_not_print_token(tmp_path, env_file, capsys, monkeypatch):
    monkeypatch.setattr(cli, "make_bot", lambda token: RejectingBot())
    code = cli.main(cli_args(tmp_path, env_file, "once", "--dry-run"))
    assert_token_hidden(code, capsys.readouterr())


def test_run_with_rejected_token_does_not_print_or_log_token(
    tmp_path, env_file, capsys, monkeypatch
):
    class RejectingApp:
        def run_polling(self, allowed_updates=None) -> None:
            # PTB и asyncio пишут ошибку в лог вместе с текстом исключения
            logging.getLogger("asyncio").error("Task exception was never retrieved: %s", REJECTED)
            raise InvalidToken(REJECTED)

    monkeypatch.setattr(
        bot_module, "build_application", lambda settings, storage, pipeline: RejectingApp()
    )
    with bare_root_logger():
        code = cli.main(cli_args(tmp_path, env_file, "run", "--dry-run"))
    captured = capsys.readouterr()
    assert_token_hidden(code, captured)
    assert "Task exception was never retrieved" in captured.err  # лог есть, токен скрыт


def test_run_asks_telegram_for_button_presses(tmp_path, env_file, monkeypatch):
    calls = []

    class App:
        def run_polling(self, allowed_updates=None) -> None:
            calls.append(allowed_updates)

    monkeypatch.setattr(bot_module, "build_application", lambda settings, storage, pipeline: App())
    assert cli.main(cli_args(tmp_path, env_file, "run", "--dry-run")) == 0
    # иначе действует allowed_updates прошлого getUpdates (setup-telegram: только message)
    assert calls == [["message", "callback_query"]]  # только то, что бот обрабатывает


def test_other_errors_are_not_swallowed(tmp_path, env_file, monkeypatch):
    class BrokenBot(RejectingBot):
        async def initialize(self) -> None:
            raise ZeroDivisionError("ошибка в коде, а не в Telegram")

    monkeypatch.setattr(cli, "make_bot", lambda token: BrokenBot())
    with pytest.raises(ZeroDivisionError):
        cli.main(cli_args(tmp_path, env_file, "once", "--dry-run"))


def test_run_without_token_hint_names_the_readme_command(tmp_path, capsys):
    command = ".venv/bin/python -m freelance_bot once --console"
    assert command in (ROOT / "README.md").read_text(encoding="utf-8")
    code = cli.main(cli_args(tmp_path, tmp_path / "none.env", "run"))
    out = capsys.readouterr().out
    assert code != 0
    assert command in out


def test_hh_login_writes_session_to_env_and_never_prints_it(tmp_path, capsys, monkeypatch):
    from freelance_bot.sources import hh

    session = "1BVtsOK4Bu-FAKE-SESSION-not-real"
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_OWNER_ID=42\nHH_TG_API_ID=111\nHH_TG_API_HASH=hhhash\n")
    seen = {}

    async def fake_login(api_id, api_hash, ask, secret, *, by_code, chat):
        seen.update(api_id=api_id, api_hash=api_hash, by_code=by_code, chat=chat)
        n = hh.Notice(1, NOW_HH, "1. Python-разработчик\n📍 Москва. 👉 Ромашка\n"
                      "Откликнуться на сайте: https://my.hh.ru/b/x1")  # fmt: skip
        return session, [n]

    monkeypatch.setattr(hh, "login", fake_login)
    code = cli.main(cli_args(tmp_path, env, "hh-login", "--code"))

    out = capsys.readouterr().out
    assert code == 0
    assert seen == {"api_id": 111, "api_hash": "hhhash", "by_code": True, "chat": "hh_rabota_bot"}
    assert session not in out and "hhhash" not in out
    assert "вакансий в них: 1" in out and "Python-разработчик — Ромашка" in out
    assert f"HH_TG_SESSION={session}" in env.read_text()
    assert "TELEGRAM_OWNER_ID=42" in env.read_text()  # прочее не тронуто
    assert env.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / ".env.bak").stat().st_mode & 0o777 == 0o600


def test_hh_login_failure_writes_nothing_and_hides_details(tmp_path, capsys, monkeypatch):
    from freelance_bot.sources import hh

    env = tmp_path / ".env"
    env.write_text("HH_TG_API_ID=111\nHH_TG_API_HASH=hhhash\n")

    async def failing_login(*args, **kwargs):
        raise RuntimeError("phone +79990000000 is banned")

    monkeypatch.setattr(hh, "login", failing_login)
    code = cli.main(cli_args(tmp_path, env, "hh-login"))

    out = capsys.readouterr().out
    assert code == 1 and "RuntimeError" in out and "+7999" not in out
    assert "HH_TG_SESSION" not in env.read_text()
