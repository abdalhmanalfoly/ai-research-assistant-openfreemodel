import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select

from app.api.deps import Container, get_container
from app.core.errors import NotFound
from app.db.models import IngestionJob, Paper
from app.schemas.papers import JobList, JobOut

router = APIRouter()


def to_out(j: IngestionJob, title: str | None) -> JobOut:
    return JobOut(id=str(j.id), paper_id=str(j.paper_id), paper_title=title, status=j.status,
                  error_message=j.error_message, created_at=j.created_at, updated_at=j.updated_at)


@router.get("/jobs", response_model=JobList)
def list_jobs(limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0), status: Optional[str] = None,
              paper_id: Optional[uuid.UUID] = None, c: Container = Depends(get_container)):
    cond = []
    if status:
        cond.append(IngestionJob.status == status)
    if paper_id:
        cond.append(IngestionJob.paper_id == paper_id)
    with c.session_factory() as s:
        total = s.scalar(select(func.count()).select_from(IngestionJob).where(*cond)) or 0
        rows = s.execute(select(IngestionJob, Paper.title).join(Paper, Paper.id == IngestionJob.paper_id)
                         .where(*cond).order_by(IngestionJob.created_at.desc()).limit(limit).offset(offset)).all()
        return JobList(items=[to_out(j, t) for j, t in rows], total=total, limit=limit, offset=offset)


@router.get("/jobs/{job_id}", response_model=JobOut)
def get_job(job_id: uuid.UUID, c: Container = Depends(get_container)):
    with c.session_factory() as s:
        row = s.execute(select(IngestionJob, Paper.title).join(Paper, Paper.id == IngestionJob.paper_id)
                        .where(IngestionJob.id == job_id)).first()
        if not row:
            raise NotFound("Job not found.")
        return to_out(*row)
