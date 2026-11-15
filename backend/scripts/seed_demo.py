"""幂等 seed：3 账号 / 8,000 会员 / 30,000 订单 / 8 分群 / 2 活动 / 2 份名单。

自然键 upsert：
  users.email / members.member_no / orders.order_no
  segments.code（只更新快照字段，不动运营改过的券面额）
  campaigns.code / audiences.seed_key
  audit_logs.seed_key

重复执行不新增、不覆盖已确认名单；SEED_SUMMARY 两次逐字一致。
数量口径（与 batch-plan 的 seed 段对应）：
  sources = 会员数、runs = 订单数、findings = 分群数、pending = 待确认名单数。
"""
from __future__ import annotations

import random
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, insert, select

from src.config.database import session_scope
from src.models.audit import AuditLog
from src.models.audience import Audience
from src.models.campaign import Campaign
from src.models.channel_profile import ChannelProfile
from src.models.coupon_response import CouponResponse
from src.models.member import Member
from src.models.order import Order
from src.models.segment import Segment
from src.models.user import User
from src.services import audience_service, segment_service, snapshot_service
from src.services.merge_engine import normalize_phone
from src.constants.limits import RFM_WINDOW_DAYS
from src.utils.security import hash_password
from src.utils.timeutil import utcnow

from scripts.demo_config import (
    ARCHETYPES,
    CAMPAIGNS,
    CHANNELS,
    CITIES,
    CONFIRMED_CAMPAIGN,
    COUPON_PER_PROFILE,
    DEMO_PASSWORD,
    DEMO_USERS,
    DUPLICATE_ONE_RATE,
    DUPLICATE_TWO_RATE,
    EXTERNAL_MATCH_SHARE,
    EXTERNAL_TYPES,
    GIVEN,
    MEMBER_TARGET,
    ORDER_TARGET,
    ORG,
    RANDOM_SEED,
    SEED_ANCHOR,
    SEED_AUDIENCES,
    SEED_CAMPAIGN_CODES,
    SEED_CONFIRMED_AUDIENCE,
    SEGMENT_COUPON,
    STORES,
    SURNAMES,
)


def _pick_weighted(rng: random.Random, items: list[tuple[str, float]]) -> str:
    point = rng.random()
    cumulative = 0.0
    for key, weight in items:
        cumulative += weight
        if point <= cumulative:
            return key
    return items[-1][0]


