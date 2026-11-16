"""筛选预设接口：维护预设模板，覆盖保存、详情、套用、快照与使用记录查询。

路由层只做参数搬运，所有校验、状态流转和计数都在 app/services/filterpreset.py。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.filterpreset import STATUS_ORDER as SERVICE_STATUSES
from app.services.filterpreset import FilterpresetService

router = APIRouter(prefix="/api/filterpreset", tags=["筛选预设"])

service = FilterpresetService()

LIST_FIELDS = ["预设编号", "预设名称", "维度条件组", "时间范围", "排序规则", "显示列", "共享状态", "使用次数"]
STATUSES = SERVICE_STATUSES


@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按预设编号或预设名称检索"),
    status: str | None = Query(default=None, description="草稿、预设评审中、已发布、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按预设编号/名称与状态过滤筛选预设列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出筛选预设清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "filterpreset", "total": total, "items": items}


@router.get("/snapshots", response_model=dict)
def list_snapshots(entry_id: int | None = Query(default=None, description="只看某个预设的快照")) -> dict[str, Any]:
    """筛选条件数据快照列表：失效维度、显示列与使用次数都在快照里可直接追溯。"""
    snapshots = service.list_snapshots(entry_id)
    return {"total": len(snapshots), "items": snapshots}


@router.get("/snapshots/{snapshot_id}", response_model=dict)
def get_snapshot(snapshot_id: int) -> dict:
    """读取单个筛选条件数据快照（含消费方）。"""
    snapshot = service.get_snapshot(snapshot_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"筛选条件快照 {snapshot_id} 不存在")
    snapshot["消费方"] = service.list_consumers(snapshot_id)
    return snapshot


@router.post("/snapshots/{snapshot_id}/consume", response_model=ActionResult)
def consume_snapshot(snapshot_id: int, payload: EntryPayload) -> ActionResult:
    """下游模块（如多维下钻）消费快照；只登记消费轨迹，不回写使用次数。"""
    consumer = str(payload.values.get("consumer") or payload.values.get("消费方") or "").strip()
    if not consumer:
        return ActionResult(ok=False, message="缺少消费方名称")
    snapshot, message = service.consume_snapshot(snapshot_id, consumer)
    if snapshot is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=snapshot)


@router.get("/usage", response_model=dict)
def list_usage(entry_id: int | None = Query(default=None, description="只看某个预设的使用记录")) -> dict[str, Any]:
    """使用记录：每条记录都绑定一个已发布版本与一个快照，可核对计数来源。"""
    records = service.list_usage(entry_id)
    return {"total": len(records), "items": records}


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条预设详情：与套用入口共用同一份条件解析结果。"""
    entry = service.get_entry_detail(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"预设模板 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条预设模板，字段整体保存，缺必填字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"预设模板未保存：{'、'.join(missing)}")
    return ActionResult(ok=True, message="预设模板已登记", entry=entry)


@router.put("/{entry_id}", response_model=ActionResult)
def update_entry(entry_id: int, payload: EntryPayload) -> ActionResult:
    """修改预设模板字段；已废弃预设只读，不能被再次编辑。"""
    entry, message = service.update_entry(entry_id, payload.values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message="预设模板已保存", entry=entry)


@router.post("/{entry_id}/apply", response_model=ActionResult)
def apply_preset(entry_id: int, payload: EntryPayload) -> ActionResult:
    """套用已发布预设并生成筛选条件快照。

    ``request_id`` 是幂等键：前端重复刷新时沿用同一个键，使用次数只增加一次；
    回传 ``snapshot_id`` 则直接只读回看已有快照，不增加使用次数。
    """
    snapshot, message = service.apply_preset(entry_id, payload.values)
    if snapshot is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=snapshot)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条预设模板执行提交预设评审、发布预设、废弃预设；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
