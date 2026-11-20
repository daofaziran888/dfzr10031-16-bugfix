"""关账账本的核心规则测试。

覆盖需求里的每条硬约束：调拨守恒、重复回补幂等、两仓并发只记一次、盘点换版
不重复增减、晚到更正重述到正确业务日、数据集市只认关账批次、重启后截面一致。
"""
from __future__ import annotations

import threading

import pytest

from app.services.ledger import LedgerError, LedgerService

DAY1 = "2026-09-01"
DAY2 = "2026-09-02"
SKU = "SKU-1001"


def _rows(service: LedgerService, day: str, **kwargs):
    return {(r["warehouse"], r["sku"]): r for r in service.day(day, **kwargs)["rows"]}


def _transfer(service: LedgerService, **overrides):
    payload = {
        "from_warehouse": "WH-A", "to_warehouse": "WH-B", "sku": SKU,
        "biz_date": DAY1, "qty": 10, "lot": "LOT-01", "transfer_id": "TRF-0001",
    }
    payload.update(overrides)
    return service.record_transfer(payload)


# ---------------------------------------------------------------------------
# 调拨守恒 & 幂等
# ---------------------------------------------------------------------------

def test_transfer_lands_both_sides_and_conserves(service: LedgerService) -> None:
    result = _transfer(service)
    assert result["conserved"] is True
    assert [line["qty"] for line in result["lines"]] == [-10, 10]
    rows = _rows(service, DAY1)
    assert rows[("WH-A", SKU)]["closing_qty"] == 90
    assert rows[("WH-B", SKU)]["closing_qty"] == 90


def test_transfer_rejects_single_sided_submission(service: LedgerService) -> None:
    with pytest.raises(LedgerError) as error:
        service.record_transfer({**{
            "from_warehouse": "WH-A", "to_warehouse": "WH-A", "sku": SKU,
            "biz_date": DAY1, "qty": 10, "lot": "LOT-01",
        }})
    assert error.value.code == "TRANSFER_UNBALANCED"
    with pytest.raises(LedgerError) as error:
        service.record_transfer({
            "from_warehouse": "WH-A", "to_warehouse": "WH-B", "sku": SKU,
            "biz_date": DAY1, "qty": -10, "lot": "LOT-01",
        })
    assert error.value.code == "TRANSFER_UNBALANCED"


def test_repeated_backfill_is_idempotent(service: LedgerService) -> None:
    first = _transfer(service)
    second = _transfer(service)
    third = _transfer(service, idempotency_key="TRF-0001")
    assert first["replayed"] is False
    assert second["replayed"] is True
    assert third["replayed"] is True
    assert len(service.trace(transfer_id="TRF-0001")["movements"]) == 2
    rows = _rows(service, DAY1)
    assert rows[("WH-A", SKU)]["closing_qty"] == 90
    assert rows[("WH-B", SKU)]["closing_qty"] == 90


