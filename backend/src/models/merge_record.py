"""归并记录：每个被合并档案一行，可核对主档 / 被合并档案 / 识别依据 / 冲突依据。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class MergeRecord(Base):
    __tablename__ = "merge_records"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    merge_job_id: Mapped[int] = mapped_column(ForeignKey("merge_jobs.id", ondelete="CASCADE"), index=True)
    master_member_id: Mapped[int] = mapped_column(Integer, index=True)
    merged_member_id: Mapped[int] = mapped_column(Integer, index=True)
    master_profile_id: Mapped[int] = mapped_column(Integer)
    merged_profile_id: Mapped[int] = mapped_column(Integer)
    # 识别依据：phone / external；external 时带 external_type
    match_kind: Mapped[str] = mapped_column(String(16))
    match_value: Mapped[str | None] = mapped_column(String(120), nullable=True)
    external_type: Mapped[str | None] = mapped_column(String(24), nullable=True)
    # 主档归属原因（订单量/最早入会等确定性排序说明）
    master_reason: Mapped[str] = mapped_column(String(240), default="")
    # 冲突字段的取舍依据：[{field, chosen:{value,channel,source_updated_at}, dropped:[...]}]
    conflicts: Mapped[list] = mapped_column(JSON, default=list)
    orders_moved: Mapped[int] = mapped_column(Integer, default=0)
    coupons_moved: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
