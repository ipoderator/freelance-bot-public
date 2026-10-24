"""Источники проектов: FL.ru (официальный RSS) и Kwork (публичная страница биржи)."""

from __future__ import annotations

from typing import Protocol

from freelance_bot.http import SourceError
from freelance_bot.models import Platform, Project


class SourceFormatError(SourceError):
    """Ответ площадки пришёл, но структура не та, что ожидали (сменился формат)."""


class Source(Protocol):
    platform: Platform

    async def fetch(self) -> list[Project]: ...
