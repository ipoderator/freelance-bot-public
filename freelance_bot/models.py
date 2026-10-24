"""Общие типы проекта. Без ввода-вывода и бизнес-логики."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, get_args

Platform = Literal["fl", "kwork", "work24", "freelance_ru", "workzilla", "hh"]
Mode = Literal["notify", "draft", "auto"]

PLATFORMS: tuple[str, ...] = get_args(Platform)
MODES: tuple[str, ...] = get_args(Mode)


@dataclass(frozen=True)
class Project:
    platform: Platform
    external_id: str  # FL: число из URL /projects/<id>/; Kwork: wants.id; Work24: id заказа;
    # Freelance.ru: число из /task/view/<id>; Work-zilla: id задания; HH: vacancy_key
    url: str
    title: str
    description: str  # полный или обрезанный текст ТЗ
    published_at: datetime  # tz-aware UTC
    budget_min: int | None  # рубли; None — не указан. HH: зарплата в месяц
    budget_max: int | None
    offers_count: int | None  # None — нет данных
    category: str | None
    deadline_days: int | None = None  # Kwork max_days; Work24 — дней до «Срок сдачи»;
    # Freelance.ru — «Срок» из карточки
    extra: dict = field(default_factory=dict)  # платформенные мелочи (buyer stats и т.п.)


@dataclass(frozen=True)
class ScoreResult:
    score: int  # 0..100
    passed: bool  # score >= min_score и не исключён
    directions: list[str]  # id направлений: backend, telegram, ai_agents, automation, parsing
    matched_terms: list[str]
    excluded_reason: str | None  # причина отсечения
    reasons: list[str]  # человекочитаемые причины совпадения для карточки
    client_requirements: list[str]  # извлечённые требования заказчика к отклику


@dataclass(frozen=True)
class Draft:
    text: str
    generator: Literal["builtin", "llm"]
    price_placeholder: bool  # True — в тексте стоит [ЦЕНА]
    deadline_placeholder: bool  # True — в тексте стоит [СРОК]


@dataclass(frozen=True)
class ModeDecision:
    requested: Mode
    effective: Mode
    reasons: list[str]  # непусто, если effective != requested
    send: bool  # True только при effective == "auto" и не dry-run
