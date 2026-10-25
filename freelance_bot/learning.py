"""Самообучение по отметкам пользователя (ADR 0013).

«👎 Не подходит» под карточкой и причина кнопкой — правило в таблице `lessons`:
- 🧭 не моя тема — слова из названия (без слов профиля и общих) получают штраф;
- 🎓 не мой уровень — слова уровня из названия (senior, ведущий, junior…) — штраф;
- 🏢 офис/переезд — вакансии без удалённой работы получают штраф;
- 💰 мало платят — заказы/вакансии с указанной оплатой не выше этой больше не приходят;
- 🚫 компания — вакансии компании больше не приходят.
«✅ Откликнулся» — слова из названия получают небольшую прибавку.

Правила видны в /learned и снимаются там же; в карточке — строка «🧠 Ваши отметки».
Без нейросети: решение пользователя — «правила из отметок».
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from freelance_bot.config import Settings
from freelance_bot.models import VACANCY_PLATFORMS, Lesson, Project
from freelance_bot.textmatch import find_terms

TOPIC_STEP = -15  # за отметку «не моя тема»: обычно ниже проходного; повтор — сильнее
TOPIC_FLOOR = -45
LEVEL_STEP = -15
LEVEL_FLOOR = -30
OFFICE_STEP = -15
OFFICE_FLOOR = -30
APPLIED_STEP = 5  # «откликнулся» — слова из названия чуть выше
APPLIED_CEIL = 15
PENALTY_CAP = 45  # сумма штрафов отметок за один заказ
BONUS_CAP = 15
MAX_TERMS = 5  # слов из одного названия

REASONS: dict[str, str] = {
    "topic": "🧭 Не моя тема",
    "pay": "💰 Мало платят",
    "office": "🏢 Офис / переезд",
    "company": "🚫 Компания",
    "level": "🎓 Не мой уровень",
}
VACANCY_ONLY = frozenset({"office", "company"})  # у заказов бирж нет офиса и работодателя

LEVEL_TERMS = (
    "senior", "lead", "team lead", "teamlead", "тимлид", "head", "architect", "архитектор",
    "principal", "ведущ", "старш", "главн", "руководител", "middle", "мидл", "junior",
    "джуниор", "младш", "стажер", "intern", "trainee", "начинающ",
)  # fmt: skip
REMOTE_TERMS = ("удаленн", "удаленк", "remote", "на дому", "дистанцион")
# Слова, по которым «не моя тема» не учится: они есть почти в любой IT-вакансии, и штраф
# за них отсёк бы всё подряд
GENERIC_TERMS = frozenset(
    {"разработчик", "программист", "developer", "engineer", "инженер", "специалист",
     "сотрудник", "работа", "работы", "работе", "работу", "работ", "отдел", "отдела",
     "группа", "группы", "вакансия", "требуется", "нужен", "нужна", "нужно",
     "срочно", "проект", "проекта", "задача", "задачи", "компания", "компании", "для",
     "или", "без", "при", "под", "над", "как", "все", "нас", "вас", "наш", "the", "and",
     "for", "with", "удаленно", "удаленная", "удаленный", "офис", "город", "москва",
     "оплата", "зарплата", "день", "дня", "дней", "месяц"}
)  # fmt: skip
_WORD_RE = re.compile(r"[a-zа-я0-9+#]+")
_ENDINGS = (
    "ами", "ями", "ого", "его", "ому", "ему", "ыми", "ими", "ой", "ый", "ий", "ая", "яя",
    "ое", "ее", "ые", "ие", "ов", "ев", "ам", "ям", "ах", "ях", "ом", "ем", "а", "я", "ы",
    "и", "у", "ю", "е", "о", "ь",
)  # fmt: skip
MIN_STEM = 4


@dataclass(frozen=True)
class Lessons:
    """Поправка отметок к оценке одного заказа."""

    points: int = 0
    reasons: tuple[str, ...] = ()
    excluded: str | None = None


@dataclass(frozen=True)
class Change:
    """Что сохранить после отметки и что сказать пользователю."""

    kind: str
    value: str
    weight: int


def _norm(text: str) -> str:
    return text.lower().replace("ё", "е")


def _stem(word: str) -> str:
    """Грубая основа русского слова: «аналитика» → «аналитик» — find_terms ловит формы."""
    if not re.fullmatch(r"[а-я]+", word) or len(word) < MIN_STEM + 2:
        return word
    for ending in _ENDINGS:
        if word.endswith(ending) and len(word) - len(ending) >= MIN_STEM:
            return word[: -len(ending)]
    return word


def _profile_words(settings: Settings) -> set[str]:
    return {
        w for d in settings.directions.values() for t in d.keywords
        for w in _WORD_RE.findall(_norm(t))
    }  # fmt: skip


def _in_profile(word: str, stem: str, profile: set[str]) -> bool:
    """«ботов» — форма «бот», «fastapi» — само слово профиля: за них не штрафовать."""
    return any(
        word == k or (len(k) >= 3 and (word.startswith(k) or k.startswith(stem)))
        for k in profile
    )


def topic_terms(title: str, settings: Settings) -> list[str]:
    """Слова названия, за которые можно штрафовать: не слова профиля, не общие, не уровень."""
    profile = _profile_words(settings)
    terms: list[str] = []
    for word in _WORD_RE.findall(_norm(title)):
        if len(word) < 3 or word.isdigit() or word in GENERIC_TERMS:
            continue
        stem = _stem(word)
        if _in_profile(word, stem, profile) or find_terms(word, LEVEL_TERMS):
            continue
        if stem not in terms and stem not in GENERIC_TERMS:
            terms.append(stem)
    return terms[:MAX_TERMS]


def level_terms(title: str) -> list[str]:
    return find_terms(_norm(title), LEVEL_TERMS)


def is_remote(p: Project) -> bool:
    area = _norm(str(p.extra.get("area", "")))
    return "удал" in area or bool(find_terms(_norm(f"{p.title}\n{p.description}"), REMOTE_TERMS))


def company(p: Project) -> str:
    return " ".join(str(p.extra.get("company", "")).split())


def budget(p: Project) -> int | None:
    amounts = [v for v in (p.budget_min, p.budget_max) if v]
    return max(amounts) if amounts else None


def pay_scope(p: Project) -> str:
    return "vacancy" if p.platform in VACANCY_PLATFORMS else "order"


def reasons_for(p: Project) -> list[str]:
    """Причины для кнопок под «Не подходит»: у заказов бирж — без офиса и компании."""
    vacancy = p.platform in VACANCY_PLATFORMS
    return [k for k in REASONS if vacancy or k not in VACANCY_ONLY]


def _clamp(value: int, low: int, high: int) -> int:
    return max(low, min(high, value))


def learn(
    reason: str, p: Project, settings: Settings, known: Sequence[Lesson]
) -> list[Change]:
    """Правила из отметки. Пусто — запоминать нечего (нет слов, зарплаты, компании)."""
    current = {(x.kind, x.value): x.weight for x in known}

    def step(kind: str, value: str, delta: int, low: int, high: int = 0) -> Change:
        old = current.get((kind, value), 0)
        start = min(old, 0) if delta < 0 else max(old, 0)
        return Change(kind, value, _clamp(start + delta, low, high))

    if reason == "topic":
        return [step("topic", t, TOPIC_STEP, TOPIC_FLOOR) for t in topic_terms(p.title, settings)]
    if reason == "level":
        return [step("level", t, LEVEL_STEP, LEVEL_FLOOR) for t in level_terms(p.title)]
    if reason == "office":
        return [step("office", "", OFFICE_STEP, OFFICE_FLOOR)]
    if reason == "pay":
        amount = budget(p)
        if amount is None:
            return []
        scope = pay_scope(p)
        return [Change("pay", scope, max(amount, current.get(("pay", scope), 0)))]
    if reason == "company":
        name = company(p)
        return [Change("company", _norm(name), 0)] if name else []
    if reason == "applied":
        return [
            step("topic", t, APPLIED_STEP, TOPIC_FLOOR, APPLIED_CEIL)
            for t in topic_terms(p.title, settings)
        ]
    return []


def apply(p: Project, lessons: Iterable[Lesson]) -> Lessons:
    """Поправка к баллу и отсечение по правилам пользователя."""
    title = _norm(p.title or "")
    penalty, bonus, parts = 0, 0, []
    excluded: str | None = None
    vacancy = p.platform in VACANCY_PLATFORMS
    for x in lessons:
        if x.kind in ("topic", "level") and x.weight and find_terms(title, [x.value]):
            if x.weight < 0:
                penalty += -x.weight
            else:
                bonus += x.weight
            parts.append(f"«{x.value}» {_signed(x.weight)}")
        elif x.kind == "office" and vacancy and x.weight < 0 and not is_remote(p):
            penalty += -x.weight
            parts.append(f"без удалёнки {_signed(x.weight)}")
        elif x.kind == "pay" and x.value == pay_scope(p) and excluded is None:
            amount = budget(p)
            if amount is not None and amount <= x.weight:
                excluded = f"мало платят: по вашей отметке — не выше {_rub(x.weight)} ₽"
        elif x.kind == "company" and vacancy and company(p) and _norm(company(p)) == x.value:
            excluded = "компания в вашем стоп-листе"
    points = min(bonus, BONUS_CAP) - min(penalty, PENALTY_CAP)
    reasons = (f"🧠 Ваши отметки: {', '.join(parts)}",) if parts else ()
    return Lessons(points, reasons, excluded)


def _signed(value: int) -> str:
    return f"+{value}" if value > 0 else f"−{-value}"


def _rub(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def describe(change: Change) -> str:
    """Одно правило словами — для ответа на отметку и /learned."""
    if change.kind in ("topic", "level"):
        return f"«{change.value}» в названии: {_signed(change.weight)} к баллу"
    if change.kind == "office":
        return f"вакансии без удалённой работы: {_signed(change.weight)} к баллу"
    if change.kind == "pay":
        what = "вакансии с зарплатой" if change.value == "vacancy" else "заказы с бюджетом"
        return f"{what} {_rub(change.weight)} ₽ и ниже не присылаю"
    if change.kind == "company":
        return f"вакансии компании «{change.value}» не присылаю"
    return change.value


def nothing_to_learn(reason: str) -> str:
    """Подсказка, когда из отметки нечего запомнить: выбрать другую причину."""
    return {
        "topic": "В названии только слова из вашего профиля и общие — запоминать нечего. "
        "Выберите другую причину.",
        "level": "В названии нет уровня (senior, ведущий, junior…). Выберите другую причину.",
        "pay": "Оплата не указана — нечего сравнивать. Выберите другую причину.",
        "company": "Компания не указана. Выберите другую причину.",
    }.get(reason, "Запоминать нечего.")
