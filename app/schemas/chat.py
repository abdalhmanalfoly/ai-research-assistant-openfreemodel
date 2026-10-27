from pydantic import BaseModel, Field, field_validator, model_validator


class ChatRequest(BaseModel):
    question: str = Field(max_length=2000)
    paper_ids: list[str] = Field(default_factory=list, max_length=100)
    top_k: int | None = Field(default=None, ge=1, le=20)
    year_min: int | None = Field(default=None, ge=1000, le=3000)
    year_max: int | None = Field(default=None, ge=1000, le=3000)

    @field_validator("question")
    @classmethod
    def _q(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("question must not be empty")
        return v

    @field_validator("paper_ids")
    @classmethod
    def _ids(cls, v: list[str]) -> list[str]:
        import uuid
        for i in v:
            uuid.UUID(i)  # ValueError -> 422
        return v

    @model_validator(mode="after")
    def _years(self):
        if self.year_min and self.year_max and self.year_min > self.year_max:
            raise ValueError("year_min must be <= year_max")
        return self


class CitationOut(BaseModel):
    id: str
    paper_id: str
    title: str
    authors: list[str]
    page_number: int | None
    section: str | None
    doi: str | None
    source_url: str | None
    relevance_score: float
    excerpt: str | None = None


class ChatResponse(BaseModel):
    answer: str
    citations: list[CitationOut]
    retrieved_chunks: int
    model: str
    insufficient_evidence: bool
    warnings: list[str]
