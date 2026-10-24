"""Источники проектов: FL.ru (официальный RSS), Kwork (публичная страница биржи), Work24,
Freelance.ru и Work-zilla (операции API Anything), HH.ru (вакансии из сообщений бота HH в
Telegram пользователя)."""

from __future__ import annotations

from typing import Protocol

from freelance_bot.http import SourceError
from freelance_bot.models import Platform, Project


class SourceFormatError(SourceError):
    """Ответ площадки пришёл, но структура не та, что ожидали (сменился формат)."""


class Source(Protocol):
    platform: Platform

    async def fetch(self) -> list[Project]: ...
