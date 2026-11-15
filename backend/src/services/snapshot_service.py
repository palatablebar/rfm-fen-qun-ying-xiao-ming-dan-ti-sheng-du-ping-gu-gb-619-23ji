"""名单会员快照：生成时冻结会员资料；已确认名单导出走快照，待确认走归并后主档。

口径（与业务需求一致）：
- 名单生成时把当时入名单的会员资料写入 audience_snapshots（幂等，重复生成不重写）；
- 已确认/已推送名单：导出始终按生成时的会员快照，归并不改变已确认名单；
- 待确认名单：不冻结导出，改用归并后的主档（merged_into_id 为空的 member）。
"""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.models.audience import Audience
from src.models.audience_snapshot import AudienceSnapshot
from src.models.member import Member


def snapshot_audience_members(db: Session, audience: Audience) -> int:
    """按名单 allocation 的分群配额冻结会员资料。幂等：已有快照则直接返回条数。"""
    existing = db.scalar(
        select(func.count()).select_from(AudienceSnapshot).where(
            AudienceSnapshot.audience_id == audience.id
        )
    ) or 0
    if existing:
        return existing

    written = 0
    for row in audience.allocation or []:
        if row.get("status") != "allocated" or not row.get("allocated"):
            continue
        members = db.scalars(
            select(Member)
            .where(Member.segment_code == row["segment_code"])
            .order_by(Member.composite_score.desc().nullslast(), Member.id)
            .limit(int(row["allocated"]))
        ).all()
        for member in members:
            db.add(
                AudienceSnapshot(
                    audience_id=audience.id,
                    member_id=member.id,
                    member_no=member.member_no,
                    name=member.name,
                    phone=member.phone,
                    city=member.city,
                    tier=member.tier,
                    segment_code=member.segment_code,
                    composite_score=member.composite_score,
                    purchase_rate=member.purchase_rate or 0.0,
                )
            )
            written += 1
    db.flush()
    return written


def has_snapshot(db: Session, audience_id: int) -> bool:
    return bool(
        db.scalar(
            select(func.count()).select_from(AudienceSnapshot).where(
                AudienceSnapshot.audience_id == audience_id
            )
        )
    )
