"""CSV 导出：会员名单 / 分群表 / 名单明细（UTF-8 BOM）。"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from src.constants.enums import (
    AUDIENCE_STATUS_LABELS,
    CAMPAIGN_STATUS_LABELS,
    MEMBER_TIER_LABELS,
    SEGMENT_LABELS,
)
from src.constants.limits import MAX_EXPORT_ROWS
from src.models.audience import Audience
from src.models.audience_snapshot import AudienceSnapshot
from src.models.campaign import Campaign
from src.models.member import Member
from src.models.segment import Segment
from src.utils.csv_export import to_csv


def _stamp(value) -> str:
    return value.strftime("%Y-%m-%d %H:%M") if value else ""


def members_csv(db: Session) -> bytes:
    rows = db.scalars(
        select(Member)
        .where(Member.merged_into_id.is_(None))
        .order_by(Member.composite_score.desc().nullslast(), Member.id)
        .limit(MAX_EXPORT_ROWS)
    ).all()
    headers = [
        "会员号", "姓名", "手机号", "城市", "会员等级", "入会日期", "最近下单",
        "R(天)", "窗口内单数", "窗口内金额", "R分", "F分", "M分", "综合分",
        "分群", "历史券响应率", "客单价",
    ]
    payload = [
        [
            item.member_no, item.name, item.phone, item.city,
            MEMBER_TIER_LABELS.get(item.tier, item.tier),
            item.joined_on.isoformat() if item.joined_on else "",
            _stamp(item.last_order_at),
            item.r_days if item.r_days is not None else "",
            item.orders_window,
            f"{item.amount_window or 0:.2f}",
            item.r_score if item.r_score is not None else "",
            item.f_score if item.f_score is not None else "",
            item.m_score if item.m_score is not None else "",
            f"{item.composite_score or 0:.2f}",
            SEGMENT_LABELS.get(item.segment_code or "", ""),
            f"{(item.purchase_rate or 0) * 100:.2f}%",
            f"{item.avg_ticket or 0:.2f}",
        ]
        for item in rows
    ]
    return to_csv(headers, payload)


def segments_csv(db: Session) -> bytes:
    rows = db.scalars(select(Segment).order_by(Segment.sort_order, Segment.id)).all()
    headers = [
        "分群编码", "分群", "规则", "人数", "占比", "平均R分", "平均F分", "平均M分",
        "平均R天数", "人均窗口金额", "购买率", "lift", "券面额", "核心盘占比", "启用",
    ]
    payload = [
        [
            item.code, item.name, item.rule_text, item.member_count, f"{(item.share or 0) * 100:.2f}%",
            f"{item.avg_r_score or 0:.2f}", f"{item.avg_f_score or 0:.2f}", f"{item.avg_m_score or 0:.2f}",
            f"{item.avg_r_days or 0:.1f}", f"{item.avg_amount_window or 0:.2f}",
            f"{(item.purchase_rate or 0) * 100:.2f}%", f"{item.lift or 0:.4f}",
            f"{item.coupon_face or 0:.2f}", f"{item.core_share or 0:.2f}",
            "启用" if item.enabled else "停用",
        ]
        for item in rows
    ]
    return to_csv(headers, payload)


def audience_csv(db: Session, audience_id: int | None = None) -> bytes:
    """名单明细导出。

    已确认/已推送名单：按生成时冻结的会员快照（audience_snapshots）导出；
    待确认名单：改用归并后的会员主档（merged_into_id 为空），避免重复触达。
    """
    stmt = select(Audience).order_by(Audience.id.desc())
    if audience_id:
        stmt = select(Audience).where(Audience.id == audience_id)
    audiences = db.scalars(stmt).all()
    campaigns = {item.id: item for item in db.scalars(select(Campaign)).all()}
    headers = [
        "名单号", "活动", "名单状态", "导出依据", "分群", "分群lift", "会员号", "姓名", "手机号", "城市",
        "会员等级", "R分", "F分", "M分", "综合分", "历史券响应率", "该群分到人数", "该群预算",
        "该群预计增量单量", "该群ROI",
    ]
    payload: list[list] = []
    for audience in audiences:
        campaign = campaigns.get(audience.campaign_id)
        campaign_name = campaign.name if campaign else ""
        status_label = AUDIENCE_STATUS_LABELS.get(audience.status, audience.status)
        frozen = audience.status in {"confirmed", "pushed"}
        basis_label = "生成时会员快照" if frozen else "归并后会员主档"
        for row in audience.allocation or []:
            if row.get("status") != "allocated" or not row.get("allocated"):
                continue
            if frozen:
                lines = _snapshot_lines(db, audience, row)
            else:
                lines = _master_lines(db, row)
            for line in lines:
                payload.append([
                    audience.audience_no, campaign_name, status_label, basis_label,
                    SEGMENT_LABELS.get(row["segment_code"], row["segment_code"]),
                    f"{row.get('lift', 0):.4f}",
                    *line,
                    row.get("allocated", 0), f"{row.get('budget', 0):.2f}",
                    row.get("inc_orders", 0), f"{row.get('roi', 0):.2f}",
                ])
                if len(payload) >= MAX_EXPORT_ROWS:
                    return to_csv(headers, payload)
    return to_csv(headers, payload)


def _snapshot_lines(db: Session, audience: Audience, row: dict) -> list[list]:
    snaps = db.scalars(
        select(AudienceSnapshot)
        .where(AudienceSnapshot.audience_id == audience.id)
        .where(AudienceSnapshot.segment_code == row["segment_code"])
        .order_by(AudienceSnapshot.composite_score.desc().nullslast(), AudienceSnapshot.member_id)
        .limit(int(row["allocated"]))
    ).all()
    if not snaps:  # 兼容历史无快照名单：回退到当前主档
        return _master_lines(db, row)
    return [
        [
            s.member_no, s.name, s.phone, s.city,
            MEMBER_TIER_LABELS.get(s.tier, s.tier),
            "", "", "",
            f"{s.composite_score or 0:.2f}",
            f"{(s.purchase_rate or 0) * 100:.2f}%",
        ]
        for s in snaps
    ]


def _master_lines(db: Session, row: dict) -> list[list]:
    members = db.scalars(
        select(Member)
        .where(Member.segment_code == row["segment_code"])
        .where(Member.merged_into_id.is_(None))
        .order_by(Member.composite_score.desc().nullslast(), Member.id)
        .limit(int(row["allocated"]))
    ).all()
    return [
        [
            member.member_no, member.name, member.phone, member.city,
            MEMBER_TIER_LABELS.get(member.tier, member.tier),
            member.r_score if member.r_score is not None else "",
            member.f_score if member.f_score is not None else "",
            member.m_score if member.m_score is not None else "",
            f"{member.composite_score or 0:.2f}",
            f"{(member.purchase_rate or 0) * 100:.2f}%",
        ]
        for member in members
    ]


def merge_records_csv(db: Session, job_id: int | None = None) -> bytes:
    """归并结果导出：每个被合并档案一行，含主档/被合并档案/识别与冲突依据/前后人数。"""
    from src.constants.enums import MERGE_JOB_STATUS_LABELS
    from src.models.channel_profile import ChannelProfile
    from src.models.merge_job import MergeJob
    from src.models.merge_record import MergeRecord

    stmt = select(MergeRecord).order_by(MergeRecord.id).limit(MAX_EXPORT_ROWS)
    if job_id:
        stmt = stmt.where(MergeRecord.merge_job_id == job_id)
    records = db.scalars(stmt).all()
    member_ids = {r.master_member_id for r in records} | {r.merged_member_id for r in records}
    profile_ids = {r.master_profile_id for r in records} | {r.merged_profile_id for r in records}
    members = {m.id: m for m in db.scalars(select(Member).where(Member.id.in_(member_ids))).all()} if member_ids else {}
    profiles = (
        {p.id: p for p in db.scalars(select(ChannelProfile).where(ChannelProfile.id.in_(profile_ids))).all()}
        if profile_ids
        else {}
    )
    jobs = {j.id: j for j in db.scalars(select(MergeJob)).all()}
    headers = [
        "归并任务", "任务状态", "识别依据", "匹配值", "主档会员号", "主档姓名", "主档渠道",
        "被合并会员号", "被合并姓名", "被合并渠道", "主档归属原因", "冲突字段数",
        "冲突取舍依据", "移动订单数", "移动券响应数", "归并前档案数", "归并后档案数",
    ]
    payload: list[list] = []
    for r in records:
        job = jobs.get(r.merge_job_id)
        master, merged = members.get(r.master_member_id), members.get(r.merged_member_id)
        mp, sp = profiles.get(r.master_profile_id), profiles.get(r.merged_profile_id)
        conflict_text = "；".join(
            f"{c['field']}={c['chosen']['value']}(来源 {c['chosen']['channel']} "
            f"{c['chosen'].get('source_updated_at') or ''})"
            for c in (r.conflicts or [])
        )
        payload.append([
            job.job_no if job else r.merge_job_id,
            MERGE_JOB_STATUS_LABELS.get(job.status, job.status) if job else "",
            "手机号一致" if r.match_kind == "phone" else "外部账号一致",
            r.match_value or "",
            master.member_no if master else "", master.name if master else "",
            mp.channel if mp else "",
            merged.member_no if merged else "", merged.name if merged else "",
            sp.channel if sp else "",
            r.master_reason, len(r.conflicts or []), conflict_text,
            r.orders_moved, r.coupons_moved,
            job.profiles_before if job else "", job.profiles_after if job else "",
        ])
    return to_csv(headers, payload)


def campaigns_csv(db: Session) -> bytes:
    rows = db.scalars(select(Campaign).order_by(Campaign.id)).all()
    headers = ["活动编码", "活动", "渠道", "预算", "状态", "开始", "结束", "触达成本", "人均增量单量", "折减系数"]
    payload = [
        [
            item.code, item.name, item.channel, f"{item.budget:.2f}",
            CAMPAIGN_STATUS_LABELS.get(item.status, item.status),
            item.starts_on.isoformat() if item.starts_on else "",
            item.ends_on.isoformat() if item.ends_on else "",
            f"{item.reach_cost:.4f}", f"{item.orders_per_converter:.4f}", f"{item.tail_factor:.2f}",
        ]
        for item in rows
    ]
    return to_csv(headers, payload)
