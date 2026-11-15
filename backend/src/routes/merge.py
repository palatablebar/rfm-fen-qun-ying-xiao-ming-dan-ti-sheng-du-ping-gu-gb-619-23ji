from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.orm import Session

from src.config.database import get_db
from src.constants.roles import MERGE_RUN_ROLES, READ_ROLES
from src.controllers import merge_controller
from src.middlewares.rbac import require_roles
from src.schemas.merge import MergeJobCreateIn
from src.utils.pagination import page_params

router = APIRouter(prefix="/merge", tags=["merge"])


@router.get("/overview")
def overview(
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return merge_controller.overview(db)


@router.get("/jobs")
def list_jobs(
    status: str | None = Query(default=None, max_length=16),
    limit: int = Query(20),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    page = page_params(limit, offset)
    return merge_controller.list_jobs(db, status=status, limit=page.limit, offset=page.offset)


@router.get("/jobs/{job_id}")
def get_job(
    job_id: int,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return merge_controller.get_job(db, job_id)


@router.post("/jobs", status_code=201)
def start_job(
    request: Request,
    payload: MergeJobCreateIn = MergeJobCreateIn(),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*MERGE_RUN_ROLES)),
) -> dict:
    return merge_controller.start_job(db, principal, request, note=payload.note)


@router.post("/jobs/{job_id}/retry")
def retry_job(
    job_id: int,
    request: Request,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*MERGE_RUN_ROLES)),
) -> dict:
    return merge_controller.retry_job(db, job_id, principal, request)


@router.get("/records")
def list_records(
    job_id: int | None = Query(default=None, ge=1),
    master_member_id: int | None = Query(default=None, ge=1),
    match_kind: str | None = Query(default=None, max_length=16),
    limit: int = Query(20),
    offset: int = Query(0),
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    page = page_params(limit, offset)
    return merge_controller.list_records(
        db,
        job_id=job_id,
        master_member_id=master_member_id,
        match_kind=match_kind,
        limit=page.limit,
        offset=page.offset,
    )


@router.get("/masters/{member_id}")
def master_detail(
    member_id: int,
    db: Session = Depends(get_db),
    principal: dict = Depends(require_roles(*READ_ROLES)),
) -> dict:
    return merge_controller.master_detail(db, member_id)
