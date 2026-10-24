"""Общие фикстуры тестов. Тесты не ходят в сеть и не читают настоящий .env."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from freelance_bot.models import Project

SECRET_ENV_VARS = (
    "TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID", "ANTHROPIC_API_KEY", "LLM_MODEL",
    "OPENAI_API_KEY", "OPENAI_MODEL", "GROQ_API_KEY", "GROQ_MODEL",
    "HH_TG_API_ID", "HH_TG_API_HASH", "HH_TG_SESSION", "WORKZILLA_COOKIE", "WORKZILLA_AGENT_ID",
)  # fmt: skip
REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _clean_secret_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Переменные окружения машины не должны влиять на тесты."""
    for name in SECRET_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture
def make_project():
    """Фабрика проектов: make_project(external_id="1", title="...", ...)."""

    def _make(**overrides) -> Project:
        values = dict(
            platform="fl",
            external_id="1",
            url="https://www.fl.ru/projects/1/test/",
            title="Telegram-бот на Python",
            description="Нужен бот с FastAPI-бэкендом.",
            published_at=datetime(2026, 10, 21, 9, 0, tzinfo=UTC),
            budget_min=None,
            budget_max=30000,
            offers_count=3,
            category="Программирование",
        )
        values.update(overrides)
        return Project(**values)

    return _make
