"""Самообучение по отметкам (ADR 0013): правила из причин «не подходит» и их действие."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from freelance_bot import learning
from freelance_bot.config import load_settings
from freelance_bot.models import Lesson
from freelance_bot.scoring import score_project

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 25, 10, 0, tzinfo=UTC)


@pytest.fixture(scope="module")
def settings():
    s = load_settings(ROOT / "settings.toml", ROOT / "no-such.env")
    return replace(s, min_score=25)


def vacancy(make_project, title="Бизнес-аналитик", **kw):
    kw.setdefault("extra", {"company": "ООО Ромашка", "area": "Москва"})
    return make_project(
        platform="superjob", external_id=kw.pop("external_id", "1"), title=title,
        description=kw.pop("description", "Python, SQL, отчёты."), published_at=NOW,
        budget_min=kw.pop("budget_min", 80000), budget_max=None, offers_count=None, **kw,
    )  # fmt: skip


def lessons(changes, start=1):
    return tuple(
        Lesson(i, c.kind, c.value, c.weight, 1, "") for i, c in enumerate(changes, start)
    )


def test_topic_terms_skip_profile_generic_and_level_words(settings):
    assert learning.topic_terms("Бизнес-аналитик", settings) == ["бизнес", "аналитик"]
    assert learning.topic_terms("Python-разработчик Telegram-ботов", settings) == []
    assert learning.topic_terms("Инженер-программист", settings) == []
    assert learning.topic_terms("Старший менеджер по работе", settings) == ["менеджер"]
    assert learning.level_terms("Старший менеджер") == ["старш"]
    # основа ловит формы слова: «аналитика» → «аналитик»
    assert learning.topic_terms("Аналитика продаж", settings) == ["аналитик", "продаж"]


def test_topic_lesson_grows_with_repeats_and_has_a_floor(make_project, settings):
    p = vacancy(make_project)
    first = learning.learn("topic", p, settings, ())
    assert [(c.kind, c.value, c.weight) for c in first] == [
        ("topic", "бизнес", -15), ("topic", "аналитик", -15),
    ]  # fmt: skip
    known = lessons(first)
    again = learning.learn("topic", p, settings, known)
    assert {c.weight for c in again} == {-30}
    known = lessons([replace(c, weight=-45) for c in again])
    assert {c.weight for c in learning.learn("topic", p, settings, known)} == {-45}


def test_topic_lesson_lowers_similar_vacancy_below_threshold(make_project, settings):
    similar = vacancy(make_project, title="Системный аналитик", external_id="2")
    before = score_project(similar, settings, NOW)
    known = lessons(learning.learn("topic", vacancy(make_project), settings, ()))
    after = score_project(similar, replace(settings, lessons=known), NOW)
    assert abs(before.score - after.score - 15) <= 1  # балл округляется до штрафа
    assert "🧠 Ваши отметки: «аналитик» −15" in after.reasons
    other = vacancy(make_project, title="Python-разработчик", external_id="3")
    assert score_project(other, replace(settings, lessons=known), NOW).score == score_project(
        other, settings, NOW
    ).score  # другое название — правило не действует


def test_pay_lesson_excludes_lower_known_budget_only_in_its_scope(make_project, settings):
    known = lessons(learning.learn("pay", vacancy(make_project, budget_min=60000), settings, ()))
    assert known[0].weight == 60000
    s = replace(settings, lessons=known)
    same = score_project(vacancy(make_project, budget_min=60000, external_id="2"), s, NOW)
    assert same.passed is False
    assert same.excluded_reason == "мало платят: по вашей отметке — не выше 60 000 ₽"
    higher = vacancy(make_project, budget_min=60001, external_id="4")
    assert score_project(higher, s, NOW).excluded_reason is None
    unknown = vacancy(make_project, budget_min=None, external_id="3")
    assert score_project(unknown, s, NOW).excluded_reason is None
    order = make_project(budget_max=5000, published_at=NOW)  # у заказов бирж свой порог
    assert score_project(order, s, NOW).excluded_reason is None
    assert learning.learn("pay", unknown, settings, ()) == []  # без оплаты запоминать нечего


def test_office_lesson_penalizes_only_vacancies_without_remote(make_project, settings):
    known = lessons(learning.learn("office", vacancy(make_project), settings, ()))
    s = replace(settings, lessons=known)
    office = vacancy(make_project, external_id="2")
    remote = vacancy(make_project, external_id="3", extra={"area": "Москва, удалённо"})
    drop = score_project(office, settings, NOW).score - score_project(office, s, NOW).score
    assert abs(drop - 15) <= 1
    assert score_project(remote, s, NOW).score == score_project(remote, settings, NOW).score
    assert learning.reasons_for(make_project()) == ["topic", "skills", "pay", "level"]


def test_company_lesson_excludes_its_vacancies(make_project, settings):
    known = lessons(learning.learn("company", vacancy(make_project), settings, ()))
    assert known[0].value == "ооо ромашка"
    s = replace(settings, lessons=known)
    r = score_project(vacancy(make_project, title="Python-разработчик", external_id="2"), s, NOW)
    assert r.passed is False and r.excluded_reason == "компания в вашем стоп-листе"


def test_applied_raises_title_words_with_a_ceiling(make_project, settings):
    p = vacancy(make_project, title="DevOps-инженер")
    first = learning.learn("applied", p, settings, ())
    assert [(c.value, c.weight) for c in first] == [("devops", 5)]
    capped = learning.learn("applied", p, settings, lessons([replace(first[0], weight=15)]))
    assert capped[0].weight == 15
    # отклик после «не моя тема» снимает штраф, а не прибавляет к нему
    back = learning.learn("applied", p, settings, lessons([replace(first[0], weight=-20)]))
    assert back[0].weight == 5


def test_describe_and_nothing_to_learn_texts():
    assert learning.describe(learning.Change("topic", "аналитик", -10)) == (
        "«аналитик» в названии: −10 к баллу"
    )
    assert learning.describe(learning.Change("pay", "vacancy", 60000)) == (
        "вакансии с зарплатой 60 000 ₽ и ниже не присылаю"
    )
    assert "другую причину" in learning.nothing_to_learn("topic")


def test_level_learns_experience_from_description_and_excludes_it(make_project, settings):
    # в названии уровня нет, а в описании — стаж: раньше «запоминать нечего»
    hard = "Требования: опыт коммерческой разработки от 3 лет, Python, FastAPI."
    p = vacancy(make_project, title="Python-разработчик", description=hard)
    first = learning.learn("level", p, settings, ())
    assert [(c.kind, c.value, c.weight) for c in first] == [("experience", "", 3)]
    assert learning.describe(first[0]) == "где нужен опыт от 3 лет и больше — не присылаю"
    s = replace(settings, lessons=lessons(first))
    more = vacancy(make_project, title=p.title, external_id="2", description="Опыт от 5 лет.")
    r = score_project(more, s, NOW)
    assert r.passed is False
    assert r.excluded_reason == "нужен опыт от 5 лет: по вашей отметке — не ваш уровень"
    easy = vacancy(make_project, title=p.title, external_id="3", description="Опыт: 1–3 года.")
    assert score_project(easy, s, NOW).excluded_reason is None
    # отметка на «от 2 лет» опускает порог, на «от 5 лет» — не поднимает
    two = vacancy(make_project, external_id="4", description="Опыт от 2 лет.")
    assert learning.learn("level", two, settings, lessons(first))[0].weight == 2
    assert learning.learn("level", more, settings, lessons(first))[0].weight == 3


def test_level_learns_title_words_and_experience_together(make_project, settings):
    p = vacancy(make_project, title="Senior Python developer", description="Опыт от 5 лет.")
    changes = learning.learn("level", p, settings, ())
    assert [(c.kind, c.value) for c in changes] == [("level", "senior"), ("experience", "")]
    plain = vacancy(make_project, title="Python-разработчик", description="Нужен бот.")
    assert learning.learn("level", plain, settings, ()) == []
    assert "стажа" in learning.nothing_to_learn("level")


def test_skill_terms_skip_profile_generic_links_and_level(make_project, settings):
    p = vacancy(
        make_project, title="Python-разработчик (Middle)",
        description="Python, FastAPI, PostgreSQL, Docker, Kubernetes, Kafka, Go, C++, node.js, "
        "1C, Git, REST API, английский B2. Резюме: hr@firm.ru, https://t.me/firm_hr",
    )  # fmt: skip
    assert learning.skill_terms(p, settings) == [
        "kubernetes", "kafka", "go", "c++", "node.js", "1с",
    ]  # fmt: skip
    # «go» — не начало «google» из профиля: короткие слова — только целиком
    go = vacancy(make_project, description="Go, Google Sheets")
    assert learning.skill_terms(go, settings) == ["go"]


def test_skill_lesson_penalizes_text_mentions(make_project, settings):
    p = vacancy(make_project, title="Backend-разработчик", description="Java, Spring, Python.")
    first = learning.learn("skills", p, settings, ())
    assert [(c.kind, c.value, c.weight) for c in first] == [
        ("skill", "java", -10), ("skill", "spring", -10),
    ]  # fmt: skip
    assert learning.describe(first[0]) == "«java» в тексте: −10 к баллу"
    s = replace(settings, lessons=lessons(first))
    similar = vacancy(make_project, external_id="2", description="Нужен Java-разработчик.")
    drop = score_project(similar, settings, NOW).score - score_project(similar, s, NOW).score
    assert abs(drop - 10) <= 1
    assert "🧠 Ваши отметки: «java» −10" in score_project(similar, s, NOW).reasons
    # навык — целым словом: «java» не штрафует JavaScript
    js = vacancy(make_project, external_id="5", description="Нужен JavaScript-разработчик.")
    assert score_project(js, s, NOW).score == score_project(js, settings, NOW).score
    again = learning.learn("skills", p, settings, lessons(first))
    assert {c.weight for c in again} == {-20}
    only_profile = vacancy(make_project, description="Python, FastAPI, Docker.")
    assert learning.learn("skills", only_profile, settings, ()) == []
    assert "другую причину" in learning.nothing_to_learn("skills")
