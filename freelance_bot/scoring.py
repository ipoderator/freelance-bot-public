"""Отбор и ранжирование проектов под профиль.

score = релевантность направлений (до 40) + бонус пересечений (до 25) + свежесть (до 15)
        + бюджет (5..10, нет данных = 5, меньше BUDGET_LOW_RUB = 0)
        + число предложений (0..10, нет данных = 5)
        + поправка по проценту найма заказчика Kwork (±3) − штрафы за требования к
        исполнителю; результат ограничен 0..100. Веса — константы ниже: в settings.toml
        лежат только направления, бонусы пересечений, min_score и requirement_penalty.
Чистая функция: время подаётся параметром now, случайности нет.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime

from freelance_bot.config import Settings
from freelance_bot.models import Project, ScoreResult

try:  # общий матчер каркаса; пока его нет — приватная копия с той же семантикой
    from freelance_bot.textmatch import find_terms as _find_terms
except ImportError:  # pragma: no cover - зависит от состояния каркаса

    def _find_terms(text: str, terms: Iterable[str]) -> list[str]:
        """Регистронезависимо; термин начинается на границе слова, короткие (≤3) и заканчиваются."""
        haystack = _norm(text)
        found: list[str] = []
        for term in terms:
            needle = _norm(term)
            if not needle:
                continue
            tail = r"(?!\w)" if len(needle) <= 3 else ""
            if re.search(rf"(?<!\w){re.escape(needle)}{tail}", haystack):
                found.append(term)
        return found


# Названия направлений из settings.toml для людей и нейросети; своё направление — по ключу
DIRECTION_LABELS: dict[str, str] = {
    "backend": "backend на Python и API",
    "telegram": "Telegram-боты",
    "ai_agents": "AI-агенты и LLM",
    "automation": "автоматизация и интеграции",
    "parsing": "парсинг и сбор данных",
}
RELEVANCE_MAX = 30.0  # лучшее направление
RELEVANCE_REST_MAX = 10.0  # остальные направления вместе
POINTS_FOR_FULL_STRENGTH = 4  # очки: слово в заголовке = 2, в описании = 1
INTERSECTION_CAP = 25
FRESHNESS_MAX = 15.0
BUDGET_NEUTRAL = 5.0
BUDGET_MAX = 10.0
BUDGET_FULL_RUB = 30000
# Бюджет ниже этого — обычно «сделайте сайт за 500 ₽»: хуже, чем бюджет не указан.
BUDGET_LOW_RUB = 2000
OFFERS_NEUTRAL = 5.0
OFFERS_MAX = 10.0
OFFERS_ZERO_AT = 30
HIRED_BONUS = 3.0
HIRED_GOOD_PERCENT = 30
HIRED_BAD_PERCENT = 10
HIRED_BAD_MIN_PROJECTS = 10
# Требование «опыт от N лет» невыполнимо, если N больше подтверждённого стажа; не меньше 2:
# «от 1 года» в заказах пишут почти всегда (при стаже ≈1 год 10 мес → порог 2).
MIN_EXPERIENCE_LIMIT = 2


def experience_limit(experience_years: float) -> int:
    """С какого N требование «опыт от N лет» не выполнено: 1,8 → 2; 3 → 4; 5,5 → 6."""
    return max(MIN_EXPERIENCE_LIMIT, int(experience_years) + 1)

MIN_STOP_POINTS = 2
PHP_KEY = "php"
# PHP-CMS, которых нет в settings.toml: дополняют exclusions.php.
PHP_CMS_TERMS = (
    "october cms", "octobercms", "drupal", "magento", "prestashop", "symfony", "umi.cms",
    "netcat", "webasyst",
)  # fmt: skip

AI_CONTENT_KEY = "ai_content"
AI_CONTENT_REASON = "вне профиля: генерация контента в готовых AI-сервисах"
# Совпадения, которые сами по себе не говорят о разработке: «нейросеть», «промпт», «AI».
# Если кроме них в проекте ничего из профиля нет, а есть генеративный сервис контента
# (exclusions.ai_content) — работа в сервисе, а не интеграция: вне профиля.
GENERIC_AI_TERMS = frozenset(
    {"ai", "ии", "искусственный интеллект", "нейросеть", "нейросет", "промпт", "prompt",
     "gpt", "chatgpt", "автоматизац", "automation", "рутин"}
)  # fmt: skip
# Общие слова: встречаются в любом ИТ-заказе и сами по себе профиль не подтверждают.
# Проект, где совпали только они и либо одним упоминанием в описании («сервер» в
# «Настройка VPS», «ии» в «Браузер-поисковик»), либо при бюджете меньше BUDGET_LOW_RUB
# («Сделать ии видео» за 500 ₽), отсекается как слабое совпадение; в заголовке они не
# спасают PHP-проект («скрипт на php»).
WEAK_TERMS = GENERIC_AI_TERMS | frozenset(
    {"api", "апи", "сервер", "скрипт", "script", "по расписанию", "синхронизац", "excel",
     "выгрузк", "crm", "база данных", "docker", "cron", "веб-сервис", "веб-сервиса",
     "web-сервис", "веб-платформ", "веб-приложени", "тг", "tg"}
)  # fmt: skip
WEAK_REASON = "слабое совпадение с профилем"

_EXCLUSION_LABELS = {
    "design": "дизайн",
    "mobile": "мобильная разработка",
    "php": "PHP/CMS",
    "onec": "1С",
    "marketing": "маркетинг/SMM",
    "teaching": "учёба/консультации",
    "manual_qa": "ручное тестирование",
}


def _norm(text: str) -> str:
    return text.lower().replace("ё", "е")


def _points(title: str, description: str, terms: list[str]) -> tuple[int, list[str]]:
    in_title = _find_terms(title, terms)
    in_desc = [t for t in _find_terms(description, terms) if t not in in_title]
    return 2 * len(in_title) + len(in_desc), in_title + in_desc


# --- требования заказчика к отклику ------------------------------------------

_CLIENT_PATTERNS = [
    re.compile(
        r"\bв\s+(?:отклике|предложении|сообщении|ответе)\s+(?:\w+\s+){0,2}?"
        r"(?:укажите|напишите|пришлите|приложите|опишите|расскажите|сообщите|обязательно)\b"
    ),
    re.compile(r"\bпри\s+отклике\s+(?:\w+\s+){0,2}?(?:укажите|напишите|пришлите|приложите)\b"),
    re.compile(
        r"\b(?:начните|начинайте|начни)\s+(?:\w+\s+){0,2}?(?:отклик\w*|ответ\w*|сообщени\w*)\s+со\s+(?:слова|фразы)\b"
    ),
    re.compile(
        r"\bотклик\w*\s+без\s+.{0,80}?(?:не\s+рассматрива\w+|не\s+читаю|игнорир\w+|не\s+буду)"
    ),
    re.compile(
        r"\b(?:пришлите|приложите|скиньте|отправьте|покажите|прикрепите)\s+(?:\w+\s+){0,3}?"
        r"(?:пример(?!н)\w*|портфолио|кейс\w*)\b"  # «примерный ценник» — не примеры
    ),
    re.compile(
        r"\b(?:укажите|напишите|назовите|сообщите)\s+(?:\w+\s+){0,3}?(?:стоимост\w*|цен[уаы]|ценник\w*|сроки?|бюджет)\b"
    ),
]
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+|\n+")


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT_RE.split(text) if s.strip()]


_BULLET_RE = re.compile(r"^\s*(?:[-–—•*▪●·]|\d{1,2}[.)])\s+")
_ITEM_SPLIT_RE = re.compile(r"\s*,\s*")


def _clean_item(item: str) -> str:
    return _BULLET_RE.sub("", item).strip().rstrip(".;, ").strip()


def _list_items(tail: str) -> list[str]:
    """Пункты перечня через «;» и запятые; пункт со своим двоеточием («стек: Python,
    FastAPI») по запятым не режется — это его содержимое, а не новые пункты."""
    items: list[str] = []
    for part in tail.split(";"):
        items.extend([part] if ":" in part else _ITEM_SPLIT_RE.split(part))
    return items


def extract_client_requirements(text: str) -> list[str]:
    """Требования к отклику. Если после двоеточия идёт перечень (маркированный, нумерованный,
    через запятую), пункты входят в требование: «В отклике укажите: а; б; в»."""
    found: list[str] = []
    lines = (text or "").split("\n")
    for index, line in enumerate(lines):
        for sentence in _sentences(line):
            if not any(p.search(_norm(sentence)) for p in _CLIENT_PATTERNS):
                continue
            requirement = sentence
            head, colon, tail = sentence.partition(":")
            if colon and tail.strip():
                # перечень в той же строке: «укажите: опыт, сроки и цену»
                items = [_clean_item(i) for i in _list_items(tail) if _clean_item(i)]
                requirement = f"{head.strip()}: {'; '.join(items)}"
            elif sentence.rstrip().endswith(":"):
                items = []
                for follow in lines[index + 1 :]:
                    if not follow.strip():
                        if items:
                            break
                        continue
                    if not _BULLET_RE.match(follow):
                        break
                    items.append(_clean_item(follow))
                if items:
                    requirement = f"{sentence.rstrip().rstrip(':').strip()}: {'; '.join(items)}"
            if requirement not in found:
                found.append(requirement)
    return found


# --- требования к исполнителю, которые профиль не выполняет ------------------

_EXP_PATTERNS = [
    re.compile(
        r"(?:опыт\w*|стаж\w*)(?:\s+работы)?\s+(?:от|не\s+менее|более|свыше|минимум)\s+(\d+)(?:-х)?\s*\+?\s*(?:лет|года|год)\b"
    ),
    re.compile(r"\b(\d+)(?:-х)?\s*\+?\s*(?:лет|года)\s+(?:опыта|стажа|работы)\b"),
    re.compile(r"(?:опыт\w*|стаж\w*)(?:\s+работы)?\s+(\d+)\s*\+?\s*(?:лет|года)\b"),
]
_LEGAL_WORD_RE = re.compile(r"\b(?:самозанят\w*|ип|юрлиц\w*|юр\.?\s?лиц\w*|ооо)\b")
_LEGAL_NEED_RE = re.compile(
    r"\b(?:только|обязательно|обязательн\w+|необходим\w*|наличие|требуется|должен|должны|иметь|"
    r"работа\s+с|работаем\s+с|оформлен\w*|статус\w*)\b"
)
_OFFICE_RE = re.compile(
    r"(?<!не )(?<!без )\b(?:работа\s+в\s+офисе|в\s+офисе|офис\s+в|офисн\w+|переезд\w*|релокац\w*|"
    r"очное\s+присутствие|на\s+территории\s+заказчика)"
)
_REVIEWS_RES = [
    re.compile(
        r"\b(?:только|обязательно|обязательны\w*|наличие|необходим\w*|с)\s+(?:\w+\s+){0,2}?(?:отзыв\w*|рейтинг\w*)"
    ),
    re.compile(r"\b(?:отзыв\w*|рейтинг\w*)\s+(?:\w+\s+){0,2}?(?:обязательн\w*|не\s+ниже)"),
]


def _performer_requirements(text: str, experience_years: float = 0.0) -> list[str]:
    normalized = _norm(text)
    found: list[str] = []
    for pattern in _EXP_PATTERNS:
        years = [int(m.group(1)) for m in pattern.finditer(normalized)]
        if any(y >= experience_limit(experience_years) for y in years):
            found.append(f"опыт от {max(years)} лет")
            break
    for sentence in _sentences(normalized):
        if _LEGAL_WORD_RE.search(sentence) and _LEGAL_NEED_RE.search(sentence):
            found.append("нужен статус ИП/самозанятого/юрлица")
            break
    if _OFFICE_RE.search(normalized):
        found.append("офис/переезд")
    if any(p.search(normalized) for p in _REVIEWS_RES):
        found.append("обязательные отзывы/рейтинг на площадке")
    return found


# --- основной расчёт ----------------------------------------------------------


def score_project(p: Project, settings: Settings, now: datetime) -> ScoreResult:
    title, description = p.title or "", p.description or ""
    full_text = f"{title}\n{description}"
    reasons: list[str] = []

    # направления
    strengths: dict[str, float] = {}
    matched_terms: list[str] = []
    profile_points = 0
    for key, direction in settings.directions.items():
        pts, terms = _points(title, description, direction.keywords)
        if not terms:
            continue
        profile_points += pts
        strengths[key] = min(1.0, pts / POINTS_FOR_FULL_STRENGTH) * direction.weight
        matched_terms.extend(t for t in terms if t not in matched_terms)
        reasons.append(f"Направление «{key}»: {', '.join(terms)}")
    ordered = sorted(strengths.values(), reverse=True)
    relevance = 0.0
    if ordered:
        relevance = RELEVANCE_MAX * ordered[0] + RELEVANCE_REST_MAX * min(1.0, sum(ordered[1:]))
    directions = list(strengths)

    bonus = 0
    for pair in settings.intersections:
        if all(d in strengths for d in pair.directions):
            bonus += pair.bonus
            reasons.append(f"Пересечение {' + '.join(pair.directions)} (+{pair.bonus})")
    bonus = min(bonus, INTERSECTION_CAP)

    # свежесть
    age_hours = max(0.0, (now - p.published_at).total_seconds() / 3600)
    freshness = FRESHNESS_MAX * max(0.0, 1.0 - age_hours / settings.max_age_hours)

    # бюджет
    amounts = [v for v in (p.budget_min, p.budget_max) if v]
    if amounts and max(amounts) < BUDGET_LOW_RUB:
        budget = 0.0
        reasons.append(f"Маленький бюджет: {max(amounts):,} ₽".replace(",", " "))
    elif amounts:
        budget = BUDGET_NEUTRAL + (BUDGET_MAX - BUDGET_NEUTRAL) * min(
            1.0, max(amounts) / BUDGET_FULL_RUB
        )
        reasons.append(f"Бюджет: {max(amounts):,} ₽".replace(",", " "))
    else:
        budget = BUDGET_NEUTRAL

    # предложения
    if p.offers_count is None:
        offers = OFFERS_NEUTRAL
    else:
        offers = OFFERS_MAX * max(0.0, 1.0 - p.offers_count / OFFERS_ZERO_AT)
        reasons.append(f"Откликов: {p.offers_count}")

    # процент найма заказчика Kwork
    hired_adjust = 0.0
    hired = p.extra.get("hired_percent") if p.platform == "kwork" else None
    if isinstance(hired, int):
        if hired >= HIRED_GOOD_PERCENT:
            hired_adjust = HIRED_BONUS
            reasons.append(f"Заказчик часто нанимает ({hired}%)")
        elif (
            hired < HIRED_BAD_PERCENT
            and (p.extra.get("buyer_projects") or 0) >= HIRED_BAD_MIN_PROJECTS
        ):
            hired_adjust = -HIRED_BONUS
            reasons.append(f"Заказчик редко нанимает ({hired}%)")

    # требования к исполнителю
    performer = _performer_requirements(full_text, settings.experience_years)
    penalty = settings.requirement_penalty * len(performer)
    for item in performer:
        reasons.append(f"⚠ требование: {item} (−{settings.requirement_penalty})")

    total = relevance + bonus + freshness + budget + offers + hired_adjust - penalty
    score = max(0, min(100, round(total)))

    # отсечение
    excluded: str | None = None
    if age_hours > settings.max_age_hours:
        excluded = "устарел"
    else:
        best_stop, best_label = 0, ""
        for key, words in settings.exclusions.items():
            if key in (PHP_KEY, AI_CONTENT_KEY):  # у PHP/CMS и AI-контента свои правила ниже
                continue
            pts, _ = _points(title, description, words)
            if pts > best_stop:
                best_stop, best_label = pts, _EXCLUSION_LABELS.get(key, key)
        php_terms = [*settings.exclusions.get(PHP_KEY, []), *PHP_CMS_TERMS]
        php_points, _ = _points(title, description, php_terms)
        php_in_title = bool(_find_terms(title, php_terms))
        profile_in_title = any(
            _norm(t) not in WEAK_TERMS
            for d in settings.directions.values()
            for t in _find_terms(title, d.keywords)
        )
        # Заголовок с обоими стеками («с WordPress на Django») — перенос или интеграция:
        # ничья по очкам в пользу профиля.
        php_outweighs = (
            php_points > profile_points
            if php_in_title and profile_in_title
            else php_points >= profile_points
        )
        strong_profile = bool(ordered) and ordered[0] >= 1.0
        if (php_in_title and not profile_in_title) or (
            php_points >= MIN_STOP_POINTS and php_outweighs
        ):
            # PHP/CMS-стек не перевешивается сильным профилем: основной объём — PHP.
            excluded = f"доминирует {_EXCLUSION_LABELS[PHP_KEY]}"
        elif _find_terms(full_text, settings.exclusions.get(AI_CONTENT_KEY, [])) and all(
            _norm(t) in GENERIC_AI_TERMS for t in matched_terms
        ):
            excluded = AI_CONTENT_REASON
        elif best_stop >= MIN_STOP_POINTS and best_stop >= profile_points and not strong_profile:
            excluded = f"доминирует {best_label}"
        elif not directions:
            excluded = "вне профиля"
        elif all(_norm(t) in WEAK_TERMS for t in matched_terms) and (
            profile_points <= 1 or (amounts and max(amounts) < BUDGET_LOW_RUB)
        ):
            excluded = WEAK_REASON

    return ScoreResult(
        score=score,
        passed=excluded is None and score >= settings.min_score,
        directions=directions,
        matched_terms=matched_terms,
        excluded_reason=excluded,
        reasons=reasons,
        client_requirements=extract_client_requirements(full_text),
    )
