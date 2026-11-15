"""审计动作 -> 中文标签。"""
from __future__ import annotations

ACTIONS: dict[str, str] = {
    "auth.login": "登录",
    "member.view": "查看会员明细",
    "segment.recompute": "重算 RFM 分群",
    "segment.update": "调整分群券口径",
    "campaign.create": "新建活动",
    "campaign.update": "修改活动预算",
    "audience.generate": "生成营销名单",
    "audience.confirm": "确认名单",
    "export.members": "导出会员名单",
    "export.segments": "导出分群表",
    "export.audiences": "导出名单明细",
    "export.merge": "导出归并结果",
    "merge.start": "发起跨渠道归并",
    "merge.retry": "续跑归并任务",
    "merge.complete": "归并任务完成",
    "http.mutation": "写请求",
}
