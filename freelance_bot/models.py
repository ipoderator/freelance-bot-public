"""Общие типы проекта. Без ввода-вывода и бизнес-логики."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal, get_args

Platform = Literal[
    "fl", "kwork", "work24", "freelance_ru", "workzilla", "hh", "superjob", "trudvsem",
    "tgchannels",
]
Mode = Literal["notify", "draft", "auto"]

PLATFORMS: tuple[str, ...] = get_args(Platform)
MODES: tuple[str, ...] = get_args(Mode)
# Вакансии (HH, SuperJob, «Работа России», Telegram-каналы): карточка без черновика, отклик
# пользователь делает на сайте
VACANCY_PLATFORMS = frozenset({"hh", "superjob", "trudvsem", "tgchannels"})


@dataclass(frozen=True)
class Project:
    platform: Platform
    external_id: str  # FL: число из URL /projects/<id>/; Kwork: wants.id; Work24: id заказа;
    # Freelance.ru: число из /task/view/<id>; Work-zilla: id задания; HH: vacancy_key;
    # SuperJob: id вакансии; Telegram-каналы: хэш названия и описания поста
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


@dataclass(frozen=True)
class User:
    """Пользователь публичного бота (анкета — onboarding.py, ADR 0011)."""

    tg_id: int
    step: str  # шаг анкеты; "done" — пройдена
    data: dict[str, Any]  # ФИО, обращение, профиль из резюме, направления, площадки
    consent_at: datetime | None = None
    completed_at: datetime | None = None


LessonKind = Literal["topic", "level", "office", "pay", "company", "skill", "experience"]
LESSON_KINDS: tuple[str, ...] = get_args(LessonKind)


@dataclass(frozen=True)
class Lesson:
    """Чему бот научился по отметкам пользователя (learning.py, ADR 0013)."""

    id: int
    kind: LessonKind  # topic/level — слово в названии; skill — в тексте; office; pay; company;
    # experience — требуемый стаж из описания
    value: str  # слово, название компании, «vacancy»/«order» для pay; у office и experience — ""
    weight: int  # баллы (topic, level, skill, office); у pay — порог в рублях; у experience — лет
    hits: int  # сколько раз отмечено
    example: str  # название последнего заказа или вакансии, по которому научился
