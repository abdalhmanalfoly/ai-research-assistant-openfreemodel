"""Answer generation with OpenRouter's Chat Completions API (free model: nvidia/nemotron-3.5-super-vl:free).

The client is the official OpenAI SDK configured with the OpenRouter base URL (`OPENROUTER_BASE_URL`) and
`OPENROUTER_API_KEY`. The model id always comes from `LLM_MODEL`. `max_tokens` is sent (NOT
`max_completion_tokens`): OpenRouter's catalog for this free model lists `max_tokens` among its supported
parameters. JSON mode (`response_format={"type": "json_object"}`) is listed as supported and keeps the existing
contract: the RAG system prompt asks for a single JSON object {"answer": ..., "insufficient_evidence": ...} that
citation_service.parse_llm_output() parses (JSON mode requires the word "JSON" in the messages; the system prompt
contains it).
"""
from __future__ import annotations

import logging

import httpx
import openai

from app.core.config import Settings
from app.core.errors import InvalidLLMResponse
from app.services.providers.llm_base import LLMProvider
from app.services.providers.openrouter_common import make_async_client, translate_openai_error

log = logging.getLogger(__name__)


class OpenRouterChatModel(LLMProvider):
    def __init__(self, client: openai.AsyncOpenAI, model: str, temperature: float, max_output_tokens: int):
        self._client, self.model = client, model
        self.temperature, self.max_output_tokens = temperature, max_output_tokens

    @classmethod
    def from_settings(cls, s: Settings, http_client: httpx.AsyncClient | None = None) -> "OpenRouterChatModel":
        client = make_async_client(s, timeout=s.llm_timeout_seconds, http_client=http_client)
        return cls(client, s.llm_model, s.llm_temperature, s.llm_max_output_tokens)

    async def aclose(self) -> None:
        await self._client.close()

    async def chat(self, messages: list[dict]) -> str:
        # Retries (connection errors, 408/409/429/5xx) are bounded and handled by the SDK (OPENROUTER_MAX_RETRIES).
        try:
            resp = await self._client.chat.completions.create(
                model=self.model, messages=messages, temperature=self.temperature,
                max_tokens=self.max_output_tokens, response_format={"type": "json_object"})
        except openai.OpenAIError as e:
            raise translate_openai_error(e, "llm") from None
        if not resp.choices:
            raise InvalidLLMResponse()
        choice = resp.choices[0]
        if choice.finish_reason == "length":  # JSON cut off at max tokens: unusable, never guess the rest
            log.warning("chat completion hit max_tokens (%s)", self.max_output_tokens)
            raise InvalidLLMResponse("The language model's answer was cut off. Increase LLM_MAX_OUTPUT_TOKENS.")
        if getattr(choice.message, "refusal", None) or choice.finish_reason == "content_filter":
            raise InvalidLLMResponse("The language model declined to answer.")
        content = choice.message.content
        if not isinstance(content, str) or not content.strip():
            raise InvalidLLMResponse()
        return content
