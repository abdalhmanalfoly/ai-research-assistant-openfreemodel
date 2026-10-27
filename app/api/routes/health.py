import asyncio

from fastapi import APIRouter, Depends, Query
from sqlalchemy import text

from app.api.deps import Container, get_container
from app.core.errors import ServiceUnavailableForCheck

router = APIRouter()


def _db_check(sf, model: str, dim: int) -> dict:
    try:
        with sf() as s:
            s.execute(text("SELECT 1"))
            ext = s.scalar(text("SELECT 1 FROM pg_extension WHERE extname='vector'"))
    except Exception:
        return {"database": "down", "pgvector": "unknown", "schema_dimension": None, "chunks_pending_reindex": None}
    out = {"database": "up", "pgvector": "installed" if ext else "missing",
           "schema_dimension": None, "chunks_pending_reindex": None}
    try:  # these need the tables to exist; failures just report "unknown" (None)
        with sf() as s:
            typ = s.scalar(text("select format_type(atttypid, atttypmod) from pg_attribute "
                                "where attrelid = 'public.chunks'::regclass and attname = 'embedding'"))
            if typ and typ.startswith("vector(") and typ.endswith(")"):
                out["schema_dimension"] = int(typ[7:-1])
            out["chunks_pending_reindex"] = s.scalar(
                text("select count(*) from chunks where embedding is null or embedding_model is distinct from :m "
                     "or embedding_dim is distinct from :d"), {"m": model, "d": dim})
    except Exception:
        pass
    return out


@router.get("/health")
async def health(c: Container = Depends(get_container)):
    """Cheap and free: never calls OpenRouter. `llm`/`embedding` report CONFIGURATION only (key present, models named);
    use POST /api/health/provider-check to verify the key and model access with a real request."""
    s = c.settings
    db = await asyncio.to_thread(_db_check, c.session_factory, s.embedding_model, s.embedding_dim)
    configured = "configured" if s.openrouter_key_configured else "not_configured"
    schema_ok = db["schema_dimension"] in (None, s.embedding_dim)
    ok = (configured == "configured" and db["database"] == "up" and db["pgvector"] == "installed" and schema_ok)
    ocr_state = ("disabled" if (not s.ocr_enabled or s.ocr_provider == "none")
                 else (getattr(c.ocr, "name", None) or "unavailable"))
    return {"status": "ok" if ok else "degraded", "api": "up",
            "llm_provider": s.llm_provider, "llm": configured, "chat_model_name": s.llm_model,
            "embedding_provider": s.embedding_provider, "embedding": configured,
            "embedding_model_name": s.embedding_model, "embedding_dimension": s.embedding_dim,
            "ocr": ocr_state, "ocr_language": s.ocr_language, "ocr_dpi": s.ocr_dpi, **db,
            "max_pdf_mb": s.max_pdf_mb}


@router.post("/health/provider-check")
async def provider_check(deep: bool = Query(False, description="also send one tiny BILLABLE request per endpoint"),
                         c: Container = Depends(get_container)):
    """Explicit OpenRouter connectivity test (see app/services/provider_check.py). Cached for 30 s per mode."""
    if c.provider_check is None:
        raise ServiceUnavailableForCheck()
    return await c.provider_check.run(deep=deep)
