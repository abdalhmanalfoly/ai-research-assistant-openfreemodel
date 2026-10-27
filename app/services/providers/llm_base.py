"""Provider boundary for answer generation. Application code depends only on this interface (and on
app.services.types.ChatModel); provider-specific request/response formats stay inside the implementation."""
from abc import ABC, abstractmethod


class LLMProvider(ABC):
    model: str

    @abstractmethod
    async def chat(self, messages: list[dict]) -> str:
        """Return the model's raw text reply (a JSON object string for the RAG prompt)."""

    @abstractmethod
    async def aclose(self) -> None: ...
