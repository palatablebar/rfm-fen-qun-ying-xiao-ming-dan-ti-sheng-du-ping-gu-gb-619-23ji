"""跨渠道身份识别与归并算法单测（纯 Python，不依赖数据库）。"""
from __future__ import annotations

import pytest

from src.services.merge_engine import (
    chosen_field_values,
    normalize_external,
    normalize_phone,
    plan_merge,
    resolve_field_conflicts,
    resolve_groups,
)


def _profile(pid, member_id, channel, **over):
    base = {
        "id": pid,
        "member_id": member_id,
        "channel": channel,
        "channel_member_no": f"{channel}-{pid}",
        "name": "王若溪",
        "phone": "13812345678",
        "external_type": None,
        "external_id": None,
        "city": "上海",
        "tier": "gold",
        "source_updated_at": "2026-10-01T00:00:00",
        "order_count": 1,
        "joined_on": "2025-01-01",
        "joined_on_sort": "2025-01-01",
    }
    base.update(over)
    return base


def test_normalize_phone_strips_separators_and_rejects_short():
    assert normalize_phone("138-1234 5678") == "13812345678"
    assert normalize_phone("12345") == ""
    assert normalize_phone(None) == ""
    assert normalize_phone("") == ""


def test_normalize_external_requires_type_and_min_length():
    assert normalize_external("WX_OpenID", "oid123") == ("wx_openid", "oid123")
    assert normalize_external("", "oid123") == ("", "")
    assert normalize_external("wx_openid", "x") == ("", "")


def test_phone_match_across_channels_merges_into_one_group():
    profiles = [
        _profile(1, 101, "store", order_count=9),
        _profile(2, 102, "miniapp", phone="138 1234 5678", order_count=3),
        _profile(3, 103, "delivery", order_count=1),
    ]
    groups = resolve_groups(profiles)
    assert len(groups) == 1
    assert {p["id"] for p in groups[0]} == {1, 2, 3}


def test_external_account_bridges_different_phones():
    profiles = [
        _profile(1, 101, "miniapp", phone="13700000001", external_type="alipay_uid", external_id="ab-99"),
        _profile(2, 102, "delivery", phone="13700000002", external_type="alipay_uid", external_id="ab-99"),
    ]
    plans = plan_merge(profiles)
    assert len(plans) == 1
    assert plans[0]["match_kind"] == "external"
    assert plans[0]["external_type"] == "alipay_uid"
    assert plans[0]["match_value"] == "ab-99"


def test_transitive_identity_chain_merges_three_profiles():
    # A~B 同手机号；B~C 同外部账号；A 与 C 无直接共同标识，仍应并为一组
    profiles = [
        _profile(1, 101, "store", phone="13800000001", order_count=5),
        _profile(2, 102, "miniapp", phone="13800000001",
                 external_type="wx_openid", external_id="wx-7", order_count=2),
        _profile(3, 103, "delivery", phone="13800000009",
                 external_type="wx_openid", external_id="wx-7", order_count=1),
    ]
    groups = resolve_groups(profiles)
    assert len(groups) == 1
    assert {p["id"] for p in groups[0]} == {1, 2, 3}


def test_profiles_without_identity_are_not_falsely_merged():
    profiles = [
        _profile(1, 101, "store", phone="", name="a"),
        _profile(2, 102, "miniapp", phone="", name="b"),
    ]
    assert resolve_groups(profiles) == []


def test_master_chosen_by_order_count_then_joined_then_id():
    profiles = [
        _profile(1, 101, "store", order_count=2, joined_on="2025-03-01", joined_on_sort="2025-03-01"),
        _profile(2, 102, "miniapp", order_count=8, joined_on="2025-04-01", joined_on_sort="2025-04-01"),
        _profile(3, 103, "delivery", order_count=8, joined_on="2025-01-01", joined_on_sort="2025-01-01"),
    ]
    plan = plan_merge(profiles)[0]
    # 订单量最高（8）里入会最早的是 id=3
    assert plan["master"]["id"] == 3
    assert {p["id"] for p in plan["merged"]} == {1, 2}
    assert "订单 8 笔" in plan["master_reason"]


def test_conflict_keeps_latest_source_with_full_evidence():
    profiles = [
        _profile(1, 101, "store", name="王若溪", city="上海",
                 source_updated_at="2026-09-01T00:00:00", order_count=5),
        _profile(2, 102, "miniapp", name="王小溪", city="杭州",
                 source_updated_at="2026-11-01T00:00:00", order_count=2),
    ]
    conflicts = resolve_field_conflicts(profiles)
    fields = {c["field"]: c for c in conflicts}
    assert set(fields) == {"name", "city"}
    name = fields["name"]
    assert name["chosen"]["value"] == "王小溪"
    assert name["chosen"]["channel"] == "miniapp"
    assert name["chosen"]["source_updated_at"].startswith("2026-11-01")
    # 每个来源都保留，不丢资料
    assert {s["profile_id"] for s in name["sources"]} == {1, 2}
    values = chosen_field_values(plan_merge(profiles)[0])
    assert values["name"] == "王小溪"
    assert values["city"] == "杭州"


def test_conflict_tie_breaks_by_channel_priority_then_id():
    same_ts = "2026-10-01T00:00:00"
    profiles = [
        _profile(1, 101, "delivery", name="外卖名", source_updated_at=same_ts, order_count=4),
        _profile(2, 102, "store", name="门店名", source_updated_at=same_ts, order_count=2),
        _profile(3, 103, "miniapp", name="小程序名", source_updated_at=same_ts, order_count=1),
    ]
    values = chosen_field_values(plan_merge(profiles)[0])
    # 时间相同：小程序优先级最高
    assert values["name"] == "小程序名"


def test_no_conflict_when_values_identical():
    profiles = [_profile(1, 101, "store"), _profile(2, 102, "miniapp")]
    assert resolve_field_conflicts(profiles) == []


def test_plan_merge_is_deterministic_on_repeated_runs():
    profiles = [
        _profile(1, 101, "store", order_count=3),
        _profile(2, 102, "miniapp", order_count=1, name="王小溪",
                 source_updated_at="2026-11-01T00:00:00"),
        _profile(3, 103, "delivery", order_count=2),
    ]
    first = plan_merge(profiles)
    second = plan_merge(list(reversed(profiles)))
    assert first[0]["master"]["id"] == second[0]["master"]["id"] == 1
    assert chosen_field_values(first[0]) == chosen_field_values(second[0])
