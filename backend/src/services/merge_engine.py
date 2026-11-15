"""跨渠道身份识别与归并算法（纯 Python，确定性，不碰数据库，便于单测）。

输入是渠道档案 dict 列表（每条含 id/member_id/channel/phone/external_type/
external_id/name/city/tier/source_updated_at 等），输出归并分组与每个分组的
主档选择、识别依据、字段冲突取舍依据。

身份识别（并查集，两条等价关系）：
  1. 规范化手机号相同 → 同一人；
  2. 外部账号类型 + 外部账号相同 → 同一人。
传递闭包后，三条渠道档案可能经由不同链路落到同一组。

主档选择（确定性排序）：关联订单量降序 → 最早入会 → 档案 id 升序。
冲突字段取舍：来源资料时间（source_updated_at）最新者胜；时间相同按渠道
优先级（小程序 > 门店 > 外卖），再相同按档案 id 升序。每条冲突保留全部来源
与时间依据，不丢任何一份资料。
"""
from __future__ import annotations

from src.constants.enums import PROFILE_CHANNEL_PRIORITY
from src.constants.limits import EXTERNAL_ID_MIN_LEN, NORMALIZE_PHONE_MIN_LEN

# 参与冲突检测、需要保留来源与时间依据的资料字段
CONFLICT_FIELDS = ("name", "city", "tier", "phone", "external_id")


def normalize_phone(phone: str | None) -> str:
    """手机号规范化：去空白与连字符；过短视为缺失（返回空串，不参与匹配）。"""
    if not phone:
        return ""
    text = "".join(ch for ch in str(phone) if ch.isdigit())
    return text if len(text) >= NORMALIZE_PHONE_MIN_LEN else ""


def normalize_external(external_type: str | None, external_id: str | None) -> tuple[str, str]:
    """外部账号键：类型 + 规范化账号；任一缺失或账号过短则不参与匹配。"""
    etype = (external_type or "").strip().lower()
    eid = (external_id or "").strip()
    if not etype or len(eid) < EXTERNAL_ID_MIN_LEN:
        return "", ""
    return etype, eid


class _UnionFind:
    def __init__(self, size: int) -> None:
        self.parent = list(range(size))
        self.rank = [0] * size

    def find(self, x: int) -> int:
        root = x
        while self.parent[root] != root:
            root = self.parent[root]
        # 路径压缩
        while self.parent[x] != root:
            self.parent[x], x = root, self.parent[x]
        return root

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return
        if self.rank[ra] < self.rank[rb]:
            ra, rb = rb, ra
        self.parent[rb] = ra
        if self.rank[ra] == self.rank[rb]:
            self.rank[ra] += 1


def _profile_sort_key(profile: dict):
    """主档排序：订单量降序 → 最早入会 → id 升序（全部确定性）。"""
    return (
        -int(profile.get("order_count") or 0),
        profile.get("joined_on_sort") or "9999-12-31",
        int(profile["id"]),
    )


def _conflict_winner_key(profile: dict):
    """冲突字段取值排序：来源时间最新 → 渠道优先级 → id 升序。"""
    ts = profile.get("source_updated_at")
    ts_text = ts if isinstance(ts, str) else (ts.isoformat() if ts else "")
    return (
        ts_text or "",
        -PROFILE_CHANNEL_PRIORITY.get(profile.get("channel", ""), 99),
        -int(profile["id"]),
    )


def resolve_groups(profiles: list[dict]) -> list[list[dict]]:
    """按手机号 + 外部账号做并查集聚类，返回归并分组（仅含 ≥2 条档案的组）。

    单条档案、无任何有效标识的档案各自成组，不会被错误合并。
    """
    index_by_id = {int(item["id"]): idx for idx, item in enumerate(profiles)}
    uf = _UnionFind(len(profiles))

    phone_owner: dict[str, int] = {}
    external_owner: dict[tuple[str, str], int] = {}
    for idx, item in enumerate(profiles):
        phone = normalize_phone(item.get("phone"))
        if phone:
            if phone in phone_owner:
                uf.union(idx, phone_owner[phone])
            else:
                phone_owner[phone] = idx
        etype, eid = normalize_external(item.get("external_type"), item.get("external_id"))
        if etype:
            key = (etype, eid)
            if key in external_owner:
                uf.union(idx, external_owner[key])
            else:
                external_owner[key] = idx

    clusters: dict[int, list[dict]] = {}
    for idx, item in enumerate(profiles):
        root = uf.find(idx)
        clusters.setdefault(root, []).append(item)

    groups = [sorted(members, key=lambda row: int(row["id"])) for members in clusters.values()]
    groups.sort(key=lambda group: min(int(row["id"]) for row in group))
    return [group for group in groups if len(group) >= 2]


