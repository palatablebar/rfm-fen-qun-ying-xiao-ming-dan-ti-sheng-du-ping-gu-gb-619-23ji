"""跨渠道归并控制器：发起/续跑任务、查询看板/任务/记录/主档，全部落审计。"""
from __future__ import annotations

from fastapi import Request
from sqlalchemy.orm import Session

from src.services import audit_service, merge_service


def overview(db: Session) -> dict:
    return merge_service.merge_overview(db)


def list_jobs(db: Session, **filters) -> dict:
    return merge_service.list_jobs(db, **filters)


def get_job(db: Session, job_id: int) -> dict:
    return merge_service.serialize_job(merge_service.get_job(db, job_id))


def list_records(db: Session, **filters) -> dict:
    return merge_service.list_records(db, **filters)


def master_detail(db: Session, member_id: int) -> dict:
    return merge_service.get_master_detail(db, member_id)


def start_job(db: Session, principal: dict, request: Request, *, note: str | None = None) -> dict:
    """发起归并：无未完成任务则建任务并同步执行；已有失败任务则在原任务上续跑。"""
    job = merge_service.create_job(db, operator_email=principal["email"], trigger_source="manual")
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="merge.start",
        entity="merge_job",
        entity_id=job.id,
        detail={"job_no": job.job_no, "note": note},
        ip=request.client.host if request.client else None,
    )
    db.commit()
    merge_service.run_job(db, job.id)
    db.commit()
    job = merge_service.get_job(db, job.id)
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="merge.complete",
        entity="merge_job",
        entity_id=job.id,
        detail={
            "job_no": job.job_no,
            "before": job.profiles_before,
            "after": job.profiles_after,
            "records": job.records_merged,
            "conflicts": job.conflict_count,
        },
        ip=request.client.host if request.client else None,
    )
    db.commit()
    return merge_service.serialize_job(job)


def retry_job(db: Session, job_id: int, principal: dict, request: Request) -> dict:
    """从已处理位置续跑：不新建任务、不新增重复档案。"""
    job = merge_service.get_job(db, job_id)
    audit_service.record(
        db,
        actor_email=principal["email"],
        actor_role=principal["role"],
        action="merge.retry",
        entity="merge_job",
        entity_id=job.id,
        detail={"job_no": job.job_no, "last_cursor": job.last_cursor},
        ip=request.client.host if request.client else None,
    )
    db.commit()
    merge_service.run_job(db, job_id)
    db.commit()
    job = merge_service.get_job(db, job_id)
    return merge_service.serialize_job(job)
