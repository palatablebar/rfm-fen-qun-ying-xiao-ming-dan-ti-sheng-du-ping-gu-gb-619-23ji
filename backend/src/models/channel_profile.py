"""跨渠道会员档案：门店 / 小程序 / 外卖各一份的原始档案，归并的输入来源。

每个渠道档案挂在一个 member 行上（归并前 1:1）；归并后被归并档案的 member 行
通过 merged_into_id 指向主档 member，档案本身置为 merged，不物理删除。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from src.models import Base
from src.utils.timeutil import utcnow


class ChannelProfile(Base):
    __tablename__ = "channel_profiles"
    __table_args__ = (
        Index("ix_channel_profiles_external", "external_type", "external_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), index=True)
    channel: Mapped[str] = mapped_column(String(16), index=True)
    # 渠道侧自己的会员号（与 members.member_no 独立，便于溯源）
    channel_member_no: Mapped[str] = mapped_column(String(40), index=True)
    name: Mapped[str] = mapped_column(String(40))
    phone: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # 规范化手机号（仅数字、过短为空），用于跨渠道同号匹配的索引查询
    phone_norm: Mapped[str | None] = mapped_column(String(20), nullable=True, index=True)
    external_type: Mapped[str | None] = mapped_column(String(24), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    city: Mapped[str] = mapped_column(String(24), default="上海")
    tier: Mapped[str] = mapped_column(String(12), default="normal")
    status: Mapped[str] = mapped_column(String(16), default="active", index=True)
    # 归并后指向主档 member；未归并为 NULL
    merged_into_id: Mapped[int | None] = mapped_column(
        ForeignKey("members.id", ondelete="SET NULL"), nullable=True, index=True
    )
    source_updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)
