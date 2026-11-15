"""归并任务：待处理 → 处理中 → 已完成 / 失败（可续跑）。

断点续跑：last_cursor 记录已处理到的 channel_profiles.id；重试只从游标之后继续，
已归并的档案带幂等校验，重试不新增重复档案、不重复产生归并记录。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class MergeJob(Base):
    __tablename__ = "merge_jobs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_no: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    trigger_source: Mapped[str] = mapped_column(String(16), default="manual")  # manual/schedule/seed
    operator_email: Mapped[str] = mapped_column(String(160), default="system")
    # 断点游标：已处理到的 channel_profiles.id
    last_cursor: Mapped[int] = mapped_column(Integer, default=0)
    # 归并前后人数（完成时落快照，用于页面/导出核对）
    profiles_before: Mapped[int] = mapped_column(Integer, default=0)
    profiles_after: Mapped[int] = mapped_column(Integer, default=0)
    members_before: Mapped[int] = mapped_column(Integer, default=0)
    members_after: Mapped[int] = mapped_column(Integer, default=0)
    groups_merged: Mapped[int] = mapped_column(Integer, default=0)
    records_merged: Mapped[int] = mapped_column(Integer, default=0)
    conflict_count: Mapped[int] = mapped_column(Integer, default=0)
    processed_profiles: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
