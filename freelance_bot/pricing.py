"""Ориентировочная цена и срок отклика: тип работы, сложность ТЗ, бюджет заказчика.

Оценка = вилка по типу работы (TIERS: цена и дни) × сложность (признаки в ТЗ: оплата,
админка, LLM, несколько сайтов…), затем привязка к бюджету заказчика:
- бюджет не ниже оценки — предлагаем бюджет: заказчик сам назвал сумму;
- бюджет ниже — Kwork: не выше «допустимого» бюджета (possiblePriceLimit, выше площадка
  не даст), FL: нижняя граница вилки с пометкой «бюджет ниже оценки».
Срок не длиннее срока заказчика (Kwork max_days, «Срок сдачи» Work24). Вилки — рынок FL/Kwork для
разработчика с подтверждённым опытом ≈2 года (profile/profile.toml); правятся здесь.
Чистая функция: без сети и случайности.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from freelance_bot.config import Settings
from freelance_bot.models import Project, ScoreResult
from freelance_bot.textmatch import find_terms


@dataclass(frozen=True)
class Tier:
    label: str
    price: tuple[int, int]  # ₽, обычная вилка
    days: tuple[int, int]


TIERS: dict[str, Tier] = {
    "fix": Tier("доработка или настройка", (3000, 12000), (1, 4)),
    "parsing": Tier("парсер", (5000, 15000), (2, 5)),
    "automation": Tier("автоматизация или интеграция", (7000, 25000), (3, 7)),
    "telegram": Tier("бот", (10000, 30000), (4, 10)),
    "backend": Tier("backend или API", (15000, 45000), (5, 14)),
    "ai_agents": Tier("AI-агент или LLM-интеграция", (20000, 50000), (7, 14)),
    "service": Tier("веб-сервис или MVP", (40000, 120000), (14, 30)),
}
# При равенстве совпадений — более дорогой тип: в ТЗ «бот с ИИ» основная работа — ИИ.
_DIRECTION_PRIORITY = ("ai_agents", "backend", "telegram", "automation", "parsing")
BASE_POSITION = 0.35  # точка внутри вилки до поправок: ближе к низу, опыт ≈2 года
FEATURE_STEP = 0.15  # +15% за каждый признак сложности
FEATURES_MAX = 5
LONG_BRIEF_CHARS = 1500
LONG_BRIEF_STEP = 0.2
SIMPLE_FACTOR = 0.7

# Признаки сервиса ищутся в заголовке: «на платформе Nethouse» в описании — не MVP.
_SERVICE_RE = re.compile(
    r"\bmvp\b|веб-платформ|web-платформ|ai-платформ|ии-платформ|\bплатформ\w*\s+(?:для|по)\b|"
    r"веб-сервис|web-сервис|crm-систем|\berp\b|с\s+нуля",
    re.IGNORECASE,
)
_FIX_RE = re.compile(
    r"\b(?:исправ\w*|почин\w*|поправ\w*|доработ\w*|настро\w*|подключ\w*|установ\w*|"
    r"перенест\w*|перенос\w*|устран\w*|ошибк\w*|баг\w*|не\s+работает|перестал\w*)",
    re.IGNORECASE,
)
# «Создание бота и подключение оплаты» — новая работа, хотя в заголовке есть «подключение».
_CREATE_RE = re.compile(
    r"\b(?:созда\w*|разработ\w*|написа\w*|сделать|нужен|нужна|нужно|требуется)\b",
    re.IGNORECASE,
)
_SIMPLE_RE = re.compile(
    r"\b(?:прост\w*|небольш\w*|мелк\w*|несложн\w*|быстр\w+\s+задач\w*|на\s+час|пару\s+час\w*)",
    re.IGNORECASE,
)
_FEATURES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("оплата", re.compile(r"оплат|платеж|платёж|эквайр|юкасс|yookassa|robokassa|stripe", re.I)),
    ("админка", re.compile(r"админк|админ-панел|панел\w+\s+администр|личн\w+\s+кабинет", re.I)),
    ("база данных", re.compile(r"баз\w+\s+данных|postgres|mysql|sqlite|mongodb|\bбд\b", re.I)),
    ("LLM", re.compile(r"\b(?:llm|gpt|openai|claude|gigachat|yandexgpt|нейросет|rag)", re.I)),
    ("Mini App", re.compile(r"mini\s*app|мини-?апп|webapp", re.I)),
    ("интеграции", re.compile(r"интеграц|\bcrm\b|amocrm|битрикс24|bitrix24|webhook|вебхук", re.I)),
    ("авторизация", re.compile(r"авторизац|регистрац\w+\s+пользоват|роли\s+пользоват", re.I)),
    ("отчёты", re.compile(r"дашборд|dashboard|отч[её]т|аналитик|графи[кч]", re.I)),
    ("рассылки", re.compile(r"рассылк|уведомлен", re.I)),
    ("голос/распознавание", re.compile(r"распозна|\bocr\b|\bstt\b|\btts\b|голосов", re.I)),
    ("несколько источников", re.compile(r"\b\d{2,}\s+сайт|нескольк\w+\s+сайт|маркетплейс", re.I)),
    ("антибот/прокси", re.compile(r"прокси|proxy|капч|captcha|антибот|cloudflare", re.I)),
)


@dataclass(frozen=True)
class Estimate:
    price_rub: int
    days: int
    basis: str  # «бот; учтено: оплата, база данных; в пределах бюджета заказчика»
    below_budget: bool  # бюджет заказчика ниже оценки


def _fmt(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _round_price(value: float) -> int:
    step = 500 if value < 10000 else 1000 if value < 100000 else 5000
    return max(step, int(round(value / step)) * step)


def _tier_key(p: Project, r: ScoreResult, settings: Settings) -> str:
    if _SERVICE_RE.search(p.title) or re.search(r"\bmvp\b", p.description, re.IGNORECASE):
        return "service"
    fix = bool(_FIX_RE.search(p.title)) and not _CREATE_RE.search(p.title)
    best, best_points = "", 0
    for key in _DIRECTION_PRIORITY:
        direction = settings.directions.get(key)
        if direction is None or key not in r.directions:
            continue
        in_title = find_terms(p.title, direction.keywords)
        in_desc = [t for t in find_terms(p.description, direction.keywords) if t not in in_title]
        points = 2 * len(in_title) + len(in_desc)
        if points > best_points:
            best, best_points = key, points
    if fix:  # «Настроить ChatGPT», «подключить ИИ к CRM» — не правка, но и не агент с нуля
        return "automation" if best == "ai_agents" else "fix"
    return best or "fix"


def estimate(p: Project, r: ScoreResult, settings: Settings) -> Estimate:
    text = f"{p.title}\n{p.description}"
    tier = TIERS[_tier_key(p, r, settings)]
    features = [name for name, pattern in _FEATURES if pattern.search(text)][:FEATURES_MAX]
    factor = 1.0 + FEATURE_STEP * len(features)
    if len(p.description) > LONG_BRIEF_CHARS:
        factor += LONG_BRIEF_STEP
    simple = bool(_SIMPLE_RE.search(text))
    if simple:
        factor *= SIMPLE_FACTOR

    lo, hi = tier.price
    ours = (lo + (hi - lo) * BASE_POSITION) * factor
    ours = min(max(ours, lo * (SIMPLE_FACTOR if simple else 1.0)), hi * 2)
    d_lo, d_hi = tier.days
    days = math.ceil(min(max((d_lo + (d_hi - d_lo) * BASE_POSITION) * factor, d_lo), d_hi * 2))

    notes = [tier.label]
    if features:
        notes.append("учтено: " + ", ".join(features))
    elif simple:
        notes.append("простая задача")

    budget = max((v for v in (p.budget_min, p.budget_max) if v), default=None)
    ceiling = p.extra.get("possible_price_limit") if p.platform == "kwork" else None
    below = False
    if budget is None:
        price = _round_price(ours)
        notes.append("бюджет не указан")
    elif budget >= ours:
        price = budget
        notes.append("по бюджету заказчика")
        # бюджет сильно выше оценки — заказчик ждёт больше работы, чем видно из ТЗ
        days = min(math.ceil(days * math.sqrt(budget / ours)), d_hi * 3)
    else:
        below = True
        floor = lo * (SIMPLE_FACTOR if simple else 1.0)
        if isinstance(ceiling, int) and ceiling > budget:
            price = min(_round_price(ours), ceiling)
            notes.append(f"бюджет ниже оценки, допустимый на Kwork — до {_fmt(ceiling)} ₽")
        else:
            price = _round_price(max(budget, floor))
            notes.append("бюджет заказчика ниже оценки")
    if p.deadline_days and p.deadline_days < days:  # Kwork max_days, срок сдачи Work24
        days = p.deadline_days
        notes.append(f"срок заказчика {p.deadline_days} дн.")
    return Estimate(price_rub=price, days=max(1, days), basis="; ".join(notes), below_budget=below)
