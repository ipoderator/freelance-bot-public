"""Письмо к вакансии (cover_letter.py): шаблон по резюме, нейросеть с проверкой стажа и
контактов, вакансии Telegram-каналов — только шаблон (ADR 0016). Сети нет."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from freelance_bot.config import load_settings
from freelance_bot.cover_letter import (
    Applicant,
    CoverLetterWriter,
    applicant_from_anketa,
    builtin_letter,
    letter_problems,
)
from freelance_bot.models import Project

ROOT = Path(__file__).resolve().parent.parent
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
ANNA = Applicant(
    name="Анна Петрова", profession="Продавец-консультант", experience_years=4.5,
    facts=["Продавец-консультант — Магазин товаров для дома (2023 — н.в.)",
           "Кассир — Супермаркет (2021 — 2023)"],
    skills=["Продажи", "Работа с кассой"], education=["Колледж сервиса"],
)  # fmt: skip


class FakeMessages:
    def __init__(self, *results) -> None:
        self.results, self.calls = list(results), []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        result = self.results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def client(messages):
    return SimpleNamespace(beta=SimpleNamespace(messages=messages), messages=messages)


def reply(text, stop_reason="end_turn"):
    return SimpleNamespace(stop_reason=stop_reason,
                           content=[SimpleNamespace(type="text", text=text)])  # fmt: skip


def vacancy(platform="superjob", title="Продавец-консультант", description="Обязанности: продажи"):
    return Project(
        platform=platform, external_id="1", url="https://example.org/v/1", title=title,
        description=description, published_at=NOW, budget_min=None, budget_max=None,
        offers_count=None, category="Вакансия", extra={"area": "Пермь", "salary": "40 000 ₽"},
    )  # fmt: skip


@pytest.fixture
def settings():
    return load_settings(ROOT / "settings.toml", ROOT / "no-such.env")


def test_template_from_resume():
    text = builtin_letter(vacancy(), ANNA)
    assert text.startswith("Здравствуйте!") and "«Продавец-консультант» (Пермь)" in text
    assert "опыт — 4 года" in text  # вниз, а не «5 лет»
    assert "Кассир — Супермаркет" in text and "Навыки: Продажи, Работа с кассой." in text
    assert text.endswith("\nАнна Петрова")
    assert builtin_letter(vacancy(), ANNA, 1) != text  # «Другое письмо» — другой текст


def test_applicant_from_anketa_has_no_contacts():
    data = {"full_name": "Петрова Анна Сергеевна",
            "profile": {"profession": "Повар", "experience_years": 2, "jobs": ["Повар — Кафе"],
                        "skills": ["Выпечка"], "keywords": ["выпечка", "заготовки"]}}  # fmt: skip
    a = applicant_from_anketa(data)
    assert a is not None and a.name == "Петрова Анна Сергеевна" and a.facts == ["Повар — Кафе"]
    assert a.skills == ["Выпечка", "выпечка", "заготовки"]
    assert applicant_from_anketa({"full_name": "Кто-то"}) is None


def test_problems_years_and_contacts():
    assert letter_problems("Опыт — 4 года, зарплата 80000 ₽ устроит.", ANNA) == []
    assert letter_problems("У меня 6 лет опыта.", ANNA)
    assert letter_problems("Звоните +7 912 345-67-89", ANNA)
    assert letter_problems("Пишите @anna_shop", ANNA)
    assert letter_problems("Более пяти лет в рознице.", ANNA)  # 5 > 4,5 — словами тоже
    assert letter_problems("Мой опыт — четыре года.", ANNA) == []
    assert letter_problems("Работаю с августа 2021 года, 3 года в рознице.", ANNA) == []
    for text in (builtin_letter(vacancy(), ANNA, n) for n in range(3)):
        assert letter_problems(text, ANNA) == []  # шаблон сам проходит проверку


async def test_llm_letter_uses_resume_and_vacancy(settings):
    messages = FakeMessages(reply("Здравствуйте!\n\nХочу к вам продавцом.\n\nАнна Петрова"))
    letter = await CoverLetterWriter(settings, client(messages)).write(vacancy(), ANNA)
    assert letter.generator == "llm" and letter.text.endswith("продавцом.\n\nАнна Петрова")
    prompt = messages.calls[0]["messages"][0]["content"]
    assert "по имени Анна Петрова" in messages.calls[0]["system"]  # род — по имени
    assert "<vacancy>" in prompt and "Должность: Продавец-консультант" in prompt
    assert "Кассир — Супермаркет" in prompt and "Стаж: 4 года" in prompt
    assert "не больше, чем в резюме (4 года)" in messages.calls[0]["system"]


async def test_llm_letter_is_fixed_then_falls_back_to_template(settings):
    messages = FakeMessages(reply("У меня 10 лет опыта."), reply("Всё ещё 10 лет опыта."))
    letter = await CoverLetterWriter(settings, client(messages)).write(vacancy(), ANNA)
    assert letter.generator == "builtin" and len(messages.calls) == 2
    assert "Проверка отклонила" in messages.calls[1]["messages"][-1]["content"]
    failing = FakeMessages(RuntimeError("API недоступен"))
    letter = await CoverLetterWriter(settings, client(failing)).write(vacancy(), ANNA)
    assert letter.generator == "builtin"


async def test_another_letter_must_differ(settings):
    same = "Здравствуйте!\n\nМой опыт — кассир в супермаркете и продавец в магазине для дома."
    messages = FakeMessages(reply(same), reply("Здравствуйте!\n\nДругой текст о продажах."))
    letter = await CoverLetterWriter(settings, client(messages)).write(
        vacancy(), ANNA, 1, f"{same}\n\nАнна Петрова"
    )
    assert letter.text.endswith("Другой текст о продажах.\n\nАнна Петрова")
    assert len(messages.calls) == 2
    first = messages.calls[0]["messages"][0]["content"]
    assert "<previous_letter>" in first and "Анна" not in first  # подпись не дублируется
    assert "слишком похоже" in messages.calls[1]["messages"][-1]["content"]


async def test_telegram_channel_vacancy_never_goes_to_llm(settings):
    """Условия Telegram: собранное из каналов нельзя отдавать нейросети."""
    messages = FakeMessages()
    letter = await CoverLetterWriter(settings, client(messages)).write(
        vacancy("tgchannels"), ANNA
    )
    assert letter.generator == "builtin" and messages.calls == []


async def test_without_key_template(settings):
    letter = await CoverLetterWriter(settings).write(vacancy(), ANNA)
    assert letter.generator == "builtin"


SHOP = vacancy(
    title="Продавец-кассир",
    description="Обязанности: выкладка товара, контроль сроков годности, работа на кассе, "
    "приёмка товара по накладным.",
)


def test_vacancy_duties_not_in_resume_are_flagged():
    """Проверка 2026-10-25: второе письмо приписало продавцу «контролировал сроки годности» —
    это требование вакансии, в резюме его нет."""
    invented = (
        "Здравствуйте!\n\nОткликаюсь на вакансию «Продавец-кассир».\n\n"
        "В магазине товаров для дома я контролировал сроки годности и работал с кассой.\n\n"
        "Готов обсудить условия."
    )
    [problem] = letter_problems(invented, ANNA, f"{SHOP.title}\n{SHOP.description}")
    assert "«сроки»" in problem and "«годности»" in problem
    assert "«кассой»" not in problem  # касса есть в резюме
    honest = invented.replace("контролировал сроки годности и ", "")
    assert letter_problems(honest, ANNA, SHOP.description) == []
    # название вакансии в первом абзаце и в кавычках — не утверждение об опыте
    assert letter_problems("Здравствуйте!\n\nХочу на вакансию «Приёмщик накладных».",
                           ANNA, SHOP.description) == []  # fmt: skip


async def test_llm_letter_with_invented_duty_is_fixed(settings):
    invented = ("Здравствуйте!\n\nОткликаюсь на вакансию.\n\nЯ контролировал сроки годности "
                "товара.")  # fmt: skip
    fixed = "Здравствуйте!\n\nОткликаюсь на вакансию.\n\nРаботал с кассой и продажами."
    messages = FakeMessages(reply(invented), reply(fixed))
    letter = await CoverLetterWriter(settings, client(messages)).write(SHOP, ANNA)
    assert letter.generator == "llm" and "годности" not in letter.text
    assert "в резюме этого нет" in messages.calls[1]["messages"][-1]["content"]


def test_resume_lines_go_to_prompt_and_check():
    data = {"full_name": "Анна Петрова", "profile": {
        "profession": "Продавец", "experience_years": 2,
        "facts": [{"text": "следила за сроками годности", "source": "Контроль сроков годности"}],
    }}  # fmt: skip
    anna = applicant_from_anketa(data)
    assert anna is not None and anna.sources == ["Контроль сроков годности"]
    claim = "Здравствуйте!\n\nОткликаюсь.\n\nЯ контролировала сроки годности."
    assert letter_problems(claim, anna, SHOP.description) == []  # в резюме есть
