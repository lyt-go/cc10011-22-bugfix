"""筛选预设业务规则：字段保存、状态流转、条件解析、快照与使用计数。

这个模块是「筛选预设」的唯一业务边界，路由层只负责转发请求：

* 保存：新建/修改时把时间范围、排序规则（显示顺序）、显示列、共享状态和发布状态
  一起落库，不再只保留编号、名称、维度条件组三个必填字段。
* 条件解析：``维度条件组`` 支持 JSON 数组、``{"conditions": [...]}`` 以及
  ``区域=华东;渠道=线上`` 这类文本写法，解析时保留全部条件，不会只取第一个。
* 快照：套用已发布预设时生成不可变的筛选条件数据快照，记录解析后的全部条件、
  失效维度、显示列、时间范围、预设版本、使用次数和幂等键，便于追溯。
* 使用次数：只有「已发布」预设被成功套用才计数；同一个幂等键重复刷新只返回原快照，
  不重复累加。已废弃预设仍可只读查看历史快照，但不能再被套用。
* 上游依赖：生成快照前必须能读到预设模板、维度条件组和时间范围记录；多个依赖同时
  提交时按提交顺序只采纳每类依赖中首个通过校验的记录。
"""
from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import date, datetime
from typing import Any

from app.store import store

MODULE = "filterpreset"
SNAPSHOT_TABLE = "filterpreset_snapshots"
USAGE_TABLE = "filterpreset_usage"
CONSUMER_TABLE = "filterpreset_snapshot_consumers"

REQUIRED_FIELDS = ["预设编号", "预设名称", "维度条件组"]
# 新建时必须一并保存的字段，避免「只留下三个必填字段」。
OPTIONAL_FIELDS = ["时间范围", "排序规则", "显示列", "共享状态"]
FIELD_ALIASES = {
    "显示顺序": "排序规则",
    "排序列": "排序规则",
    "排序": "排序规则",
    "展示列": "显示列",
    "显示字段": "显示列",
    "共享范围": "共享状态",
    "发布状态": "status",
}
STATUS_ORDER = ["草稿", "预设评审中", "已发布", "已废弃"]
STATUS_DRAFT = STATUS_ORDER[0]
STATUS_REVIEWING = STATUS_ORDER[1]
STATUS_PUBLISHED = STATUS_ORDER[2]
STATUS_DEPRECATED = STATUS_ORDER[3]
ACTION_RULES = {"提交预设评审": STATUS_REVIEWING, "发布预设": STATUS_PUBLISHED, "废弃预设": STATUS_DEPRECATED}
NEGATIVE_ACTIONS = ["废弃预设"]
SHARE_PRIVATE = "未共享"
SHARE_TEAM = "团队共享"
SHARE_STATES = [SHARE_PRIVATE, SHARE_TEAM]

# 维度注册表：维度映射模块登记的原始/标准维度名，加上平台内置的分析维度。
# 不在集合里的维度会在快照中记为「失效维度」，不再参与查询。
BUILTIN_DIMENSIONS = {
    "日期", "时间", "区域", "省份", "城市", "渠道", "平台", "设备", "版本",
    "用户类型", "产品线", "业务线", "指标", "指标名称", "指标值", "数值",
}
BUILTIN_COLUMNS = BUILTIN_DIMENSIONS | {"统计时间", "总量", "环比", "同比", "占比"}

JSON_TYPES = (list, dict)
CONDITION_KEYS = {
    "dimension": ("维度", "维度名", "字段", "字段名", "dimension", "field", "name"),
    "operator": ("操作符", "运算符", "比较符", "op", "operator"),
    "value": ("值", "取值", "维度值", "value", "values"),
}
DEFAULT_OPERATOR = "="
CONDITION_RE = re.compile(
    r"^(?P<dimension>[^=:：<>!\s][^=:：<>!]*?)\s*"
    r"(?:(?P<operator>>=|<=|!=|<>|==|=|>|<)|(?P<word>not\s+in|in|contains|like))\s*"
    r"(?P<value>.+?)\s*$",
    re.IGNORECASE,
)
RELATIVE_RANGE_RE = re.compile(r"^\s*(近|最近|last\s*)?\d+\s*(天|日|周|星期|月|year|week|day|month)s?\s*$", re.IGNORECASE)
DATE_RANGE_SPLIT_RE = re.compile(r"\s*(?:~|～|至|到|—|–|--|\s-\s)\s*")


