"""历史券响应事实：会员级发券/核销记录，归并时随主档合并。

归并只改 member_id（被合并档案 → 主档），不删事实；归并后主档的历史券响应
由这些行聚合得到，订单与券响应不再被渠道拆散。
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from src.models import Base
from src.utils.timeutil import utcnow


class CouponResponse(Base):
    __tablename__ = "coupon_responses"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    member_id: Mapped[int] = mapped_column(ForeignKey("members.id", ondelete="CASCADE"), index=True)
    campaign_code: Mapped[str] = mapped_column(String(40), index=True)
    coupon_name: Mapped[str] = mapped_column(String(80))
    channel: Mapped[str] = mapped_column(String(16), default="miniapp")
    status: Mapped[str] = mapped_column(String(16), default="sent")  # sent/redeemed
    face_value: Mapped[float] = mapped_column(default=0.0)
    sent_at: Mapped[datetime] = mapped_column(DateTime)
    redeemed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
