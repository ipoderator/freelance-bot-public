"""Отбор и ранжирование: score_project и extract_client_requirements."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from freelance_bot.config import load_settings
from freelance_bot.scoring import extract_client_requirements, score_project
from freelance_bot.sources.fl import parse_fl_rss
from freelance_bot.sources.kwork import parse_kwork_projects

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)
FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def settings():
    repo_root = Path(__file__).resolve().parent.parent
    return load_settings(repo_root / "settings.toml", repo_root / "no-such.env")


def mk(make_project, *, title="Telegram-бот на Python", description="", age_h=1.0, **kw):
    kw.setdefault("budget_max", 20000)
    kw.setdefault("offers_count", 5)
    return make_project(
        title=title,
        description=description,
        published_at=NOW - timedelta(hours=age_h),
        **kw,
    )


def score(p, settings, now=NOW):
    return score_project(p, settings, now)


# --- направления и пересечения ----------------------------------------------


def test_telegram_bot_with_fastapi_ranks_above_plain_bot(make_project, settings):
    plain = score(mk(make_project, title="Telegram-бот для магазина"), settings)
    both = score(mk(make_project, title="Telegram-бот для магазина на FastAPI"), settings)
    assert both.score > plain.score
    assert {"telegram", "backend"} <= set(both.directions)
    assert plain.directions == ["telegram"]


def test_intersection_bonus_is_applied_for_ai_agent_plus_api(make_project, settings):
    ai_only = score(mk(make_project, title="AI-агент для поддержки клиентов"), settings)
    both = score(mk(make_project, title="AI-агент для поддержки клиентов с REST API"), settings)
    assert both.score - ai_only.score >= 15
    assert any("ai_agents" in r and "backend" in r for r in both.reasons)


def test_intersection_bonus_automation_plus_python(make_project, settings):
    auto = score(mk(make_project, title="Автоматизация отчётов"), settings)
    both = score(mk(make_project, title="Автоматизация отчётов на Python"), settings)
    assert {"automation", "backend"} <= set(both.directions)
    assert both.score - auto.score >= 10


def test_matched_terms_and_reasons_are_reported(make_project, settings):
    r = score(mk(make_project, title="Парсер сайта на Python"), settings)
    assert "парсер" in r.matched_terms and "python" in r.matched_terms
    assert r.reasons and all(isinstance(x, str) for x in r.reasons)


def test_short_keywords_do_not_match_inside_words(make_project, settings):
    # «ии» в «компании», «ai» в «email», «api» в «капитан» не должны давать направление
    p = mk(make_project, title="Рассылка по email для компании", description="Напишите капитану")
    r = score(p, settings)
    assert r.directions == []
    assert r.excluded_reason == "вне профиля"
    assert r.passed is False


def test_good_project_passes(make_project, settings):
    r = score(mk(make_project, title="Telegram-бот на Python + FastAPI"), settings)
    assert r.passed and r.excluded_reason is None and 40 <= r.score <= 100


# --- отсечение --------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "description", "word"),
    [
        ("Дизайн логотипа и баннеров", "Нужен дизайнер, Figma.", "дизайн"),
        ("Мобильное приложение на Flutter", "Android и iOS, бэкенд есть.", "мобиль"),
        ("Сайт на PHP и WordPress", "Доработать плагин, немного Python.", "PHP"),
        ("Доработка конфигурации 1С", "Обработка 1С для бухгалтерии.", "1С"),
    ],
)
def test_domination_by_stop_words_excludes_with_reason(
    make_project, settings, title, description, word
):
    r = score(mk(make_project, title=title, description=description), settings)
    assert r.passed is False
    assert r.excluded_reason and word.lower() in r.excluded_reason.lower()


def test_php_cms_in_title_is_excluded_even_with_strong_profile_match(make_project, settings):
    # Пример из ревью: Kwork-проект с OpenAI/amoCRM проходил с 87 баллами.
    p = mk(
        make_project,
        platform="kwork",
        title="Доработка сайта на October CMS (Laravel), PHP-разработчик",
        description=(
            "Доработать админ-панель, проверить автоматические переводы через OpenAI API, "
            "подготовить интеграцию с amoCRM, настроить вебхуки и API."
        ),
        offers_count=2,
        budget_max=60000,
    )
    r = score(p, settings)
    assert r.passed is False
    assert r.excluded_reason == "доминирует PHP/CMS"


def test_php_cms_project_from_kwork_fixture_is_excluded(settings):
    # «Доработка сайта и админ-панели на October CMS»: ищут PHP-разработчика (Laravel).
    now = datetime(2026, 10, 21, 18, 40, tzinfo=UTC)
    items = parse_kwork_projects((FIXTURES / "kwork_projects.html").read_text(encoding="utf-8"))
    r = next(score_project(p, settings, now) for p in items if p.external_id == "3268569")
    assert r.passed is False
    assert r.excluded_reason == "доминирует PHP/CMS"


def test_php_stop_points_not_below_profile_points_exclude(make_project, settings):
    # PHP не в заголовке, профиль «сильный» (OpenAI и ChatGPT в заголовке = 4 очка),
    # но очков PHP-стека (php, laravel, wordpress, bitrix в описании) столько же.
    p = mk(
        make_project,
        title="Интеграция OpenAI и ChatGPT в личный кабинет",
        description=(
            "Личный кабинет на Laravel (PHP), часть страниц на WordPress, магазин на Bitrix."
        ),
    )
    r = score(p, settings)
    assert r.excluded_reason == "доминирует PHP/CMS"


@pytest.mark.parametrize(
    "title",
    [
        "Tilda + Telegram-бот на Python",
        "Перенос сайта с WordPress на Django",
        "Парсер для PHP-сайта на Python",
        "Миграция с Bitrix на FastAPI",
    ],
)
def test_php_cms_in_title_next_to_profile_stack_is_not_excluded(make_project, settings, title):
    # В заголовке назван и PHP/CMS, и стек профиля: это перенос/интеграция, а не PHP-заказ.
    r = score(mk(make_project, title=title), settings)
    assert r.excluded_reason is None, r
    assert r.passed


def test_python_project_with_a_bit_of_php_is_not_excluded(make_project, settings):
    p = mk(
        make_project,
        title="Telegram-бот на Python + FastAPI",
        description="Бот на aiogram, бэкенд FastAPI и PostgreSQL. Сайт клиента на WordPress.",
    )
    r = score(p, settings)
    assert r.excluded_reason is None and r.passed


def test_python_backend_with_a_bit_of_design_is_not_excluded(make_project, settings):
    p = mk(
        make_project,
        title="Python-бэкенд на FastAPI",
        description=(
            "Нужно сделать API и подключить PostgreSQL. Немного дизайна в Figma по готовому макету."
        ),
    )
    r = score(p, settings)
    assert r.excluded_reason is None and r.passed


def test_stale_project_is_not_passed(make_project, settings):
    r = score(mk(make_project, title="Telegram-бот на Python + FastAPI", age_h=7), settings)
    assert r.passed is False
    assert r.excluded_reason == "устарел"


def test_project_just_inside_max_age_is_not_stale(make_project, settings):
    r = score(mk(make_project, title="Telegram-бот на Python + FastAPI", age_h=5.9), settings)
    assert r.excluded_reason is None


def test_platform_max_age_keeps_older_vacancies_only_there(make_project, settings):
    superjob = replace(settings.sources["superjob"], max_age_hours=72)
    s = replace(settings, max_age_hours=6, sources={**settings.sources, "superjob": superjob})
    title = "Python-разработчик Telegram-ботов"
    assert score(mk(make_project, title=title, age_h=48, platform="superjob"), s).passed
    old = score(mk(make_project, title=title, age_h=73, platform="superjob"), s)
    assert old.excluded_reason == "устарел"
    fl = score(mk(make_project, title=title, age_h=7, platform="fl"), s)
    assert fl.excluded_reason == "устарел"


# --- монотонность и детерминированность ------------------------------------


def test_deterministic(make_project, settings):
    p = mk(make_project, title="Бот на Python")
    assert score(p, settings) == score(p, settings)


def test_fresher_is_not_worse(make_project, settings):
    scores = [score(mk(make_project, age_h=h), settings).score for h in (0.0, 1.0, 3.0, 5.0)]
    assert scores == sorted(scores, reverse=True) and scores[0] > scores[-1]


def test_fewer_offers_is_not_worse(make_project, settings):
    scores = [score(mk(make_project, offers_count=n), settings).score for n in (0, 5, 15, 40)]
    assert scores == sorted(scores, reverse=True) and scores[0] > scores[-1]


def test_unknown_offers_is_between_few_and_many(make_project, settings):
    few = score(mk(make_project, offers_count=0), settings).score
    none = score(mk(make_project, offers_count=None), settings).score
    many = score(mk(make_project, offers_count=40), settings).score
    assert few >= none >= many


def test_budget_ranks_high_over_unknown_over_tiny(make_project, settings):
    none = score(mk(make_project, budget_max=None), settings).score
    tiny = score(mk(make_project, budget_max=500), settings)
    mid = score(mk(make_project, budget_max=5000), settings).score
    high = score(mk(make_project, budget_max=50000), settings).score
    assert high > mid >= none > tiny.score
    assert "Маленький бюджет: 500 ₽" in tiny.reasons


def test_score_is_clamped_to_0_100(make_project, settings):
    best = score(
        mk(
            make_project,
            title="Telegram-бот на Python FastAPI, AI-агент, автоматизация, парсер",
            description="aiogram, OpenAI, REST API, PostgreSQL, парсинг, скрипт",
            age_h=0,
            offers_count=0,
            budget_max=500000,
        ),
        settings,
    )
    assert 0 <= best.score <= 100
    worst = score(mk(make_project, title="Дизайн логотипа", description="Figma", age_h=9), settings)
    assert worst.score >= 0


# --- требования к исполнителю -----------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Нужен специалист с опытом от 5 лет.",
        "Только ИП или самозанятые.",
        "Работа в офисе в Москве, переезд обязателен.",
        "Обязательно наличие отзывов на бирже.",
    ],
)
def test_uncomfortable_performer_requirement_costs_requirement_penalty(
    make_project, settings, text
):
    base = mk(make_project, title="Telegram-бот на Python", description="Нужен бот.")
    flagged = mk(make_project, title="Telegram-бот на Python", description="Нужен бот. " + text)
    s0, s1 = score(base, settings), score(flagged, settings)
    assert s0.score - s1.score == settings.requirement_penalty
    assert any(r.startswith("⚠ требование") for r in s1.reasons)
    assert not any(r.startswith("⚠ требование") for r in s0.reasons)


def test_penalty_follows_settings_and_stacks_per_requirement(make_project, settings):
    custom = replace(settings, requirement_penalty=7)
    base = mk(make_project, title="Python бот Telegram", description="Нужен бот.")
    two = mk(
        make_project,
        title="Python бот Telegram",
        description="Нужен бот. Опыт от 5 лет. Только самозанятые.",
    )
    assert score(base, custom).score - score(two, custom).score == 14
    assert sum(r.startswith("⚠ требование") for r in score(two, custom).reasons) == 2


def test_small_experience_requirement_is_not_penalized(make_project, settings):
    base = mk(make_project, description="Нужен бот.")
    easy = mk(make_project, description="Нужен бот. Опыт от 1 года приветствуется.")
    assert score(base, settings).score == score(easy, settings).score


@pytest.mark.parametrize("text", ["Опыт от 2 лет.", "Опыт работы от 2-х лет на Python."])
def test_two_years_experience_exceeds_confirmed_and_is_penalized(make_project, settings, text):
    # В резюме подтверждено ≈1 год 10 мес: «от 2 лет» — уже больше подтверждённого.
    base = mk(make_project, description="Нужен бот.")
    two = mk(make_project, description="Нужен бот. " + text)
    s0, s2 = score(base, settings), score(two, settings)
    assert s0.score - s2.score == settings.requirement_penalty
    assert any(r.startswith("⚠ требование: опыт от 2 лет") for r in s2.reasons), s2.reasons


def test_experience_requirement_depends_on_own_experience(make_project, settings):
    from dataclasses import replace

    from freelance_bot.scoring import experience_limit

    assert [experience_limit(y) for y in (0, 1.8, 2, 3.5, 5)] == [2, 2, 3, 4, 6]
    order = mk(make_project, description="Нужен бот. Опыт от 3 лет.")
    senior = replace(settings, experience_years=4)
    assert any("опыт от 3 лет" in r for r in score(order, settings).reasons)
    assert not any("опыт от" in r for r in score(order, senior).reasons)


def test_negated_office_is_not_a_requirement(make_project, settings):
    base = mk(make_project, description="Нужен бот.")
    remote = mk(make_project, description="Нужен бот. Работа удалённо, без офиса.")
    assert score(base, settings).score == score(remote, settings).score


# --- Kwork: процент найма заказчика -----------------------------------------


def kw(make_project, **extra):
    return mk(make_project, platform="kwork", extra=extra)


def test_kwork_high_hire_rate_gives_bonus_low_with_history_gives_penalty(make_project, settings):
    neutral = score(kw(make_project, hired_percent=20, buyer_projects=20), settings).score
    high = score(kw(make_project, hired_percent=60, buyer_projects=5), settings).score
    low = score(kw(make_project, hired_percent=5, buyer_projects=12), settings).score
    assert high > neutral > low


def test_kwork_low_hire_rate_with_few_projects_is_not_penalized(make_project, settings):
    neutral = score(kw(make_project, hired_percent=20, buyer_projects=20), settings).score
    new_buyer = score(kw(make_project, hired_percent=0, buyer_projects=1), settings).score
    assert new_buyer == neutral
    assert score(kw(make_project), settings).score == neutral


# --- extract_client_requirements --------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Нужен бот. В отклике укажите опыт с aiogram.",
        "Начните свой отклик со слова «Python», иначе не рассмотрю.",
        "Отклики без примеров работ не рассматриваю.",
        "Пришлите примеры похожих работ.",
        "Укажите стоимость и сроки.",
        "В отклике напишите, какой стек используете.",
        "Приложите портфолио.",
    ],
)
def test_extracts_client_requirement_phrasings(text):
    found = extract_client_requirements(text)
    assert len(found) == 1
    assert found[0] in text or text.rstrip(".") in found[0] or found[0].rstrip(".") in text


def test_extracts_only_requirement_sentences_from_long_text():
    text = (
        "Нужен Telegram-бот для записи клиентов.\n"
        "Функционал: меню, оплата, админка.\n"
        "В отклике укажите, делали ли похожее. Укажите стоимость и сроки!"
    )
    found = extract_client_requirements(text)
    assert len(found) == 2
    assert "Функционал" not in " ".join(found)


def test_no_requirements_in_plain_task():
    assert extract_client_requirements("Нужен парсер каталога. Результат в Excel.") == []
    assert extract_client_requirements("") == []


def test_client_requirements_do_not_change_score_but_are_reported(make_project, settings):
    base = mk(make_project, description="Нужен бот.")
    asked = mk(make_project, description="Нужен бот. В отклике укажите стоимость и сроки.")
    s0, s1 = score(base, settings), score(asked, settings)
    assert s0.score == s1.score
    assert s1.client_requirements and s0.client_requirements == []


# --- на реальных фикстурах ---------------------------------------------------


def test_real_fl_feed_ranking_sanity(settings):
    now = datetime(2026, 10, 21, 20, 50, tzinfo=UTC)
    settings = replace(
        settings, max_age_hours=24 * 30
    )  # лента в фикстуре охватывает несколько дней
    items = parse_fl_rss((FIXTURES / "fl_rss.xml").read_text(encoding="utf-8"))
    by_title = {p.title: score_project(p, settings, now) for p in items}
    assert by_title["1С аналитик"].passed is False
    assert by_title["Программист 1С"].passed is False
    assert by_title["Разработка мобильного приложение на Flutter"].passed is False
    bot = by_title["Telegram-бот консультант на Python (OpenAI API)"]
    assert bot.passed and {"telegram", "ai_agents"} <= set(bot.directions)
    parsing = by_title["Парсинг ВБ Каспи"]
    assert "parsing" in parsing.directions
    assert all(0 <= r.score <= 100 for r in by_title.values())
    assert sum(r.passed for r in by_title.values()) > 5


def test_real_kwork_page_ranking_sanity(settings):
    now = datetime(2026, 10, 21, 18, 40, tzinfo=UTC)
    items = parse_kwork_projects((FIXTURES / "kwork_projects.html").read_text(encoding="utf-8"))
    by_id = {p.external_id: score_project(p, settings, now) for p in items}
    assert by_id["3268440"].passed and "telegram" in by_id["3268440"].directions
    assert by_id["3268588"].passed  # «Разработка бота на питоне»
    assert by_id["3210602"].passed is False  # мессенджер с публикацией в Google Play


# --- перечень после «В отклике укажите:» -------------------------------------


def _fl_description(external_id: str) -> str:
    items = parse_fl_rss((FIXTURES / "fl_rss.xml").read_text(encoding="utf-8"))
    return next(p.description for p in items if p.external_id == external_id)


def test_bulleted_list_after_colon_joins_the_requirement_project_5525741():
    found = extract_client_requirements(_fl_description("5525741"))
    assert found == [
        "В отклике укажите: Пример похожей работы: какая была системная проблема, что изменили "
        "и как проверили результат; Сможете ли начать в ближайшее время и сколько часов выделите "
        "за неделю; Какие материалы нужны для подтверждения объёма, стоимости и срока"
    ]


def test_numbered_list_after_colon_joins_the_requirement_project_5525445():
    found = extract_client_requirements(_fl_description("5525445"))
    assert found == [
        "В отклике укажите: Опыт подобных запусков и примеры работ; "
        "Когда готовы начать: 15 или 16 октября; "
        "Подтверждение доступности на период поддержки и дежурство 30 октября; "
        "Что потребуется от нас для старта"
    ]


def test_truncated_rss_description_keeps_what_is_there():
    text = "Нужен бот.\nВ отклике укажите:\n- пример работы\n- когда начнёте\n- сколько часов..."
    assert extract_client_requirements(text) == [
        "В отклике укажите: пример работы; когда начнёте; сколько часов"
    ]


def test_header_without_items_stays_as_is():
    assert extract_client_requirements("Нужен бот.\nВ отклике укажите:") == ["В отклике укажите:"]


def test_list_stops_at_blank_line_and_at_next_paragraph():
    text = "В отклике укажите:\n1) опыт\n2) сроки\n\nПосле отбора пришлю материалы.\n- не пункт"
    assert extract_client_requirements(text) == ["В отклике укажите: опыт; сроки"]


def test_inline_comma_list_after_colon():
    found = extract_client_requirements("В отклике укажите: опыт с aiogram, сроки и цену; примеры.")
    assert found == ["В отклике укажите: опыт с aiogram; сроки и цену; примеры"]


# --- генерация AI-контента в готовых сервисах — вне профиля (поправка G4) ----

HIGGSFIELD_TITLE = (
    "Требуется эксперт по Higgsfield AI для практического обучения созданию видео (4 часа)"
)
# Текст проекта FL 5526001 из реального dry-run (фрагменты карточки), RSS-фикстура его не содержит.
HIGGSFIELD_DESCRIPTION = (
    "Нам нужен не начинающий пользователь нейросетей и не теоретик, а профессионал, который "
    "глубоко разбирается в Higgsfield, регулярно создаёт качественные видео и может показать "
    "свои работы.\n"
    "Задача: за 4 часа онлайн-обучения разобрать профессиональные техники работы в Higgsfield "
    "и совместно создать 5 коротких видеофрагментов разной сложности.\n"
    "Разберём промпты, движение камеры, персонажей и стили."
)


@pytest.mark.parametrize(
    ("title", "description"),
    [
        (HIGGSFIELD_TITLE, HIGGSFIELD_DESCRIPTION),
        ("Сгенерировать картинки нейросетью", "Нужны 20 картинок в Midjourney по промптам."),
        ("Озвучка роликов с помощью ИИ", "Озвучить 10 видео нейросетью ElevenLabs."),
        ("AI-видео для рекламы", "Сделать 3 ролика в Runway или Kling, музыка — в Suno."),
    ],
    ids=["higgsfield-5526001", "midjourney", "voiceover", "runway-kling-suno"],
)
def test_ready_made_ai_content_services_are_out_of_profile(
    make_project, settings, title, description
):
    r = score(mk(make_project, title=title, description=description), settings)
    assert r.passed is False
    assert r.excluded_reason is not None and r.excluded_reason.startswith("вне профиля"), r


@pytest.mark.parametrize(
    ("title", "description"),
    [
        ("AI-агент для поддержки клиентов с REST API", "Агент отвечает клиентам по базе знаний."),
        (
            "Telegram-бот для генерации изображений",
            "Бот принимает промпт и генерирует картинку через API Midjourney или Flux.",
        ),
        ("Интеграция Kling API в сервис на Python", "Генерация видео по запросу пользователя."),
    ],
    ids=["agent-api", "bot-api", "kling-api"],
)
def test_ai_content_with_integration_still_passes(make_project, settings, title, description):
    r = score(mk(make_project, title=title, description=description), settings)
    assert r.excluded_reason is None and r.passed, r


# --- слабые совпадения: одно общее слово не подтверждает профиль (2026-10-24) ------
# Примеры — заказы, которые бот прислал или пропустил 2026-10-22 (data/bot.sqlite3).


@pytest.mark.parametrize(
    ("title", "description", "budget"),
    [
        ("Настройка VPS", "Нужно поднять сервер и поставить панель.", 500),
        ("Создать сайт", "Сайт-визитка, обновление новостей по расписанию.", 500),
        ("Браузер-поисковик создать и мне продать", "Свой поисковик, можно с ии.", 500),
        ("Сделать ии видео", "Ролик для рекламы, 30 секунд.", 500),
    ],
    ids=["vps-server", "site-schedule", "browser-ai", "ai-title-tiny-budget"],
)
def test_only_generic_terms_are_a_weak_match(make_project, settings, title, description, budget):
    r = score(mk(make_project, title=title, description=description, budget_max=budget), settings)
    assert r.passed is False
    assert r.excluded_reason == "слабое совпадение с профилем", r


@pytest.mark.parametrize(
    ("title", "description"),
    [
        ("Править шаблоны API выдачи", "Шаблоны ответов сервиса."),  # пользователь откликнулся
        ("ИИ ассистент", "Ассистент для отдела продаж."),
        ("Создать почтовый сервис", "Свой почтовый сервер, рассылки через Python."),
    ],
    ids=["api-in-title", "ai-in-title", "generic-plus-python"],
)
def test_generic_term_in_title_or_with_profile_term_still_passes(
    make_project, settings, title, description
):
    r = score(mk(make_project, title=title, description=description), settings)
    assert r.excluded_reason is None and r.passed, r


def test_generic_term_in_title_does_not_save_php_project(make_project, settings):
    r = score(mk(make_project, title="скрипт на php", description="Скрипт на сервере."), settings)
    assert r.excluded_reason == "доминирует PHP/CMS"


@pytest.mark.parametrize(
    "title",
    ["Настроить тг и мах бота", "Создать чат бота на тему сказки", "Макс + Тг чат боты"],
)
def test_short_bot_phrasings_match_telegram(make_project, settings, title):
    r = score(mk(make_project, title=title), settings)
    assert "telegram" in r.directions and r.passed, r


@pytest.mark.parametrize(
    ("title", "description", "label"),
    [
        ("Написание смм-стратегии", "Стратегия для телеграм-канала, контент-план.", "маркетинг"),
        ("Ищу наставника по курсу ИИ", "Нужна практика и консультации.", "учёба"),
        ("Тестирование регистрации на веб-сервисе", "Протестировать форму.", "ручное"),
    ],
    ids=["smm", "mentor", "manual-qa"],
)
def test_non_development_work_is_excluded(make_project, settings, title, description, label):
    r = score(mk(make_project, title=title, description=description), settings)
    assert r.passed is False
    assert r.excluded_reason and label in r.excluded_reason, r


@pytest.mark.parametrize(
    ("title", "description"),
    [
        (
            "Военнослужащий по контракту (служба в ВМП ДНР)",
            "Требуются специалисты: операторы БПЛА, программисты: C++, Python.",
        ),
        (
            "Python-разработчик Telegram-ботов на FastAPI",
            "Служба по контракту с Минобороны, разработка ботов и парсеров.",
        ),
    ],
    ids=["superjob-contract", "strong-profile"],
)
def test_military_service_is_excluded_even_with_strong_profile(
    make_project, settings, title, description
):
    r = score(mk(make_project, title=title, description=description), settings)
    assert r.passed is False
    assert r.excluded_reason == "военная служба", r


def test_civilian_contract_is_not_military(make_project, settings):
    r = score(
        mk(
            make_project,
            title="Telegram-бот на Python",
            description="Работа по контракту на 3 месяца, оплата по этапам.",
        ),
        settings,
    )
    assert r.excluded_reason is None and r.passed, r


@pytest.mark.parametrize(
    "title",
    ["Сбор базы данных компаний Германии", "Собрать каталог компаний с портала"],
)
def test_data_collection_phrasings_match_parsing(make_project, settings, title):
    r = score(mk(make_project, title=title), settings)
    assert "parsing" in r.directions and r.passed, r


def test_approximate_price_request_is_price_not_examples():
    reqs = extract_client_requirements("Пришлите примерный ценник. Напишите ценник и сроки.")
    assert reqs == ["Напишите ценник и сроки."]  # «примерный» — не «пример работ»
    assert extract_client_requirements("Пришлите примеры работ.") == ["Пришлите примеры работ."]


def test_requirement_item_keeps_its_own_commas_when_items_split_by_semicolon():
    text = "В отклике укажите: стек: Python, FastAPI; сроки; цену"
    assert extract_client_requirements(text) == [
        "В отклике укажите: стек: Python, FastAPI; сроки; цену"
    ]
    assert extract_client_requirements("В отклике укажите: опыт, сроки и цену") == [
        "В отклике укажите: опыт; сроки и цену"
    ]


def test_experience_with_words_between_is_penalized(make_project, settings):
    # «опыт коммерческой разработки от 3 лет» раньше не находился: слова между «опыт» и «от»
    base = mk(make_project, description="Нужен бэкенд на Python.")
    hard = mk(
        make_project, description="Нужен бэкенд на Python. Опыт коммерческой разработки от 3 лет."
    )
    assert score(base, settings).score - score(hard, settings).score == (
        settings.requirement_penalty
    )
    assert "⚠ требование: опыт от 3 лет" in " ".join(score(hard, settings).reasons)
