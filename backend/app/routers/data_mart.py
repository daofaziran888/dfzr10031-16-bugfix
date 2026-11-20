"""数据集市接口：维护集市表，覆盖提交建模、发布集市、下线集市等动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.data_mart import DataMartService
from app.services.ledger import ledger_service

router = APIRouter(prefix="/api/data_mart", tags=["数据集市"])

service = DataMartService()

LIST_FIELDS = ["表编号", "表名称", "主题域", "事实粒度", "关联维度", "度量字段", "刷新周期", "表状态"]
STATUSES = ["草稿", "已建模", "已发布", "已下线"]


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出数据集市清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "data_mart", "total": total, "items": items}


@router.get("/balances")
def available_balances(
    biz_date: str = Query(description="业务日 YYYY-MM-DD"),
    warehouse: str | None = Query(default=None, description="按仓过滤"),
    sku: str | None = Query(default=None, description="按商品过滤"),
) -> dict[str, Any]:
    """集市口径的按仓可用结余：由关账批次账本生成，未关账的日期不给数。"""
    payload = ledger_service.balances(biz_date, warehouse=warehouse, sku=sku)
    return {"module": "data_mart", **payload}


@router.get("", response_model=PageResult[dict])
def list_entries(
    request: Request,
    keyword: str | None = Query(default=None, description="按表编号检索"),
    status: str | None = Query(default=None, description="草稿、已建模、已发布、已下线"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按表编号与状态过滤数据集市列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    extra = {k: v for k, v in request.query_params.items() if k not in ("keyword", "status", "page", "size") and v.strip()}
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size, extra=extra)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条集市表明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"集市表 {entry_id} 不存在或已归档")
    return entry


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条集市表，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="集市表已登记", entry=entry)


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条集市表执行提交建模、发布集市、下线集市；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
