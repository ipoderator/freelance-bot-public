"""Прогресс подборки: сообщение «⏳ … ▰▰▰▱▱▱▱▱▱▱ 30%», которое правится на месте и удаляется,
когда список готов, — видно, что бот работает, и незачем нажимать кнопку ещё раз.

Быстрый список (из базы, суть в кэше) приходит без него: сообщение появляется, только если
подборка идёт дольше SHOW_AFTER_SEC. Повторное нажатие, пока подборка идёт, новую не
запускает — `Tracker.running` отдаёт идущую, и человеку отвечают, сколько готово.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

log = logging.getLogger(__name__)

BAR_CELLS = 10
SHOW_AFTER_SEC = 1.5  # быстрее — список придёт раньше, чем прогресс успеют прочитать
MIN_EDIT_SEC = 1.0  # Telegram ограничивает частоту правок сообщений одного чата
BUSY_TEXT = "⏳ Уже ищу — готово {percent}%. Список придёт сюда, нажимать ещё раз не нужно."

Send = Callable[[str], Awaitable[Any]]
Step = Callable[[], Awaitable[None]]


def bar(percent: int) -> str:
    filled = percent * BAR_CELLS // 100  # полная полоса — только на 100%
    return f"{'▰' * filled}{'▱' * (BAR_CELLS - filled)} {percent}%"


class Progress:
    """Одно сообщение прогресса. `send(text)` присылает его и возвращает сообщение Telegram
    (с edit_text и delete). Сбой Telegram прогресс молча пропускает: он — удобство, список
    важнее."""

    def __init__(
        self,
        title: str,
        send: Send,
        *,
        show_after: float = SHOW_AFTER_SEC,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.title = title
        self.percent = 0
        self.stage = ""
        self._send = send
        self._show_after = show_after
        self._sleep = sleep
        self._clock = clock
        self._message: Any = None
        self._shown = ""
        self._edited_at = 0.0
        self._sending = False
        self._task: asyncio.Task[None] | None = None

    def text(self) -> str:
        lines = [f"⏳ {self.title}", bar(self.percent)]  # подписи — константы бота
        if self.stage:
            lines.append(self.stage)
        return "\n".join(lines)

    def start(self) -> None:
        self._task = asyncio.create_task(self._show_later())

    async def _show_later(self) -> None:
        await self._sleep(self._show_after)
        self._sending = True
        self._shown = self.text()
        try:
            self._message = await self._send(self._shown)
        except Exception as exc:  # без прогресса список всё равно придёт
            log.warning("прогресс не показан: %s", type(exc).__name__)
        self._edited_at = self._clock()

    async def update(self, percent: float, stage: str | None = None) -> None:
        """Проценты только растут; правка — не чаще MIN_EDIT_SEC."""
        self.percent = max(self.percent, min(100, int(percent)))
        if stage is not None:
            self.stage = stage
        text = self.text()
        if self._message is None or text == self._shown:
            return
        if self._clock() - self._edited_at < MIN_EDIT_SEC:
            return
        self._shown, self._edited_at = text, self._clock()
        with contextlib.suppress(Exception):
            await self._message.edit_text(text)

    def steps(self, start: float, end: float, total: int) -> Step:
        """Шаг для `total` одинаковых частей работы: проценты от `start` до `end`."""
        done = 0

        async def step() -> None:
            nonlocal done
            done += 1
            await self.update(start + (end - start) * done / max(total, 1))

        return step

    async def close(self) -> None:
        """Подборка закончена: сообщение прогресса убрать (уже отправляется — дождаться)."""
        task = self._task
        if task is not None and not task.done():
            if self._sending:
                with contextlib.suppress(Exception):
                    await task
            else:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if self._message is not None:
            with contextlib.suppress(Exception):
                await self._message.delete()


class Tracker:
    """Чьи подборки сейчас идут: по одной на чат."""

    def __init__(self, **progress_options: Any) -> None:
        self._active: dict[int, Progress] = {}
        self._options = progress_options

    def running(self, chat_id: int) -> Progress | None:
        return self._active.get(chat_id)

    def busy_text(self, chat_id: int) -> str | None:
        """Идёт подборка — ответ на повторное нажатие; нет — None."""
        progress = self.running(chat_id)
        return BUSY_TEXT.format(percent=progress.percent) if progress else None

    @contextlib.asynccontextmanager
    async def run(self, chat_id: int, title: str, send: Send) -> AsyncIterator[Progress]:
        progress = Progress(title, send, **self._options)
        self._active[chat_id] = progress
        progress.start()
        try:
            yield progress
        finally:
            self._active.pop(chat_id, None)
            await progress.close()