def test_concurrent_transfer_from_both_warehouses_records_once(service: LedgerService) -> None:
    barrier = threading.Barrier(2)
    outcomes: list[dict] = []
    errors: list[Exception] = []

    def submit(from_wh: str, to_wh: str) -> None:
        try:
            barrier.wait(timeout=5)
            outcomes.append(_transfer(service, from_warehouse=from_wh, to_warehouse=to_wh))
        except Exception as exc:  # pragma: no cover - 失败时由断言暴露
            errors.append(exc)

    threads = [
        threading.Thread(target=submit, args=("WH-A", "WH-B")),
        threading.Thread(target=submit, args=("WH-B", "WH-A")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert not errors
    assert sorted(item["replayed"] for item in outcomes) == [False, True]
    movements = service.trace(transfer_id="TRF-0001")["movements"]
    # 只保留最先落账的那一对，后到的一侧不再产生反向调拨
    assert sorted(line["qty"] for line in movements) == [-10, 10]
    rows = _rows(service, DAY1)
    assert rows[("WH-A", SKU)]["closing_qty"] + rows[("WH-B", SKU)]["closing_qty"] == 180


# ---------------------------------------------------------------------------
# 盘点
# ---------------------------------------------------------------------------

def test_count_versions_replace_instead_of_stacking(service: LedgerService) -> None:
    service.record_count({"warehouse": "WH-A", "sku": SKU, "biz_date": DAY1,
                          "counted_qty": 97, "lot": "LOT-01", "count_version": "v1"})
    rows = _rows(service, DAY1)
    assert rows[("WH-A", SKU)]["count_adjustment"] == -3
    assert rows[("WH-A", SKU)]["closing_qty"] == 97

    # 同版本重提：幂等回放，不再扣一次
    replay = service.record_count({"warehouse": "WH-A", "sku": SKU, "biz_date": DAY1,
                                   "counted_qty": 97, "lot": "LOT-01",
                                   "count_version": "v1"})
    assert replay["replayed"] is True
    rows = _rows(service, DAY1)
    assert rows[("WH-A", SKU)]["closing_qty"] == 97
    assert rows[("WH-A", SKU)]["count_adjustment"] == -3

    # 换版：只替换盘点数，不叠加
    service.record_count({"warehouse": "WH-A", "sku": SKU, "biz_date": DAY1,
                          "counted_qty": 95, "lot": "LOT-01", "count_version": "v2"})
    rows = _rows(service, DAY1)
    assert rows[("WH-A", SKU)]["counted_qty"] == 95
    assert rows[("WH-A", SKU)]["count_adjustment"] == -5
    assert rows[("WH-A", SKU)]["closing_qty"] == 95
    assert [v["version"] for v in rows[("WH-A", SKU)]["count_versions"]] == ["v1", "v2"]
    # 盘盈亏不向下一天结转成重复变动
    rows_day2 = _rows(service, DAY2)
    assert rows_day2[("WH-A", SKU)]["opening_qty"] == 95
    assert rows_day2[("WH-A", SKU)]["net_movement"] == 0


# ---------------------------------------------------------------------------
# 晚到更正：重述两日账本
# ---------------------------------------------------------------------------

def _closed_two_days(service: LedgerService) -> None:
    service.close_day({"biz_date": DAY1})
    service.record_movement({"warehouse": "WH-A", "sku": SKU, "biz_date": DAY2,
                             "qty": -4, "lot": "LOT-02", "ref": "销售出库"})
    service.record_movement({"warehouse": "WH-B", "sku": SKU, "biz_date": DAY2,
                             "qty": 6, "lot": "LOT-02", "ref": "采购入库"})
    service.close_day({"biz_date": DAY2})


def test_late_correction_restates_both_days_and_keeps_first_edition(service: LedgerService) -> None:
    _closed_two_days(service)

    first_day2 = service.day(DAY2, edition=1)
    assert first_day2["rows"][0]["opening_qty"] == 100  # 甲仓按住旧账开账

    result = _transfer(service)  # 第 1 日的 10 件晚到
    revised_days = [item["biz_date"] for item in result["restatements"]]
    assert revised_days == [DAY1, DAY2]

    for day in (DAY1, DAY2):
        editions = service.editions(day)["items"]
        assert [item["edition"] for item in editions] == [1, 2]
        latest = editions[-1]["rows"]
        assert {(r["warehouse"]): r["closing_delta"] for r in latest} == {
            "WH-A": -10, "WH-B": 10,
        }

    # 首发账本仍可回看，且没有被就地改写
    assert service.day(DAY1, edition=1)["rows"][0]["closing_qty"] == 100
    assert service.day(DAY1, edition=1)["rows"][1]["closing_qty"] == 80
    # 最新版：第 1 日结余改成正确值，第 2 日期初跟着重述
    assert service.day(DAY1)["rows"][0]["closing_qty"] == 90
    assert service.day(DAY2)["rows"][0]["opening_qty"] == 90
    # 第 2 日自己的经营变动保持原样，没有被混入补记差额
    assert service.day(DAY2)["rows"][0]["net_movement"] == -4
    assert service.day(DAY2)["rows"][1]["net_movement"] == 6
    relation = service.delta(DAY1, DAY2)
    assert relation["balanced"] is True
    assert {item["warehouse"]: item["carry_delta"] for item in relation["items"]} == {
        "WH-A": 0, "WH-B": 0,
    }


def test_correction_arrives_a_day_late_for_earlier_closed_day(service: LedgerService) -> None:
    _closed_two_days(service)
    trace = service.trace(transfer_id="TRF-0001")
    assert trace["found"] is False

    _transfer(service)
    trace = service.trace(transfer_id="TRF-0001")
    assert trace["found"] is True
    by_wh = {(item["warehouse"], item["biz_date"]): item for item in trace["days"]}
    assert by_wh[("WH-A", DAY1)]["day_adjustment"] == -10
    assert by_wh[("WH-B", DAY1)]["day_adjustment"] == 10
    # 来源批号：首发与修订都能追溯到，且只有修订版包含这笔补录
    for key in by_wh:
        batches = by_wh[key]["batches"]
        assert [b["edition"] for b in batches] == [1, 2]
        assert [b["contains_line"] for b in batches] == [False, True]


# ---------------------------------------------------------------------------
# 数据集市与重启
# ---------------------------------------------------------------------------

def test_market_balances_follow_closed_batches_only(service: LedgerService) -> None:
    assert service.balances(DAY1)["available"] is False
    service.close_day({"biz_date": DAY1})
    first = service.balances(DAY1)
    assert first["available"] is True
    assert [(i["warehouse"], i["available_qty"]) for i in first["items"]] == [
        ("WH-A", 100), ("WH-B", 80),
    ]
    _transfer(service)
    latest = service.balances(DAY1)
    assert latest["source_batch_id"] != first["source_batch_id"]
    assert latest["edition"] == 2
    assert [(i["warehouse"], i["available_qty"]) for i in latest["items"]] == [
        ("WH-A", 90), ("WH-B", 90),
    ]
    # 首发批次按批号取仍得旧结余，两个截面可独立核对
    frozen = service.batch(first["source_batch_id"])
    assert [(r["warehouse"], r["closing_qty"]) for r in frozen["rows"]] == [
        ("WH-A", 100), ("WH-B", 80),
    ]


def test_snapshot_is_stable_across_reads_and_restart(tmp_path) -> None:
    path = str(tmp_path / "ledger.json")
    service = LedgerService(path=path)
    _closed_two_days(service)
    _transfer(service)

    before_day1 = service.day(DAY1)
    before_day2 = service.day(DAY2)
    before_delta = service.delta(DAY1, DAY2)
    # 同一进程内重复取数完全一致
    assert service.day(DAY1) == before_day1
    assert service.day(DAY2) == before_day2

    restarted = LedgerService(path=path)
    assert restarted.day(DAY1) == before_day1
    assert restarted.day(DAY2) == before_day2
    assert restarted.delta(DAY1, DAY2) == before_delta
    assert restarted.balances(DAY2) == service.balances(DAY2)
    # 重启后再提交同一笔回补，依然幂等
    replay = restarted.record_transfer({
        "from_warehouse": "WH-A", "to_warehouse": "WH-B", "sku": SKU,
        "biz_date": DAY1, "qty": 10, "lot": "LOT-01", "transfer_id": "TRF-0001",
    })
    assert replay["replayed"] is True
    assert restarted.day(DAY1) == before_day1


def test_verify_catches_consistent_books(service: LedgerService) -> None:
    _closed_two_days(service)
    _transfer(service)
    for day in (DAY1, DAY2):
        report = service.verify(day)
        assert report["ok"] is True
        assert report["transfers_conserved"] is True
        assert report["rows_consistent"] is True
        assert all(item["ok"] for item in report["carry_checks"])
