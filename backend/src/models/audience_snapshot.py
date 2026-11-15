"""名单会员快照：名单生成那一刻的会员资料，按名单冻结。

已确认名单导出始终走这份快照（生成时的会员快照），不受后续归并影响；
待确认名单不冻结，导出改用归并后的主档。生成动作幂等（同名单不重复写快照）。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class AudienceSnapshot(Base):
    __tablename__ = "audience_snapshots"
    __table_args__ = (
        Index("ix_audience_snapshots_audience", "audience_id", "member_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    audience_id: Mapped[int] = mapped_column(ForeignKey("audiences.id", ondelete="CASCADE"), index=True)
    member_id: Mapped[int] = mapped_column(Integer, index=True)
    # 生成时冻结的资料（冗余，保证后续归并/改名不改变已确认名单）
    member_no: Mapped[str] = mapped_column(String(24))
    name: Mapped[str] = mapped_column(String(40))
    phone: Mapped[str] = mapped_column(String(20))
    city: Mapped[str] = mapped_column(String(24))
    tier: Mapped[str] = mapped_column(String(12))
    segment_code: Mapped[str | None] = mapped_column(String(20), nullable=True)
    composite_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    purchase_rate: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
