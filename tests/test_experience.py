"""Требуемый стаж в тексте (experience.py): общий разбор для скоринга и самообучения."""

from __future__ import annotations

import pytest

from freelance_bot.experience import required_years, years_word


@pytest.mark.parametrize(
    ("text", "years"),
    [
        ("Опыт коммерческой разработки от 3 лет", 3),
        ("Опыт промышленной разработки от 3 лет", 3),
        ("Требования: опыт коммерческой разработки на Python — от 3-х лет;", 3),
        ("Опыт работы от 3 лет", 3),
        ("Опыт работы От 3 до 6 лет", 3),  # так SuperJob пишет поле experience
        ("Стаж работы не менее 5 лет", 5),
        ("от 3 лет коммерческой разработки", 3),
        ("3+ года опыта", 3),
        ("3+ years of commercial experience", 3),
        ("Experience: 5+ years", 5),
        # у вилки требование — нижняя граница
        ("Опыт работы: 1–3 года", 1),
        ("1-3 года опыта", 1),
        ("Опыт от 1 года приветствуется", 1),
    ],
)
def test_required_years_found(text, years):
    assert required_years(text) == years


@pytest.mark.parametrize(
    "text",
    [
        "Опыт не обязателен, гарантия 3 года",
        "Компания на рынке более 10 лет",
        "Нужен бот за 3 дня",
        "В отклике укажите опыт с aiogram, сроки 2 недели",
        "Опыт работы Без опыта",
    ],
)
def test_no_requirement(text):
    assert required_years(text) is None


def test_years_word():
    assert [years_word(n) for n in (1, 2, 3, 5, 11, 21)] == [
        "года", "лет", "лет", "лет", "лет", "года",
    ]  # fmt: skip