def _identity_evidence(group: list[dict]) -> tuple[str, str, str | None]:
    """给出该组的归并识别依据（优先手机号，其次外部账号）。"""
    phones = {normalize_phone(item.get("phone")) for item in group}
    phones.discard("")
    if len(phones) == 1:
        return "phone", next(iter(phones)), None
    # 多手机号但同组：必然由外部账号桥接，或多手机号经由外部链路连通
    externals: dict[tuple[str, str], int] = {}
    for item in group:
        etype, eid = normalize_external(item.get("external_type"), item.get("external_id"))
        if etype:
            externals[(etype, eid)] = externals.get((etype, eid), 0) + 1
    if externals:
        # 取覆盖档案数最多的外部账号作为说明依据
        (etype, eid), _ = max(externals.items(), key=lambda kv: (kv[1], kv[0]))
        return "external", eid, etype
    # 兜底：手机号传递连通（不同手机号不应到这里，防御性处理）
    phone = normalize_phone(group[0].get("phone"))
    return "phone", phone, None


def resolve_field_conflicts(group: list[dict]) -> list[dict]:
    """对每个资料字段检测冲突，返回带完整来源/时间依据的取舍清单。"""
    conflicts: list[dict] = []
    for field in CONFLICT_FIELDS:
        distinct = {item.get(field) for item in group if item.get(field) not in (None, "")}
        if len(distinct) <= 1:
            continue
        ranked = sorted(group, key=_conflict_winner_key, reverse=True)
        winner = ranked[0]
        conflicts.append(
            {
                "field": field,
                "chosen": {
                    "value": winner.get(field),
                    "profile_id": int(winner["id"]),
                    "channel": winner.get("channel"),
                    "source_updated_at": _iso(winner.get("source_updated_at")),
                },
                "sources": [
                    {
                        "value": item.get(field),
                        "profile_id": int(item["id"]),
                        "member_id": int(item["member_id"]),
                        "channel": item.get("channel"),
                        "source_updated_at": _iso(item.get("source_updated_at")),
                    }
                    for item in sorted(group, key=lambda row: int(row["id"]))
                    if item.get(field) not in (None, "")
                ],
            }
        )
    return conflicts


def _iso(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def plan_merge(profiles: list[dict]) -> list[dict]:
    """生成完整归并计划（不写库）：每组含主档、被合并档案、识别依据、冲突依据。"""
    plans: list[dict] = []
    for group in resolve_groups(profiles):
        ordered = sorted(group, key=_profile_sort_key)
        master = ordered[0]
        merged = ordered[1:]
        match_kind, match_value, external_type = _identity_evidence(group)
        conflicts = resolve_field_conflicts(group)
        master_reason = (
            f"关联订单 {int(master.get('order_count') or 0)} 笔"
            f"（组内最高）；入会 {master.get('joined_on') or '—'}；档案 id {master['id']}"
        )
        plans.append(
            {
                "master": master,
                "merged": merged,
                "all": ordered,
                "match_kind": match_kind,
                "match_value": match_value,
                "external_type": external_type,
                "master_reason": master_reason,
                "conflicts": conflicts,
            }
        )
    return plans


def chosen_field_values(plan: dict) -> dict[str, object]:
    """从冲突计划中取出主档最终应采用的字段值（无冲突字段取主档原值）。"""
    master = plan["master"]
    values = {field: master.get(field) for field in CONFLICT_FIELDS}
    for conflict in plan["conflicts"]:
        values[conflict["field"]] = conflict["chosen"]["value"]
    return values
