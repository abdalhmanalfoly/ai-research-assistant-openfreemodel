from dataclasses import dataclass

from fastapi import Request

from app.core.config import Settings
from app.services.chat_service import ChatService
from app.services.ingestion_service import IngestionService
from app.services.providers.llm_base import LLMProvider


@dataclass
class Container:
    settings: Settings
    llm: LLMProvider
    chat: ChatService
    ingestion: IngestionService
    session_factory: object
    embedder: object = None
    ocr: object = None
    provider_check: object = None


def get_container(request: Request) -> Container:
    return request.app.state.container