def _build_dataset() -> tuple[list[dict], list[dict], dict[str, dict]]:
    """生成会员与订单（纯内存、纯确定性），返回 (member_rows, order_rows, stats)。"""
    rng = random.Random(RANDOM_SEED)
    archetype_by_key = {item["key"]: item for item in ARCHETYPES}
    weights = [(item["key"], item["weight"]) for item in ARCHETYPES]

    plans: list[dict] = []
    for index in range(MEMBER_TARGET):
        key = _pick_weighted(rng, weights)
        profile = archetype_by_key[key]
        base = profile["orders"]
        count = int(round(base + rng.uniform(-1.2, 1.2)))
        if key == "hibernating":
            count = max(0, count)
        else:
            count = max(1, count)
        plans.append({"index": index, "archetype": key, "profile": profile, "orders": count})

    total = sum(item["orders"] for item in plans)
    # 调整到精确 30,000 单：先削多、再补少（顺序由固定 rng 决定，可重放）
    rng.shuffle(plans)
    while total > ORDER_TARGET:
        for item in plans:
            if total <= ORDER_TARGET:
                break
            if item["orders"] > 0:
                item["orders"] -= 1
                total -= 1
    if total < ORDER_TARGET:
        candidates = [item for item in plans if item["archetype"] != "new"]
        cursor = 0
        while total < ORDER_TARGET and candidates:
            item = candidates[cursor % len(candidates)]
            item["orders"] += 1
            total += 1
            cursor += 1
    plans.sort(key=lambda item: item["index"])

    member_rows: list[dict] = []
    order_rows: list[dict] = []
    order_seq = 0
    for plan in plans:
        index = plan["index"]
        profile = plan["profile"]
        member_no = f"MY{index + 1:05d}"
        low, high = profile["recent"]
        joined_on = SEED_ANCHOR - timedelta(days=rng.randint(420, 1500))
        paid_days: list[int] = []
        for _ in range(plan["orders"]):
            span = rng.randint(low, high)
            paid_days.append(max(0, min(520, span + rng.randint(-18, 18))))
        paid_days.sort(reverse=True)
        amounts: list[float] = []
        last_paid: datetime | None = None
        for order_index, days in enumerate(paid_days):
            order_seq += 1
            items = rng.choice([1, 1, 1, 2, 2, 3])
            ticket = profile["ticket"] * rng.uniform(0.78, 1.32)
            amount = round(ticket * (1 + (items - 1) * 0.42), 2)
            amounts.append(amount)
            paid_at = datetime.combine(SEED_ANCHOR - timedelta(days=days), time(hour=rng.randint(8, 21), minute=rng.randint(0, 59)))
            if last_paid is None or paid_at > last_paid:
                last_paid = paid_at
            channel = _pick_weighted(rng, CHANNELS)
            order_rows.append(
                {
                    "order_no": f"MYO{order_seq:06d}",
                    "member_no": member_no,
                    "store_code": rng.choice(STORES),
                    "channel": channel,
                    "amount": amount,
                    "items": items,
                    "coupon_used": rng.random() < profile["rate"],
                    "paid_at": paid_at,
                    "created_at": paid_at,
                }
            )
        in_window = [amount for days, amount in zip(paid_days, amounts) if days <= RFM_WINDOW_DAYS]
        orders_window = len(in_window)
        amount_window = round(sum(in_window), 2)
        avg_ticket = round(sum(amounts) / len(amounts), 2) if amounts else 0.0
        rate = round(profile["rate"] * rng.uniform(0.82, 1.18), 6)
        tier = profile["tier"]
        if tier == "silver" and rng.random() < 0.18:
            tier = "gold"
        elif tier == "gold" and rng.random() < 0.12:
            tier = "black"
        member_rows.append(
            {
                "member_no": member_no,
                "name": rng.choice(SURNAMES) + rng.choice(GIVEN),
                "phone": "1" + rng.choice("35789") + "".join(str(rng.randint(0, 9)) for _ in range(9)),
                "city": rng.choice(CITIES),
                "tier": tier,
                "joined_on": joined_on,
                "last_order_at": last_paid,
                "orders_window": orders_window,
                "amount_window": amount_window,
                "purchase_rate": rate,
                "avg_ticket": avg_ticket,
            }
        )
    return member_rows, order_rows, {"members": len(member_rows), "orders": len(order_rows)}


def ensure_users(db) -> int:
    for email, display_name, role in DEMO_USERS:
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            db.add(
                User(
                    email=email,
                    password_hash=hash_password(DEMO_PASSWORD),
                    display_name=display_name,
                    role=role,
                    org=ORG,
                )
            )
    db.flush()
    return db.scalar(select(func.count()).select_from(User)) or 0


def ensure_members_and_orders(db) -> tuple[int, int]:
    members_total = db.scalar(select(func.count()).select_from(Member)) or 0
    orders_total = db.scalar(select(func.count()).select_from(Order)) or 0
    if members_total and orders_total:
        return members_total, orders_total

    member_rows, order_rows, _stats = _build_dataset()
    exist_members = {row for (row,) in db.execute(select(Member.member_no)).all()}
    new_members = [row for row in member_rows if row["member_no"] not in exist_members]
    for offset in range(0, len(new_members), 1000):
        db.execute(insert(Member), new_members[offset : offset + 1000])
    db.flush()

    id_by_no = {no: pk for no, pk in db.execute(select(Member.member_no, Member.id)).all()}
    exist_orders = {row for (row,) in db.execute(select(Order.order_no)).all()}
    payload = []
    for row in order_rows:
        if row["order_no"] in exist_orders:
            continue
        payload.append(
            {
                "order_no": row["order_no"],
                "member_id": id_by_no[row["member_no"]],
                "store_code": row["store_code"],
                "channel": row["channel"],
                "amount": row["amount"],
                "items": row["items"],
                "coupon_used": row["coupon_used"],
                "paid_at": row["paid_at"],
                "created_at": row["created_at"],
            }
        )
    for offset in range(0, len(payload), 2000):
        db.execute(insert(Order), payload[offset : offset + 2000])
    db.flush()
    members_total = db.scalar(select(func.count()).select_from(Member)) or 0
    orders_total = db.scalar(select(func.count()).select_from(Order)) or 0
    return members_total, orders_total


