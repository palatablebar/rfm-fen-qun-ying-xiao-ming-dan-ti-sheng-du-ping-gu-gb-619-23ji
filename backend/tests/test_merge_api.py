"""跨渠道归并 HTTP 接口集成测试：FastAPI TestClient + 单连接内存 SQLite。

覆盖：三角色 RBAC（viewer 403、未登录 401）、任务生命周期接口、断点续跑、
归并看板/记录/主档详情、归并结果导出，以及已确认/待确认名单的不同导出依据。
"""
from __future__ import annotations

import os

os.environ.setdefault("DB_KIND", "sqlite")
os.environ.setdefault("SEED_DEMO", "false")

from datetime import date, datetime  # noqa: E402

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import create_engine, func, select  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402
from sqlalchemy.orm import sessionmaker  # noqa: E402

from src.config import database as dbmod  # noqa: E402
from src.config.app import create_app  # noqa: E402
from src.config.database import get_db  # noqa: E402
from src.models import Base  # noqa: E402
from src.models.channel_profile import ChannelProfile  # noqa: E402
from src.models.member import Member  # noqa: E402
from src.services.merge_engine import normalize_phone  # noqa: E402
from src.utils.security import create_access_token  # noqa: E402


@pytest.fixture()
def client():
    engine = create_engine(
        "sqlite://",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, future=True, autoflush=False, expire_on_commit=False)
    dbmod.engine = engine
    dbmod.SessionLocal.configure(bind=engine)

    app = create_app()

    def _override_get_db():
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = _override_get_db
    with TestClient(app) as c:
        c._session_factory = TestingSession  # type: ignore[attr-defined]
        yield c
    app.dependency_overrides.clear()
    engine.dispose()


def _auth(role: str) -> dict:
    token, _ = create_access_token(f"{role}@rfmseg.io", role)
    return {"Authorization": f"Bearer {token}"}


def _seed_duplicate(client):
    db = client._session_factory()
    try:
        master = Member(member_no="MY00001", name="王若溪", phone="13812345678", city="上海",
                        tier="gold", joined_on=date(2025, 1, 1), purchase_rate=0.05)
        dup = Member(member_no="MYX00001", name="王小溪", phone="138 1234 5678", city="杭州",
                     tier="gold", joined_on=date(2025, 1, 1), purchase_rate=0.05)
        db.add_all([master, dup])
        db.flush()
        db.add_all([
            ChannelProfile(member_id=master.id, channel="store", channel_member_no="S-MY00001",
                           name="王若溪", phone="13812345678", phone_norm=normalize_phone("13812345678"),
                           city="上海", tier="gold", source_updated_at=datetime(2026, 9, 1)),
            ChannelProfile(member_id=dup.id, channel="miniapp", channel_member_no="M-MYX00001",
                           name="王小溪", phone="138 1234 5678", phone_norm=normalize_phone("138 1234 5678"),
                           city="杭州", tier="gold", source_updated_at=datetime(2026, 11, 1)),
        ])
        db.commit()
    finally:
        db.close()


def test_overview_requires_login_and_reader_roles(client):
    # 未登录 401
    assert client.get("/api/merge/overview").status_code == 401
    # viewer 可读
    r = client.get("/api/merge/overview", headers=_auth("viewer"))
    assert r.status_code == 200
    data = r.json()
    assert {"profiles_total", "members_master", "members_merged", "before_count", "after_count"} <= set(data)


def test_viewer_cannot_start_merge_but_admin_can_and_job_completes(client):
    _seed_duplicate(client)
    # viewer 发起归并 → 真实 403
    r = client.post("/api/merge/jobs", json={}, headers=_auth("viewer"))
    assert r.status_code == 403
    assert r.json()["code"] == "FORBIDDEN"

    # analyst 可发起
    r = client.post("/api/merge/jobs", json={}, headers=_auth("analyst"))
    assert r.status_code == 201, r.text
    job = r.json()
    assert job["status"] == "completed"
    assert job["profiles_before"] == 2
    assert job["profiles_after"] == 1
    assert job["members_before"] == 2 and job["members_after"] == 1
    assert job["records_merged"] == 1


