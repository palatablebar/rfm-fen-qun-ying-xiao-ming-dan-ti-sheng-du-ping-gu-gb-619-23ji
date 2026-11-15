"""ORM 模型聚合导出（Base 用于 create_all）。"""
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


from src.models.user import User  # noqa: E402
from src.models.audit import AuditLog  # noqa: E402
from src.models.member import Member  # noqa: E402
from src.models.order import Order  # noqa: E402
from src.models.segment import Segment  # noqa: E402
from src.models.campaign import Campaign  # noqa: E402
from src.models.audience import Audience  # noqa: E402
from src.models.channel_profile import ChannelProfile  # noqa: E402
from src.models.coupon_response import CouponResponse  # noqa: E402
from src.models.merge_job import MergeJob  # noqa: E402
from src.models.merge_record import MergeRecord  # noqa: E402
from src.models.audience_snapshot import AudienceSnapshot  # noqa: E402

__all__ = [
    "Base",
    "User",
    "AuditLog",
    "Member",
    "Order",
    "Segment",
    "Campaign",
    "Audience",
    "ChannelProfile",
    "CouponResponse",
    "MergeJob",
    "MergeRecord",
    "AudienceSnapshot",
]
