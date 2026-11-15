"""Celery 任务：心跳、每日重算 RFM 并为进行中的活动补一份名单。"""
from __future__ import annotations

import time

from sqlalchemy import select

from src.config.celery_app import TASK_DAILY, TASK_HEARTBEAT, TASK_MERGE, celery_app
from src.config.settings import settings
from src.utils.logger import get_logger

logger = get_logger("tasks")


def _redis():
    import redis

    return redis.Redis.from_url(settings.redis_url, decode_responses=True)


@celery_app.task(name=TASK_HEARTBEAT)
def heartbeat() -> str:
    try:
        _redis().set(f"{settings.app_name}:worker:heartbeat", time.time())
        return "ok"
    except Exception as exc:  # noqa: BLE001
        logger.warning("heartbeat failed: %s", exc)
        return "error"


@celery_app.task(name=TASK_DAILY)
def daily_refresh() -> dict:
    from src.config.database import session_scope
    from src.models.campaign import Campaign
    from src.services import audit_service, audience_service, segment_service

    with session_scope() as db:
        result = segment_service.recompute(db)
        audit_service.record(
            db,
            actor_email=f"scheduler@{settings.app_name}.io",
            actor_role="admin",
            action="segment.recompute",
            entity="segment",
            entity_id=None,
            detail={"as_of": result["as_of"], "members": result["member_count"], "trigger": "schedule"},
            layer="worker",
        )
        campaigns = db.scalars(select(Campaign).where(Campaign.status == "active")).all()
        generated = 0
        for campaign in campaigns:
            try:
                audience_service.generate(
                    db,
                    campaign=campaign,
                    budget=campaign.budget,
                    operator_email=f"scheduler@{settings.app_name}.io",
                    note="每日定时重算生成",
                )
                generated += 1
            except Exception as exc:  # noqa: BLE001 - 单个活动失败不影响其它
                logger.warning("audience generate failed for %s: %s", campaign.code, exc)
        logger.info("daily refresh: %s members, %s audiences", result["member_count"], generated)
        return {"members": result["member_count"], "audiences": generated}


@celery_app.task(name=TASK_MERGE)
def merge_profiles() -> dict:
    """跨渠道归并定时任务：无未完成任务才新建；失败任务自动从断点续跑。"""
    from sqlalchemy import select

    from src.config.database import session_scope
    from src.constants.enums import MergeJobStatus
    from src.models.merge_job import MergeJob
    from src.services import audit_service, merge_service

    actor = f"scheduler@{settings.app_name}.io"
    with session_scope() as db:
        latest = db.scalar(select(MergeJob).order_by(MergeJob.id.desc()).limit(1))
        if latest is None:
            job = merge_service.create_job(db, operator_email=actor, trigger_source="schedule")
            db.commit()
            job_id = job.id
        elif latest.status == MergeJobStatus.COMPLETED.value:
            # 已完成：归并幂等，定时任务不再新建重复任务
            return {"job_no": latest.job_no, "status": latest.status, "skipped": "already_completed"}
        else:
            # 待处理/处理中/失败：在原任务上从断点续跑
            job_id = latest.id
        merge_service.run_job(db, job_id)
        job = merge_service.get_job(db, job_id)
        audit_service.record(
            db,
            actor_email=actor,
            actor_role="admin",
            action="merge.complete",
            entity="merge_job",
            entity_id=job.id,
            detail={
                "job_no": job.job_no,
                "before": job.profiles_before,
                "after": job.profiles_after,
                "records": job.records_merged,
                "trigger": "schedule",
            },
            layer="worker",
        )
        return {
            "job_no": job.job_no,
            "status": job.status,
            "before": job.profiles_before,
            "after": job.profiles_after,
            "records": job.records_merged,
        }
