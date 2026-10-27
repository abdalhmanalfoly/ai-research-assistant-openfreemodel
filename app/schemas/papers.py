from datetime import datetime

from pydantic import BaseModel


class PaperOut(BaseModel):
    id: str
    title: str
    authors: list[str]
    abstract: str | None
    publication_year: int | None
    doi: str | None
    source_url: str | None
    status: str
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class PaperList(BaseModel):
    items: list[PaperOut]
    total: int
    limit: int
    offset: int


class UploadResponse(BaseModel):
    paper_id: str
    job_id: str | None
    already_registered: bool


class JobOut(BaseModel):
    id: str
    paper_id: str
    paper_title: str | None = None
    status: str
    error_message: str | None
    created_at: datetime
    updated_at: datetime


class JobList(BaseModel):
    items: list[JobOut]
    total: int
    limit: int
    offset: int
