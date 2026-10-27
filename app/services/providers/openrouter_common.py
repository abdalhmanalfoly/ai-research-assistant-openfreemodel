"""Shared OpenRouter plumbing: client construction and translation of SDK errors into safe application errors.

OpenRouter exposes an OpenAI-compatible API, so the official `openai` SDK is reused as the HTTP client with the
OpenRouter base URL (https://openrouter.ai/api/v1) and OPENROUTER_API_KEY — it is never pointed at api.openai.com.

SECURITY: SDK exception messages can contain a masked fragment of the API key. They are therefore NEVER logged or
returned. Only the exception class, HTTP status, provider error `code` and request id are.
"""
from __future__ import annotations

import logging
from typing import Literal

import httpx
import openai

from app.core import errors as E
from app.core.config import Settings

log = logging.getLogger(__name__)
Kind = Literal["llm", "embedding"]
# OpenRouter marks free models' limits as HTTP 429 or reports them with its own codes; 402 is "payment required".
_QUOTA_STATUSES = {402}  # OpenRouter: account has no credits (free-model daily limits instead surface as 429)


def make_async_client(s: Settings, *, timeout: float, http_client: httpx.AsyncClient | None = None) -> openai.AsyncOpenAI:
    return openai.AsyncOpenAI(api_key=s.require_openrouter_api_key(), base_url=s.openrouter_base_url, timeout=timeout,
                              max_retries=s.openrouter_max_retries, http_client=http_client)


def make_sync_client(s: Settings, *, timeout: float, http_client: httpx.Client | None = None) -> openai.OpenAI:
    return openai.OpenAI(api_key=s.require_openrouter_api_key(), base_url=s.openrouter_base_url, timeout=timeout,
                         max_retries=s.openrouter_max_retries, http_client=http_client)


def translate_openai_error(exc: BaseException, kind: Kind) -> E.AppError:
    """Map an openai SDK exception (raised against OpenRouter) to an AppError with a fixed, secret-free message."""
    status = getattr(exc, "status_code", None)
    code = getattr(exc, "code", None)
    log.warning("openrouter %s request failed: %s status=%s code=%s request_id=%s", kind, type(exc).__name__, status,
                code, getattr(exc, "request_id", None))
    llm = kind == "llm"
    # APITimeoutError subclasses APIConnectionError: test it first.
    if isinstance(exc, openai.APITimeoutError):
        return E.LLMTimeout() if llm else E.EmbeddingTimeout()
    if isinstance(exc, openai.APIConnectionError):
        return E.LLMUnavailable() if llm else E.EmbeddingUnavailable()
    if isinstance(exc, openai.AuthenticationError):  # 401
        return E.LLMAuthFailed() if llm else E.EmbeddingAuthFailed()
    if status in _QUOTA_STATUSES:  # OpenRouter reports missing credits explicitly as HTTP 402
        return E.LLMQuotaExceeded() if llm else E.EmbeddingQuotaExceeded()
    if isinstance(exc, openai.PermissionDeniedError):  # 403
        if code == "insufficient_quota":  # some gateways express quota as 403 + this code
            return E.LLMQuotaExceeded() if llm else E.EmbeddingQuotaExceeded()
        return E.ModelNotAvailable() if llm else E.EmbeddingModelNotAvailable()
    if isinstance(exc, openai.NotFoundError):  # 404
        return E.ModelNotAvailable() if llm else E.EmbeddingModelNotAvailable()
    if isinstance(exc, openai.RateLimitError):  # 429: free-tier rate limit (or quota, per status above)
        if code == "insufficient_quota":
            return E.LLMQuotaExceeded() if llm else E.EmbeddingQuotaExceeded()
        return E.LLMRateLimited() if llm else E.EmbeddingRateLimited()
    if isinstance(exc, (openai.BadRequestError, openai.UnprocessableEntityError)):
        if llm:
            return E.InvalidLLMResponse("The language model provider rejected the request.")
        return E.InvalidEmbeddingInput("The embedding provider rejected the input (is the text too long?).")
    return E.LLMUnavailable() if llm else E.EmbeddingUnavailable()