def ensure_segments(db) -> int:
    if db.scalar(select(func.count()).select_from(Segment)) == 0:
        segment_service.recompute(db)
        for code, config in SEGMENT_COUPON.items():
            segment = db.scalar(select(Segment).where(Segment.code == code))
            if segment is not None:
                segment.coupon_face = config["coupon_face"]
                segment.core_share = config["core_share"]
        db.flush()
    return db.scalar(select(func.count()).select_from(Segment)) or 0


def ensure_campaigns(db) -> int:
    for spec in CAMPAIGNS:
        exists = db.scalar(select(Campaign).where(Campaign.code == spec["code"]))
        if exists is not None:
            continue
        audience_service.create_campaign(
            db,
            {
                "code": spec["code"],
                "name": spec["name"],
                "channel": spec["channel"],
                "budget": spec["budget"],
                "status": spec["status"],
                "starts_on": spec["starts_on"],
                "ends_on": spec["ends_on"],
                "note": spec["note"],
            },
            operator_email="seed@rfmseg.io",
            seed_key=spec["seed_key"],
        )
        db.flush()
    return db.scalar(select(func.count()).select_from(Campaign)) or 0


def ensure_audiences(db) -> int:
    from src.models.audience import Audience

    campaigns = {item.code: item for item in db.scalars(select(Campaign)).all()}
    for seed_key, campaign_code, note in SEED_AUDIENCES:
        campaign = campaigns.get(campaign_code)
        if campaign is None:
            continue
        audience_service.generate(
            db,
            campaign=campaign,
            budget=None,
            operator_email="seed@rfmseg.io",
            note=note,
            seed_key=seed_key,
        )
    db.flush()
    return db.scalar(select(func.count()).select_from(Audience)) or 0


