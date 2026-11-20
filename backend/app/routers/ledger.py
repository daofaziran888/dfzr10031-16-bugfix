"""关账账本接口：登记出入库、跨仓调拨、盘点，关账并回看各版本账本。

页面上的「关账记录」「账页」「两日核对」「批次溯源」全部走这里；数据集市
取可用结余也走同一份账本，只认已关账的批次。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload
from app.services.ledger import LedgerError, ledger_service

router = APIRouter(prefix="/api/ledger", tags=["关账账本"])

_STATUS = {
    "DATE_INVALID": 400,
    "BATCH_NOT_FOUND": 404,
    "WH_UNKNOWN": 404,
    "SKU_REQUIRED": 400,
    "QTY_INVALID": 400,
    "COUNT_INVALID": 400,
    "LOT_REQUIRED": 400,
    "TRANSFER_UNBALANCED": 409,
    "IDEMPOTENCY_CONFLICT": 409,
    "WH_EXISTS": 409,
}


def _guard(handler, *args, **kwargs) -> ActionResult:
    try:
        payload = handler(*args, **kwargs)
    except LedgerError as error:
        return ActionResult(ok=False, message=error.message, entry=error.as_dict())
    return ActionResult(ok=True, message=str(payload.get("message", "已处理")), entry=payload)


def _read(handler, *args, **kwargs) -> dict[str, Any]:
    try:
        return handler(*args, **kwargs)
    except LedgerError as error:
        raise HTTPException(status_code=_STATUS.get(error.code, 400),
                            detail=error.as_dict())


@router.get("/catalog", response_model=dict)
def catalog() -> dict[str, Any]:
    """仓库、商品、期初结余与已关账的业务日。"""
    return ledger_service.catalog()


@router.post("/seed-demo", response_model=dict)
def seed_demo(force: bool = Query(default=False, description="true 时覆盖现有账本")) -> dict[str, Any]:
    """预置一份两日账本（第 1 日漏记 10 件调拨），便于直接复核修订逻辑。"""
    return ledger_service.seed_demo(force=force)


@router.post("/movements", response_model=ActionResult)
def record_movement(payload: EntryPayload) -> ActionResult:
    """登记一条出入库变动（正数入库、负数出库），幂等键重复时回放原结果。"""
    return _guard(ledger_service.record_movement, payload.values)


@router.post("/transfers", response_model=ActionResult)
def record_transfer(payload: EntryPayload) -> ActionResult:
    """登记跨仓调拨：出库仓与入库仓成对落账，只提交一侧会被拒绝。"""
    return _guard(ledger_service.record_transfer, payload.values)


@router.post("/counts", response_model=ActionResult)
def record_count(payload: EntryPayload) -> ActionResult:
    """登记盘点合计；同一 count_version 幂等，换版只替换不叠加。"""
    return _guard(ledger_service.record_count, payload.values)


@router.post("/close", response_model=ActionResult)
def close_day(payload: EntryPayload) -> ActionResult:
    """关账某业务日；重复关账返回首发批次，不会重复盖章。"""
    return _guard(ledger_service.close_day, payload.values)


@router.get("/days/{biz_date}", response_model=dict)
def day(
    biz_date: str,
    warehouse: str | None = Query(default=None, description="按仓过滤"),
    sku: str | None = Query(default=None, description="按商品过滤"),
    edition: int | None = Query(default=None, description="取第几版账本，1 为首发"),
    batch_id: str | None = Query(default=None, description="按批次号取，优先于 edition"),
) -> dict[str, Any]:
    """某业务日的关账截面；带 edition 或 batch_id 可回看历史版本。"""
    return _read(
        ledger_service.day, biz_date,
        warehouse=warehouse, sku=sku, edition=edition, batch_id=batch_id,
    )


@router.get("/days/{biz_date}/editions", response_model=dict)
def editions(biz_date: str, warehouse: str | None = None, sku: str | None = None) -> dict[str, Any]:
    """某业务日所有版本（首发 + 每次修订）的逐行差额。"""
    return _read(ledger_service.editions, biz_date, warehouse=warehouse, sku=sku)


@router.get("/days/{biz_date}/verify", response_model=dict)
def verify(biz_date: str) -> dict[str, Any]:
    """独立核对某日账本：调拨守恒、结余公式、与前一日的衔接。"""
    return _read(ledger_service.verify, biz_date)


@router.get("/delta", response_model=dict)
def delta(
    earlier: str = Query(description="前一日 YYYY-MM-DD"),
    later: str = Query(description="后一日 YYYY-MM-DD"),
    warehouse: str | None = None,
    sku: str | None = None,
) -> dict[str, Any]:
    """两日账本关系：后一日期初 vs 前一日期末，差额逐仓列出。"""
    return _read(ledger_service.delta, earlier, later, warehouse=warehouse, sku=sku)


@router.get("/batches", response_model=dict)
def batches(biz_date: str | None = None) -> dict[str, Any]:
    """关账批次列表；带 biz_date 时只看那一天的各版本。"""
    return _read(ledger_service.batches, biz_date=biz_date)


@router.get("/batches/{batch_id}", response_model=dict)
def batch(batch_id: str) -> dict[str, Any]:
    """按批次号取冻结截面（含当时逐行账页与自检结果）。"""
    payload = ledger_service.batch(batch_id)
    if payload is None:
        return {"ok": False, "code": "BATCH_NOT_FOUND", "message": f"批次 {batch_id} 不存在"}
    return {"ok": True, "batch": payload}


@router.get("/trace", response_model=dict)
def trace(
    transfer_id: str | None = None,
    idempotency_key: str | None = None,
    ref: str | None = None,
    count_id: str | None = None,
) -> dict[str, Any]:
    """追溯一笔补录：实际结余、当天调整、命中/新增了哪些批次。"""
    return ledger_service.trace(
        transfer_id=transfer_id, idempotency_key=idempotency_key,
        ref=ref, count_id=count_id,
    )


@router.get("/balances", response_model=dict)
def balances(
    biz_date: str = Query(description="业务日 YYYY-MM-DD"),
    warehouse: str | None = None,
    sku: str | None = None,
) -> dict[str, Any]:
    """数据集市口径的按仓可用结余；未关账的日期不给数。"""
    return _read(ledger_service.balances, biz_date, warehouse=warehouse, sku=sku)
