"""Расписание уведомлений: разбор своего тайминга, описание, ближайшая отправка."""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from freelance_bot.schedule import (
    DEFAULT,
    PRESETS,
    Schedule,
    describe,
    next_send_at,
    parse_schedule,
)

TZ = "Europe/Moscow"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("каждые 30 минут", Schedule(every_min=30)),
        ("Каждые 2 часа с 8 до 23", Schedule(every_min=120, start_hour=8, end_hour=23)),
        ("каждый час", Schedule(every_min=60)),
        ("раз в 3 часа", Schedule(every_min=180)),
        ("каждые 15 мин с 0 до 24", Schedule(every_min=15, start_hour=0, end_hour=24)),
        ("10:00, 14:30 и 19", Schedule(times=("10:00", "14:30", "19:00"))),
        ("в 9.15", Schedule(times=("09:15",))),
        ("18:00 10:00 10:00", Schedule(times=("10:00", "18:00"))),
    ],
)
def test_parse(text, expected):
    assert parse_schedule(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "иногда",
        "каждые 5 минут",  # чаще 15 минут
        "каждые 25 часов",
        "с 9 до 18",  # окно без частоты
        "каждые 30 минут с 22 до 8",  # окно через полночь
        "25:00",
        "10:75",
        "раз в день",
        ", ".join(f"{h}:00" for h in range(13)),  # больше 12 раз
    ],
)
def test_parse_rejects(text):
    assert parse_schedule(text) is None


@pytest.mark.parametrize(
    ("schedule", "text"),
    [
        (DEFAULT, "раз в час, 9:00–21:00"),
        (Schedule(every_min=30, start_hour=8, end_hour=23), "каждые 30 минут, 8:00–23:00"),
        (Schedule(every_min=120), "каждые 2 часа, 9:00–21:00"),
        (Schedule(every_min=300), "каждые 5 часов, 9:00–21:00"),
        (Schedule(times=("10:00",)), "раз в день в 10:00"),
        (Schedule(times=("09:15", "13:00", "18:00")), "в 9:15, 13:00 и 18:00"),
    ],
)
def test_describe(schedule, text):
    assert describe(schedule) == text


def _msk(day: int, hour: int, minute: int = 0) -> datetime:
    """Время МСК, переведённое в UTC."""
    return datetime(2026, 10, day, hour, minute, tzinfo=ZoneInfo(TZ)).astimezone(UTC)


@pytest.mark.parametrize(
    ("schedule", "after", "expected"),
    [
        (DEFAULT, _msk(24, 10, 0), _msk(24, 11, 0)),  # строго позже
        (DEFAULT, _msk(24, 10, 30), _msk(24, 11, 0)),
        (DEFAULT, _msk(24, 21, 0), _msk(25, 9, 0)),  # ночью не пишем — утром
        (DEFAULT, _msk(24, 7, 0), _msk(24, 9, 0)),
        (Schedule(every_min=45, start_hour=9, end_hour=11), _msk(24, 10, 31), _msk(25, 9, 0)),
        (Schedule(times=("10:00", "18:00")), _msk(24, 12, 0), _msk(24, 18, 0)),
        (Schedule(times=("10:00", "18:00")), _msk(24, 18, 0), _msk(25, 10, 0)),
        (Schedule(every_min=15, start_hour=0, end_hour=24), _msk(24, 23, 50), _msk(25, 0, 0)),
    ],
)
def test_next_send_at(schedule, after, expected):
    assert next_send_at(schedule, after, TZ) == expected


def test_roundtrip_and_presets():
    for _, schedule in PRESETS.values():
        assert Schedule.from_dict(schedule.to_dict()) == schedule
    assert Schedule.from_dict(None) == DEFAULT
    assert PRESETS["hourly"][1] == DEFAULT
