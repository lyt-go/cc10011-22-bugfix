"""多维下钻业务规则：状态流转、字段校验与筛选口径都收在这里。

下钻路径套用筛选预设时，不再复制预设里的条件文本，而是消费「筛选条件数据快照」：
路径只记录快照编号，查询口径以快照里的有效条件/失效维度/显示列为准，
这样预设后续被修改或废弃也不会让旧条件重新出现。
"""
from __future__ import annotations

from typing import Any

from app.services.filterpreset import FilterpresetService
from app.store import store

MODULE = "drilldown"
REQUIRED_FIELDS = ["路径编号", "起始维度", "下钻层级序列"]
STATUS_ORDER = ["草稿", "路径评审中", "已启用", "已归档"]
ACTION_RULES = {"提交路径评审": "路径评审中", "启用路径": "已启用", "归档路径": "已归档"}
NEGATIVE_ACTIONS = ["归档路径"]

filterpreset_service = FilterpresetService()


class DrilldownService:
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("路径编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def apply_snapshot(self, entry_id: int, snapshot_id: int) -> tuple[dict[str, Any] | None, str]:
        """把筛选条件快照挂到下钻路径上，并登记消费轨迹。

        快照不可变：预设之后被修改或废弃，路径上已挂的快照仍然可追溯、可复现。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"下钻路径 {entry_id} 不存在或已归档"
        snapshot, message = filterpreset_service.consume_snapshot(snapshot_id, "drilldown")
        if snapshot is None:
            return None, message
        entry["筛选快照编号"] = snapshot_id
        entry["应用条件"] = snapshot.get("有效条件") or []
        entry["失效维度"] = [item.get("维度") for item in snapshot.get("失效维度") or []]
        entry["显示列"] = snapshot.get("显示列") or []
        entry["查询口径"] = snapshot.get("查询")
        return entry, message

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"下钻路径 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于多维下钻可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"下钻路径已{action}"
