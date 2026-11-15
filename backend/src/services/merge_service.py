"""跨渠道归并编排：任务生命周期、批处理断点续跑、幂等、订单/券响应合并、主档重算。

幂等与续跑口径：
- 每个渠道档案只被处理一次：按 id 升序批处理，last_cursor 记录已处理到的最大 id；
- 重试从 last_cursor 之后继续；已 merged 的档案跳过，绝不新增重复主档/记录；
- 一批一个事务，批末推进游标并提交；失败时该批回滚、游标停在上一批末尾，可续跑；
- 归并记录按 (merge_job_id, merged_profile_id) 去重，重复执行不产生重复行。
"""
from __future__ import annotations

from datetime import datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.constants.enums import (
    COUPON_SENT_STATUS_LABELS,
    IDENTITY_KIND_LABELS,
    MERGE_CONFLICT_FIELD_LABELS,
    MERGE_JOB_STATUS_LABELS,
    PROFILE_CHANNEL_LABELS,
    PROFILE_STATUS_LABELS,
    MergeJobStatus,
)
from src.constants.limits import MERGE_BATCH_SIZE, RFM_WINDOW_DAYS
from src.models.channel_profile import ChannelProfile
from src.models.coupon_response import CouponResponse
from src.models.member import Member
from src.models.merge_job import MergeJob
from src.models.merge_record import MergeRecord
from src.models.order import Order
from src.services import merge_engine
from src.utils.errors import AppError, not_found
from src.utils.timeutil import iso_date, iso_utc, local_now, utcnow


# ----------------------------- 任务号 -----------------------------

def _next_job_no(db: Session) -> str:
    prefix = f"M{local_now():%Y%m%d}"
    count = db.scalar(select(func.count()).select_from(MergeJob).where(MergeJob.job_no.like(f"{prefix}%"))) or 0
    return f"{prefix}{count + 1:02d}"


# ----------------------------- 查询 -----------------------------

def _count(db: Session, model) -> int:
    return db.scalar(select(func.count()).select_from(model)) or 0


def merge_overview(db: Session) -> dict:
    """归并看板：归并前后人数、档案/主档/被合并规模、最近任务。"""
    profiles_total = _count(db, ChannelProfile)
    profiles_active = db.scalar(
        select(func.count()).select_from(ChannelProfile).where(ChannelProfile.status == "active")
    ) or 0
    profiles_merged = profiles_total - profiles_active
    members_total = _count(db, Member)
    members_master = db.scalar(
        select(func.count()).select_from(Member).where(Member.merged_into_id.is_(None))
    ) or 0
    members_merged = members_total - members_master
    coupon_total = _count(db, CouponResponse)
    coupon_redeemed = db.scalar(
        select(func.count()).select_from(CouponResponse).where(CouponResponse.status == "redeemed")
    ) or 0
    latest_completed = db.scalar(
        select(MergeJob).where(MergeJob.status == MergeJobStatus.COMPLETED.value).order_by(MergeJob.id.desc()).limit(1)
    )
    return {
        "profiles_total": profiles_total,
        "profiles_active": profiles_active,
        "profiles_merged": profiles_merged,
        "members_total": members_total,
        "members_master": members_master,
        "members_merged": members_merged,
        "coupon_total": coupon_total,
        "coupon_redeemed": coupon_redeemed,
        "before_count": latest_completed.profiles_before if latest_completed else profiles_total,
        "after_count": latest_completed.profiles_after if latest_completed else profiles_active,
        "latest_job": serialize_job(latest_completed) if latest_completed else None,
    }


def list_jobs(db: Session, *, status: str | None = None, limit: int = 20, offset: int = 0) -> dict:
    stmt = select(MergeJob)
    count_stmt = select(func.count()).select_from(MergeJob)
    if status:
        stmt = stmt.where(MergeJob.status == status)
        count_stmt = count_stmt.where(MergeJob.status == status)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(stmt.order_by(MergeJob.id.desc()).limit(limit).offset(offset)).all()
    return {"items": [serialize_job(row) for row in rows], "total": total, "limit": limit, "offset": offset}


def get_job(db: Session, job_id: int) -> MergeJob:
    job = db.get(MergeJob, job_id)
    if job is None:
        raise not_found("merge_job", job_id)
    return job


