"""Расписание уведомлений пользователя публичного бота (анкета, ADR 0011): когда присылать
новые заказы и вакансии. По умолчанию — раз в час с 9 до 21 (решение пользователя
2026-10-24: «Раз в час (по умолчанию) — с возможностью замены на свой тайминг»).

Два вида: интервал внутри окна дня («каждые 30 минут с 8 до 23») или время по часам
(«10:00, 18:00»). Время — по часовому поясу настроек (general.timezone). Рассылка по
расписанию — этап 2; здесь — разбор, описание и ближайшее время отправки.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

MIN_INTERVAL_MIN = 15  # чаще — уже поток, а не подборка
MAX_INTERVAL_MIN = 24 * 60
MAX_TIMES = 12
DEFAULT_START_HOUR = 9
DEFAULT_END_HOUR = 21

_WINDOW_RE = re.compile(r"\bс\s*(\d{1,2})(?:[:.]00)?\s*(?:ч\w*\s*)?до\s*(\d{1,2})(?:[:.]00)?")
_INTERVAL_RE = re.compile(
    r"(?:кажд\w*|раз\s+в)\s*(\d{1,4})?\s*(мин\w*|час\w*|ч\b)", re.IGNORECASE
)
_TIME_RE = re.compile(r"^(\d{1,2})(?:[:.](\d{2}))?$")


@dataclass(frozen=True)
class Schedule:
    every_min: int | None = None  # интервал; None — время по часам (times)
    start_hour: int = DEFAULT_START_HOUR  # окно интервала: первая отправка в start_hour:00,
    end_hour: int = DEFAULT_END_HOUR  # последняя — не позже end_hour:00
    times: tuple[str, ...] = ()  # «10:00», «18:30» — по возрастанию

    def to_dict(self) -> dict[str, Any]:
        if self.every_min is not None:
            return {"every_min": self.every_min, "start_hour": self.start_hour,
                    "end_hour": self.end_hour}  # fmt: skip
        return {"times": list(self.times)}

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> Schedule:
        if not raw:
            return DEFAULT
        if raw.get("every_min") is not None:
            return cls(
                every_min=int(raw["every_min"]),
                start_hour=int(raw.get("start_hour", DEFAULT_START_HOUR)),
                end_hour=int(raw.get("end_hour", DEFAULT_END_HOUR)),
            )
        return cls(times=tuple(raw.get("times") or ()))


DEFAULT = Schedule(every_min=60)
PRESETS: dict[str, tuple[str, Schedule]] = {
    "hourly": ("Раз в час (9–21)", DEFAULT),
    "twice": ("2 раза в день", Schedule(times=("10:00", "18:00"))),
    "daily": ("Раз в день", Schedule(times=("10:00",))),
}


def parse_schedule(text: str) -> Schedule | None:
    """«каждые 30 минут», «раз в 2 часа с 8 до 23», «каждый час», «10:00, 14:30 и 19» →
    расписание; непонятное или вне пределов — None."""
    raw = " ".join(text.lower().replace("ё", "е").split())
    start, end = DEFAULT_START_HOUR, DEFAULT_END_HOUR
    if window := _WINDOW_RE.search(raw):
        start, end = int(window.group(1)), int(window.group(2))
        if not 0 <= start < end <= 24:
            return None
        raw = (raw[: window.start()] + raw[window.end() :]).strip()
    if interval := _INTERVAL_RE.search(raw):
        n = int(interval.group(1) or 1)
        minutes = n * 60 if interval.group(2).startswith(("час", "ч")) else n
        if not MIN_INTERVAL_MIN <= minutes <= MAX_INTERVAL_MIN:
            return None
        return Schedule(every_min=minutes, start_hour=start, end_hour=end)
    if window:  # окно без интервала («с 9 до 18») — непонятно, как часто
        return None
    times = []
    for token in re.sub(r"\b(?:в|и)\b|[,;]", " ", raw).split():
        m = _TIME_RE.match(token)
        if m is None:
            return None
        hour, minute = int(m.group(1)), int(m.group(2) or 0)
        if hour > 23 or minute > 59:
            return None
        times.append(f"{hour:02d}:{minute:02d}")
    times = sorted(set(times))
    if not 1 <= len(times) <= MAX_TIMES:
        return None
    return Schedule(times=tuple(times))


def _clock(hhmm: str) -> str:
    hour, minute = hhmm.split(":")
    return f"{int(hour)}:{minute}"


def describe(s: Schedule) -> str:
    """«раз в час, 9:00–21:00», «каждые 30 минут, 8:00–23:00», «в 10:00 и 18:00»."""
    if s.every_min is not None:
        if s.every_min == 60:
            how = "раз в час"
        elif s.every_min % 60 == 0:
            hours = s.every_min // 60
            how = f"каждые {hours} {_plural(hours, 'час', 'часа', 'часов')}"
        else:
            how = f"каждые {s.every_min} {_plural(s.every_min, 'минуту', 'минуты', 'минут')}"
        return f"{how}, {s.start_hour}:00–{s.end_hour}:00"
    shown = [_clock(t) for t in s.times]
    if len(shown) == 1:
        return f"раз в день в {shown[0]}"
    return "в " + ", ".join(shown[:-1]) + f" и {shown[-1]}"


def _plural(n: int, one: str, few: str, many: str) -> str:
    if 11 <= n % 100 <= 14:
        return many
    return {1: one, 2: few, 3: few, 4: few}.get(n % 10, many)


def slots(s: Schedule) -> list[time]:
    """Время отправок в течение одного дня (местное)."""
    if s.every_min is None:
        return [time(int(t[:2]), int(t[3:])) for t in s.times]
    result, minute = [], s.start_hour * 60
    while minute <= min(s.end_hour * 60, 24 * 60 - 1):
        result.append(time(minute // 60, minute % 60))
        minute += s.every_min
    return result


def next_send_at(s: Schedule, after: datetime, tz: str) -> datetime:
    """Ближайшее время отправки строго позже `after` (tz-aware); возвращается в том же поясе,
    что и `after`."""
    zone = ZoneInfo(tz)
    local = after.astimezone(zone)
    for days in range(0, 3):
        day = (local + timedelta(days=days)).date()
        for t in slots(s):
            candidate = datetime.combine(day, t, tzinfo=zone)
            if candidate > local:
                return candidate.astimezone(after.tzinfo)
    raise ValueError("в расписании нет ни одной отправки")  # pragma: no cover - parse не даст
