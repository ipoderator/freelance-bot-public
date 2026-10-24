"""GPT через OpenAI Chat Completions — в форме клиента Anthropic, которую ждут LlmDrafter
и LlmSummarizer: `messages.create(max_tokens, system, messages)` → `content[].text` и
`stop_reason` («max_tokens» — ответ обрезан, «refusal» — отказ).

Тот же клиент ходит в Groq: его API повторяет OpenAI по адресу GROQ_API_URL (`groq()`).

Без SDK: один POST через httpx, он уже в зависимостях. Модель — OPENAI_MODEL или GROQ_MODEL
из .env, аргумент `model` (имя Claude из LLM_MODEL) и параметры Claude (output_config,
betas, fallbacks) игнорируются.
"""

from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from typing import Any

import httpx

API_URL = "https://api.openai.com/v1/chat/completions"
GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
TIMEOUT_SEC = 120.0
RETRIES = 2  # как max_retries у клиента Anthropic
RETRY_STATUSES = frozenset({408, 409, 429, 500, 502, 503, 504})
# Рассуждающие модели: max_completion_tokens включает рассуждение; temperature не принимают.
REASONING_PREFIXES = ("gpt-5", "o1", "o3", "o4")
REASONING_EFFORT = "low"  # отклик и суть — короткий текст, долгое рассуждение не нужно
# Бесплатный Groq: 8K токенов в минуту на gpt-oss, и max_completion_tokens входит в этот
# счёт — запрос с max_tokens 16000 отклонён бы целиком. Отклику и сути хватает 4096.
GROQ_MAX_TOKENS = 4096
RETRY_AFTER_MAX_SEC = 30.0  # retry-after дольше (кончился дневной лимит) — не ждём
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)  # Qwen и др. в формате raw


class OpenAIError(Exception):
    """Ответ API с ошибкой: status_code и message — как у ошибок SDK Anthropic (llm_error)."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(f"{status_code}: {message}")
        self.status_code = status_code
        self.message = message


class OpenAIChat:
    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        timeout: float = TIMEOUT_SEC,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_delay: float = 1.0,
        api_url: str = API_URL,
        max_tokens_cap: int | None = None,
        extra_body: dict[str, Any] | None = None,
    ) -> None:
        self.model = model
        self.api_url = api_url
        self._max_tokens_cap = max_tokens_cap
        self._extra_body = extra_body or {}
        self._http = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout, transport=transport
        )
        self._retry_delay = retry_delay
        self.messages = self
        self.beta = SimpleNamespace(messages=self)

    async def create(
        self, *, max_tokens: int, system: str, messages: list[dict[str, str]], **_claude: Any
    ) -> SimpleNamespace:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, *messages],
            "max_completion_tokens": min(max_tokens, self._max_tokens_cap or max_tokens),
            **self._extra_body,
        }
        if self.api_url == API_URL and self.model.startswith(REASONING_PREFIXES):
            body["reasoning_effort"] = REASONING_EFFORT
        data = await self._post(body)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        finish = choice.get("finish_reason")
        if message.get("refusal") or finish == "content_filter":
            stop = "refusal"
        elif finish == "length":
            stop = "max_tokens"
        else:
            stop = "end_turn"
        content = _THINK_RE.sub("", message.get("content") or "")
        text = SimpleNamespace(type="text", text=content)
        return SimpleNamespace(stop_reason=stop, content=[text])

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        for attempt in range(RETRIES + 1):
            last = attempt == RETRIES
            try:
                response = await self._http.post(self.api_url, json=body)
            except httpx.TransportError:
                if last:
                    raise
                delay = self._retry_delay * (attempt + 1)
            else:
                if response.status_code < 400:
                    return response.json()
                delay = _retry_after(response) or self._retry_delay * (attempt + 1)
                if (
                    last
                    or response.status_code not in RETRY_STATUSES
                    or _no_quota(response)
                    or delay > RETRY_AFTER_MAX_SEC
                ):
                    raise OpenAIError(response.status_code, _error_message(response))
            await asyncio.sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover


def _error_message(response: httpx.Response) -> str:
    try:
        error = response.json().get("error") or {}
    except ValueError:
        return response.reason_phrase
    return str(error.get("message") or error.get("code") or response.reason_phrase)


def _no_quota(response: httpx.Response) -> bool:
    """429 из-за баланса — повтор не поможет (в отличие от 429 из-за частоты запросов)."""
    try:
        return (response.json().get("error") or {}).get("code") == "insufficient_quota"
    except ValueError:
        return False


def _retry_after(response: httpx.Response) -> float | None:
    """Сколько секунд просит подождать 429 (Groq шлёт retry-after); нет или мусор — None."""
    try:
        return max(float(response.headers["retry-after"]), 0.0)
    except (KeyError, ValueError):
        return None


def groq(api_key: str, model: str, *, timeout: float = TIMEOUT_SEC, **kwargs: Any) -> OpenAIChat:
    """Клиент Groq: свой адрес, потолок max_tokens и параметры рассуждения под модель."""
    if model.startswith("openai/gpt-oss"):
        # рассуждение коротко и без поля reasoning в ответе — нужен только текст
        extra: dict[str, Any] = {"reasoning_effort": "low", "include_reasoning": False}
    elif model.startswith("qwen/"):
        extra = {"reasoning_effort": "none"}
    else:
        extra = {}  # Llama и др. не рассуждают — лишний параметр Groq отклонил бы
    return OpenAIChat(
        api_key, model, timeout=timeout, api_url=GROQ_API_URL,
        max_tokens_cap=GROQ_MAX_TOKENS, extra_body=extra, **kwargs,
    )  # fmt: skip