def list_records(
    db: Session,
    *,
    job_id: int | None = None,
    master_member_id: int | None = None,
    match_kind: str | None = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    stmt = select(MergeRecord)
    count_stmt = select(func.count()).select_from(MergeRecord)
    if job_id:
        stmt = stmt.where(MergeRecord.merge_job_id == job_id)
        count_stmt = count_stmt.where(MergeRecord.merge_job_id == job_id)
    if master_member_id:
        stmt = stmt.where(MergeRecord.master_member_id == master_member_id)
        count_stmt = count_stmt.where(MergeRecord.master_member_id == master_member_id)
    if match_kind:
        stmt = stmt.where(MergeRecord.match_kind == match_kind)
        count_stmt = count_stmt.where(MergeRecord.match_kind == match_kind)
    total = db.scalar(count_stmt) or 0
    rows = db.scalars(stmt.order_by(MergeRecord.id.desc()).limit(limit).offset(offset)).all()
    member_ids = {r.master_member_id for r in rows} | {r.merged_member_id for r in rows}
    profile_ids = {r.master_profile_id for r in rows} | {r.merged_profile_id for r in rows}
    members = {m.id: m for m in db.scalars(select(Member).where(Member.id.in_(member_ids))).all()} if member_ids else {}
    profiles = (
        {p.id: p for p in db.scalars(select(ChannelProfile).where(ChannelProfile.id.in_(profile_ids))).all()}
        if profile_ids
        else {}
    )
    items = [serialize_record(r, members, profiles) for r in rows]
    return {"items": items, "total": total, "limit": limit, "offset": offset}


def get_master_detail(db: Session, member_id: int) -> dict:
    """主档详情：主档资料 + 被合并档案 + 归并前后人数依据。"""
    master = db.get(Member, member_id)
    if master is None:
        raise not_found("member", member_id)
    if master.merged_into_id is not None:
        # 传入的是被合并档案，归一到其主档
        master = db.get(Member, master.merged_into_id)
    merged_members = db.scalars(
        select(Member).where(Member.merged_into_id == master.id).order_by(Member.id)
    ).all()
    profiles = db.scalars(
        select(ChannelProfile)
        .where(
            (ChannelProfile.member_id == master.id)
            | (ChannelProfile.merged_into_id == master.id)
        )
        .order_by(ChannelProfile.id)
    ).all()
    orders = db.scalars(select(Order).where(Order.member_id == master.id).order_by(Order.paid_at.desc()).limit(50)).all()
    coupons = db.scalars(
        select(CouponResponse).where(CouponResponse.member_id == master.id).order_by(CouponResponse.sent_at.desc()).limit(50)
    ).all()
    records = db.scalars(
        select(MergeRecord).where(MergeRecord.master_member_id == master.id).order_by(MergeRecord.id)
    ).all()
    return {
        "master": _member_brief(master),
        "merged_members": [_member_brief(m) for m in merged_members],
        "profiles": [_profile_brief(p) for p in profiles],
        "orders_count": db.scalar(
            select(func.count()).select_from(Order).where(Order.member_id == master.id)
        ) or 0,
        "orders": [
            {
                "order_no": o.order_no,
                "channel": o.channel,
                "channel_label": PROFILE_CHANNEL_LABELS.get(o.channel, o.channel),
                "amount": round(o.amount or 0.0, 2),
                "coupon_used": bool(o.coupon_used),
                "paid_at": iso_utc(o.paid_at),
            }
            for o in orders
        ],
        "coupons": [
            {
                "campaign_code": c.campaign_code,
                "coupon_name": c.coupon_name,
                "channel": c.channel,
                "status": c.status,
                "status_label": COUPON_SENT_STATUS_LABELS.get(c.status, c.status),
                "face_value": round(c.face_value or 0.0, 2),
                "sent_at": iso_utc(c.sent_at),
                "redeemed_at": iso_utc(c.redeemed_at),
            }
            for c in coupons
        ],
        "coupons_count": db.scalar(
            select(func.count()).select_from(CouponResponse).where(CouponResponse.member_id == master.id)
        ) or 0,
        "merge_records": [_record_brief(r) for r in records],
    }


# ----------------------------- 序列化 -----------------------------

def serialize_job(job: MergeJob) -> dict:
    return {
        "id": job.id,
        "job_no": job.job_no,
        "status": job.status,
        "status_label": MERGE_JOB_STATUS_LABELS.get(job.status, job.status),
        "trigger_source": job.trigger_source,
        "operator_email": job.operator_email,
        "last_cursor": job.last_cursor,
        "profiles_before": job.profiles_before,
        "profiles_after": job.profiles_after,
        "members_before": job.members_before,
        "members_after": job.members_after,
        "groups_merged": job.groups_merged,
        "records_merged": job.records_merged,
        "conflict_count": job.conflict_count,
        "processed_profiles": job.processed_profiles,
        "error_message": job.error_message,
        "stats": job.stats or {},
        "started_at": iso_utc(job.started_at),
        "finished_at": iso_utc(job.finished_at),
        "created_at": iso_utc(job.created_at),
    }


def serialize_record(record: MergeRecord, members: dict, profiles: dict) -> dict:
    master = members.get(record.master_member_id)
    merged = members.get(record.merged_member_id)
    master_profile = profiles.get(record.master_profile_id)
    merged_profile = profiles.get(record.merged_profile_id)
    return {
        "id": record.id,
        "merge_job_id": record.merge_job_id,
        "match_kind": record.match_kind,
        "match_kind_label": IDENTITY_KIND_LABELS.get(record.match_kind, record.match_kind),
        "match_value": record.match_value,
        "external_type": record.external_type,
        "master_reason": record.master_reason,
        "conflicts": [_decorate_conflict(c) for c in (record.conflicts or [])],
        "orders_moved": record.orders_moved,
        "coupons_moved": record.coupons_moved,
        "created_at": iso_utc(record.created_at),
        "master": _member_brief(master) if master else {"id": record.master_member_id},
        "merged": _member_brief(merged) if merged else {"id": record.merged_member_id},
        "master_profile": _profile_brief(master_profile) if master_profile else {"id": record.master_profile_id},
        "merged_profile": _profile_brief(merged_profile) if merged_profile else {"id": record.merged_profile_id},
    }


def _decorate_conflict(conflict: dict) -> dict:
    field = conflict.get("field")
    return {
        **conflict,
        "field_label": MERGE_CONFLICT_FIELD_LABELS.get(field, field),
        "chosen": {
            **conflict.get("chosen", {}),
            "channel_label": PROFILE_CHANNEL_LABELS.get(conflict.get("chosen", {}).get("channel"), ""),
        },
        "sources": [
            {
                **src,
                "channel_label": PROFILE_CHANNEL_LABELS.get(src.get("channel"), src.get("channel")),
            }
            for src in conflict.get("sources", [])
        ],
    }


def _member_brief(member: Member | None) -> dict:
    if member is None:
        return {}
    return {
        "id": member.id,
        "member_no": member.member_no,
        "name": member.name,
        "phone": member.phone,
        "city": member.city,
        "tier": member.tier,
        "merged_into_id": member.merged_into_id,
        "orders_window": member.orders_window,
        "amount_window": round(member.amount_window or 0.0, 2),
        "purchase_rate": round(member.purchase_rate or 0.0, 6),
        "segment_code": member.segment_code,
    }


def _profile_brief(profile: ChannelProfile | None) -> dict:
    if profile is None:
        return {}
    return {
        "id": profile.id,
        "member_id": profile.member_id,
        "channel": profile.channel,
        "channel_label": PROFILE_CHANNEL_LABELS.get(profile.channel, profile.channel),
        "channel_member_no": profile.channel_member_no,
        "name": profile.name,
        "phone": profile.phone,
        "external_type": profile.external_type,
        "external_id": profile.external_id,
        "city": profile.city,
        "tier": profile.tier,
        "status": profile.status,
        "status_label": PROFILE_STATUS_LABELS.get(profile.status, profile.status),
        "merged_into_id": profile.merged_into_id,
        "source_updated_at": iso_utc(profile.source_updated_at),
    }


def _record_brief(record: MergeRecord) -> dict:
    return {
        "id": record.id,
        "match_kind": record.match_kind,
        "match_kind_label": IDENTITY_KIND_LABELS.get(record.match_kind, record.match_kind),
        "master_reason": record.master_reason,
        "conflict_count": len(record.conflicts or []),
        "orders_moved": record.orders_moved,
        "coupons_moved": record.coupons_moved,
    }


# ----------------------------- 任务执行 -----------------------------

def create_job(db: Session, *, operator_email: str, trigger_source: str = "manual") -> MergeJob:
    """新建待处理任务。

    存在未完成（待处理/处理中/失败）任务时返回 409，要求在原任务上续跑；
    已存在已完成任务时同样返回 409——归并是全量幂等动作，重跑应在原任务上
    重试（直接返回完成），避免产生重复任务与重复档案。
    """
    latest = db.scalar(select(MergeJob).order_by(MergeJob.id.desc()).limit(1))
    if latest is not None:
        raise AppError(
            "CONFLICT",
            409,
            {
                "reason": (
                    f"归并任务 {latest.job_no} 已存在（状态："
                    f"{MERGE_JOB_STATUS_LABELS.get(latest.status, latest.status)}），"
                    "请在原任务上续跑；归并幂等，不会新增重复档案"
                ),
                "job_id": latest.id,
                "status": latest.status,
            },
        )
    job = MergeJob(
        job_no=_next_job_no(db),
        status=MergeJobStatus.PENDING.value,
        trigger_source=trigger_source,
        operator_email=operator_email,
        profiles_before=_count(db, ChannelProfile),
        members_before=_count(db, Member),
    )
    db.add(job)
    db.flush()
    return job


def run_job(db: Session, job_id: int, *, fail_after_batches: int | None = None) -> MergeJob:
    """执行（或断点续跑）一个归并任务，直到全部处理完成或抛错。

    fail_after_batches 仅供测试：处理指定批数后主动抛错，验证断点续跑。
    """
    job = get_job(db, job_id)
    if job.status == MergeJobStatus.COMPLETED.value:
        return job
    job.status = MergeJobStatus.RUNNING.value
    if job.started_at is None:
        job.started_at = utcnow()
    db.flush()

    batches_done = 0
    try:
        while True:
            handled = _process_batch(db, job)
            batches_done += 1
            db.commit()
            if fail_after_batches is not None and batches_done >= fail_after_batches:
                raise RuntimeError("injected failure for resume test")
            if handled < MERGE_BATCH_SIZE:
                break
        _finalize(db, job)
        db.commit()
    except Exception as exc:
        db.rollback()
        job = get_job(db, job_id)
        job.status = MergeJobStatus.FAILED.value
        job.error_message = str(exc)[:1000]
        db.commit()
        if isinstance(exc, RuntimeError) and str(exc) == "injected failure for resume test":
            return job
        raise
    return job


def _load_batch(db: Session, cursor: int) -> list[ChannelProfile]:
    return db.scalars(
        select(ChannelProfile)
        .where(ChannelProfile.id > cursor)
        .order_by(ChannelProfile.id.asc())
        .limit(MERGE_BATCH_SIZE)
    ).all()


def _process_batch(db: Session, job: MergeJob) -> int:
    """处理一批：把批内档案与库内同标识档案聚类后归并。返回本批读取条数。"""
    batch = _load_batch(db, job.last_cursor)
    if not batch:
        return 0

    # 收集批内档案的标识，预取库内全部同标识档案（含历史批已落库的主档），
    # 保证跨批传递连通与「重试不新增重复档案」。
    phones, ext_keys = set(), set()
    for p in batch:
        phone = merge_engine.normalize_phone(p.phone)
        if phone:
            phones.add(phone)
        etype, eid = merge_engine.normalize_external(p.external_type, p.external_id)
        if etype:
            ext_keys.add((etype, eid))

    related_q = select(ChannelProfile)
    conditions = []
    if phones:
        # 走规范化手机号索引，容忍空格/连字符等格式差异
        conditions.append(ChannelProfile.phone_norm.in_(phones))
    if ext_keys:
        for etype, eid in ext_keys:
            conditions.append(
                (ChannelProfile.external_type == etype) & (ChannelProfile.external_id == eid)
            )
    if conditions:
        from sqlalchemy import or_

        related = db.scalars(related_q.where(or_(*conditions))).all()
    else:
        related = []
    related_by_id = {p.id: p for p in related}
    for p in batch:
        related_by_id.setdefault(p.id, p)

    # 已被归并的档案直接跳过（幂等：重试/重复任务不会二次处理）
    candidate_ids = [pid for pid, p in sorted(related_by_id.items()) if p.status != "merged"]
    if not candidate_ids:
        job.last_cursor = max(p.id for p in batch)
        job.processed_profiles += len(batch)
        return len(batch)

    profiles = [_profile_payload(db, p) for p in (related_by_id[i] for i in candidate_ids)]
    plans = merge_engine.plan_merge(profiles)
    existing_pairs = {
        (r.merge_job_id, r.merged_profile_id)
        for r in db.scalars(
            select(MergeRecord).where(MergeRecord.merge_job_id == job.id)
        ).all()
    }

    for plan in plans:
        _apply_plan(db, job, plan, existing_pairs)

    job.last_cursor = max(p.id for p in batch)
    job.processed_profiles += len(batch)
    return len(batch)


def _profile_payload(db: Session, profile: ChannelProfile) -> dict:
    member = db.get(Member, profile.member_id)
    order_count = db.scalar(
        select(func.count()).select_from(Order).where(Order.member_id == profile.member_id)
    ) or 0
    return {
        "id": profile.id,
        "member_id": profile.member_id,
        "channel": profile.channel,
        "channel_member_no": profile.channel_member_no,
        "name": profile.name,
        "phone": profile.phone,
        "external_type": profile.external_type,
        "external_id": profile.external_id,
        "city": profile.city,
        "tier": profile.tier,
        "source_updated_at": profile.source_updated_at,
        "order_count": order_count,
        "joined_on": iso_date(member.joined_on) if member else "9999-12-31",
        "joined_on_sort": iso_date(member.joined_on) if member else "9999-12-31",
    }


def _apply_plan(db: Session, job: MergeJob, plan: dict, existing_pairs: set) -> None:
    # 主档稳定化：若算法选出的主档已在更早批次被归并进别的档案，则一路向上取其
    # 根主档，避免出现「主档又被别人归并」的链式结构，保证主档只有一层。
    root_profile = db.get(ChannelProfile, plan["master"]["id"])
    seen = set()
    while root_profile.merged_into_id is not None and root_profile.id not in seen:
        seen.add(root_profile.id)
        root_member = db.get(Member, root_profile.merged_into_id)
        root_profile = db.scalar(
            select(ChannelProfile)
            .where(ChannelProfile.member_id == root_member.id)
            .where(ChannelProfile.status == "active")
            .order_by(ChannelProfile.id)
            .limit(1)
        )
        if root_profile is None:
            break
    master_profile = root_profile
    master_member = db.get(Member, master_profile.member_id)
    values = merge_engine.chosen_field_values(plan)

    # 主档资料采用冲突取舍结果（冲突字段取最新来源，其余保留主档原值）
    if values.get("name"):
        master_member.name = values["name"]
    if values.get("city"):
        master_member.city = values["city"]
    if values.get("tier"):
        master_member.tier = values["tier"]
    if values.get("phone"):
        master_member.phone = values["phone"]
    master_profile.name = master_member.name
    master_profile.city = master_member.city
    master_profile.tier = master_member.tier
    master_profile.phone = master_member.phone
    master_profile.phone_norm = merge_engine.normalize_phone(master_member.phone)
    if values.get("external_id"):
        master_profile.external_id = values["external_id"]

    # 已经直接挂在该根主档下的档案不再重复产生归并记录
    groups_counted = False
    for merged_payload in plan["merged"] + (
        [plan["master"]] if plan["master"]["id"] != master_profile.id else []
    ):
        merged_profile = db.get(ChannelProfile, merged_payload["id"])
        if merged_profile.id == master_profile.id:
            continue
        pair = (job.id, merged_profile.id)
        if pair in existing_pairs:  # 幂等：已归并过则跳过
            continue
        if merged_profile.status == "merged" and merged_profile.merged_into_id == master_member.id:
            continue
        groups_counted = True
        merged_member = db.get(Member, merged_profile.member_id)
        orders_moved = _reassign(
            db, Order, Order.member_id, merged_member.id, master_member.id
        )
        coupons_moved = _reassign(
            db, CouponResponse, CouponResponse.member_id, merged_member.id, master_member.id
        )
        merged_member.merged_into_id = master_member.id
        merged_member.merged_at = utcnow()
        merged_profile.status = "merged"
        merged_profile.merged_into_id = master_member.id

        record = MergeRecord(
            merge_job_id=job.id,
            master_member_id=master_member.id,
            merged_member_id=merged_member.id,
            master_profile_id=master_profile.id,
            merged_profile_id=merged_profile.id,
            match_kind=plan["match_kind"],
            match_value=plan["match_value"],
            external_type=plan["external_type"],
            master_reason=plan["master_reason"],
            conflicts=plan["conflicts"],
            orders_moved=orders_moved,
            coupons_moved=coupons_moved,
        )
        db.add(record)
        existing_pairs.add(pair)
        job.records_merged += 1
        job.conflict_count += len(plan["conflicts"])

    if groups_counted:
        job.groups_merged += 1

    # 重算主档聚合（订单与券响应已合并到主档）
    recompute_master_aggregates(db, master_member)
    db.flush()


def _reassign(db: Session, model, member_id_attr, from_id: int, to_id: int) -> int:
    """把某会员名下的事实（订单/券响应）整体改挂到主档，返回移动条数。"""
    rows = db.scalars(select(model).where(member_id_attr == from_id)).all()
    for row in rows:
        row.member_id = to_id
    return len(rows)


def recompute_master_aggregates(db: Session, member: Member) -> None:
    """归并后按主档名下全部事实重算窗口指标、最近下单、客单价与历史券响应率。"""
    # 会话可能关闭 autoflush：先把订单/券响应的改挂落库，聚合查询才能读到合并后事实
    db.flush()
    as_of = db.scalar(select(func.max(Order.paid_at)))
    as_of_date = as_of.date() if isinstance(as_of, datetime) else local_now().date()
    since = datetime.combine(as_of_date - timedelta(days=RFM_WINDOW_DAYS), time.min)

    agg = db.execute(
        select(
            func.count(Order.id),
            func.coalesce(func.sum(Order.amount), 0.0),
            func.max(Order.paid_at),
        ).where(Order.member_id == member.id)
    ).one()
    total_orders, total_amount, last_order = int(agg[0] or 0), float(agg[1] or 0.0), agg[2]

    window_agg = db.execute(
        select(
            func.count(Order.id),
            func.coalesce(func.sum(Order.amount), 0.0),
        ).where(Order.member_id == member.id).where(Order.paid_at >= since)
    ).one()
    member.orders_window = int(window_agg[0] or 0)
    member.amount_window = round(float(window_agg[1] or 0.0), 2)
    member.last_order_at = last_order
    member.avg_ticket = round(total_amount / total_orders, 2) if total_orders else 0.0

    coupon_total = db.scalar(
        select(func.count()).select_from(CouponResponse).where(CouponResponse.member_id == member.id)
    ) or 0
    coupon_redeemed = db.scalar(
        select(func.count())
        .select_from(CouponResponse)
        .where(CouponResponse.member_id == member.id)
        .where(CouponResponse.status == "redeemed")
    ) or 0
    # 归并把多渠道券响应汇到主档：只要主档有名下券事实，就按合并后口径重算响应率
    if coupon_total:
        member.purchase_rate = round(coupon_redeemed / coupon_total, 6)


def _finalize(db: Session, job: MergeJob) -> None:
    job.status = MergeJobStatus.COMPLETED.value
    job.finished_at = utcnow()
    job.error_message = None
    active_profiles = db.scalar(
        select(func.count()).select_from(ChannelProfile).where(ChannelProfile.status == "active")
    ) or 0
    master_members = db.scalar(
        select(func.count()).select_from(Member).where(Member.merged_into_id.is_(None))
    ) or 0
    job.profiles_after = active_profiles
    job.members_after = master_members
    job.stats = {
        "duplicate_profiles_removed": job.profiles_before - active_profiles,
        "master_members": master_members,
        "identity_by_phone": db.scalar(
            select(func.count()).select_from(MergeRecord)
            .where(MergeRecord.merge_job_id == job.id, MergeRecord.match_kind == "phone")
        ) or 0,
        "identity_by_external": db.scalar(
            select(func.count()).select_from(MergeRecord)
            .where(MergeRecord.merge_job_id == job.id, MergeRecord.match_kind == "external")
        ) or 0,
    }