class FilterpresetService:
    """筛选预设的保存、流转、套用和快照消费。"""

    def __init__(self) -> None:
        # 同一预设的套用串行处理：先到且通过校验的请求生成快照，后到的同幂等键请求
        # 直接回放首个快照，不会把使用次数打散或重复累加。
        self._locks: dict[int, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, entry_id: int) -> threading.Lock:
        with self._locks_guard:
            return self._locks.setdefault(entry_id, threading.Lock())

    # ------------------------------------------------------------------ 查询

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
            rows = [
                row
                for row in rows
                if keyword in str(row.get("预设编号", "")) or keyword in str(row.get("预设名称", ""))
            ]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return [self._public_entry(row) for row in rows[start:start + size]], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        """列表/导出用的明细（不包含派生字段）。"""
        entry = store.find(MODULE, entry_id)
        return self._public_entry(entry) if entry else None

    def get_entry_detail(self, entry_id: int) -> dict[str, Any] | None:
        """详情入口：与套用入口共用同一份当前条件，避免两个入口读到不同口径。

        详情只读，不增加使用次数；旧快照以只读形式一并返回。
        """
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None
        detail = self._public_entry(entry)
        conditions = self.parse_conditions(entry.get("维度条件组"))
        detail["解析条件"] = conditions
        detail["条件数量"] = len(conditions)
        detail["时间范围解析"] = self.parse_time_range(entry.get("时间范围"))
        detail["显示列清单"] = self.parse_display_columns(entry.get("显示列"))
        detail["最新快照"] = self.latest_snapshot(entry_id)
        detail["使用记录"] = self.list_usage(entry_id)
        detail["可套用"] = entry.get("status") == STATUS_PUBLISHED
        return detail

    # ------------------------------------------------------------------ 保存

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        normalized = self._normalize_values(values)
        missing = [field for field in REQUIRED_FIELDS if not str(normalized.get(field) or "").strip()]
        if missing:
            return None, missing

        preset_number = str(normalized["预设编号"]).strip()
        if any(str(row.get("预设编号", "")).strip() == preset_number for row in store.rows(MODULE)):
            return None, [f"预设编号 {preset_number} 已存在"]

        status = normalized.get("status") or STATUS_DRAFT
        if status not in STATUS_ORDER:
            return None, [f"发布状态「{status}」不在允许的状态序列里"]
        if status == STATUS_DEPRECATED:
            return None, ["不能把新建预设直接登记为「已废弃」，请通过废弃预设动作流转"]

        now = self._now()
        entry: dict[str, Any] = {
            "id": store.next_id(MODULE),
            "预设编号": preset_number,
            "预设名称": str(normalized.get("预设名称") or "").strip(),
            "维度条件组": self._stringify_condition_group(normalized.get("维度条件组")),
            "时间范围": str(normalized.get("时间范围") or "").strip(),
            "排序规则": str(normalized.get("排序规则") or "").strip(),
            "显示列": self._stringify_display_columns(normalized.get("显示列")),
            "共享状态": self._normalize_share_status(normalized.get("共享状态")),
            # 使用次数是系统计数，新建时一律从 0 开始，不接受外部写入。
            "使用次数": 0,
            "status": status,
            "发布状态": status,
            "版本": 1 if status == STATUS_PUBLISHED else 0,
            "修订号": 1,
            "创建人": str(normalized.get("创建人") or "值班管理员").strip() or "值班管理员",
            "创建时间": now,
            "更新时间": now,
            "published_at": now if status == STATUS_PUBLISHED else None,
            "deprecated_at": now if status == STATUS_DEPRECATED else None,
            "pending": status not in (STATUS_PUBLISHED, STATUS_DEPRECATED),
            "abnormal": status == STATUS_DEPRECATED,
        }
        store.rows(MODULE).append(entry)
        return self._public_entry(entry), []

    def update_entry(self, entry_id: int, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """修改预设字段；已废弃预设只读，已发布预设的修改会生成新的发布版本。"""
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"预设模板 {entry_id} 不存在或已归档"
        if entry.get("status") == STATUS_DEPRECATED:
            return None, f"预设模板 {entry_id} 已废弃，旧版本只供只读引用，不能再修改"

        normalized = self._normalize_values(values)
        for field in REQUIRED_FIELDS:
            if field in normalized and not str(normalized.get(field) or "").strip():
                return None, f"必填字段「{field}」不能为空"
        if "status" in normalized and normalized["status"] != entry.get("status"):
            # 发布状态只能通过动作流转，避免绕过评审/废弃规则直接改状态。
            return None, f"发布状态不能直接改为「{normalized['status']}」，请使用发布/废弃动作"

        preset_number = str(normalized.get("预设编号") or entry.get("预设编号") or "").strip()
        duplicated = any(
            int(row.get("id", 0)) != entry_id and str(row.get("预设编号", "")).strip() == preset_number
            for row in store.rows(MODULE)
        )
        if duplicated:
            return None, f"预设编号 {preset_number} 已存在"

        conditions_changed = False
        if "维度条件组" in normalized:
            new_conditions = self._stringify_condition_group(normalized["维度条件组"])
            conditions_changed = new_conditions != str(entry.get("维度条件组") or "")
            entry["维度条件组"] = new_conditions
        entry["预设编号"] = preset_number
        if "预设名称" in normalized:
            entry["预设名称"] = str(normalized.get("预设名称") or "").strip()
        if "时间范围" in normalized:
            entry["时间范围"] = str(normalized.get("时间范围") or "").strip()
        if "排序规则" in normalized:
            entry["排序规则"] = str(normalized.get("排序规则") or "").strip()
        if "显示列" in normalized:
            entry["显示列"] = self._stringify_display_columns(normalized.get("显示列"))
        if "共享状态" in normalized:
            entry["共享状态"] = self._normalize_share_status(normalized.get("共享状态"))
        if "status" in normalized and normalized["status"] != entry.get("status"):
            # 发布状态只能通过动作流转，避免绕过评审/废弃规则直接改状态。
            return None, f"发布状态不能直接改为「{normalized['status']}」，请使用发布/废弃动作"

        entry["修订号"] = int(entry.get("修订号") or 1) + 1
        entry["更新时间"] = self._now()
        if entry.get("status") == STATUS_PUBLISHED and conditions_changed:
            # 已发布预设改了条件：新条件当作新的评审稿，状态回落到「预设评审中」。
            # 版本号只在重新发布成功时递增；旧版本的快照与使用记录保持只读可查，
            # 重新发布后再从 0 开始累计新版本的使用次数。
            entry["status"] = STATUS_REVIEWING
            entry["发布状态"] = STATUS_REVIEWING
            entry["published_at"] = None
        entry["abnormal"] = entry.get("status") == STATUS_DEPRECATED
        entry["pending"] = entry.get("status") not in (STATUS_PUBLISHED, STATUS_DEPRECATED)
        return self._public_entry(entry), ""

    # ------------------------------------------------------------------ 流转

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"预设模板 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于筛选预设可执行范围"
        target = ACTION_RULES[action]
        current = entry.get("status")
        if current == target:
            return self._public_entry(entry), f"预设模板已是「{target}」状态，无需重复{action}"

        allowed_from = {
            "提交预设评审": {STATUS_DRAFT},
            "发布预设": {STATUS_REVIEWING},
            "废弃预设": {STATUS_DRAFT, STATUS_REVIEWING, STATUS_PUBLISHED},
        }[action]
        if current not in allowed_from:
            return None, f"预设模板当前状态为「{current}」，不能执行「{action}」"

        if action == "发布预设":
            error = self._publish_prerequisites(entry)
            if error:
                return None, error

        now = self._now()
        entry["status"] = target
        entry["发布状态"] = target
        entry["pending"] = target not in (STATUS_PUBLISHED, STATUS_DEPRECATED)
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        entry["更新时间"] = now
        if action == "发布预设":
            # 每次成功发布都对应一个可被使用计数引用的版本；使用次数按当前版本重新累计，
            # 历史版本的使用记录仍保留在 filterpreset_usage 中可追溯。
            entry["版本"] = max(int(entry.get("版本") or 0) + 1, 1)
            entry["使用次数"] = 0
            entry["published_at"] = now
        if action == "废弃预设":
            entry["deprecated_at"] = now
        return self._public_entry(entry), f"预设模板已{action}"

    def _publish_prerequisites(self, entry: dict[str, Any]) -> str:
        if not str(entry.get("预设编号") or "").strip() or not str(entry.get("预设名称") or "").strip():
            return "预设模板字段不完整，不能发布"
        if not self.parse_conditions(entry.get("维度条件组")):
            return "维度条件组为空或无法解析，不能发布"
        # 时间范围可以在生成快照时由上游「时间范围记录」补齐；真正缺失会在套用阶段
        # 明确拒绝生成快照，而不是在发布时把预设卡死。
        return ""

    # ------------------------------------------------------------------ 套用

    def apply_preset(self, entry_id: int, values: dict[str, Any] | None = None) -> tuple[dict[str, Any] | None, str]:
        """套用预设并生成筛选条件数据快照。

        只有「已发布」且允许共享的预设才能生成新快照；同一个幂等键（或直接回看已有
        snapshot_id）重复请求只返回原快照，不重复增加使用次数。详情/列表读取不会计数。
        """
        values = dict(values or {})
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"预设模板 {entry_id} 不存在或已归档"

        operator = str(values.get("operator") or values.get("操作人") or "值班管理员").strip() or "值班管理员"
        request_id = str(
            values.get("request_id")
            or values.get("requestId")
            or values.get("幂等键")
            or values.get("refresh_token")
            or ""
        ).strip()
        existing_snapshot_id = self._to_int(values.get("snapshot_id") or values.get("快照编号"))
        dependencies = values.get("dependencies") or values.get("依赖记录") or []

        if existing_snapshot_id is not None:
            snapshot = self._find_snapshot(existing_snapshot_id)
            if snapshot is None or int(snapshot.get("预设id") or 0) != entry_id:
                return None, f"筛选条件快照 {existing_snapshot_id} 不存在"
            return dict(snapshot), "仅回看已有快照，使用次数未增加"

        status = entry.get("status")
        if status == STATUS_DEPRECATED:
            return None, "预设模板已废弃，不能再被套用；历史快照仅支持只读回看"
        if status != STATUS_PUBLISHED:
            return None, f"预设模板当前状态为「{status}」，只有已发布预设才能套用"
        if entry.get("共享状态") != SHARE_TEAM and operator != str(entry.get("创建人") or ""):
            return None, "预设模板未共享，仅创建人可以套用"

        if not request_id:
            # 没有显式幂等键时按请求生成；调用方重复刷新时请回传同一个键或 snapshot_id。
            request_id = f"auto-{uuid.uuid4().hex}"

        # 同一预设的套用串行执行：无论调用方是否带幂等键，先拿到锁的请求先落库。
        with self._lock_for(entry_id):
            replay = self._find_usage(entry_id, request_id)
            if replay is not None:
                snapshot = self._find_snapshot(int(replay.get("快照id") or 0))
                if snapshot is not None:
                    return dict(snapshot), "重复套用已按幂等键去重，使用次数未增加"

            dependency_result = self._select_dependencies(dependencies)
            snapshot, error = self._build_snapshot(entry, operator, request_id, dependency_result)
            if snapshot is None:
                return None, error

            entry["使用次数"] = int(entry.get("使用次数") or 0) + 1
            snapshot["使用次数"] = entry["使用次数"]
            snapshot["使用序号"] = entry["使用次数"]
            store.aux_rows(SNAPSHOT_TABLE).append(snapshot)

            usage = {
                "id": store.next_id(USAGE_TABLE),
                "预设id": entry_id,
                "预设编号": entry.get("预设编号"),
                "预设版本": snapshot.get("预设版本"),
                "快照id": snapshot["id"],
                "快照编号": snapshot.get("快照编号"),
                "幂等键": request_id,
                "操作人": operator,
                "使用时间": snapshot.get("生成时间"),
                "结果": "成功",
            }
            store.aux_rows(USAGE_TABLE).append(usage)
            return dict(snapshot), f"预设模板已套用，生成筛选条件快照 {snapshot['快照编号']}"

    def _build_snapshot(
        self,
        entry: dict[str, Any],
        operator: str,
        request_id: str,
        dependency_result: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, str]:
        adopted = dependency_result.get("adopted", {})

        preset_number = str(entry.get("预设编号") or adopted.get("预设模板", {}).get("预设编号") or "").strip()
        preset_name = str(entry.get("预设名称") or adopted.get("预设模板", {}).get("预设名称") or "").strip()
        if not preset_number or not preset_name:
            return None, "上游预设模板记录未完成，无法生成筛选条件快照"

        conditions = self.parse_conditions(entry.get("维度条件组"))
        condition_source = "预设模板"
        if not conditions and "维度条件组" in adopted:
            conditions = self.parse_conditions(adopted["维度条件组"].get("维度条件组"))
            condition_source = "上游维度条件组"
        if not conditions:
            return None, "上游维度条件组为空或无法解析，无法生成筛选条件快照"

        time_range = self.parse_time_range(entry.get("时间范围"))
        time_source = "预设模板"
        if time_range is None and "时间范围" in adopted:
            time_range = self.parse_time_range(adopted["时间范围"].get("时间范围"))
            time_source = "上游时间范围记录"
        if time_range is None:
            return None, "上游时间范围记录缺失或无法识别，无法生成筛选条件快照"

        registry = self._dimension_registry()
        valid_conditions: list[dict[str, Any]] = []
        invalid_dimensions: list[dict[str, Any]] = []
        for condition in conditions:
            dimension = str(condition.get("维度") or "").strip()
            if dimension in registry:
                valid_conditions.append(condition)
            else:
                invalid_dimensions.append({
                    "维度": dimension or "（空维度）",
                    "原因": "维度未在维度映射或内置维度中登记" if dimension else "条件缺少维度名",
                    "原始条件": condition,
                })

        display_columns = self.parse_display_columns(entry.get("显示列"))
        if not display_columns:
            display_columns = ["日期", "指标值"]
        invalid_columns = [column for column in display_columns if column not in self._column_registry(registry)]
        sort_rule = str(entry.get("排序规则") or "").strip()

        snapshot_id = store.next_id(SNAPSHOT_TABLE)
        now = self._now()
        query = {
            "模块": "filterpreset",
            "预设编号": preset_number,
            "时间范围": time_range,
            "过滤条件": valid_conditions,
            "排序": sort_rule or "默认排序",
            "显示列": display_columns,
            "失效维度": [item["维度"] for item in invalid_dimensions],
            "说明": "失效维度已记录但不参与查询",
        }
        snapshot = {
            "id": snapshot_id,
            "快照编号": f"SNAP-{snapshot_id:04d}",
            "预设id": int(entry.get("id") or 0),
            "预设编号": preset_number,
            "预设名称": preset_name,
            "预设版本": int(entry.get("版本") or 0),
            "预设状态": entry.get("status"),
            "时间范围": time_range["原始"],
            "时间范围解析": time_range,
            "排序规则": sort_rule,
            "显示列": display_columns,
            "失效显示列": invalid_columns,
            "解析条件": conditions,
            "有效条件": valid_conditions,
            "失效维度": invalid_dimensions,
            "条件来源": condition_source,
            "时间范围来源": time_source,
            "依赖采纳": dependency_result.get("adopted_trace", []),
            "依赖跳过": dependency_result.get("skipped_trace", []),
            "查询": query,
            "使用次数": 0,
            "使用序号": 0,
            "操作人": operator,
            "幂等键": request_id,
            "生成时间": now,
            "只读": True,
        }
        return snapshot, ""

    # ------------------------------------------------------------------ 快照/使用记录

    def list_snapshots(self, entry_id: int | None = None) -> list[dict[str, Any]]:
        snapshots = store.aux_rows(SNAPSHOT_TABLE)
        if entry_id is not None:
            snapshots = [item for item in snapshots if int(item.get("预设id") or 0) == entry_id]
        return sorted(snapshots, key=lambda item: int(item.get("id") or 0), reverse=True)

    def latest_snapshot(self, entry_id: int) -> dict[str, Any] | None:
        snapshots = self.list_snapshots(entry_id)
        return dict(snapshots[0]) if snapshots else None

    def get_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        snapshot = self._find_snapshot(snapshot_id)
        return dict(snapshot) if snapshot else None

    def list_usage(self, entry_id: int | None = None) -> list[dict[str, Any]]:
        records = store.aux_rows(USAGE_TABLE)
        if entry_id is not None:
            records = [item for item in records if int(item.get("预设id") or 0) == entry_id]
        return sorted(records, key=lambda item: int(item.get("id") or 0), reverse=True)

    def consume_snapshot(self, snapshot_id: int, consumer: str) -> tuple[dict[str, Any] | None, str]:
        """供多维下钻等下游模块消费快照：只登记消费轨迹，不回写使用次数。"""
        snapshot = self._find_snapshot(snapshot_id)
        if snapshot is None:
            return None, f"筛选条件快照 {snapshot_id} 不存在"
        record = {
            "id": store.next_id(CONSUMER_TABLE),
            "快照id": snapshot_id,
            "快照编号": snapshot.get("快照编号"),
            "消费方": consumer,
            "预设编号": snapshot.get("预设编号"),
            "消费时间": self._now(),
        }
        store.aux_rows(CONSUMER_TABLE).append(record)
        result = dict(snapshot)
        result["消费方"] = consumer
        result["消费时间"] = record["消费时间"]
        return result, f"快照 {snapshot.get('快照编号')} 已由{consumer}消费"

    def list_consumers(self, snapshot_id: int) -> list[dict[str, Any]]:
        return [
            item
            for item in store.aux_rows(CONSUMER_TABLE)
            if int(item.get("快照id") or 0) == snapshot_id
        ]

    def _find_snapshot(self, snapshot_id: int) -> dict[str, Any] | None:
        for item in store.aux_rows(SNAPSHOT_TABLE):
            if int(item.get("id") or 0) == snapshot_id:
                return item
        return None

    def _find_usage(self, entry_id: int, request_id: str) -> dict[str, Any] | None:
        for item in store.aux_rows(USAGE_TABLE):
            if int(item.get("预设id") or 0) == entry_id and item.get("幂等键") == request_id:
                return item
        return None

    # ------------------------------------------------------------------ 解析

    def parse_conditions(self, raw: Any) -> list[dict[str, Any]]:
        """把维度条件组解析为完整条件列表，保留每一个条件。"""
        items = self._load_json(raw)
        if isinstance(items, dict):
            items = items.get("conditions") or items.get("条件组") or items.get("维度条件组") or items
        if isinstance(items, dict):
            items = [items]
        if isinstance(items, list):
            conditions = [self._normalize_condition(item) for item in items]
            return [item for item in conditions if item]
        text = str(raw or "").strip()
        if not text:
            return []
        conditions = []
        for part in self._split_condition_text(text):
            condition = self._normalize_condition(part)
            if condition:
                conditions.append(condition)
        return conditions

    def _split_condition_text(self, text: str) -> list[str]:
        """按分号/逗号/换行拆分文本条件，但不拆 [] 里的值列表。"""
        parts: list[str] = []
        buffer: list[str] = []
        depth = 0
        for char in text:
            if char in "[{(":
                depth += 1
            elif char in "]})":
                depth = max(depth - 1, 0)
            if depth == 0 and char in "\n;；,，":
                parts.append("".join(buffer))
                buffer = []
                continue
            buffer.append(char)
        parts.append("".join(buffer))
        return parts

    def parse_time_range(self, raw: Any) -> dict[str, Any] | None:
        text = str(raw or "").strip()
        if not text:
            return None
        candidates = [part.strip() for part in DATE_RANGE_SPLIT_RE.split(text) if part.strip()]
        if not candidates:
            return None
        if RELATIVE_RANGE_RE.match(text):
            return {"原始": text, "起始": None, "结束": None, "类型": "相对区间"}
        if len(candidates) == 1 and "," in text:
            candidates = [part.strip() for part in text.split(",") if part.strip()]
        try:
            if len(candidates) == 1:
                start = end = date.fromisoformat(candidates[0])
            elif len(candidates) == 2:
                start = date.fromisoformat(candidates[0])
                end = date.fromisoformat(candidates[1])
            else:
                return None
        except ValueError:
            return None
        if end < start:
            start, end = end, start
        return {
            "原始": text,
            "起始": start.isoformat(),
            "结束": end.isoformat(),
            "类型": "绝对区间" if start != end else "单日",
        }

    def parse_display_columns(self, raw: Any) -> list[str]:
        items = self._load_json(raw)
        if isinstance(items, dict):
            items = items.get("columns") or items.get("显示列") or list(items.keys())
        if isinstance(items, list):
            columns = [str(item).strip() for item in items]
        else:
            columns = [part.strip() for part in re.split(r"[,，;；、|]+", str(raw or ""))]
        result: list[str] = []
        for column in columns:
            if column and column not in result:
                result.append(column)
        return result

    # ------------------------------------------------------------------ 内部工具

    def _select_dependencies(self, dependencies: Any) -> dict[str, Any]:
        """按提交顺序校验上游依赖，每类只采纳首个通过校验的记录。"""
        adopted: dict[str, dict[str, Any]] = {}
        adopted_trace: list[dict[str, Any]] = []
        skipped_trace: list[dict[str, Any]] = []
        if not isinstance(dependencies, list):
            return {"adopted": adopted, "adopted_trace": adopted_trace, "skipped_trace": skipped_trace}

        for index, dependency in enumerate(dependencies, start=1):
            if not isinstance(dependency, dict):
                skipped_trace.append({"顺序": index, "原因": "依赖记录格式不可识别"})
                continue
            kind = str(
                dependency.get("kind")
                or dependency.get("类型")
                or dependency.get("name")
                or dependency.get("名称")
                or ""
            ).strip()
            values = dependency.get("values")
            if not isinstance(values, dict):
                values = {
                    key: value
                    for key, value in dependency.items()
                    if key not in {"kind", "类型", "name", "名称", "values"}
                }
            kind_alias = {
                "预设模板": "预设模板",
                "preset": "预设模板",
                "维度条件组": "维度条件组",
                "conditions": "维度条件组",
                "时间范围": "时间范围",
                "time_range": "时间范围",
            }.get(kind, kind)
            if kind_alias not in {"预设模板", "维度条件组", "时间范围"}:
                skipped_trace.append({"顺序": index, "类型": kind, "原因": "未知依赖类型"})
                continue
            if kind_alias in adopted:
                skipped_trace.append({"顺序": index, "类型": kind_alias, "原因": "已采纳更早通过的同类依赖"})
                continue

            valid, reason = self._check_dependency(kind_alias, values)
            if not valid:
                skipped_trace.append({"顺序": index, "类型": kind_alias, "原因": reason})
                continue
            adopted[kind_alias] = values
            adopted_trace.append({"顺序": index, "类型": kind_alias, "结果": "采纳"})
        return {"adopted": adopted, "adopted_trace": adopted_trace, "skipped_trace": skipped_trace}

    def _check_dependency(self, kind: str, values: dict[str, Any]) -> tuple[bool, str]:
        if kind == "预设模板":
            if not str(values.get("预设编号") or "").strip():
                return False, "缺少预设编号"
            if not str(values.get("预设名称") or "").strip():
                return False, "缺少预设名称"
            return True, ""
        if kind == "维度条件组":
            if not self.parse_conditions(values.get("维度条件组")):
                return False, "维度条件组为空或无法解析"
            return True, ""
        if kind == "时间范围":
            if self.parse_time_range(values.get("时间范围")) is None:
                return False, "时间范围缺失或无法识别"
            return True, ""
        return False, "未知依赖类型"

    def _dimension_registry(self) -> set[str]:
        registry = set(BUILTIN_DIMENSIONS)
        for row in store.rows("dimensionmap"):
            for field in ("原始维度名", "标准维度名"):
                value = str(row.get(field) or "").strip()
                if value:
                    registry.add(value)
        return registry

    def _column_registry(self, dimensions: set[str]) -> set[str]:
        return set(BUILTIN_COLUMNS) | set(dimensions)

    def _normalize_condition(self, item: Any) -> dict[str, Any] | None:
        if isinstance(item, dict):
            dimension = self._pick(item, CONDITION_KEYS["dimension"])
            value = self._pick(item, CONDITION_KEYS["value"])
            operator = self._pick(item, CONDITION_KEYS["operator"]) or DEFAULT_OPERATOR
            condition = {
                "维度": str(dimension or "").strip(),
                "操作符": str(operator).strip() or DEFAULT_OPERATOR,
                "值": value if value is not None else "",
            }
            if not condition["维度"] and condition["值"] == "":
                return None
            return condition
        text = str(item or "").strip()
        if not text:
            return None
        match = CONDITION_RE.match(text)
        if match:
            value: Any = match.group("value").strip()
            value = self._parse_literal_value(value)
            operator = match.group("operator") or match.group("word") or DEFAULT_OPERATOR
            return {
                "维度": match.group("dimension").strip(),
                "操作符": operator.strip(),
                "值": value,
            }
        # 纯文本条件无法拆出维度/值时，仍保留下来并在快照中记为失效维度，
        # 这样不会丢条件，也不会让它悄悄参与查询。
        return {"维度": text, "操作符": DEFAULT_OPERATOR, "值": ""}

    def _parse_literal_value(self, text: str) -> Any:
        stripped = text.strip()
        if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in "\"'":
            stripped = stripped[1:-1]
        if stripped.startswith("[") and stripped.endswith("]"):
            parsed = self._load_json(stripped)
            if isinstance(parsed, list):
                return parsed
        return stripped

    def _normalize_values(self, values: dict[str, Any]) -> dict[str, Any]:
        normalized: dict[str, Any] = {}
        for key, value in (values or {}).items():
            target = FIELD_ALIASES.get(key, key)
            normalized[target] = value
        return normalized

    def _normalize_share_status(self, value: Any) -> str:
        text = str(value or "").strip()
        if text in SHARE_STATES:
            return text
        if text in {"共享", "已共享", "公开", "team"}:
            return SHARE_TEAM
        return SHARE_PRIVATE

    def _stringify_condition_group(self, value: Any) -> str:
        if isinstance(value, JSON_TYPES):
            return json.dumps(value, ensure_ascii=False)
        return str(value or "").strip()

    def _stringify_display_columns(self, value: Any) -> str:
        if isinstance(value, list):
            return ",".join(str(item).strip() for item in value if str(item).strip())
        return str(value or "").strip()

    def _public_entry(self, entry: dict[str, Any] | None) -> dict[str, Any] | None:
        if entry is None:
            return None
        row = dict(entry)
        # 同时暴露 status 与「发布状态」，列表、详情和导出都用同一份值。
        row["发布状态"] = row.get("status")
        row["使用次数"] = int(row.get("使用次数") or 0)
        return row

    def _pick(self, item: dict[str, Any], keys: tuple[str, ...]) -> Any:
        for key in keys:
            if key in item and item[key] is not None:
                return item[key]
        return None

    def _load_json(self, raw: Any) -> Any:
        if isinstance(raw, JSON_TYPES):
            return raw
        text = str(raw or "").strip()
        if not text or text[0] not in "[{":
            return None
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return None

    def _to_int(self, value: Any) -> int | None:
        if value is None or str(value).strip() == "":
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _now(self) -> str:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
