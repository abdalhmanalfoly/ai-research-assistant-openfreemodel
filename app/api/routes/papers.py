import uuid
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Query, UploadFile
from sqlalchemy import func, select

from app.api.deps import Container, get_container
from app.core.errors import InvalidUpload, NotFound
from app.db.models import Paper
from app.schemas.papers import PaperList, PaperOut, UploadResponse

router = APIRouter()


def to_out(p: Paper) -> PaperOut:
    return PaperOut(id=str(p.id), title=p.title, authors=list(p.authors or []), abstract=p.abstract,
                    publication_year=p.publication_year, doi=p.doi, source_url=p.source_url, status=p.status,
                    error_message=p.error_message, created_at=p.created_at, updated_at=p.updated_at)


@router.get("/papers", response_model=PaperList)
def list_papers(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                year: Optional[int] = None, status: Optional[str] = None, q: Optional[str] = Query(None, max_length=200),
                c: Container = Depends(get_container)):
    cond = []
    if year:
        cond.append(Paper.publication_year == year)
    if status:
        cond.append(Paper.status == status)
    if q:
        cond.append(Paper.title.ilike(f"%{q}%"))
    with c.session_factory() as s:
        total = s.scalar(select(func.count()).select_from(Paper).where(*cond)) or 0
        rows = s.scalars(select(Paper).where(*cond).order_by(Paper.created_at.desc()).limit(limit).offset(offset)).all()
        return PaperList(items=[to_out(p) for p in rows], total=total, limit=limit, offset=offset)


@router.get("/papers/{paper_id}", response_model=PaperOut)
def get_paper(paper_id: uuid.UUID, c: Container = Depends(get_container)):
    with c.session_factory() as s:
        p = s.get(Paper, paper_id)
        if not p:
            raise NotFound("Paper not found.")
        return to_out(p)


@router.post("/papers/upload", response_model=UploadResponse, status_code=202)
async def upload(background: BackgroundTasks, file: UploadFile = File(...), title: Optional[str] = Form(None),
                 authors: Optional[str] = Form(None, description="Comma-separated"), year: Optional[int] = Form(None),
                 doi: Optional[str] = Form(None), source_url: Optional[str] = Form(None),
                 c: Container = Depends(get_container)):
    limit = c.settings.max_pdf_mb * 1024 * 1024
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise InvalidUpload(f"PDF exceeds the {c.settings.max_pdf_mb} MB limit.")
    if not (file.filename or "").lower().endswith(".pdf"):
        raise InvalidUpload("Only .pdf files are accepted.")
    name = (title or (file.filename or "untitled")[:-4]).strip()[:500] or "untitled"
    author_list = [a.strip() for a in authors.split(",") if a.strip()] if authors else []
    import asyncio
    paper_id, job_id = await asyncio.to_thread(
        c.ingestion.register, data, title=name, authors=author_list, year=year, doi=doi or None, source_url=source_url)
    if job_id:
        background.add_task(c.ingestion.run_job, job_id)
    return UploadResponse(paper_id=paper_id, job_id=job_id, already_registered=job_id is None)