def test_duplicate_start_returns_conflict_and_retry_is_idempotent(client):
    _seed_duplicate(client)
    first = client.post("/api/merge/jobs", json={}, headers=_auth("admin")).json()
    # 再次发起 → 409，引导在原任务续跑
    r = client.post("/api/merge/jobs", json={}, headers=_auth("admin"))
    assert r.status_code == 409
    assert r.json()["code"] == "CONFLICT"
    # 对已完成任务续跑：不新增记录
    r2 = client.post(f"/api/merge/jobs/{first['id']}/retry", headers=_auth("admin"))
    assert r2.status_code == 200
    assert r2.json()["status"] == "completed"
    recs = client.get("/api/merge/records", headers=_auth("admin")).json()
    assert recs["total"] == 1


def test_records_filters_and_master_detail(client):
    _seed_duplicate(client)
    client.post("/api/merge/jobs", json={}, headers=_auth("admin"))
    recs = client.get("/api/merge/records?match_kind=phone", headers=_auth("admin")).json()
    assert recs["total"] == 1
    row = recs["items"][0]
    assert row["match_kind"] == "phone"
    assert row["master"]["member_no"] == "MY00001"
    assert row["merged"]["member_no"] == "MYX00001"
    # 冲突依据带来源与时间
    name_conflict = next(c for c in row["conflicts"] if c["field"] == "name")
    assert name_conflict["chosen"]["value"] == "王小溪"
    assert len(name_conflict["sources"]) == 2

    detail = client.get(f"/api/merge/masters/{row['master']['id']}", headers=_auth("admin")).json()
    assert detail["master"]["member_no"] == "MY00001"
    assert len(detail["merged_members"]) == 1
    assert {p["channel"] for p in detail["profiles"]} == {"store", "miniapp"}
    # 被合并档案 id 也能归一到主档详情
    detail2 = client.get(f"/api/merge/masters/{row['merged']['id']}", headers=_auth("admin")).json()
    assert detail2["master"]["id"] == detail["master"]["id"]


def test_jobs_list_runs_pending_running_completed_lifecycle(client):
    _seed_duplicate(client)
    job = client.post("/api/merge/jobs", json={}, headers=_auth("admin")).json()
    listed = client.get("/api/merge/jobs", headers=_auth("viewer")).json()
    assert listed["total"] == 1
    one = client.get(f"/api/merge/jobs/{job['id']}", headers=_auth("viewer")).json()
    assert one["job_no"] == job["job_no"]
    assert one["status"] == "completed"
    # 状态过滤
    assert client.get("/api/merge/jobs?status=failed", headers=_auth("viewer")).json()["total"] == 0
    assert client.get("/api/merge/jobs?status=completed", headers=_auth("viewer")).json()["total"] == 1


def test_merge_records_export_rbac_and_content(client):
    _seed_duplicate(client)
    client.post("/api/merge/jobs", json={}, headers=_auth("admin"))
    # viewer 不能导出 → 403
    assert client.get("/api/exports/merge-records.csv", headers=_auth("viewer")).status_code == 403
    r = client.get("/api/exports/merge-records.csv", headers=_auth("analyst"))
    assert r.status_code == 200
    text = r.content.decode("utf-8-sig")
    assert "归并任务" in text and "主档会员号" in text and "归并前档案数" in text
    assert "MY00001" in text and "MYX00001" in text


def test_members_list_defaults_to_masters_only(client):
    _seed_duplicate(client)
    client.post("/api/merge/jobs", json={}, headers=_auth("admin"))
    # 默认只返回主档
    data = client.get("/api/members", headers=_auth("admin")).json()
    assert data["total"] == 1
    assert data["items"][0]["member_no"] == "MY00001"
    assert data["items"][0]["is_master"] is True
    # 显式带出被合并档案
    all_data = client.get("/api/members?include_merged=true", headers=_auth("admin")).json()
    assert all_data["total"] == 2