def ensure_channel_world(db) -> dict:
    """构造跨渠道档案世界（幂等）：

    - 每个原始会员一份与主数据一致的渠道档案（门店/小程序/外卖按其订单渠道选主）；
    - 给部分会员追加 1~2 份重复档案，落到新的 member 行，并把该会员对应渠道的部分
      订单改挂过去，模拟「同一人三个渠道各留一份、订单被拆散」；
    - 每个渠道档案挂 0~3 条历史券响应（发券/核销事实）。
    已存在 channel_profiles 时整体跳过，保证重复 seed 不新增重复档案。
    """
    if db.scalar(select(func.count()).select_from(ChannelProfile)):
        return {
            "profiles": db.scalar(select(func.count()).select_from(ChannelProfile)) or 0,
            "duplicate_members": db.scalar(
                select(func.count()).select_from(Member).where(Member.merged_into_id.is_not(None))
            ) or 0,
            "coupons": db.scalar(select(func.count()).select_from(CouponResponse)) or 0,
        }

    rng = random.Random(RANDOM_SEED + 7)
    members = db.scalars(select(Member).order_by(Member.id)).all()
    # 会员 id -> 其订单的主导渠道
    member_channel: dict[int, str] = {}
    for member in members:
        rows = db.execute(
            select(Order.channel, func.count())
            .where(Order.member_id == member.id)
            .group_by(Order.channel)
            .order_by(func.count().desc())
        ).all()
        member_channel[member.id] = rows[0][0] if rows else rng.choice([c for c, _ in CHANNELS])

    profile_id_seq = 0
    duplicate_member_count = 0
    # 外部账号全局唯一，避免不同自然人的档案因外部 id 撞号被误并
    used_externals: set[tuple[str, str]] = set()
    dup_member_seq = 0

    def next_member_no() -> str:
        nonlocal dup_member_seq
        dup_member_seq += 1
        return f"MYX{dup_member_seq:05d}"

    for member in members:
        base_channel = member_channel[member.id]
        extra_channels = [c for c in ("store", "miniapp", "delivery") if c != base_channel]
        roll = rng.random()
        dup_count = 2 if roll < DUPLICATE_TWO_RATE else (1 if roll < DUPLICATE_TWO_RATE + DUPLICATE_ONE_RATE else 0)
        chosen_dups = rng.sample(extra_channels, dup_count) if dup_count else []

        # 1) 原始会员的基线档案
        profile_id_seq += 1
        external_type, external_id = _maybe_external(rng, base_channel, member.id, used_externals)
        base_updated = datetime.combine(member.joined_on, time(10, 0)) if member.joined_on else utcnow()
        base_profile = ChannelProfile(
            id=profile_id_seq,
            member_id=member.id,
            channel=base_channel,
            channel_member_no=f"{base_channel.upper()}-{member.member_no}",
            name=member.name,
            phone=member.phone,
            phone_norm=normalize_phone(member.phone),
            external_type=external_type,
            external_id=external_id,
            city=member.city,
            tier=member.tier,
            status="active",
            source_updated_at=base_updated,
        )
        db.add(base_profile)

        # 2) 重复档案：新 member 行 + 部分订单改挂 + 资料冲突
        for dup_channel in chosen_dups:
            # 外部账号桥接（手机号可能不同）或同手机号，二者都保证可识别为同一人
            use_external_bridge = rng.random() < EXTERNAL_MATCH_SHARE
            if use_external_bridge:
                etype = rng.choice(EXTERNAL_TYPES)
                eid = f"{etype}-{member.id:05d}"
                # 同一自然人的桥接账号唯一；万一撞号则换类型直到不撞
                while (etype, eid) in used_externals:
                    etype = rng.choice(EXTERNAL_TYPES)
                    eid = f"{etype}-{member.id:05d}-{dup_channel}"
                used_externals.add((etype, eid))
                dup_phone = _maybe_wrong_phone(member.phone)
                # 基线档案补上同一外部账号，桥接才闭合
                base_profile.external_type, base_profile.external_id = etype, eid
            else:
                etype, eid = None, None
                dup_phone = member.phone
            new_member = Member(
                member_no=next_member_no(),
                name=_maybe_conflict_name(rng, member.name),
                phone=dup_phone,
                city=rng.choice(CITIES) if rng.random() < 0.4 else member.city,
                tier=member.tier,
                joined_on=member.joined_on,
                purchase_rate=member.purchase_rate,
                avg_ticket=member.avg_ticket,
            )
            db.add(new_member)
            db.flush()
            duplicate_member_count += 1

            # 该渠道约 40% 的订单改挂到重复会员（订单总数不变，只是被渠道拆散）
            dup_orders = db.scalars(
                select(Order).where(Order.member_id == member.id).where(Order.channel == dup_channel)
            ).all()
            move = dup_orders[: max(1, int(len(dup_orders) * 0.4))]
            for order in move:
                order.member_id = new_member.id

            profile_id_seq += 1
            db.add(
                ChannelProfile(
                    id=profile_id_seq,
                    member_id=new_member.id,
                    channel=dup_channel,
                    channel_member_no=f"{dup_channel.upper()}-{new_member.member_no}",
                    name=new_member.name,
                    phone=dup_phone,
                    phone_norm=normalize_phone(dup_phone),
                    external_type=etype,
                    external_id=eid,
                    city=new_member.city,
                    tier=new_member.tier,
                    status="active",
                    source_updated_at=datetime.combine(member.joined_on, time(rng.randint(8, 20), rng.randint(0, 59))),
                )
            )
    db.flush()

    # 3) 每个渠道档案挂历史券响应
    profiles = db.scalars(select(ChannelProfile).order_by(ChannelProfile.id)).all()
    coupon_seq = 0
    for profile in profiles:
        count = rng.choice(COUPON_PER_PROFILE)
        for idx in range(count):
            coupon_seq += 1
            sent_days = rng.randint(20, 480)
            sent_at = datetime.combine(SEED_ANCHOR - timedelta(days=sent_days), time(rng.randint(9, 20), rng.randint(0, 59)))
            redeemed = rng.random() < 0.35
            db.add(
                CouponResponse(
                    member_id=profile.member_id,
                    campaign_code=rng.choice(SEED_CAMPAIGN_CODES),
                    coupon_name=rng.choice(["满减券", "买一赠一券", "新客尝鲜券", "会员日折扣券"]),
                    channel=profile.channel,
                    status="redeemed" if redeemed else "sent",
                    face_value=float(rng.choice([4, 5, 6, 8, 10, 12])),
                    sent_at=sent_at,
                    redeemed_at=sent_at + timedelta(days=rng.randint(1, 14)) if redeemed else None,
                )
            )
    db.flush()
    return {
        "profiles": len(profiles),
        "duplicate_members": duplicate_member_count,
        "coupons": coupon_seq,
    }


def _maybe_external(
    rng: random.Random, channel: str, member_id: int, used: set[tuple[str, str]]
) -> tuple[str | None, str | None]:
    if rng.random() < 0.5:
        etype = rng.choice(EXTERNAL_TYPES)
        eid = f"{etype}-{member_id:05d}"
        # 撞号则追加随机后缀，保证外部账号全局唯一
        while (etype, eid) in used:
            eid = f"{etype}-{member_id:05d}-{rng.randint(1000, 9999)}"
        used.add((etype, eid))
        return etype, eid
    return None, None


