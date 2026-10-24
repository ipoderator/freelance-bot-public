"""GPT-адаптер (openai_llm.py): запрос и разбор ответа через httpx.MockTransport, без сети."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from freelance_bot import openai_llm
from freelance_bot.config import load_settings
from freelance_bot.drafting import BuiltinDrafter, LlmDrafter, make_drafter, make_llm_client
from freelance_bot.openai_llm import API_URL, GROQ_API_URL, OpenAIChat, OpenAIError, groq
from freelance_bot.summary import LlmSummarizer, make_summarizer

ROOT = Path(__file__).resolve().parent.parent
KEY = "sk-test-not-a-real-key"


def chat(handler, model="gpt-5-mini") -> OpenAIChat:
    return OpenAIChat(KEY, model, transport=httpx.MockTransport(handler), retry_delay=0)


def reply(content="Готово", finish="stop", refusal=None, status=200) -> httpx.Response:
    message = {"role": "assistant", "content": content, "refusal": refusal}
    return httpx.Response(status, json={"choices": [{"message": message, "finish_reason": finish}]})


async def test_request_shape_and_text_answer():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return reply("Понял задачу.")

    client = chat(handler)
    response = await client.beta.messages.create(  # путь Claude с server-side fallbacks
        model="claude-sonnet-5-5",
        max_tokens=4000,
        system="Правила",
        messages=[{"role": "user", "content": "ТЗ"}],
        betas=["x"],
        fallbacks="default",
    )

    assert response.stop_reason == "end_turn"
    assert [b.text for b in response.content if b.type == "text"] == ["Понял задачу."]
    request = seen[0]
    assert str(request.url) == API_URL
    assert request.headers["authorization"] == f"Bearer {KEY}"
    body = json.loads(request.content)
    assert body["model"] == "gpt-5-mini"  # имя Claude из LLM_MODEL не уходит в OpenAI
    assert body["messages"] == [
        {"role": "system", "content": "Правила"},
        {"role": "user", "content": "ТЗ"},
    ]
    assert body["max_completion_tokens"] == 4000 and body["reasoning_effort"] == "low"
    assert "betas" not in body and "fallbacks" not in body


async def test_non_reasoning_model_gets_no_reasoning_effort():
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return reply()

    await chat(handler, "gpt-4.1").messages.create(max_tokens=10, system="s", messages=[])
    assert "reasoning_effort" not in bodies[0]


@pytest.mark.parametrize(
    ("kwargs", "stop"),
    [
        ({"finish": "length"}, "max_tokens"),
        ({"finish": "content_filter"}, "refusal"),
        ({"content": None, "refusal": "Не могу помочь"}, "refusal"),
    ],
)
async def test_truncation_and_refusal_map_to_claude_stop_reasons(kwargs, stop):
    response = await chat(lambda r: reply(**kwargs)).messages.create(
        max_tokens=10, system="s", messages=[]
    )
    assert response.stop_reason == stop


async def test_api_error_carries_status_and_message_without_retry_on_no_quota():
    calls = []

    def handler(request):
        calls.append(1)
        error = {"message": "You exceeded your current quota", "code": "insufficient_quota"}
        return httpx.Response(429, json={"error": error})

    with pytest.raises(OpenAIError) as info:
        await chat(handler).messages.create(max_tokens=10, system="s", messages=[])
    assert info.value.status_code == 429 and "quota" in info.value.message
    assert len(calls) == 1


async def test_temporary_errors_are_retried():
    answers = [httpx.Response(503, json={"error": {"message": "busy"}}), reply("ok")]

    def handler(request):
        if not answers:
            raise AssertionError("лишний запрос")
        return answers.pop(0)

    response = await chat(handler).messages.create(max_tokens=10, system="s", messages=[])
    assert response.content[0].text == "ok"


def groq_chat(handler, model="openai/gpt-oss-120b") -> OpenAIChat:
    return groq("gsk_test", model, transport=httpx.MockTransport(handler), retry_delay=0)


@pytest.mark.parametrize(
    ("model", "reasoning"),
    [
        ("openai/gpt-oss-120b", {"reasoning_effort": "low", "include_reasoning": False}),
        ("qwen/qwen3.8-27b", {"reasoning_effort": "none"}),
        ("llama-3.3-70b-versatile", {}),
    ],
)
async def test_groq_request_shape(model, reasoning):
    seen = []

    def handler(request):
        seen.append(request)
        return reply("<think>план</think>Понял задачу.")

    response = await groq_chat(handler, model).messages.create(
        max_tokens=16000, system="s", messages=[]
    )
    assert response.content[0].text == "Понял задачу."  # рассуждение в ответ не попадает
    assert str(seen[0].url) == GROQ_API_URL
    assert seen[0].headers["authorization"] == "Bearer gsk_test"
    body = json.loads(seen[0].content)
    assert body["model"] == model
    # бесплатный Groq считает max_completion_tokens в лимит 8K токенов в минуту
    assert body["max_completion_tokens"] == openai_llm.GROQ_MAX_TOKENS
    assert {k: body[k] for k in ("reasoning_effort", "include_reasoning") if k in body} == (
        reasoning
    )


async def test_rate_limit_waits_retry_after_and_gives_up_on_long_wait(monkeypatch):
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(openai_llm.asyncio, "sleep", fake_sleep)
    limited = httpx.Response(429, headers={"retry-after": "7"}, json={"error": {"message": "x"}})
    answers = [limited, reply("ok")]
    response = await groq_chat(lambda r: answers.pop(0)).messages.create(
        max_tokens=10, system="s", messages=[]
    )
    assert response.content[0].text == "ok" and slept == [7.0]

    calls = []

    def daily_limit(request):
        calls.append(1)
        return httpx.Response(
            429, headers={"retry-after": "3600"}, json={"error": {"message": "TPD"}}
        )

    with pytest.raises(OpenAIError, match="TPD"):
        await groq_chat(daily_limit).messages.create(max_tokens=10, system="s", messages=[])
    assert len(calls) == 1 and slept == [7.0]  # час ждать не стали


@pytest.fixture
def settings():
    return load_settings(ROOT / "settings.toml", ROOT / "no-such.env")


def test_openai_key_selects_gpt_even_with_anthropic_key(settings):
    both = replace(settings, openai_api_key=KEY, anthropic_api_key="sk-ant-test")
    assert isinstance(make_llm_client(both), OpenAIChat)
    assert make_llm_client(both).model == settings.openai_model


def test_factories_use_llm_with_only_openai_key(settings):
    gpt_only = replace(settings, openai_api_key=KEY, anthropic_api_key=None)
    drafter = make_drafter_with_profile(gpt_only)
    assert isinstance(drafter, LlmDrafter) and isinstance(drafter._client, OpenAIChat)
    summarizer = make_summarizer(gpt_only, storage=None)
    assert isinstance(summarizer.inner, LlmSummarizer)
    assert isinstance(
        make_drafter_with_profile(replace(gpt_only, openai_api_key=None)), BuiltinDrafter
    )


def make_drafter_with_profile(settings):
    from freelance_bot.profile import load_profile

    return make_drafter(load_profile(ROOT / "profile" / "profile.example.toml"), settings)


def test_groq_key_wins_over_openai_and_anthropic(settings):
    every = replace(settings, groq_api_key="gsk_test", openai_api_key=KEY, anthropic_api_key="x")
    client = make_llm_client(every)
    assert client.api_url == GROQ_API_URL and client.model == "openai/gpt-oss-120b"
    groq_only = replace(settings, groq_api_key="gsk_test", openai_api_key=None)
    assert isinstance(make_drafter_with_profile(groq_only), LlmDrafter)
    assert isinstance(make_summarizer(groq_only, storage=None).inner, LlmSummarizer)
