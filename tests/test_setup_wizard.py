"""Мастер `init`: сценарий ввода, запись файлов, повторный запуск, ошибки. Сети нет."""

from __future__ import annotations

import shutil
import stat
from pathlib import Path

import pytest

from freelance_bot.config import load_settings
from freelance_bot.profile import load_profile
from freelance_bot.setup_wizard import run_wizard, set_custom_direction, set_toml_value

ROOT = Path(__file__).resolve().parent.parent
TOKEN = "123456789:AAH-test-token-not-real-0123456789abc"
OPENAI = "sk-test-key-not-real-0123456789"


class Script:
    """Ответы по порядку; секреты — отдельной очередью."""

    def __init__(self, answers: list[str], secrets: list[str]) -> None:
        self.answers, self.secrets, self.lines = list(answers), list(secrets), []

    def ask(self, question: str, default: str = "") -> str:
        answer = self.answers.pop(0)
        return answer or default

    def secret(self, question: str) -> str:
        return self.secrets.pop(0)

    def out(self, text: str) -> None:
        self.lines.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


async def ok_token(token: str) -> str | None:
    return "my_test_bot"


async def owner(token: str) -> int | None:
    return 777


HAPPY = [
    "",  # узнать ID автоматически — да
    "1",  # OpenAI
    "Анна",
    "3,5",
    "да",  # делала ботов
    "Делала бота @anna_dev",  # контакт — отклоняется
    "3 года backend на Python и FastAPI в компании Ромашка",
    "Telegram-боты на aiogram для интернет-магазинов",
    "",  # конец фактов
    "1, 6",  # backend + свои слова
    "Vue, nuxt , vue",
    "20",
    "да",
    "9",
    "",  # Kwork — нет
]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    shutil.copy(ROOT / "settings.toml", tmp_path / "settings.toml")
    shutil.copy(ROOT / ".env.example", tmp_path / ".env.example")
    return tmp_path


async def test_wizard_writes_working_settings_profile_and_private_env(root: Path):
    p = Script(HAPPY, [TOKEN, OPENAI])

    assert await run_wizard(p, root, ok_token, owner) == 0

    env = root / ".env"
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    settings = load_settings(root / "settings.toml", env)
    assert settings.telegram_token == TOKEN and settings.owner_id == 777
    assert settings.openai_api_key == OPENAI and settings.openai_model == "gpt-5-mini"
    assert settings.min_score == 20 and settings.experience_years == 3.5
    assert list(settings.directions) == ["backend", "custom"]
    assert settings.directions["custom"].keywords == ["vue", "nuxt"]
    assert settings.intersections == []  # пары с выключенными направлениями пропущены
    assert settings.digest_enabled and settings.digest_start_hour == 9
    assert settings.sources["kwork"].enabled is False

    profile = load_profile(root / "profile" / "profile.toml")
    assert profile.name == "Анна" and profile.experience_years == 3.5
    assert profile.built_bots_or_agents is True
    assert [f.text for f in profile.confirmed_experience] == [HAPPY[6], HAPPY[7]]
    assert profile.confirmed_experience[0].tags == ["backend"]
    assert profile.include_contacts is False

    assert TOKEN not in p.text and OPENAI not in p.text  # секреты не печатаются
    assert "без ссылок, @ников и телефонов" in p.text and "@my_test_bot" in p.text
    assert "# Минимальный итоговый балл" in (root / "settings.toml").read_text()  # комментарии


async def test_rerun_asks_first_and_keeps_backups(root: Path):
    await run_wizard(Script(HAPPY, [TOKEN, OPENAI]), root, ok_token, owner)

    p = Script(["нет"], [])
    assert await run_wizard(p, root, ok_token, owner) == 0 and "Ничего не изменено" in p.text

    again = ["да", *HAPPY[:9], "1,6", "go, rust", "25", "нет", ""]
    assert await run_wizard(Script(again, [TOKEN, OPENAI]), root, ok_token, owner) == 0
    text = (root / "settings.toml").read_text()
    assert text.count("[directions.custom]") == 1 and '"go", "rust"' in text
    assert (root / "settings.toml.bak").exists() and (root / "profile" / "profile.toml.bak")
    assert stat.S_IMODE((root / ".env.bak").stat().st_mode) == 0o600
    assert load_settings(root / "settings.toml", root / ".env").digest_enabled is False


async def test_bad_input_aborts_without_writing(root: Path):
    p = Script([], ["123", "abc", "no:token"])
    assert await run_wizard(p, root, ok_token, owner) == 1
    assert "Ничего не записано" in p.text
    assert not (root / ".env").exists() and not (root / "profile").exists()


async def test_manual_owner_id_and_no_llm(root: Path):
    async def no_owner(token: str) -> int | None:
        return None

    answers = ["", "12ab", "4242", "3", *HAPPY[2:]]
    assert await run_wizard(Script(answers, [TOKEN]), root, ok_token, no_owner) == 0
    settings = load_settings(root / "settings.toml", root / ".env")
    assert settings.owner_id == 4242
    assert settings.openai_api_key is None and settings.anthropic_api_key is None


async def test_groq_key_goes_to_env(root: Path):
    groq = "gsk_testkeynotreal0123456789abcdef"
    answers = ["", "4", *HAPPY[2:]]
    p = Script(answers, [TOKEN, "sk-wrong-format-0123456789", groq])
    assert await run_wizard(p, root, ok_token, owner) == 0
    settings = load_settings(root / "settings.toml", root / ".env")
    assert settings.groq_api_key == groq and settings.groq_model == "openai/gpt-oss-120b"
    assert settings.openai_api_key is None and groq not in p.text


def test_toml_edits_keep_comments_and_escape_values():
    text = "[general]\n# порог\nmin_score = 30\n\n[digest]\nenabled = false\n"
    text = set_toml_value(text, "general", "min_score", 10)
    text = set_toml_value(text, "general", "directions", ['a"b', "в"])
    text = set_toml_value(text, "scoring", "experience_years", 2.5)
    assert "# порог\nmin_score = 10\n" in text and 'directions = ["a\\"b", "в"]' in text
    assert text.rstrip().endswith("[scoring]\nexperience_years = 2.5")
    assert set_custom_direction(set_custom_direction(text, ["x"]), []).count("custom") == 0
