"""Черновики: 4 части под ТЗ, требования заказчика, валидатор, похожесть, LLM с откатом."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2
import pytest

from freelance_bot.config import DEFAULT_LLM_MODEL, Settings, load_settings
from freelance_bot.drafting import (
    BRIEF_MAX_CHARS,
    MAX_DRAFT_CHARS,
    PROMPT_EXAMPLES,
    REVIEW_GENERATOR,
    TRUNCATED_NOTE,
    BuiltinDrafter,
    LlmDrafter,
    LlmUnavailable,
    brief_block,
    fill_offer,
    is_truncated,
    make_drafter,
    rank_facts,
    remaining_placeholders,
    review_draft,
    review_reason,
    similarity,
    tidy_llm_text,
    validate_draft,
)
from freelance_bot.models import Project, ScoreResult
from freelance_bot.profile import Fact, Profile, load_profile
from freelance_bot.scoring import extract_client_requirements, score_project
from freelance_bot.sources.fl import parse_fl_rss
from freelance_bot.sources.kwork import parse_kwork_projects
from freelance_bot.storage import Storage

TG_TITLE = "Telegram-бот для записи клиентов в салон красоты"
TG_DESCRIPTION = (
    "Нужен бот для онлайн-записи клиентов: выбор мастера, услуги и времени, напоминания "
    "за сутки до визита. Админка для мастеров, хранение данных в PostgreSQL. "
    "Интеграция с Google Calendar."
)
PARSE_TITLE = "Парсер цен конкурентов с маркетплейсов"
PARSE_DESCRIPTION = (
    "Нужно ежедневно собирать цены и остатки по 500 товарам с Ozon и Wildberries и "
    "выгружать в Google Sheets. Обязательно логирование ошибок и повтор при сбоях."
)


def score(directions: list[str], terms: list[str], requirements: list[str] | None = None):
    return ScoreResult(
        score=80,
        passed=True,
        directions=directions,
        matched_terms=terms,
        excluded_reason=None,
        reasons=[],
        client_requirements=requirements or [],
    )


@pytest.fixture
def settings(repo_root: Path, tmp_path: Path) -> Settings:
    """Настройки из репозитория: offer.price_rub/days пустые."""
    return load_settings(repo_root / "settings.toml", tmp_path / "no.env")


@pytest.fixture
def profile(repo_root: Path) -> Profile:
    return load_profile(repo_root / "profile" / "profile.example.toml")


@pytest.fixture
def storage() -> Iterator[Storage]:
    s = Storage(":memory:")
    yield s
    s.close()


@pytest.fixture
def tg_project(make_project) -> Project:
    return make_project(
        external_id="101", title=TG_TITLE, description=TG_DESCRIPTION, budget_max=30000
    )


@pytest.fixture
def parse_project(make_project) -> Project:
    return make_project(
        platform="kwork",
        external_id="202",
        title=PARSE_TITLE,
        description=PARSE_DESCRIPTION,
        budget_min=None,
        budget_max=None,
    )


TG_SCORE = score(["telegram", "backend"], ["telegram", "бот", "postgresql"])
PARSE_SCORE = score(["parsing", "automation"], ["парсер", "google sheets"])


# ── встроенный генератор ─────────────────────────────────────────────────────


async def test_builtin_draft_has_four_parts_with_specifics_of_the_brief(
    profile: Profile, settings: Settings, tg_project: Project
) -> None:
    draft = await BuiltinDrafter(profile, settings).generate(tg_project, TG_SCORE)
    text = draft.text
    assert draft.generator == "builtin"
    assert "салон красоты" in text  # что понял — из заголовка ТЗ
    assert "напоминания" in text or "Google Calendar" in text  # и из описания
    assert "предлагаю" in text.lower() or "сделаю" in text.lower()  # как решу
    assert "опыт" in text.lower()  # подтверждённый опыт
    assert text.count("?") == 1  # ровно один вопрос
    assert text.rstrip().endswith("Иван")
    assert validate_draft(text, profile, []) == []


async def test_builtin_draft_uses_only_confirmed_facts(
    profile: Profile, settings: Settings, tg_project: Project
) -> None:
    text = (await BuiltinDrafter(profile, settings).generate(tg_project, TG_SCORE)).text
    assert any(f.text in text for f in profile.confirmed_experience)


async def test_placeholders_when_price_and_days_not_set(
    profile: Profile, settings: Settings, tg_project: Project
) -> None:
    draft = await BuiltinDrafter(profile, settings).generate(tg_project, TG_SCORE)
    assert "[ЦЕНА]" in draft.text and "[СРОК]" in draft.text
    assert draft.price_placeholder and draft.deadline_placeholder
    assert "30 000 ₽" in draft.text  # бюджет заказчика рядом


async def test_price_and_days_substituted_when_set(
    profile: Profile, settings: Settings, tg_project: Project
) -> None:
    settings = dataclasses.replace(settings, offer_price_rub=25000, offer_days=7)
    draft = await BuiltinDrafter(profile, settings).generate(tg_project, TG_SCORE)
    assert "[ЦЕНА]" not in draft.text and "[СРОК]" not in draft.text
    assert "25 000 ₽" in draft.text and "7 дн" in draft.text
    assert not draft.price_placeholder and not draft.deadline_placeholder


async def test_unknown_budget_is_not_invented(
    profile: Profile, settings: Settings, parse_project: Project
) -> None:
    text = (await BuiltinDrafter(profile, settings).generate(parse_project, PARSE_SCORE)).text
    assert "бюджет" not in text.lower()


@pytest.mark.parametrize(
    ("requirement", "check"),
    [
        ("Начните отклик со слова «Банан»", lambda t: t.lstrip().startswith("Банан")),
        (
            'Начинайте сообщение с кодового слова "Синий кит"',
            lambda t: t.lstrip().startswith("Синий кит"),
        ),
        ("Напишите в отклике слово «огурец»", lambda t: "огурец" in t),
        ("В отклике укажите стоимость и сроки", lambda t: "[ЦЕНА]" in t and "[СРОК]" in t),
        ("Пришлите примеры похожих работ", lambda t: "[ПРИМЕРЫ РАБОТ" in t),
    ],
)
async def test_client_requirement_is_fulfilled(
    profile: Profile, settings: Settings, tg_project: Project, requirement: str, check
) -> None:
    r = score(["telegram", "backend"], ["бот"], [requirement])
    text = (await BuiltinDrafter(profile, settings).generate(tg_project, r)).text
    assert check(text), text
    assert validate_draft(text, profile, [requirement]) == []


def test_validator_flags_unmet_codeword_and_missing_price() -> None:
    fastapi = Fact("fastapi", "сервисы на FastAPI", ["backend"], "FastAPI")
    profile = Profile(name="Иван", confirmed_experience=[fastapi], target_directions=[])
    text = (
        "Понял задачу: бот. Предлагаю сделать на aiogram. Мой опыт: FastAPI. Какой хостинг?\nИван"
    )
    assert validate_draft(text, profile, []) == []
    assert validate_draft(text, profile, ["Начните отклик со слова «Банан»"])
    assert validate_draft(text, profile, ["Укажите стоимость работ"])
    assert validate_draft(text, profile, ["Напишите сроки выполнения"])


# ── валидатор ────────────────────────────────────────────────────────────────

BAD_INSERTS = {
    "5 лет Python": "У меня 5 лет опыта Python-разработки.",
    "пять лет": "Пять лет пишу на Python.",
    "стаж разработки 4 года": "Опыт разработки на Python — 4 года.",
    "сделанные Telegram-боты": "Уже разработал несколько Telegram-ботов для магазинов.",
    "сделанные AI-агенты": "Делал AI-агентов для поддержки клиентов.",
    "опыт с ботами": "Есть большой опыт создания чат-ботов.",
    "телефон +7": "Звоните: +7 900 123-45-67.",
    "телефон 8": "Мой номер 8 (900) 123-45-67.",
    "email": "Пишите на test.user@example.com.",
    "telegram-ник": "Мой телеграм @some_nick.",
    "ссылка t.me": "Пишите t.me/somebody.",
    "ссылка vk": "Страница vk.com/id1.",
    "ссылка hh": "Резюме: hh.ru/resume/123.",
    "ссылка instagram": "Портфолио instagram.com/someone.",
    "фамилия": "Иван Иванов, разработчик.",
    "отчество": "С уважением, Иван Сергеевич.",
    "город": "Живу в Твери.",
    "город 2": "Я из Самары, работаю удалённо.",
    "дата рождения": "Родился 01.02.1990.",
    "дата рождения 2": "Дата рождения: 1 февраля 1990.",
    "возраст": "Мне 30 лет.",
    "запрет из профиля": "Я senior-разработчик.",
}


async def test_builtin_baseline_passes_validator(
    profile: Profile, settings: Settings, tg_project: Project
) -> None:
    text = (await BuiltinDrafter(profile, settings).generate(tg_project, TG_SCORE)).text
    assert validate_draft(text, profile, []) == []


@pytest.mark.parametrize("kind", sorted(BAD_INSERTS))
async def test_validator_catches_forbidden_content(
    profile: Profile, settings: Settings, tg_project: Project, kind: str
) -> None:
    base = (await BuiltinDrafter(profile, settings).generate(tg_project, TG_SCORE)).text
    head, _, sign = base.rstrip().rpartition("\n")
    bad = f"{head}\n{BAD_INSERTS[kind]}\n{sign}"
    assert validate_draft(bad, profile, []), kind


PERSONAL_INSERTS = {
    "фамилия перед именем": "С уважением, Петров Иван",
    "год рождения г.р.": "Разработчик, 1997 г.р.",
    "возраст без «лет»": "Мне 28, работаю удалённо.",
    "telegram-ник без @": "telegram: gleb_dev",
}


@pytest.mark.parametrize("kind", sorted(PERSONAL_INSERTS))
async def test_validator_flags_each_kind_of_personal_data(
    profile: Profile, settings: Settings, tg_project: Project, kind: str
) -> None:
    base = (await BuiltinDrafter(profile, settings).generate(tg_project, TG_SCORE)).text
    head, _, sign = base.rstrip().rpartition("\n")
    bad = f"{head}\n{PERSONAL_INSERTS[kind]}\n{sign}"
    problems = validate_draft(bad, profile, [])
    assert any("личные данные" in v for v in problems), (kind, problems)


@pytest.mark.parametrize(
    "text",
    [
        "Мне 2 дня хватит на прототип.\nИван",
        "Telegram: aiogram 3, бэкенд — FastAPI.\nИван",
        "Сделаю к 2027 году.\nИван",
        "Ваш Иван",
    ],
)
def test_personal_data_rules_do_not_flag_ordinary_phrases(text: str) -> None:
    profile = Profile(name="Иван", confirmed_experience=[], target_directions=[])
    ok = f"Понял задачу. Предлагаю план. Опыт: FastAPI. Какой хостинг?\n{text}"
    assert not [v for v in validate_draft(ok, profile, []) if "личные данные" in v]


def test_validator_requires_exactly_one_question_and_all_parts() -> None:
    profile = Profile(name="Иван", confirmed_experience=[], target_directions=[])
    ok = "Понял задачу. Предлагаю план. Опыт: FastAPI. Какой хостинг?\nИван"
    assert validate_draft(ok, profile, []) == []
    assert any("вопрос" in v for v in validate_draft(ok.replace("?", "."), profile, []))
    assert any("вопрос" in v for v in validate_draft(ok + " А сроки?", profile, []))
    assert any("опыт" in v for v in validate_draft(ok.replace("Опыт", "Стек"), profile, []))
    assert any("понял" in v for v in validate_draft(ok.replace("Понял", "Вижу"), profile, []))
    assert any(
        "предлага" in v for v in validate_draft(ok.replace("Предлагаю", "Будет"), profile, [])
    )


async def test_contacts_appear_and_pass_only_when_user_enabled_them(
    profile: Profile, settings: Settings, tg_project: Project
) -> None:
    hidden = (await BuiltinDrafter(profile, settings).generate(tg_project, TG_SCORE)).text
    assert "@example_nick" not in hidden

    enabled = dataclasses.replace(
        profile, include_contacts=True, contacts={"telegram": "@example_nick"}
    )
    shown = (await BuiltinDrafter(enabled, settings).generate(tg_project, TG_SCORE)).text
    assert "@example_nick" in shown
    assert validate_draft(shown, enabled, []) == []
    assert validate_draft(shown.replace("@example_nick", "@other_nick"), enabled, [])
    # тот же контакт без разрешения — нарушение
    assert validate_draft(shown, profile, [])


# ── похожесть и варианты ─────────────────────────────────────────────────────


def test_similarity_bounds() -> None:
    assert similarity("один два три четыре", "один два три четыре") == pytest.approx(1.0)
    assert similarity("один два три четыре", "пять шесть семь восемь") == pytest.approx(0.0)
    assert similarity("", "") == 0.0


async def test_different_briefs_give_dissimilar_drafts(
    profile: Profile, settings: Settings, tg_project: Project, parse_project: Project
) -> None:
    drafter = BuiltinDrafter(profile, settings)
    a = (await drafter.generate(tg_project, TG_SCORE)).text
    b = (await drafter.generate(parse_project, PARSE_SCORE)).text
    assert similarity(a, b) < 0.6


async def test_another_variant_gives_different_text(
    profile: Profile, settings: Settings, tg_project: Project
) -> None:
    drafter = BuiltinDrafter(profile, settings)
    first = (await drafter.generate(tg_project, TG_SCORE, variant=0)).text
    second = (await drafter.generate(tg_project, TG_SCORE, variant=1)).text
    assert first != second
    assert validate_draft(second, profile, []) == []


async def test_draft_similar_to_recent_is_rebuilt(
    profile: Profile, settings: Settings, tg_project: Project, storage: Storage
) -> None:
    plain = (await BuiltinDrafter(profile, settings).generate(tg_project, TG_SCORE)).text
    storage.add_response("fl", "101", "draft", plain, "builtin", False, None)
    rebuilt = (
        await BuiltinDrafter(profile, settings, storage=storage).generate(tg_project, TG_SCORE)
    ).text
    assert similarity(rebuilt, plain) < 0.6
    assert validate_draft(rebuilt, profile, []) == []


# ── LLM ──────────────────────────────────────────────────────────────────────


class FakeMessages:
    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        result = self.result.pop(0) if isinstance(self.result, list) else self.result
        if isinstance(result, Exception):
            raise result
        return result


def fake_client(result) -> SimpleNamespace:
    messages = FakeMessages(result)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages), messages=messages)


def reply(text: str, stop_reason: str = "end_turn") -> SimpleNamespace:
    return SimpleNamespace(
        stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)]
    )


GOOD_LLM_TEXT = (
    "Здравствуйте! Понял задачу: бот записи клиентов в салон красоты с напоминаниями "
    "и Google Calendar. Предлагаю собрать бота на aiogram с бэкендом на FastAPI и PostgreSQL. "
    "Мой опыт: писал сервисы на FastAPI и Flask. "
    "Стоимость: [ЦЕНА], срок: [СРОК]. Сколько мастеров будет в салоне?\nИван"
)


@pytest.fixture
def llm_settings(settings: Settings) -> Settings:
    return dataclasses.replace(settings, anthropic_api_key="test-key-not-real")


def test_llm_drafter_requires_api_key(profile: Profile, settings: Settings) -> None:
    with pytest.raises(LlmUnavailable):
        LlmDrafter(profile, settings, fake_client(reply(GOOD_LLM_TEXT)))


async def test_llm_valid_reply_is_used_with_configured_model(
    profile: Profile, llm_settings: Settings, tg_project: Project
) -> None:
    client = fake_client(reply(GOOD_LLM_TEXT))
    draft = await LlmDrafter(profile, llm_settings, client).generate(tg_project, TG_SCORE)
    assert draft.generator == "llm"
    assert draft.text == GOOD_LLM_TEXT
    assert draft.price_placeholder and draft.deadline_placeholder
    call = client.beta.messages.calls[0]
    assert call["model"] == llm_settings.llm_model == DEFAULT_LLM_MODEL == "claude-sonnet-5-5"
    assert TG_TITLE in str(call["messages"])
    assert "test-key-not-real" not in str(call)
    rules = call["messages"][0]["content"]  # отклик — по правилам своей площадки
    assert tg_project.platform == "fl" and "Безопасной сделки" in rules
    assert "обнуляет коннекты" not in rules


async def test_llm_model_comes_from_env(
    profile: Profile, repo_root: Path, tmp_path: Path, tg_project: Project, monkeypatch
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setenv("LLM_MODEL", "claude-opus-5-5")
    settings = load_settings(repo_root / "settings.toml", tmp_path / "no.env")
    client = fake_client(reply(GOOD_LLM_TEXT))
    await LlmDrafter(profile, settings, client).generate(tg_project, TG_SCORE)
    assert client.beta.messages.calls[0]["model"] == "claude-opus-5-5"


@pytest.mark.parametrize(
    "result",
    [
        reply("У меня 5 лет опыта Python. Понял. Предлагаю. Вопрос?\nИван"),
        reply("Разработал десятки Telegram-ботов. Понял задачу. Предлагаю. Опыт есть. Да?\nИван"),
        reply(GOOD_LLM_TEXT.replace("[ЦЕНА]", "50 000 ₽")),  # выдуманная цена
        reply(GOOD_LLM_TEXT, stop_reason="refusal"),
        anthropic.APIConnectionError(request=httpx2.Request("POST", "https://api.anthropic.com")),
        RuntimeError("boom"),
    ],
    ids=["5-лет", "боты", "цена-вместо-метки", "refusal", "api-error", "runtime-error"],
)
async def test_llm_falls_back_to_builtin(
    profile: Profile, llm_settings: Settings, tg_project: Project, result
) -> None:
    draft = await LlmDrafter(profile, llm_settings, fake_client(result)).generate(
        tg_project, TG_SCORE
    )
    assert draft.generator == "builtin"
    assert validate_draft(draft.text, profile, []) == []


def test_make_drafter_picks_generator_by_key(
    profile: Profile, settings: Settings, llm_settings: Settings
) -> None:
    assert isinstance(make_drafter(profile, settings), BuiltinDrafter)
    assert isinstance(make_drafter(profile, llm_settings), LlmDrafter)


# ── ТЗ из фикстуры FL: доработка существующего и пункты требований ───────────


@pytest.fixture
def fl_fixture(repo_root: Path) -> dict[str, Project]:
    xml = (repo_root / "tests" / "fixtures" / "fl_rss.xml").read_text(encoding="utf-8")
    return {p.external_id: p for p in parse_fl_rss(xml)}


FIX_BUILD_PHRASES = ("aiogram", "python-telegram-bot", "с нуля", "соберу бота", "связку с LLM")


@pytest.mark.parametrize("variant", [0, 1, 2])
async def test_fix_brief_proposes_fixing_existing_code_not_building_new(
    profile: Profile, settings: Settings, fl_fixture: dict[str, Project], variant: int
) -> None:
    p = fl_fixture["5525741"]  # «Senior Python-разработчик: устранение ошибок в действующем боте»
    r = score(["telegram", "ai_agents", "backend"], ["бот", "python"])
    text = (await BuiltinDrafter(profile, settings).generate(p, r, variant)).text
    lowered = text.lower()
    for phrase in FIX_BUILD_PHRASES:
        assert phrase.lower() not in lowered, phrase
    assert "объединить выбор услуги" in lowered  # шаги взяты из задач ТЗ
    assert re.search(r"разбер\w*|воспроизвед\w*", lowered)
    assert "нужно сделать" not in lowered
    assert "senior" not in lowered  # заголовок-вакансия не выдаётся за задачу
    assert validate_draft(text, profile, []) == []


@pytest.mark.parametrize(
    "title",
    [
        "Senior Python-разработчик: устранение ошибок в действующем чат-боте",
        "Python-разработчик в команду интернет-магазина",
    ],
)
async def test_vacancy_title_is_not_presented_as_the_task(
    profile: Profile, settings: Settings, make_project, title: str
) -> None:
    p = make_project(
        title=title,
        description="Нужно доработать каталог товаров и фильтры в существующем сервисе на FastAPI.",
    )
    text = (await BuiltinDrafter(profile, settings).generate(p, score(["backend"], []))).text
    understood = text.split("\n\n")[1]
    assert "разработчик" not in understood.lower(), understood
    assert validate_draft(text, profile, []) == []


START_LINE = r"(?m)^(?:— )?Старт: \[ДАТА СТАРТА — впиши\]\.?$"
ITEM_ANSWERS = {
    # пункт требования → признак содержательного ответа или видимой метки на этот пункт
    "5525741": [
        r"\[ПРИМЕРЫ РАБОТ",  # пример похожей работы — метка
        START_LINE,  # когда начну — только метка, без выдуманной доступности
        r"\[ЧАСОВ В НЕДЕЛЮ",  # сколько часов — метка
        r"понадоб\w*:.*репозитор",  # какие материалы нужны — по ТЗ
        r"\[ЦЕНА\].*\[СРОК\]",  # стоимость и срок
    ],
    "5525445": [
        r"\[ПРИМЕРЫ РАБОТ",
        START_LINE,
        r"\[ПОДТВЕРДИ ДОСТУПНОСТЬ",
        r"понадоб\w*:.*(исходник|сервер)",
    ],
}


@pytest.mark.parametrize("external_id", sorted(ITEM_ANSWERS))
async def test_every_item_of_client_requirement_is_answered(
    profile: Profile, settings: Settings, fl_fixture: dict[str, Project], external_id: str
) -> None:
    p = fl_fixture[external_id]
    requirements = extract_client_requirements(p.description)
    assert any(";" in req for req in requirements)  # пункты пришли одной строкой
    r = score(["backend", "automation"], ["python"], requirements)
    for variant in range(3):
        text = (await BuiltinDrafter(profile, settings).generate(p, r, variant)).text
        for pattern in ITEM_ANSWERS[external_id]:
            assert re.search(pattern, text, re.DOTALL), (pattern, text)
        assert validate_draft(text, profile, requirements) == []


def test_validator_flags_unanswered_items_of_requirement() -> None:
    profile = Profile(name="Иван", confirmed_experience=[], target_directions=[])
    req = (
        "В отклике укажите: Пример похожей работы; Сможете ли начать в ближайшее время и "
        "сколько часов выделите за неделю; Какие материалы нужны"
    )
    base = "Понял задачу. Предлагаю план. Опыт: FastAPI. Какой хостинг?\nИван"
    problems = validate_draft(base, profile, [req])
    for word in ("пример", "начать", "час", "материал"):
        assert any(word in v for v in problems), (word, problems)
    answered = (
        base + "\nПримеры: [ПРИМЕРЫ РАБОТ — впиши]\nСтарт: могу приступить после согласования."
        "\nВремя: [ЧАСОВ В НЕДЕЛЮ — впиши].\nДля оценки понадобятся: доступ к репозиторию."
    )
    assert validate_draft(answered, profile, [req]) == []


def test_experience_and_bot_claims_follow_the_profile(profile: Profile) -> None:
    claim = "У меня 5 лет опыта Python-разработки. Делал Telegram-ботов для магазинов."
    assert profile.experience_years == 1.8 and not profile.built_bots_or_agents  # пример профиля
    problems = validate_draft(claim, profile, [])
    assert any("5 лет" in p for p in problems) and any("ботами" in p for p in problems)
    senior = dataclasses.replace(profile, experience_years=6, built_bots_or_agents=True)
    assert not any(  # остальные замечания — о структуре черновика, не о правде
        word in p for p in validate_draft(claim, senior, []) for word in ("5 лет", "ботами", "стаж")
    )
    four = dataclasses.replace(profile, experience_years=3)
    assert any("стаж разработки «4 года»" in p
               for p in validate_draft("Python-разработка: 4 года.", four, []))


def test_llm_text_is_tidied_and_signed_last() -> None:
    raw = "Здравствуйте!  \r\nПонял задачу.  \n\n\n\nИван\nКакой хостинг?  \n"
    assert tidy_llm_text(raw, "Иван") == "Здравствуйте!\nПонял задачу.\n\nКакой хостинг?\nИван"
    assert tidy_llm_text("Понял. Вопрос?", "Иван") == "Понял. Вопрос?\nИван"  # подписи не было
    assert tidy_llm_text("  ", "Иван") == ""


async def test_llm_reply_with_bare_question_mark_falls_back(
    profile: Profile, llm_settings: Settings, tg_project: Project
) -> None:
    bare = GOOD_LLM_TEXT.replace("Сколько мастеров будет в салоне?", "[ДАТА СТАРТА — впиши]\n?")
    draft = await LlmDrafter(profile, llm_settings, fake_client(reply(bare))).generate(
        tg_project, TG_SCORE
    )
    assert draft.generator == "builtin"


async def test_rejected_llm_reply_is_fixed_by_the_model_up_to_twice(
    profile: Profile, llm_settings: Settings, tg_project: Project
) -> None:
    no_price = GOOD_LLM_TEXT.replace("Стоимость: [ЦЕНА], срок: [СРОК]. ", "")
    client = fake_client([reply(no_price), reply(GOOD_LLM_TEXT)])
    draft = await LlmDrafter(profile, llm_settings, client).generate(tg_project, TG_SCORE)
    assert draft.generator == "llm" and "[ЦЕНА]" in draft.text
    fix = client.beta.messages.calls[1]["messages"]
    assert [m["role"] for m in fix] == ["user", "assistant", "user"]
    assert fix[1]["content"] == tidy_llm_text(no_price, "Иван")
    assert "- нет метки [ЦЕНА]" in fix[2]["content"]

    client = fake_client([reply(no_price), reply(no_price), reply(GOOD_LLM_TEXT)])
    draft = await LlmDrafter(profile, llm_settings, client).generate(tg_project, TG_SCORE)
    assert draft.generator == "llm" and len(client.beta.messages.calls) == 3  # со второго раза

    client = fake_client([reply(no_price)] * 3)  # не исправила дважды — встроенный
    draft = await LlmDrafter(profile, llm_settings, client).generate(tg_project, TG_SCORE)
    assert draft.generator == "builtin" and len(client.beta.messages.calls) == 3


async def test_technology_outside_profile_in_experience_is_sent_back(
    profile: Profile, llm_settings: Settings, tg_project: Project
) -> None:
    invented = (
        "Здравствуйте! Понял задачу: бот записи в салон.\n\n"
        "Предлагаю собрать бота на aiogram.\n\n"
        "Опыт: писал сервисы на FastAPI и Flask, валидацию на Pydantic.\n\n"
        "Стоимость: [ЦЕНА], срок: [СРОК].\n\nСколько мастеров будет в салоне?\nИван"
    )
    client = fake_client([reply(invented), reply(invented.replace(", валидацию на Pydantic", ""))])
    draft = await LlmDrafter(profile, llm_settings, client).generate(tg_project, TG_SCORE)
    assert draft.generator == "llm" and "Pydantic" not in draft.text
    fix = client.beta.messages.calls[1]["messages"][2]["content"]
    assert "Pydantic" in fix and "FastAPI" not in fix  # FastAPI есть в фактах профиля


async def test_llm_variant_sees_previous_draft(
    profile: Profile, llm_settings: Settings, tg_project: Project, storage: Storage
) -> None:
    storage.upsert_project(tg_project)
    storage.add_response("fl", "101", "draft", "ПРОШЛЫЙ ТЕКСТ", "llm", False, "выбран заказ")
    client = fake_client(reply(GOOD_LLM_TEXT))
    drafter = LlmDrafter(profile, llm_settings, client, storage=storage)
    await drafter.generate(tg_project, TG_SCORE, variant=0)
    await drafter.generate(tg_project, TG_SCORE, variant=1)
    first, second = (c["messages"][0]["content"] for c in client.beta.messages.calls)
    assert "previous_draft" not in first  # первый вариант пишется с чистого листа
    assert "<previous_draft>\nПРОШЛЫЙ ТЕКСТ\n</previous_draft>" in second


async def test_llm_prompt_uses_profile_name_and_experience(
    profile: Profile, llm_settings: Settings, tg_project: Project
) -> None:
    anna = dataclasses.replace(profile, name="Анна", experience_years=4, built_bots_or_agents=True)
    client = fake_client(reply(GOOD_LLM_TEXT))
    await LlmDrafter(anna, llm_settings, client).generate(tg_project, TG_SCORE)
    system = client.beta.messages.calls[0]["system"]
    assert "по имени Анна" in system and "(4 лет)" in system and "Иван" not in system
    assert "Telegram-ботов, AI-агентов" not in system  # Анна их делала


def test_approximate_price_is_not_a_request_for_examples() -> None:
    profile = Profile(name="Иван", confirmed_experience=[], target_directions=[])
    req = "В отклике напишите: примерный ценник; примерно сколько займёт"
    base = "Понял задачу. Предлагаю план. Опыт: FastAPI. Какой хостинг?\nИван"
    problems = validate_draft(base, profile, [req])
    assert not any("пример" in p for p in problems), problems  # «примерный» — не примеры работ
    assert any("стоимост" in p for p in problems), problems  # «ценник» — это цена
    answered = base + "\nСтоимость: [ЦЕНА], срок: [СРОК]."
    assert validate_draft(answered, profile, [req]) == []


async def test_unknown_item_gets_visible_placeholder(
    profile: Profile, settings: Settings, tg_project: Project
) -> None:
    req = "В отклике укажите: любимый цвет; когда начнёте"
    r = score(["telegram"], ["бот"], [req])
    text = (await BuiltinDrafter(profile, settings).generate(tg_project, r)).text
    assert "[ПУНКТ ЗАКАЗЧИКА: «любимый цвет»" in text
    assert re.search(START_LINE, text), text


@pytest.mark.parametrize("external_id", sorted(ITEM_ANSWERS))
async def test_start_line_has_only_placeholder_and_no_invented_availability(
    profile: Profile, settings: Settings, fl_fixture: dict[str, Project], external_id: str
) -> None:
    # Когда Иван может начать — знает только он: генератор ставит метку и ничего не обещает.
    p = fl_fixture[external_id]
    r = score(["backend"], ["python"], extract_client_requirements(p.description))
    for variant in range(3):
        text = (await BuiltinDrafter(profile, settings).generate(p, r, variant)).text
        start_lines = [line.removeprefix("— ") for line in text.splitlines() if "Старт" in line]
        assert start_lines == ["Старт: [ДАТА СТАРТА — впиши]."], start_lines
        assert not re.search(r"приступ\w*", text, re.IGNORECASE), text


# ── поправка после слепой приёмки (G4): «как решу» по ТЗ, без обрывков, опыт под проект ──

NOW_G4 = datetime(2026, 10, 21, 20, 0, tzinfo=UTC)
FIX_MARKERS = re.compile(r"воспроизвед|разбер\w* код|текущ\w+ (?:кода|бота|интеграц|сервиса)")
PROPOSAL_RE = re.compile(r"Предлагаю|Как решу")

# Тексты из реального dry-run FL (docs: scratchpad fl-dryrun), в RSS-фикстуре их нет:
# первое предложение — дословно из карточки, остальное — тот же смысл и те же термины.
FL_5526172 = dict(
    external_id="5526172",
    title="Ищем специалиста по интеграции ИИ и автоматизации бизнес-процессов",
    description=(
        "Необходимо внедрить ИИ-инструменты для автоматизации работы руководителя действующего "
        "бизнеса.\nОсновные направления:\n- разбор входящих писем и заявок с помощью ChatGPT;\n"
        "- подготовка еженедельных отчётов;\n- напоминания о задачах."
    ),
    budget_max=35000,
    offers_count=None,
)
FL_5525978 = dict(
    external_id="5525978",
    title="Разработка AI-агента для сбора B2B-контактов (n8n + Python)",
    description=(
        "Задача: разработать автоматизированного агента для сбора, структурирования и передачи "
        "в базу (Google Sheets) контактов потенциальных B2B-клиентов для холодных звонков.\n"
        "Стек технологий: n8n (оркестрация), Python (парсинг), Google Sheets (база данных).\n"
        "Сбор данных из каталогов компаний: BeautifulSoup, Scrapy или Selenium, где есть API — "
        "через API.\nАвтоматизация: запуск по расписанию, интеграция с amoCRM, выгрузка новых "
        "контактов, отчёт о прогоне в Telegram."
    ),
    budget_max=None,
    offers_count=None,
)


def paragraph(text: str, pattern: re.Pattern[str]) -> str:
    return next(part for part in text.split("\n\n") if pattern.search(part))


def kwork_fixture(repo_root: Path) -> dict[str, Project]:
    html = (repo_root / "tests" / "fixtures" / "kwork_projects.html").read_text(encoding="utf-8")
    return {p.external_id: p for p in parse_kwork_projects(html)}


@pytest.mark.parametrize("variant", [0, 1, 2])
@pytest.mark.parametrize("source", ["fl-5526172", "kwork-3268440"])
async def test_new_development_brief_does_not_get_fix_existing_plan(
    profile: Profile, settings: Settings, make_project, repo_root: Path, source: str, variant: int
) -> None:
    if source == "fl-5526172":  # «…руководителя действующего бизнеса» — бизнес, а не код
        p = make_project(published_at=NOW_G4, **FL_5526172)
    else:  # «Разработать Telegram-бота…»; «ошибки», «доработки» — в гарантии и оговорках
        p = kwork_fixture(repo_root)["3268440"]
    r = score_project(p, settings, NOW_G4)
    text = (await BuiltinDrafter(profile, settings).generate(p, r, variant)).text
    proposal = paragraph(text, PROPOSAL_RE)
    assert not FIX_MARKERS.search(proposal), proposal
    assert "тестовое окружение" not in text and "самая срочная" not in text, text
    assert validate_draft(text, profile, r.client_requirements) == []


@pytest.mark.parametrize("variant", [0, 1, 2])
async def test_steps_name_stack_and_services_from_the_brief(
    profile: Profile, settings: Settings, make_project, variant: int
) -> None:
    p = make_project(published_at=NOW_G4, **FL_5525978)
    r = score_project(p, settings, NOW_G4)
    assert {"telegram", "backend", "ai_agents"} <= set(r.directions)  # как в реальной карточке
    text = (await BuiltinDrafter(profile, settings).generate(p, r, variant)).text
    proposal = paragraph(text, PROPOSAL_RE)
    for absent in ("aiogram", "python-telegram-bot", "FastAPI", "PostgreSQL", "бота"):
        assert absent not in proposal, (absent, proposal)
    for named in ("n8n", "Google Sheets"):
        assert named in proposal, (named, proposal)
    assert validate_draft(text, profile, r.client_requirements) == []


UNDERSTOOD_RE = re.compile(r"Понял|Понима|Как я понял")
# Слова шаблонов части «что понял» — всё остальное в ней должно быть цитатой из ТЗ.
UNDERSTOOD_TEMPLATE_WORDS = set(
    "понял понимаю задачу задача так как я из описания описанию по тз суть такая для себя "
    "выделил ключевое в описании отдельно учел".split()
)


def _words(text: str) -> list[str]:
    return re.findall(r"[a-zа-я0-9]+", text.lower().replace("ё", "е"))


def assert_no_fragments(understood: str, p: Project) -> None:
    assert "…" not in understood and "..." not in understood, understood
    assert "Ключевые моменты" not in understood and "Опорные слова" not in understood, understood
    source = set(_words(f"{p.title}\n{p.description}"))
    # слово, которого нет в ТЗ целиком и нет в шаблоне, — обрывок («тек» из «Стек»)
    known = source | UNDERSTOOD_TEMPLATE_WORDS
    stray = [w for w in _words(understood) if w not in known]
    assert stray == [], (stray, understood)


@pytest.mark.parametrize("variant", [0, 1, 2])
async def test_understood_part_has_no_broken_fragments_on_fl_fixture(
    profile: Profile, settings: Settings, fl_fixture: dict[str, Project], variant: int
) -> None:
    drafter = BuiltinDrafter(profile, settings)
    for p in fl_fixture.values():
        r = score_project(p, settings, p.published_at)
        text = (await drafter.generate(p, r, variant)).text
        assert_no_fragments(paragraph(text, UNDERSTOOD_RE), p)


@pytest.mark.parametrize("source", ["fl-5525978", "fl-5526172", "kwork-3268440"])
async def test_understood_part_skips_headers_and_cut_sentences(
    profile: Profile, settings: Settings, make_project, repo_root: Path, source: str
) -> None:
    projects = {
        "fl-5525978": lambda: make_project(published_at=NOW_G4, **FL_5525978),
        "fl-5526172": lambda: make_project(published_at=NOW_G4, **FL_5526172),
        "kwork-3268440": lambda: kwork_fixture(repo_root)["3268440"],
    }
    p = projects[source]()
    r = score_project(p, settings, NOW_G4)
    for variant in range(3):
        text = (await BuiltinDrafter(profile, settings).generate(p, r, variant)).text
        understood = paragraph(text, UNDERSTOOD_RE)
        assert_no_fragments(understood, p)
        # заголовки-подписи «Основные направления:», «Название:», «Задача:» — не цитата
        for header in ("основные направления", "название:", "задача:", "стек технологий:"):
            assert header not in understood.lower(), (header, understood)
        proposal = paragraph(text, PROPOSAL_RE)
        assert "…" not in proposal and "..." not in proposal, proposal


EXPERIENCE_RE = re.compile(r"опыт", re.IGNORECASE)
# Как в реальном dry-run: почти в каждом ТЗ есть «API»/«Python», поэтому backend совпадает
# у всех — но опыт должен отличаться по главному направлению и терминам ТЗ.
DIRECTION_BRIEFS = {
    "backend": (
        "REST API на Django для учёта заказов склада",
        "Нужен backend на Python: REST API, база данных, оптимизация медленных запросов.",
    ),
    "telegram": (
        "Telegram-бот для записи клиентов",
        "Нужен телеграм бот с кнопками и напоминаниями, данные через API нашего сайта.",
    ),
    "parsing": (
        "Парсер цен конкурентов",
        "Парсинг 500 товаров с сайтов на Python, выгрузка в Google Sheets через API.",
    ),
    "ai_agents": (
        "AI-агент для ответов клиентам",
        "Нужен ИИ-агент на базе LLM: отвечает на вопросы по базе знаний, подключение по API.",
    ),
}


def experience_facts(text: str, profile: Profile) -> set[str]:
    part = paragraph(text, EXPERIENCE_RE)
    return {f.id for f in profile.confirmed_experience if f.text in part}


async def test_experience_block_is_picked_for_the_project(
    profile: Profile, settings: Settings, make_project
) -> None:
    chosen: dict[str, set[str]] = {}
    for i, (direction, (title, description)) in enumerate(DIRECTION_BRIEFS.items()):
        p = make_project(external_id=f"9{i}", title=title, description=description)
        r = score_project(p, settings, p.published_at)
        assert direction in r.directions, r.directions
        text = (await BuiltinDrafter(profile, settings).generate(p, r)).text
        facts = experience_facts(text, profile)
        assert 1 <= len(facts) <= 3, (direction, facts)
        # самый точный по тегам факт направления (меньше всего тегов) — в наборе
        tagged = [f for f in profile.confirmed_experience if direction in f.tags]
        fewest = min(len(f.tags) for f in tagged)
        specific = {f.id for f in tagged if len(f.tags) == fewest}
        assert facts & specific, (direction, facts, specific)
        chosen[direction] = facts
    sets = list(chosen.values())
    for a in range(len(sets)):
        for b in range(a + 1, len(sets)):
            assert sets[a] != sets[b], chosen  # не одни и те же три в другом порядке
    assert not set.intersection(*sets), chosen  # нет факта, кочующего по всем откликам


def question_of(text: str) -> str:
    line = next(line for line in text.splitlines() if "?" in line)
    return re.search(r"([^:]+\?)\s*$", line).group(1).strip()


async def test_question_is_not_repeated_in_two_drafts_in_a_row(
    profile: Profile, settings: Settings, make_project, storage: Storage
) -> None:
    drafter = BuiltinDrafter(profile, settings, storage=storage)
    previous: str | None = None
    for i in range(8):  # подряд проекты одного направления — пул вопросов один
        p = make_project(
            external_id=f"30{i}",
            title=f"Telegram-бот для записи клиентов, филиал {i}",
            description=TG_DESCRIPTION,
        )
        text = (await drafter.generate(p, TG_SCORE)).text
        question = question_of(text)
        assert question != previous, (i, question)
        storage.add_response("fl", p.external_id, "draft", text, "builtin", False, None)
        previous = question


def test_fill_offer_replaces_placeholders():
    text = "Стоимость: [ЦЕНА], срок: [СРОК]. Примеры: [ПРИМЕРЫ РАБОТ — впиши]"
    out = fill_offer(text, 150000, 14)
    assert out == "Стоимость: 150 000 ₽, срок: 14 дней. Примеры: [ПРИМЕРЫ РАБОТ — впиши]"
    assert remaining_placeholders(out) == ["[ПРИМЕРЫ РАБОТ — впиши]"]


def test_fill_offer_appends_missing_price_and_days():
    assert fill_offer("Готов взяться.\n", 5000, 2) == (
        "Готов взяться.\n\nСтоимость: 5 000 ₽, срок: 2 дня."
    )
    assert fill_offer("Цена [ЦЕНА].", 5000, 2) == "Цена 5 000 ₽.\n\nСрок: 2 дня."
    assert [fill_offer("[ЦЕНА] [СРОК]", 1, d) for d in (1, 5, 11, 21, 22, 112)] == [
        "1 ₽ 1 день", "1 ₽ 5 дней", "1 ₽ 11 дней", "1 ₽ 21 день", "1 ₽ 22 дня", "1 ₽ 112 дней",
    ]
    assert remaining_placeholders("[A] и [A], [B]") == ["[A]", "[B]"]


# ── контекст заказа для нейросети ────────────────────────────────────────────


def test_brief_has_section_and_marks_truncated_fl_description(make_project) -> None:
    cut = make_project(description="Нужен бот для записи. Подробнее...", category="AI / Боты")
    brief = brief_block(cut)
    assert brief.startswith("<brief>\nЗаголовок: Telegram-бот на Python\nРаздел: AI / Боты\n")
    assert brief.endswith(f"</brief>\n{TRUNCATED_NOTE}") and is_truncated(cut)
    whole = make_project(platform="kwork", description="Текст...", category="41")
    assert "Раздел" not in brief_block(whole)  # у Kwork в category — номер рубрики
    assert not is_truncated(whole)  # обрезает только RSS FL


def test_long_brief_keeps_beginning_and_end(make_project) -> None:
    text = "Начало ТЗ. " + "середина " * 2000 + "В отклике напишите слово «кит»."
    brief = brief_block(make_project(description=text))
    assert len(brief) < BRIEF_MAX_CHARS + 300
    assert "Начало ТЗ." in brief and "слово «кит»." in brief
    assert "(… середина описания пропущена …)" in brief


def test_facts_closest_to_the_order_go_first(profile: Profile, make_project) -> None:
    p = make_project(title="Парсер цен", description="Собрать цены через Scrapy в базу")
    ranked = rank_facts(profile.confirmed_experience, p, score(["parsing"], ["парсер"]))
    assert "Scrapy" in ranked[0].text


async def test_llm_prompt_carries_order_conditions_and_ranked_facts(
    profile: Profile, llm_settings: Settings, make_project
) -> None:
    p = make_project(
        platform="work24", title="Бот записи в Google Таблицу", category="Разработка и IT / Боты",
        description="Бот на Python пишет запись клиента в таблицу.", budget_max=5000,
        offers_count=2, deadline_days=7,
    )
    client = fake_client(reply(GOOD_LLM_TEXT))
    await LlmDrafter(profile, llm_settings, client).generate(p, TG_SCORE)
    prompt = client.beta.messages.calls[0]["messages"][0]["content"]
    assert "Раздел: Разработка и IT / Боты" in prompt
    assert (
        "Площадка: Work24. Условия: бюджет заказчика до 5 000 ₽; срок заказчика 7 дней; "
        "откликов уже 2." in prompt
    )
    assert "Совпавшие направления: Telegram-боты, backend на Python и API;" in prompt
    assert "штраф 500 ₽" in prompt  # правила Work24
    assert "Ближе всего к этой задаче:\n" in prompt and "\nОстальное:\n" in prompt
    assert "раздел биржи и условия заказа" in client.beta.messages.calls[0]["system"]


# ── эталоны откликов (2026-10-26): честные границы опыта и ручная оценка ───────────

HONEST = Profile(
    name="Иван",
    confirmed_experience=[
        Fact("rest", "делал REST API и интеграции", ["backend"], "REST API"),
        Fact("pytest", "Pytest и Docker", ["backend"], "Pytest Docker", skill=True),
    ],
    target_directions=[],
    experience_years=1.8,
)
STRUCTURE = "Здравствуйте! Понял задачу. Предлагаю план. {claim} Какой у вас доступ?\nИван"


@pytest.mark.parametrize(
    "claim",
    [
        "Ранее внедрял MCP в Revit, опыт есть.",
        "Есть опыт работы с Bitrix24.",
        "Знаю Revit API.",
        "Опыт: у меня 10 лет опыта.",
        "Опыт есть, готов начать сегодня.",
        "Опыт есть, гарантирую готовый результат за 11 дней.",
        "Опыт: ускорил API на 40%.",
    ],
)
def test_validator_rejects_made_up_experience_and_promises(claim: str) -> None:
    assert validate_draft(STRUCTURE.format(claim=claim), HONEST, [])


@pytest.mark.parametrize(
    "claim",
    [
        "Опыт: делал REST API и интеграции.",
        "Опыта с Revit и MCP резюме не подтверждает, начну с проверки осуществимости.",
        "Мой опыт с REST поможет, а подключение Revit проверю первым шагом.",
        "Опыт: в навыках резюме — Pytest и Docker.",
        "Опыт есть, старт: [ДАТА СТАРТА — впиши].",
    ],
)
def test_validator_accepts_confirmed_experience_and_honest_gaps(claim: str) -> None:
    assert validate_draft(STRUCTURE.format(claim=claim), HONEST, []) == []


REVIT_DEMAND = (
    "Нужен разработчик Revit API и MCP. Требуется показать уже выполненные проекты с Revit."
)


async def test_demand_for_unconfirmed_cases_goes_to_review_before_any_generator(
    profile: Profile, llm_settings: Settings, make_project
) -> None:
    p = make_project(title="Плагин для Revit", description=REVIT_DEMAND)
    r = dataclasses.replace(TG_SCORE, client_requirements=["Укажите два кейса Revit"])
    client = fake_client(reply(GOOD_LLM_TEXT))
    draft = await LlmDrafter(profile, llm_settings, client).generate(p, r)
    assert draft.generator == REVIEW_GENERATOR and "Revit" in draft.text
    assert client.beta.messages.calls == []  # модель не вызывалась
    assert (await BuiltinDrafter(profile, llm_settings).generate(p, r)).generator == "review"
    # «ищем опытного специалиста» — обычная формулировка, а не требование кейсов
    usual = make_project(title="WA Business API",
                         description="Ищем опытного специалиста: подключить WA Business API.")
    assert review_reason(usual, TG_SCORE, profile) is None


async def test_builtin_names_unconfirmed_tech_and_checks_feasibility_first(
    profile: Profile, settings: Settings, make_project
) -> None:
    p = make_project(title="Подключить Claude к Revit 2023 через MCP",
                     description="Нужен агент, который управляет Revit 2023 удалённо.")
    r = score_project(p, settings, p.published_at)
    text = (await BuiltinDrafter(profile, settings).generate(p, r)).text
    assert "Опыта с Revit и MCP резюме не подтверждает" in text
    assert "сначала проверю, какой способ работы с Revit и MCP" in text
    assert validate_draft(text, profile, []) == []


async def test_skills_are_not_presented_as_project_experience(
    settings: Settings, llm_settings: Settings, make_project
) -> None:
    p = make_project(title="API для заказов", description="Сервис на Python, тесты на Pytest.")
    r = score_project(p, settings, p.published_at)
    text = (await BuiltinDrafter(HONEST, settings).generate(p, r)).text
    assert "Из релевантного опыта: делал REST API" in text or "делал REST API" in text
    assert "В навыках резюме: Pytest и Docker." in text  # ТЗ называет Pytest
    client = fake_client(reply(GOOD_LLM_TEXT))
    await LlmDrafter(HONEST, llm_settings, client).generate(p, r)
    prompt = client.beta.messages.calls[0]["messages"][0]["content"]
    assert "- делал REST API и интеграции" in prompt
    assert "Навыки из резюме (инструменты без описанных проектов" in prompt
    assert "- Pytest и Docker" not in prompt


async def test_prompt_has_nearest_examples_and_never_eval_cases(
    tmp_path: Path, repo_root: Path, llm_settings: Settings, make_project
) -> None:
    import json

    folder = tmp_path / "references"
    folder.mkdir()
    (tmp_path / "profile.toml").write_text(
        (repo_root / "profile" / "profile.example.toml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    def case(i: str, title: str, brief: str, **extra) -> str:
        given = {"title": title, "brief": brief, "price": "[ЦЕНА]", "deadline": "[СРОК]"}
        return json.dumps({"id": i, "input": given, **extra}, ensure_ascii=False)

    (folder / "examples.jsonl").write_text("\n".join([
        case("a", "Импорт CSV", "Загрузка CSV в базу", ideal_response="ОБРАЗЕЦ-CSV"),
        case("b", "Telegram-бот заявок", "Бот принимает заявки", ideal_response="ОБРАЗЕЦ-БОТ"),
        case("c", "Отчёт кофейни", "Выручка за неделю", ideal_response="ОБРАЗЕЦ-КОФЕ"),
    ]), encoding="utf-8")  # fmt: skip
    (folder / "eval.jsonl").write_text(
        case("E1", "Telegram-бот заявок", "ПРОВЕРОЧНЫЙ-СЛУЧАЙ", expected_behavior="x"),
        encoding="utf-8",
    )
    own = load_profile(tmp_path / "profile.toml")
    assert [e.id for e in own.examples] == ["a", "b", "c"]
    p = make_project(title="Telegram-бот для заявок", description="Бот принимает заявки клиентов")
    client = fake_client(reply(GOOD_LLM_TEXT))
    await LlmDrafter(own, llm_settings, client).generate(p, TG_SCORE)
    prompt = client.beta.messages.calls[0]["messages"][0]["content"]
    assert prompt.count("<example>") == PROMPT_EXAMPLES == 2
    assert prompt.index("ОБРАЗЕЦ-БОТ") < prompt.index("<brief>")  # ближайший — в промпте
    assert "ПРОВЕРОЧНЫЙ-СЛУЧАЙ" not in prompt  # eval.jsonl не читается никогда


async def test_llm_draft_longer_than_limit_is_sent_back(
    profile: Profile, llm_settings: Settings, tg_project: Project
) -> None:
    long = GOOD_LLM_TEXT.replace("Мой опыт:", "Мой опыт: " + "подробно " * 120)
    client = fake_client([reply(long), reply(GOOD_LLM_TEXT)])
    draft = await LlmDrafter(profile, llm_settings, client).generate(tg_project, TG_SCORE)
    assert draft.text == GOOD_LLM_TEXT
    fix = client.beta.messages.calls[1]["messages"][-1]["content"]
    assert f"сократи до {MAX_DRAFT_CHARS}" in fix


def test_review_note_is_shown_as_review_and_kept_out_of_similarity(storage: Storage) -> None:
    from freelance_bot.telegram_ui import format_draft

    note = review_draft("заказчик требует кейсы (Revit)")
    assert format_draft(note).startswith("⚠ <b>Нужна ручная оценка</b>")
    storage.add_response("fl", "1", "draft", note.text, note.generator, False, None)
    storage.add_response("fl", "2", "draft", "обычный черновик", "builtin", False, None)
    assert storage.recent_drafts() == ["обычный черновик"]


async def test_model_typography_does_not_break_price_check(
    profile: Profile, llm_settings: Settings, tg_project: Project
) -> None:
    """Groq пишет «35 000 ₽» и «Telegram‑бот» с неразрывными символами: цена верная,
    а посимвольно не совпадала — отклик уходил встроенному генератору."""
    priced = dataclasses.replace(llm_settings, offer_price_rub=35000, offer_days=11)
    text = (GOOD_LLM_TEXT.replace("Стоимость: [ЦЕНА], срок: [СРОК].",
                                  "Стоимость: 35 000 ₽, срок: 11 дней.")
            .replace("бот записи", "Telegram‑бот записи"))  # fmt: skip
    draft = await LlmDrafter(profile, priced, fake_client(reply(text))).generate(
        tg_project, TG_SCORE
    )
    assert draft.generator == "llm"
    assert "35 000 ₽" in draft.text and "Telegram-бот" in draft.text


@pytest.mark.parametrize(
    ("line", "ok"),
    [
        ("Стоимость: 35 000 ₽, срок: 11 дней.", True),
        ("Стоимость: 35 000 рублей, срок: 11 календарных дней.", True),  # правила FL
        ("Стоимость: 35000 руб., срок: 11 рабочих дней.", True),
        ("Стоимость: 135 000 ₽, срок: 11 дней.", False),
        ("Стоимость: 35 000 ₽, срок: 111 дней.", False),
    ],
)
def test_price_and_deadline_from_settings_in_any_spelling(
    profile: Profile, llm_settings: Settings, line: str, ok: bool
) -> None:
    priced = dataclasses.replace(llm_settings, offer_price_rub=35000, offer_days=11)
    drafter = LlmDrafter(profile, priced, fake_client(reply(GOOD_LLM_TEXT)))
    assert (drafter._offer_problems(line) == []) is ok
