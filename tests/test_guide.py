"""Справка и описание бота: лимиты Telegram и подстановки в разделах."""

from __future__ import annotations

import pytest

from freelance_bot import guide
from freelance_bot.bot import COMMANDS, PUBLIC_COMMANDS


def test_descriptions_fit_telegram_limits():
    # Telegram считает в UTF-16: эмодзи — две единицы
    for text, limit in ((guide.description(False), 512), (guide.description(True), 512),
                        (guide.SHORT_DESCRIPTION, 120)):  # fmt: skip
        assert len(text.encode("utf-16-le")) // 2 <= limit
    assert "<" not in guide.DESCRIPTION  # описание — простой текст, без HTML


@pytest.mark.parametrize("public", [False, True])
def test_every_topic_renders_without_placeholders(public):
    commands = PUBLIC_COMMANDS if public else COMMANDS
    for key, label, _ in guide._topics(public):
        text = guide.topic_text(public, key, commands, "в 10:00 и 18:00")
        assert text and "{" not in text, key
        assert len(text) < 4000 and len(label) <= 24
        rows = guide.keyboard(public, key)
        assert all(len(data.encode()) <= 64 for row in rows for _, data in row)
    assert guide.topic_text(public, "нет-такого", commands) is None
