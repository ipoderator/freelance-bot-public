"""Смысл должности под резюме (ADR 0018): точная должность, её специализация, сфера бизнеса и
уровень важнее общего слова в названии.

«Администратор кофейни» и «Администратор магазина» совпадают по слову, но это разные работы;
«Повар горячего цеха» — не «Повар мясного цеха»; «Главный бухгалтер» — не для бухгалтера по
первичной документации. Сфера и специализация ищутся только в названии вакансии и названии
компании: в описании магазина «кофе» встречается среди товаров, а не как место работы.

Чистые функции без ввода-вывода: профиль собирает `matching.user_settings`, применяет
`scoring.score_project` (как отметки, learning.apply).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from freelance_bot.textmatch import find_terms

EXACT_POINTS = 15  # все уточнения желаемой должности есть в названии
PARTIAL_POINTS = 6  # часть уточнений
SAME_POINTS = 8  # та же специализация или сфера, если должность не совпала точно
CONFLICT_POINTS = -25  # другая специализация («мясной цех» повару горячего)
LEVEL_POINTS = -10  # «старший»/«ведущий» при небольшом стаже
SENIOR_YEARS = 3.0  # с таким стажем «старший» и «ведущий» — свой уровень
FIT_PREFIX = "Должность: "  # строка причины в ScoreResult.reasons (selection_reason)

# Сфера бизнеса: по названию вакансии и компании. Сети — по названию компании
SPHERES: dict[str, tuple[str, ...]] = {
    "общепит": (
        "кафе", "кофе", "кофеен", "coffee", "ресторан", "бар", "бара", "бариста", "общепит",
        "столов", "пекарн", "пиццер", "суши", "бургер", "фастфуд", "фуд-корт", "кулинар",
        "банкет", "кейтеринг", "буфет", "закусочн", "шаурм", "чайхан", "гриль-бар",
        "шоколадниц", "вкусно и точка", "kfc", "бургер кинг", "теремок", "додо", "росинтер",
    ),
    "торговля": (
        "магазин", "супермаркет", "гипермаркет", "минимаркет", "торгов", "розниц", "бутик",
        "универмаг", "пятерочк", "магнит", "перекрест", "дикси", "лента", "ашан", "вкусвилл",
        "fix price", "светофор", "красное&белое", "красное и белое", "бристоль", "spar",
        "азбука вкуса", "верный", "чижик", "монетка", "доброцен", "м.видео", "эльдорадо",
        "dns", "детский мир", "спортмастер", "леруа", "hoff", "x5", "х5",
    ),
    "гостиницы": (
        "гостиниц", "отел", "hotel", "хостел", "санатор", "пансионат", "база отдыха",
        "курорт", "апарт",
    ),
    "красота": (
        "салон красоты", "салона красоты", "барбершоп", "маникюр", "spa", "спа", "косметолог",
        "парикмахер", "beauty", "ногтев",
    ),
    "медицина": (
        "клиник", "медицин", "стоматолог", "больниц", "поликлиник", "аптек", "медцентр",
    ),
    "фитнес": ("фитнес", "спортзал", "тренажер", "бассейн", "fitness"),
    "автосервис": ("автосервис", "автомойк", "шиномонтаж", "автосалон", "автоцентр"),
    "склад": ("склад", "логистическ", "распределительн", "фулфилмент"),
    "цветы": ("цветочн", "цветов", "флорист", "букет"),
    "детский сад": ("детский сад", "детского сада", "детсад", "доу"),
}  # fmt: skip
# Одно слово должности — разная работа в разных сферах: конфликт сферы отсекает вакансию.
# У остальных (повар, продавец, уборщик) сфера только поднимает свою: работа та же
SPHERE_BOUND_HEADS = (
    "администрат", "управляющ", "менеджер", "директор", "заведующ", "руководител",
)  # fmt: skip
# Специализации внутри профессии: по основе должности → по названию специализации
SPECIALTIES: dict[str, dict[str, tuple[str, ...]]] = {
    "повар": {
        "горячий цех": ("горяч",),
        "холодный цех": ("холодн",),
        "мясной цех": ("мясн", "мясо"),
        "рыбный цех": ("рыбн",),
        "кондитерский цех": ("кондитер",),
        "заготовочный цех": ("заготов",),
        "пицца": ("пицц",),
        "суши": ("суши", "сушист", "ролл"),
        "выпечка": ("выпеч", "пекар"),
        "гриль": ("гриль", "мангал", "шашлык"),
    },
    "бухгалт": {
        "первичная документация": ("первичн", "первичк"),
        "заработная плата": ("заработн", "зарплат", "расчету заработ", "расчет заработ"),
        "банк и касса": ("банк", "касс"),
        "материалы и склад": ("материал", "тмц", "склад", "товарно"),
        "налоги и отчётность": ("ндс", "налог", "отчетност"),
        "расчёты с контрагентами": ("дебитор", "кредитор", "контрагент", "поставщик",
                                    "реализац"),
        "основные средства": ("основных средств", "основные средства"),
    },
}  # fmt: skip
# «Повар-универсал» умеет любой цех: специализация не сравнивается
UNIVERSAL = ("универсал",)
# Уровень выше рядовой должности: если его нет в резюме — не та работа
TOP_LEVELS = (
    "главн", "руководител", "начальник", "директор", "заведующ", "управляющ", "шеф",
    "заместител", "head of",
)  # fmt: skip
MID_LEVELS = ("старш", "ведущ", "senior", "lead")
# Уточнения, по которым нельзя сравнить должности: предлоги и слова вида работы
WEAK_QUALIFIERS = frozenset(
    {"цеха", "цех", "смены", "смен", "зала", "работ", "сотрудн", "специал", "по", "на", "в"}
)  # fmt: skip


def _norm(text: str) -> str:
    return text.casefold().replace("ё", "е")


def _groups(text: str, groups: dict[str, tuple[str, ...]]) -> set[str]:
    text = _norm(text)
    return {name for name, terms in groups.items() if find_terms(text, terms)}


@dataclass(frozen=True)
class ResumeFit:
    """Что в резюме говорит о должности: из профессии, прошлых должностей и мест работы."""

    heads: tuple[str, ...]  # основы должности: «администрат», «повар»
    qualifiers: tuple[str, ...]  # уточнения желаемой должности: «кофе», «горяч»
    spheres: frozenset[str]
    specialties: frozenset[str]
    levels: frozenset[str]  # TOP_LEVELS и MID_LEVELS, встреченные в должностях резюме
    experience_years: float = 0.0

    @classmethod
    def build(
        cls,
        profession: str,
        roles: Sequence[str],
        places: Sequence[str],
        details: Sequence[str],
        qualifiers: Sequence[str],
        experience_years: float,
    ) -> ResumeFit:
        """`roles` — прошлые должности, `places` — места работы целиком («Администратор —
        Кофейня «Зерно»»), `details` — навыки и опыт: по ним специализации бухгалтера."""
        titles = _norm(" \n".join([profession, *roles]))
        heads = tuple(h for h in (*SPHERE_BOUND_HEADS, *SPECIALTIES) if find_terms(titles, [h]))
        specialties: set[str] = set()
        for head, groups in SPECIALTIES.items():
            if head in heads:
                specialties |= _groups(" \n".join([titles, *places, *details]), groups)
        return cls(
            heads=heads,
            qualifiers=tuple(q for q in qualifiers if q not in WEAK_QUALIFIERS),
            spheres=frozenset(_groups(" \n".join([titles, *places]), SPHERES)),
            specialties=frozenset(specialties),
            levels=frozenset(find_terms(titles, (*TOP_LEVELS, *MID_LEVELS))),
            experience_years=experience_years,
        )


@dataclass(frozen=True)
class Fit:
    points: int = 0
    reasons: tuple[str, ...] = ()
    excluded: str | None = None


def apply(title: str, company: str, fit: ResumeFit | None) -> Fit:
    """Поправка к баллу вакансии и отсечение по смыслу должности."""
    if fit is None:
        return Fit()
    name = _norm(title)
    parts: list[str] = []
    points = 0
    top = [t for t in find_terms(name, TOP_LEVELS) if t not in fit.levels]
    if top:
        return Fit(excluded=f"уровень выше резюме: «{_word(name, top[0])}»")
    mid = [t for t in find_terms(name, MID_LEVELS) if t not in fit.levels]
    if mid and fit.experience_years < SENIOR_YEARS:
        points += LEVEL_POINTS
        parts.append(f"уровень «{_word(name, mid[0])}» {_signed(LEVEL_POINTS)}")

    found = find_terms(name, fit.qualifiers)
    exact = bool(fit.qualifiers) and len(found) == len(fit.qualifiers)
    if exact:
        points += EXACT_POINTS
        parts.append(f"точная должность {_signed(EXACT_POINTS)}")
    elif found:
        points += PARTIAL_POINTS
        parts.append(f"уточнение «{found[0]}» {_signed(PARTIAL_POINTS)}")

    for head, groups in SPECIALTIES.items():
        if head not in fit.heads or not find_terms(name, [head]) or find_terms(name, UNIVERSAL):
            continue
        wanted = _groups(name, groups)
        if wanted and fit.specialties and not wanted & fit.specialties:
            points += CONFLICT_POINTS
            parts.append(f"другая специализация: {', '.join(sorted(wanted))} "
                         f"{_signed(CONFLICT_POINTS)}")  # fmt: skip
        elif wanted & fit.specialties and not exact:
            points += SAME_POINTS
            parts.append(f"та же специализация {_signed(SAME_POINTS)}")

    spheres = _groups(f"{name}\n{company}", SPHERES)
    bound = any(find_terms(name, [h]) for h in fit.heads if h in SPHERE_BOUND_HEADS)
    if bound and spheres and fit.spheres and not spheres & fit.spheres:
        # администратор магазина — не администратор кофейни; повар в пансионате — повар
        return Fit(excluded=f"другая сфера: {', '.join(sorted(spheres))}")
    if spheres & fit.spheres and not exact:
        points += SAME_POINTS
        parts.append(f"та же сфера: {', '.join(sorted(spheres & fit.spheres))} "
                     f"{_signed(SAME_POINTS)}")  # fmt: skip
    reasons = (f"{FIT_PREFIX}{'; '.join(parts)}",) if parts else ()
    return Fit(points, reasons)


def _word(text: str, stem: str) -> str:
    """Слово названия, начинающееся с основы: «главн» → «главный»."""
    for word in text.split():
        if word.startswith(stem) or stem in word:
            return word.strip(",.()«»\"")
    return stem


def _signed(value: int) -> str:
    return f"+{value}" if value > 0 else f"−{-value}"
