"""Сжатая суть заказа: встроенная, через Claude (фейковый клиент), кэш. Сети нет."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from freelance_bot.config import load_settings
from freelance_bot.drafting import LlmUnavailable, llm_error
from freelance_bot.storage import Storage
from freelance_bot.summary import (
    SUMMARY_MAX_CHARS,
    BuiltinSummarizer,
    CachedSummarizer,
    LlmSummarizer,
    builtin_summary,
    make_summarizer,
)

ROOT = Path(__file__).resolve().parent.parent


class FakeMessages:
    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def fake_client(result):
    messages = FakeMessages(result)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages), messages=messages)


def reply(text, stop_reason="end_turn"):
    return SimpleNamespace(
        stop_reason=stop_reason, content=[SimpleNamespace(type="text", text=text)]
    )


@pytest.fixture
def settings():
    s = load_settings(ROOT / "settings.toml", ROOT / "no-such.env")
    return replace(s, anthropic_api_key="test-key-not-real", llm_model="claude-sonnet-5-5")


def test_builtin_summary_takes_whole_sentences_within_limit(make_project):
    long = "Нужен бот для записи клиентов. " + "Подробности " * 40
    assert builtin_summary(make_project(description=long)) == "Нужен бот для записи клиентов."
    one = "слово " * 100
    out = builtin_summary(make_project(description=one))
    assert out.endswith("…") and len(out) <= SUMMARY_MAX_CHARS
    assert builtin_summary(make_project(description="  \n ")) == ""
    assert builtin_summary(make_project(description="Коротко.\n\nЕщё  строка")) == (
        "Коротко. Ещё строка"
    )


async def test_llm_summary_uses_model_low_effort_and_fallbacks(settings, make_project):
    client = fake_client(reply("  «Telegram-бот на aiogram с оплатой через ЮKassa.»  "))
    summarizer = LlmSummarizer(settings, client=client)

    out = await summarizer.summarize(make_project(description="Длинное ТЗ про бота."))

    assert out == "Telegram-бот на aiogram с оплатой через ЮKassa."
    call = client.messages.calls[0]
    assert call["model"] == "claude-sonnet-5-5"
    assert call["output_config"] == {"effort": "low"}
    assert call["fallbacks"] == "default" and call["betas"]
    assert "<brief>" in call["messages"][0]["content"]
    assert "budget_tokens" not in str(call) and "thinking" not in call


async def test_llm_summary_without_server_fallbacks_for_other_models(settings, make_project):
    client = fake_client(reply("Суть."))
    summarizer = LlmSummarizer(replace(settings, llm_model="claude-haiku-4-5"), client=client)
    assert await summarizer.summarize(make_project()) == "Суть."
    assert "fallbacks" not in client.messages.calls[0]


@pytest.mark.parametrize(
    "result",
    [RuntimeError("сеть"), reply("x", stop_reason="refusal"), reply("", stop_reason="end_turn"),
     reply("обрыв", stop_reason="max_tokens")],
)
async def test_llm_failure_falls_back_to_description_without_caching(
    settings, make_project, result
):
    p = make_project(description="Нужен парсер Avito. Детали в личке.")
    assert await LlmSummarizer(settings, client=fake_client(result)).summarize(p) is None

    storage = Storage(":memory:")
    cached = CachedSummarizer(LlmSummarizer(settings, client=fake_client(result)), storage)
    assert await cached.summarize(p) == "Нужен парсер Avito. Детали в личке."
    assert storage.get_flag("summary:fl:1") is None  # после сбоя спросим Claude снова

    cached.inner = LlmSummarizer(settings, client=fake_client(reply("Парсер Avito.")))
    assert await cached.summarize(p) == "Парсер Avito."
    assert storage.get_flag("summary:fl:1") == "Парсер Avito."
    storage.close()


async def test_llm_summary_is_clipped_and_skips_empty_description(settings, make_project):
    client = fake_client(reply("очень " * 100))
    summarizer = LlmSummarizer(settings, client=client)
    out = await summarizer.summarize(make_project(description="ТЗ"))
    assert len(out) <= SUMMARY_MAX_CHARS and out.endswith("…")
    assert await summarizer.summarize(make_project(description=" ")) == ""
    assert len(client.messages.calls) == 1  # на пустое описание API не зовём


async def test_cache_calls_inner_once_per_project(make_project):
    class Counting(BuiltinSummarizer):
        calls = 0

        async def summarize(self, p):
            Counting.calls += 1
            return await super().summarize(p)

    storage = Storage(":memory:")
    cached = CachedSummarizer(Counting(), storage)
    a = make_project(external_id="1", description="Бот.")
    b = make_project(external_id="2", description="")
    assert await cached.summarize_many([a, b, a]) == ["Бот.", "", "Бот."]
    assert await cached.summarize(a) == "Бот."
    assert storage.get_flag("summary:fl:1") == "Бот."
    assert storage.get_flag("summary:fl:2") is None  # пустую суть не кэшируем
    assert Counting.calls == 2  # по одному разу на проект: повтор «a» взят из кэша
    storage.close()


def test_make_summarizer_picks_llm_only_with_key(settings):
    storage = Storage(":memory:")
    assert isinstance(make_summarizer(settings, storage).inner, LlmSummarizer)
    plain = make_summarizer(replace(settings, anthropic_api_key=None), storage)
    assert isinstance(plain.inner, BuiltinSummarizer)
    with pytest.raises(LlmUnavailable):
        LlmSummarizer(replace(settings, anthropic_api_key=None))
    storage.close()


def test_llm_error_shows_api_message_but_not_network_text():
    class ApiError(Exception):
        status_code = 400
        message = "Your credit balance is too low"

    assert llm_error(ApiError()) == "ApiError: Your credit balance is too low"
    assert llm_error(ConnectionError("https://api/?key=secret")) == "ConnectionError"


async def test_llm_summary_sees_section_and_truncation(settings, make_project):
    client = fake_client(reply("Бот записи клиентов."))
    p = make_project(category="Мессенджеры / Telegram", description="Нужен бот записи...")
    await LlmSummarizer(settings, client).summarize(p)
    call = client.messages.calls[0]
    brief = call["messages"][0]["content"]
    assert "Раздел: Мессенджеры / Telegram" in brief and "Описание обрезано" in brief
    assert "не додумывай" in call["system"]


async def test_vacancy_summary_is_beginning_of_description_without_llm(settings, make_project):
    storage = Storage(":memory:")
    llm = LlmSummarizer(settings, client=fake_client(reply("не должно понадобиться")))
    cached = CachedSummarizer(llm, storage)
    v = make_project(platform="hh", external_id="t1", description="Обязанности: backend. Ещё.")

    assert await cached.summarize(v) == "Обязанности: backend. Ещё."
    assert llm._client.messages.calls == [] and storage.get_flag("summary:hh:t1") is None
    storage.close()
