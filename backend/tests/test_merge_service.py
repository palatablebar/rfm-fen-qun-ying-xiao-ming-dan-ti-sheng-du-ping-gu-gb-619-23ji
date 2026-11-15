"""跨渠道归并的数据库级集成测试：用内存 SQLite 建全套表（无需 PostgreSQL）。

覆盖：任务生命周期、断点续跑、重试幂等不新增重复档案、订单/券响应合并、
冲突依据落库、主档稳定化（无链式归并）、名单快照与导出依据。
"""
from __future__ import annotations

import os

# 让 src.config.database 在导入前就指向内存 SQLite，避免依赖 psycopg2/PostgreSQL
os.environ.setdefault("DB_KIND", "sqlite")
os.environ.setdefault("SEED_DEMO", "false")

from datetime import date, datetime, timedelta  # noqa: E402

import pytest  # noqa: E402
from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.constants.enums import MergeJobStatus  # noqa: E402
from src.models import Base
from src.models.audience import Audience
from src.models.audience_snapshot import AudienceSnapshot
from src.models.campaign import Campaign
from src.models.channel_profile import ChannelProfile
from src.models.coupon_response import CouponResponse
from src.models.member import Member
from src.models.merge_job import MergeJob
from src.models.merge_record import MergeRecord
from src.models.order import Order
from src.services import merge_service, snapshot_service
from src.services.merge_engine import normalize_phone


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    # 与生产 SessionLocal 同构：关闭 autoflush、提交不过期，才能暴露隐式 flush 依赖
    Session = sessionmaker(bind=engine, future=True, autoflush=False, expire_on_commit=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _make_member(db, member_no, **over):
    member = Member(
        member_no=member_no,
        name=over.get("name", "王若溪"),
        phone=over.get("phone", "13812345678"),
        city=over.get("city", "上海"),
        tier=over.get("tier", "gold"),
        joined_on=over.get("joined_on", date(2025, 1, 1)),
        purchase_rate=0.05,
    )
    db.add(member)
    db.flush()
    return member


def _make_profile(db, member, channel, **over):
    phone = over.get("phone", member.phone)
    profile = ChannelProfile(
        member_id=member.id,
        channel=channel,
        channel_member_no=over.get("channel_member_no", f"{channel.upper()}-{member.member_no}"),
        name=over.get("name", member.name),
        phone=phone,
        phone_norm=normalize_phone(phone),
        external_type=over.get("external_type"),
        external_id=over.get("external_id"),
        city=over.get("city", member.city),
        tier=over.get("tier", member.tier),
        source_updated_at=over.get("source_updated_at", datetime(2026, 10, 1)),
    )
    db.add(profile)
    db.flush()
    return profile


def _make_order(db, member, channel, amount=30.0, days_ago=10):
    order = Order(
        order_no=f"O{member.id}-{channel}-{days_ago}-{amount}",
        member_id=member.id,
        store_code="MY-S01",
        channel=channel,
        amount=amount,
        paid_at=datetime(2026, 11, 14) - timedelta(days=days_ago),
    )
    db.add(order)
    return order


def _make_coupon(db, member, channel, status="sent"):
    coupon = CouponResponse(
        member_id=member.id,
        campaign_code="autumn-member-day",
        coupon_name="满减券",
        channel=channel,
        status=status,
        face_value=8.0,
        sent_at=datetime(2026, 10, 1),
        redeemed_at=datetime(2026, 10, 5) if status == "redeemed" else None,
    )
    db.add(coupon)
    return coupon


def _three_channel_person(db):
    """同一顾客的门店/小程序/外卖三份档案，订单与券响应被拆散。"""
    store_m = _make_member(db, "MY00001", name="王若溪", city="上海")
    mini_m = _make_member(db, "MYX00001", name="王小溪", phone="13812345678", city="杭州")
    deli_m = _make_member(db, "MYX00002", name="王若溪", phone="13812345678", city="上海", tier="silver")
    p1 = _make_profile(db, store_m, "store", source_updated_at=datetime(2026, 9, 1))
    p2 = _make_profile(db, mini_m, "miniapp", name="王小溪", city="杭州",
                       source_updated_at=datetime(2026, 11, 1))
    p3 = _make_profile(db, deli_m, "delivery", tier="silver",
                       source_updated_at=datetime(2026, 8, 1))
    _make_order(db, store_m, "store", amount=40, days_ago=5)
    _make_order(db, store_m, "store", amount=36, days_ago=20)
    _make_order(db, mini_m, "miniapp", amount=30, days_ago=8)
    _make_order(db, deli_m, "delivery", amount=28, days_ago=30)
    _make_coupon(db, store_m, "store", "redeemed")
    _make_coupon(db, mini_m, "miniapp", "sent")
    _make_coupon(db, deli_m, "delivery", "sent")
    db.flush()
    return store_m, mini_m, deli_m, (p1, p2, p3)


def test_merge_job_lifecycle_pending_to_completed(db):
    store_m, mini_m, deli_m, _ = _three_channel_person(db)
    job = merge_service.create_job(db, operator_email="admin@rfmseg.io")
    assert job.status == MergeJobStatus.PENDING.value
    assert job.profiles_before == 3
    assert job.members_before == 3

    merge_service.run_job(db, job.id)
    job = merge_service.get_job(db, job.id)
    assert job.status == MergeJobStatus.COMPLETED.value
    assert job.groups_merged == 1
    assert job.records_merged == 2
    assert job.profiles_after == 1
    assert job.members_after == 1
    assert job.finished_at is not None

    # 两个被合并档案指向同一主档，且只有一层（无链式归并）
    merged = db.scalars(select(Member).where(Member.merged_into_id.is_not(None))).all()
    assert {m.merged_into_id for m in merged} == {store_m.id}
    master = db.get(Member, store_m.id)
    assert master.merged_into_id is None


def test_orders_and_coupons_consolidated_into_master(db):
    store_m, mini_m, deli_m, _ = _three_channel_person(db)
    job = merge_service.create_job(db, operator_email="admin@rfmseg.io")
    merge_service.run_job(db, job.id)

    # 全部 4 笔订单、3 条券响应都归到主档
    order_count = db.scalar(
        select(func.count()).select_from(Order).where(Order.member_id == store_m.id)
    )
    assert order_count == 4
    coupon_count = db.scalar(
        select(func.count()).select_from(CouponResponse).where(CouponResponse.member_id == store_m.id)
    )
    assert coupon_count == 3
    # 被合并档案名下不再残留事实
    for mid in (mini_m.id, deli_m.id):
        assert db.scalar(select(func.count()).select_from(Order).where(Order.member_id == mid)) == 0
        assert db.scalar(select(func.count()).select_from(CouponResponse).where(CouponResponse.member_id == mid)) == 0

    # 主档券响应率按合并后事实重算（1 核销 / 3 发券）
    master = db.get(Member, store_m.id)
    assert abs(master.purchase_rate - 1 / 3) < 1e-6
    assert master.orders_window == 4


def test_conflict_evidence_persisted_with_source_and_time(db):
    store_m, mini_m, deli_m, _ = _three_channel_person(db)
    job = merge_service.create_job(db, operator_email="admin@rfmseg.io")
    merge_service.run_job(db, job.id)

    record = db.scalars(select(MergeRecord).where(MergeRecord.merge_job_id == job.id)).first()
    conflict_fields = {c["field"] for c in record.conflicts}
    # 姓名/城市/等级存在冲突
    assert {"name", "city", "tier"} <= conflict_fields
    name_conflict = next(c for c in record.conflicts if c["field"] == "name")
    # 最新来源（小程序 2026-11-01）胜出
    assert name_conflict["chosen"]["value"] == "王小溪"
    assert name_conflict["chosen"]["channel"] == "miniapp"
    assert len(name_conflict["sources"]) >= 2
    # 主档姓名已更新为冲突取舍结果
    assert db.get(Member, store_m.id).name == "王小溪"


def test_retry_is_idempotent_and_creates_no_duplicates(db):
    _three_channel_person(db)
    job = merge_service.create_job(db, operator_email="admin@rfmseg.io")
    merge_service.run_job(db, job.id)
    records_after_first = db.scalar(select(func.count()).select_from(MergeRecord))
    profiles_merged_first = db.scalar(
        select(func.count()).select_from(ChannelProfile).where(ChannelProfile.status == "merged")
    )

    # 再跑一次同一任务：直接返回 completed，不新增任何归并记录/档案
    merge_service.run_job(db, job.id)
    assert db.scalar(select(func.count()).select_from(MergeRecord)) == records_after_first
    assert (
        db.scalar(select(func.count()).select_from(ChannelProfile).where(ChannelProfile.status == "merged"))
        == profiles_merged_first
    )
    assert db.scalar(select(func.count()).select_from(ChannelProfile)) == 3

    # 已完成时不能再新建并行任务
    with pytest.raises(Exception):
        merge_service.create_job(db, operator_email="admin@rfmseg.io")


def test_resume_from_last_cursor_after_failure(db, monkeypatch):
    # 构造 6 份档案（2 组三人），把批大小压到 2，制造中途失败
    _three_channel_person(db)
    other_a = _make_member(db, "MY00002", name="李慕白", phone="13900001111")
    other_b = _make_member(db, "MYX00003", name="李慕白", phone="13900001111")
    other_c = _make_member(db, "MYX00004", name="李白", phone="13900001111")
    _make_profile(db, other_a, "store")
    _make_profile(db, other_b, "miniapp")
    _make_profile(db, other_c, "delivery")
    db.flush()

    monkeypatch.setattr("src.services.merge_service.MERGE_BATCH_SIZE", 2)
    job = merge_service.create_job(db, operator_email="admin@rfmseg.io")
    merge_service.run_job(db, job.id, fail_after_batches=1)
    job = merge_service.get_job(db, job.id)
    assert job.status == MergeJobStatus.FAILED.value
    assert job.last_cursor == 2
    assert job.error_message

    # 续跑：从游标 2 之后继续，最终完成且不产生重复
    merge_service.run_job(db, job.id)
    job = merge_service.get_job(db, job.id)
    assert job.status == MergeJobStatus.COMPLETED.value
    assert job.profiles_after == 2  # 两个自然人各一个主档
    assert job.records_merged == 4  # 每组合并 2 份，共 4 条
    assert db.scalar(select(func.count()).select_from(MergeRecord)) == 4
    # 没有链式归并：所有被合并档案的主档 merged_into_id 为空
    masters = {m.merged_into_id for m in db.scalars(select(Member).where(Member.merged_into_id.is_not(None))).all()}
    for master_id in masters:
        assert db.get(Member, master_id).merged_into_id is None


def test_external_identity_merge_with_different_phones(db):
    a = _make_member(db, "MY00010", name="陈默", phone="13700000001")
    b = _make_member(db, "MYX00010", name="陈默", phone="13700000002")
    _make_profile(db, a, "miniapp", external_type="alipay_uid", external_id="ab-99")
    _make_profile(db, b, "delivery", phone="13700000002",
                  external_type="alipay_uid", external_id="ab-99")
    db.flush()
    job = merge_service.create_job(db, operator_email="admin@rfmseg.io")
    merge_service.run_job(db, job.id)
    record = db.scalar(select(MergeRecord).where(MergeRecord.merge_job_id == job.id))
    assert record.match_kind == "external"
    assert record.external_type == "alipay_uid"
    assert record.match_value == "ab-99"
    assert db.get(Member, b.id).merged_into_id == a.id


def test_audience_snapshot_freezes_confirmed_but_pending_uses_master(db):
    store_m, mini_m, deli_m, _ = _three_channel_person(db)
    # 给主档一个分群，供名单生成
    store_m.segment_code = "champion"
    store_m.composite_score = 4.5
    campaign = Campaign(
        code="c1", name="测试活动", channel="小程序", budget=1000.0, status="active",
        starts_on=date(2026, 11, 18), ends_on=date(2026, 11, 24),
        reach_cost=0.15, orders_per_converter=1.4, tail_factor=0.6, target_segments=[],
    )
    db.add(campaign)
    db.flush()
    audience = Audience(
        audience_no="A1", campaign_id=campaign.id, status="pending", budget=1000.0,
        member_count=1, allocation=[{"status": "allocated", "segment_code": "champion", "allocated": 1}],
    )
    db.add(audience)
    db.flush()
    written = snapshot_service.snapshot_audience_members(db, audience)
    assert written == 1
    # 幂等：再调不重复写
    assert snapshot_service.snapshot_audience_members(db, audience) == 1
    snap = db.scalar(select(AudienceSnapshot).where(AudienceSnapshot.audience_id == audience.id))
    original_name = snap.name

    # 归并后被合并档案不再是主档；待确认名单导出依据为 master
    job = merge_service.create_job(db, operator_email="admin@rfmseg.io")
    merge_service.run_job(db, job.id)
    # 快照内容不随归并改变
    db.expire_all()
    snap = db.scalar(select(AudienceSnapshot).where(AudienceSnapshot.audience_id == audience.id))
    assert snap.name == original_name

    # 待确认走主档查询、已确认走快照查询
    pending_masters = db.scalars(
        select(Member).where(Member.segment_code == "champion").where(Member.merged_into_id.is_(None))
    ).all()
    assert len(pending_masters) == 1
    assert pending_masters[0].id == store_m.id