def _maybe_conflict_name(rng: random.Random, name: str) -> str:
    if rng.random() < 0.5:
        return rng.choice(SURNAMES) + rng.choice(GIVEN)
    return name


def _maybe_wrong_phone(phone: str) -> str:
    # 外部账号桥接场景下档案手机号可能不同（留空或渠道虚拟号）
    return "" if len(phone) < 7 else phone[:-4] + "0000"


def ensure_confirmed_audience(db) -> None:
    """补一个已结束活动 + 已确认名单，并冻结其会员快照（演示已确认名单按快照导出）。"""
    campaign = db.scalar(select(Campaign).where(Campaign.code == CONFIRMED_CAMPAIGN["code"]))
    if campaign is None:
        campaign = audience_service.create_campaign(
            db,
            {
                "code": CONFIRMED_CAMPAIGN["code"],
                "name": CONFIRMED_CAMPAIGN["name"],
                "channel": CONFIRMED_CAMPAIGN["channel"],
                "budget": CONFIRMED_CAMPAIGN["budget"],
                "status": CONFIRMED_CAMPAIGN["status"],
                "starts_on": CONFIRMED_CAMPAIGN["starts_on"],
                "ends_on": CONFIRMED_CAMPAIGN["ends_on"],
                "note": CONFIRMED_CAMPAIGN["note"],
            },
            operator_email="seed@rfmseg.io",
            seed_key=CONFIRMED_CAMPAIGN["seed_key"],
        )
        db.flush()
    seed_key, _campaign_code, note = SEED_CONFIRMED_AUDIENCE
    audience = db.scalar(select(Audience).where(Audience.seed_key == seed_key))
    if audience is None:
        audience = audience_service.generate(
            db,
            campaign=campaign,
            budget=None,
            operator_email="seed@rfmseg.io",
            note=note,
            seed_key=seed_key,
        )
        db.flush()
    if not snapshot_service.has_snapshot(db, audience.id):
        snapshot_service.snapshot_audience_members(db, audience)
    if audience.status == "pending":
        audience_service.confirm(
            db, audience, status="confirmed", note="seed 已确认：按生成时快照导出", actor="seed@rfmseg.io"
        )
    db.flush()


def ensure_audit(db) -> None:
    seeds = [
        ("seed-audit-1", "admin@rfmseg.io", "admin", "segment.recompute", "segment", {"weights": [0.3, 0.3, 0.4]}),
        ("seed-audit-2", "analyst@rfmseg.io", "analyst", "export.members", "member", {"rows": 8000}),
    ]
    for seed_key, email, role, action, entity, detail in seeds:
        exists = db.scalar(select(AuditLog).where(AuditLog.seed_key == seed_key))
        if exists is None:
            db.add(
                AuditLog(
                    actor_email=email,
                    actor_role=role,
                    action=action,
                    entity=entity,
                    detail=detail,
                    layer="seed",
                    seed_key=seed_key,
                )
            )
    db.flush()


def seed_all() -> dict:
    from src.models.audience import Audience

    with session_scope() as db:
        users = ensure_users(db)
        members, orders = ensure_members_and_orders(db)
        segments = ensure_segments(db)
        ensure_campaigns(db)
        ensure_audiences(db)
        channel_world = ensure_channel_world(db)
        ensure_confirmed_audience(db)
        ensure_audit(db)
        audiences = db.scalar(select(func.count()).select_from(Audience)) or 0
        pending = db.scalar(select(func.count()).select_from(Audience).where(Audience.status == "pending")) or 0
        natural_persons = db.scalar(
            select(func.count()).select_from(Member).where(Member.merged_into_id.is_(None))
        ) or 0
        summary = (
            f"SEED_SUMMARY brand=rfmseg users={users} sources={natural_persons} "
            f"runs={orders} findings={segments} pending={pending} "
            f"profiles={channel_world['profiles']} dup_members={channel_world['duplicate_members']} "
            f"coupons={channel_world['coupons']}"
        )
        print(summary, flush=True)
        return {
            "users": users,
            "members": members,
            "orders": orders,
            "segments": segments,
            "audiences": audiences,
            "pending": pending,
            "profiles": channel_world["profiles"],
            "dup_members": channel_world["duplicate_members"],
            "coupons": channel_world["coupons"],
            "natural_persons": natural_persons,
        }


if __name__ == "__main__":
    seed_all()