def test_audience_export_uses_snapshot_for_confirmed_and_master_for_pending(client):
    from src.models.audience import Audience
    from src.models.audience_snapshot import AudienceSnapshot
    from src.models.campaign import Campaign
    from src.models.segment import Segment

    db = client._session_factory()
    try:
        master = Member(member_no="MY00001", name="主档名", phone="13812345678", city="上海",
                        tier="gold", joined_on=date(2025, 1, 1), segment_code="champion",
                        composite_score=4.5, purchase_rate=0.05)
        dup = Member(member_no="MYX00001", name="重复名", phone="13812345678", city="杭州",
                     tier="gold", joined_on=date(2025, 1, 1), segment_code="champion",
                     composite_score=4.0, purchase_rate=0.05)
        db.add_all([master, dup])
        db.flush()
        db.add_all([
            ChannelProfile(member_id=master.id, channel="store", channel_member_no="S-1",
                           name="主档名", phone="13812345678", phone_norm="13812345678",
                           city="上海", tier="gold", source_updated_at=datetime(2026, 9, 1)),
            ChannelProfile(member_id=dup.id, channel="miniapp", channel_member_no="M-1",
                           name="重复名", phone="13812345678", phone_norm="13812345678",
                           city="杭州", tier="gold", source_updated_at=datetime(2026, 11, 1)),
        ])
        db.add(Segment(code="champion", name="重要价值", sort_order=0, member_count=2, enabled=True))
        campaign = Campaign(code="c1", name="活动", channel="小程序", budget=1000.0, status="active",
                            starts_on=date(2026, 11, 18), ends_on=date(2026, 11, 24),
                            reach_cost=0.15, orders_per_converter=1.4, tail_factor=0.6, target_segments=[])
        db.add(campaign)
        db.flush()
        alloc = [{"status": "allocated", "segment_code": "champion", "allocated": 2, "lift": 2.0}]
        pending = Audience(audience_no="AP1", campaign_id=campaign.id, status="pending",
                           member_count=2, allocation=alloc)
        confirmed = Audience(audience_no="AC1", campaign_id=campaign.id, status="confirmed",
                             member_count=2, allocation=alloc)
        db.add_all([pending, confirmed])
        db.flush()
        # 两份名单都在「生成时」冻结快照（快照里保留重复档案 MYX00001）
        for aud in (pending, confirmed):
            for m in (master, dup):
                db.add(AudienceSnapshot(audience_id=aud.id, member_id=m.id, member_no=m.member_no,
                                        name=m.name, phone=m.phone, city=m.city, tier=m.tier,
                                        segment_code="champion", composite_score=m.composite_score,
                                        purchase_rate=m.purchase_rate))
        db.commit()
    finally:
        db.close()

    # 归并：重复档案被并入主档
    client.post("/api/merge/jobs", json={}, headers=_auth("admin"))

    r = client.get("/api/exports/audiences.csv", headers=_auth("analyst"))
    assert r.status_code == 200
    rows = r.content.decode("utf-8-sig").splitlines()[1:]
    pending_rows = [ln for ln in rows if ln.startswith("AP1,")]
    confirmed_rows = [ln for ln in rows if ln.startswith("AC1,")]
    # 待确认：归并后主档只剩 1 人，依据为「归并后会员主档」
    assert len(pending_rows) == 1
    assert "归并后会员主档" in pending_rows[0]
    assert "MY00001" in pending_rows[0]
    # 已确认：仍按生成时快照导出 2 人（含已被归并的 MYX00001），依据为快照
    assert len(confirmed_rows) == 2
    assert all("生成时会员快照" in ln for ln in confirmed_rows)
    assert any("MYX00001" in ln for ln in confirmed_rows)
