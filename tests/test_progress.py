from __future__ import annotations

import asyncio

from freelance_bot.progress import BUSY_TEXT, Progress, Tracker, bar


class Sent:
    """Сообщение прогресса в чате: правки и удаление."""

    def __init__(self, text: str) -> None:
        self.texts = [text]
        self.deleted = False

    async def edit_text(self, text: str) -> None:
        self.texts.append(text)

    async def delete(self) -> None:
        self.deleted = True


class Chat:
    def __init__(self) -> None:
        self.sent: list[Sent] = []

    async def send(self, text: str) -> Sent:
        self.sent.append(Sent(text))
        return self.sent[-1]


async def no_wait(_: float) -> None:
    """Показать прогресс сразу, без настоящего ожидания."""


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_bar_fills_by_tens() -> None:
    assert bar(0) == "▱▱▱▱▱▱▱▱▱▱ 0%"
    assert bar(30) == "▰▰▰▱▱▱▱▱▱▱ 30%"
    assert bar(100) == "▰▰▰▰▰▰▰▰▰▰ 100%"


async def test_quick_list_comes_without_progress_message() -> None:
    chat = Chat()
    async with Tracker(show_after=60).run(1, "Подбираю…", chat.send) as progress:
        await progress.update(50, "Отбираю")
    assert chat.sent == []


async def test_slow_list_shows_progress_edits_it_and_removes_it() -> None:
    chat, clock = Chat(), Clock()
    tracker = Tracker(sleep=no_wait, clock=clock)
    async with tracker.run(1, "Подбираю…", chat.send) as progress:
        await asyncio.sleep(0)  # прогресс показан
        assert chat.sent[0].texts == ["⏳ Подбираю…\n▱▱▱▱▱▱▱▱▱▱ 0%"]
        clock.now = 5
        await progress.update(40, "Ищу вакансии")
        await progress.update(45)  # меньше секунды после правки — не правится
        clock.now = 7
        await progress.update(20)  # назад не идёт
        assert tracker.busy_text(1) == BUSY_TEXT.format(percent=45)
    message = chat.sent[0]
    assert message.texts[1:] == [
        "⏳ Подбираю…\n▰▰▰▰▱▱▱▱▱▱ 40%\nИщу вакансии",
        "⏳ Подбираю…\n▰▰▰▰▱▱▱▱▱▱ 45%\nИщу вакансии",
    ]
    assert message.deleted and tracker.busy_text(1) is None


async def test_steps_spread_parts_over_range() -> None:
    chat, clock = Chat(), Clock()
    progress = Progress("…", chat.send, sleep=no_wait, clock=clock)
    step = progress.steps(30, 90, 3)
    for _ in range(3):
        await step()
    assert progress.percent == 90


async def test_progress_failure_does_not_break_list() -> None:
    async def broken(text: str) -> None:
        raise RuntimeError("Telegram недоступен")

    async with Tracker(sleep=no_wait).run(1, "…", broken) as progress:
        await asyncio.sleep(0)
        await progress.update(50)
    # список дошёл бы: исключение прогресса наружу не вышло


async def test_finished_before_show_never_sends() -> None:
    chat = Chat()
    gate = asyncio.Event()

    async def sleep(_: float) -> None:
        await gate.wait()

    async with Tracker(sleep=sleep).run(1, "…", chat.send):
        pass
    gate.set()
    await asyncio.sleep(0)
    assert chat.sent == []
