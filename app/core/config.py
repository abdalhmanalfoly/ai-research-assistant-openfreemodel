"""Single configuration system for the backend (pydantic-settings; env vars and/or a backend-only .env file).

Models: ONE OpenRouter API key (OPENROUTER_API_KEY) serves both answer generation and embeddings.
The key is a SecretStr: it never appears in repr(), logs, API responses or error messages. It is NOT validated at
import time (Alembic and other tooling import this module and must work without it); call
`Settings.require_openrouter_api_key()` at the initialisation boundary (application startup, scripts that call
OpenRouter).
"""
import logging
import os
import re
from functools import lru_cache
from urllib.parse import urlparse

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.errors import ConfigurationError

log = logging.getLogger(__name__)

OFFICIAL_OPENROUTER_HOST = "openrouter.ai"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
# pgvector HNSW indexes support at most 2000 dimensions for the `vector` type.
MAX_INDEXED_DIM = 2000
# Per-model embedding facts, used to validate EMBEDDING_DIMENSIONS and to decide whether the `dimensions` API
# parameter is sent. Everything here was taken from the provider's published model documentation/endpoint listing;
# anything unknown is treated as "do not assume": the returned vectors are validated at runtime instead.
KNOWN_EMBEDDING_MODELS = {
    # NVIDIA documents Nemotron 3.5 Embed 1B as returning 2048-dim vectors by default and accepting any integer
    # `dimensions` value from 128 through 2048 (NVIDIA NIM support matrix). OpenRouter's catalog entry does not
    # advertise `dimensions` support, so setting EMBEDDING_DIMENSIONS below 2048 is an explicit opt-in that is
    # validated on every response (a mismatch raises EmbeddingDimensionMismatch loudly; never truncated silently).
    "nvidia/nemotron-3.5-embed-1b:free": {"default": 2048, "min": 128, "max": 2048, "dimensions_param": True},
}
# Settings from the local-inference and OpenAI-API eras. They are ignored now; a warning names them (never values).
OBSOLETE_ENV_VARS = (
    "OLLAMA_BASE_URL", "LLM_NUM_CTX", "EMBEDDING_DEVICE", "EMBEDDING_DTYPE", "EMBEDDING_MAX_SEQ_LENGTH",
    "EMBEDDING_NORMALIZE", "EMBEDDING_QUERY_INSTRUCTION", "EMBEDDING_DOCUMENT_PREFIX", "EMBEDDING_CACHE_DIR",
    "EMBEDDING_OFFLINE", "EMBEDDING_PRELOAD_ON_STARTUP",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_ALLOW_CUSTOM_BASE_URL", "OPENAI_MAX_RETRIES",
)


