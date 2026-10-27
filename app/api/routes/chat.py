from dataclasses import asdict

from fastapi import APIRouter, Depends

from app.api.deps import Container, get_container
from app.schemas.chat import ChatRequest, ChatResponse
from app.services.types import SearchFilters

router = APIRouter()


@router.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest, c: Container = Depends(get_container)):
    result = await c.chat.answer(
        body.question, SearchFilters(body.paper_ids or None, body.year_min, body.year_max), body.top_k)
    return ChatResponse(**{**asdict(result), "citations": [asdict(x) for x in result.citations]})