class Settings(BaseSettings):
    # hide_input_in_errors: a malformed OPENROUTER_API_KEY (or DATABASE_URL with a password) must never be echoed in a
    # validation error.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True, populate_by_name=True,
                                      hide_input_in_errors=True)

    app_env: str = "development"
    log_level: str = "INFO"
    database_url: str = "postgresql+psycopg://research:research@localhost:5432/research"
    # Set true ONLY when DATABASE_URL points at a transaction-mode pooler (port 6543): that mode does not support prepared statements.
    db_disable_prepared_statements: bool = False
    db_pool_size: int = Field(5, ge=1, le=50)
    db_max_overflow: int = Field(5, ge=0, le=50)

    # --- OpenRouter (shared by the chat model and the embedding model) ---
    openrouter_api_key: SecretStr | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    # Guard against sending the key to an unexpected host through a mistyped OPENROUTER_BASE_URL.
    openrouter_allow_custom_base_url: bool = False
    # SDK-level retries (connection errors, 408/409/429/5xx, with exponential backoff). Bounded; never infinite.
    openrouter_max_retries: int = Field(2, ge=0, le=5)

    llm_provider: str = "openrouter"
    llm_model: str = "nvidia/nemotron-3.5-super-vl:free"
    llm_timeout_seconds: float = Field(60, gt=0, le=600)
    llm_temperature: float = Field(0.1, ge=0, le=2)
    llm_max_output_tokens: int = Field(1000, ge=16, le=16000)

    embedding_provider: str = "openrouter"
    embedding_model: str = "nvidia/nemotron-3.5-embed-1b:free"
    # Must equal the vector(N) column (the embedding migrations create it from this value). Env: EMBEDDING_DIMENSIONS
    # (EMBEDDING_DIM is accepted as the legacy name). The model's DEFAULT output size is 2048, which exceeds the
    # pgvector HNSW limit (2000): the shipped configuration requests the model's native 2048 — the migration stores
    # vectors WITHOUT an HNSW index at that size (full-scan retrieval, fine for small/medium corpora). Requesting any
    # other size in 128..2047 via the `dimensions` parameter keeps an HNSW index possible. Every returned vector is
    # validated against this value at runtime.
    embedding_dim: int = Field(2048, gt=0, le=4096,
                               validation_alias=AliasChoices("EMBEDDING_DIMENSIONS", "EMBEDDING_DIM"))
    embedding_batch_size: int = Field(64, ge=1, le=512)  # inputs per API request
    embedding_timeout_seconds: float = Field(30, gt=0, le=300)
    embedding_max_input_chars: int = Field(24000, ge=100)  # rejected before any network call; chunks are far smaller

    retrieval_top_k: int = Field(5, ge=1, le=20)
    # UNCALIBRATED for nvidia/nemotron-3.5-embed-1b:free. Cosine scores differ between embedding models, so the BGE
    # (0.45) and OpenAI (0.30) values were not carried over. Calibrate on your corpus once retrieval works:
    # python -m scripts.calibrate_threshold questions.json
    retrieval_threshold: float = Field(0.30, ge=0, le=1)
    evidence_budget_chars: int | None = Field(None, ge=500)  # max characters of evidence sent to the chat model
    chunk_max_chars: int = Field(1200, ge=200)

    cors_origins: str = ""  # comma-separated explicit origins; empty = no CORS (same-origin only)
    serve_frontend: bool = True  # serve ./frontend at / (never shadows /api)

    max_pdf_mb: int = Field(50, ge=1)
    storage_dir: str = "./data/storage"

    # --- OCR for scanned PDFs (local; page images never leave the machine) ---
    ocr_enabled: bool = True
    ocr_provider: str = "auto"  # auto | rapidocr | tesseract | none
    ocr_language: str = "eng"  # Tesseract language(s), e.g. eng, ara, eng+ara (RapidOCR uses its bundled model)
    ocr_dpi: int = Field(200, ge=100, le=400)  # rendering resolution for OCR; higher = slower/better
    ocr_min_page_chars: int = Field(25, ge=0, le=1000)  # below this, a page is treated as scanned
    ocr_max_pages: int = Field(0, ge=0)  # 0 = no limit; guards very large scanned documents

    # ---------------- validators ----------------
    @field_validator("openrouter_api_key", mode="before")
    @classmethod
    def _key(cls, v):
        if v is None:
            return None
        raw = v.get_secret_value() if isinstance(v, SecretStr) else str(v)
        raw = raw.strip()
        if not raw:
            return None
        if re.search(r"\s", raw):
            raise ValueError("OPENROUTER_API_KEY must not contain whitespace or line breaks")  # value is never echoed
        return raw

    @field_validator("openrouter_base_url")
    @classmethod
    def _base_url(cls, v: str) -> str:
        u = urlparse(v.strip())
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError("OPENROUTER_BASE_URL must be an http(s) URL, e.g. https://openrouter.ai/api/v1")
        return v.strip().rstrip("/")

    @field_validator("llm_provider")
    @classmethod
    def _provider(cls, v: str) -> str:
        v = v.strip().lower()
        if v != "openrouter":
            raise ValueError(f"LLM_PROVIDER={v!r} is not supported any more; only 'openrouter' is implemented. "
                             "Set LLM_PROVIDER=openrouter and LLM_MODEL=nvidia/nemotron-3.5-super-vl:free.")
        return v

    @field_validator("embedding_provider")
    @classmethod
    def _emb_provider(cls, v: str) -> str:
        v = v.strip().lower()
        if v != "openrouter":
            raise ValueError(f"EMBEDDING_PROVIDER={v!r} is not supported any more; only 'openrouter' is implemented. "
                             "Set EMBEDDING_PROVIDER=openrouter and "
                             "EMBEDDING_MODEL=nvidia/nemotron-3.5-embed-1b:free, then re-index.")
        return v

    @field_validator("cors_origins")
    @classmethod
    def _cors(cls, v: str) -> str:
        if "*" in v:
            raise ValueError("CORS_ORIGINS must list explicit origins, not *")
        return v

    @field_validator("ocr_provider")
    @classmethod
    def _ocr_provider(cls, v: str) -> str:
        v = v.strip().lower()
        if v not in ("auto", "rapidocr", "tesseract", "none"):
            raise ValueError("OCR_PROVIDER must be auto, rapidocr, tesseract or none")
        return v

    @model_validator(mode="after")
    def _cross(self):
        host = urlparse(self.openrouter_base_url).hostname or ""
        local = host in LOCAL_HOSTS
        if not local and urlparse(self.openrouter_base_url).scheme != "https":
            raise ValueError("OPENROUTER_BASE_URL must use https (http is allowed only for localhost)")
        if not local and host != OFFICIAL_OPENROUTER_HOST and not self.openrouter_allow_custom_base_url:
            raise ValueError(
                f"OPENROUTER_BASE_URL points at '{host}', not {OFFICIAL_OPENROUTER_HOST}. The API key would be sent"
                " there. If that is intentional set OPENROUTER_ALLOW_CUSTOM_BASE_URL=true.")
        info = KNOWN_EMBEDDING_MODELS.get(self.embedding_model)
        if info is not None:
            if not info["min"] <= self.embedding_dim <= info["max"]:
                raise ValueError(
                    f"{self.embedding_model} supports {info['min']}..{info['max']} dimensions "
                    f"(default {info['default']}); EMBEDDING_DIMENSIONS={self.embedding_dim}")
        return self

    # ---------------- helpers ----------------
    @property
    def embedding_supports_hnsw(self) -> bool:
        """pgvector HNSW indexes cap at MAX_INDEXED_DIM dimensions; larger columns are stored unindexed."""
        return self.embedding_dim <= MAX_INDEXED_DIM
    @property
    def effective_evidence_budget_chars(self) -> int:
        return self.evidence_budget_chars or 12000  # ~3k tokens of evidence; top_k * chunk_max_chars is usually smaller

    def require_openrouter_api_key(self) -> str:
        """Return the key or raise a clear error. Never includes the value in any message."""
        if self.openrouter_api_key is None:
            raise ConfigurationError(
                "OPENROUTER_API_KEY is not set. Put it in the backend's .env file or environment "
                "(server-side only; never in the frontend).")
        return self.openrouter_api_key.get_secret_value()

    @property
    def openrouter_key_configured(self) -> bool:
        return self.openrouter_api_key is not None

    @classmethod
    def embedding_info(cls, model: str) -> dict | None:
        return KNOWN_EMBEDDING_MODELS.get(model)

    def diagnostics(self) -> dict:
        """Secret-free summary for health endpoints and logs."""
        return {
            "llm_provider": self.llm_provider, "llm_model": self.llm_model,
            "embedding_provider": self.embedding_provider, "embedding_model": self.embedding_model,
            "embedding_dimension": self.embedding_dim,
            "openrouter_api_key": "configured" if self.openrouter_key_configured else "missing",
            "openrouter_host": urlparse(self.openrouter_base_url).hostname,
        }


def find_obsolete_settings() -> list[str]:
    """Names (never values) of removed local-inference/OpenAI settings still present in the environment or .env."""
    present = {k.upper() for k in os.environ}
    try:
        from dotenv import dotenv_values
        present |= {k.upper() for k in dotenv_values(".env")}
    except Exception:
        pass
    return [n for n in OBSOLETE_ENV_VARS if n in present]


@lru_cache
def get_settings() -> Settings:
    return Settings()
