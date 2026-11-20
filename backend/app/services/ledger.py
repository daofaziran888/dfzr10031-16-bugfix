"""关账账本业务规则：出入库变动、跨仓调拨、盘点合计与业务日结余。

数据快照只登记了校验哈希，能证明「取过数」，但证明不了「那天账上应该是多少」。
关账账本把每个仓、每个商品、每个业务日的出入库和盘点冻成批次，批次一经发布
就只读；后续任何补录都只能追加**修订批次**，所以页面刷新、再次取数、重启服务
看到的都是同一份关账截面。

三条硬规则
==========

1. **业务日挂账。** 补录可以晚到，但一定记进它实际发生的 ``biz_date``，再从
   那天往后逐日重算。已经关账的日期不会被静默改写，而是追加修订批次；首发
   批次（``edition=1``）永远可以按 ``edition`` 或 ``batch_id`` 回看。

2. **调拨守恒 + 幂等。** 跨仓调拨一次落两行（出库仓负数、入库仓正数），两行
   共用 ``transfer_id``、数量绝对值相等，接口拒绝单边提交。幂等键优先取
   ``idempotency_key``，否则按业务内容（仓、商品、业务日、数量、批次、单据号）
   派生：内容完全相同的重复提交只回放第一次的结果，不参与任何求和。确实需要
   两笔内容相同的业务时，给不同的 ``ref`` 或显式幂等键。两个仓并发提交同一笔
   调拨时，写锁把它们排成先后，后到的命中幂等键，账本里只会出现一组调拨行。

3. **盘点换版是替换，不是叠加。** 盘点合计是当日实盘数，结余取最新版本并
   重算 ``count_adjustment = 实盘 - 账面``；同一版本重复提交直接回放，换版
   则重新计算盘盈亏。反复盘点不会把盘盈亏叠加多次。

修订策略
========

以「第 1 日漏记甲仓转乙仓 10 件、第 2 日两仓已按旧账开账」为例，本实现选择
**同时发布两日修订账本**，而不是只在次日补一笔差额：

* 第 1 日的关账结余本身就是错的，只在第 2 日补差额会让第 1 日的历史报表
  永远错着，而且第 2 日的经营变动与补记混在同一行，核对时分不开；
* 发布修订批次后，第 1 日结余直接改成正确值，第 2 日的**期初**跟着重述，
  第 2 日的当天出入库（``net_movement``）保持原样，差额落在哪一天一目了然；
* 首发批次仍在账本里，``GET /api/ledger/days/{date}?edition=1`` 可回看；
* 修订行带 ``opening_delta`` / ``closing_delta`` / ``previous_batch_id``，
  数据集市按批次号取数，能独立核对每一版差额。
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from copy import deepcopy
from datetime import date, datetime
from typing import Any, Callable

DATA_DIR = os.environ.get("LEDGER_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "data"
)
LEDGER_PATH = os.path.join(DATA_DIR, "ledger.json")

DEFAULT_WAREHOUSES = [
    {"code": "WH-A", "name": "甲仓"},
    {"code": "WH-B", "name": "乙仓"},
]
DEFAULT_SKUS = [{"code": "SKU-1001", "name": "商品 1001"}]
DEFAULT_OPENING = {"WH-A": {"SKU-1001": 100}, "WH-B": {"SKU-1001": 80}}


class LedgerError(Exception):
    """业务校验失败：路由层只把 code/message 原样返回。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)

    def as_dict(self) -> dict[str, Any]:
        return {"ok": False, "code": self.code, "message": self.message}


def _parse_day(value: Any) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except (TypeError, ValueError):
        raise LedgerError("DATE_INVALID", f"业务日 {value!r} 不是 YYYY-MM-DD")


def _as_int(value: Any, field: str) -> int:
    if value is None or value == "" or isinstance(value, bool):
        raise LedgerError("QTY_INVALID", f"{field} 必须是整数")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise LedgerError("QTY_INVALID", f"{field} 必须是整数")


def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


class LedgerService:
    """进程内单例，落盘可重启；读写共用一把可重入锁，写操作整体事务化。"""

    def __init__(self, path: str = LEDGER_PATH) -> None:
        self._path = path
        self._lock = threading.RLock()
        self._state: dict[str, Any] = self._fresh_state()
        self._load()

    # ------------------------------------------------------------------
    # 持久化与事务
    # ------------------------------------------------------------------

    def _fresh_state(self) -> dict[str, Any]:
        return {
            "version": 1,
            "warehouses": [dict(item) for item in DEFAULT_WAREHOUSES],
            "skus": [dict(item) for item in DEFAULT_SKUS],
            "opening": {wh: dict(items) for wh, items in DEFAULT_OPENING.items()},
            "movements": [],
            "counts": [],
            "batches": [],
            "idempotency": {},
            "sequence": {"movement": 0, "count": 0, "batch": 0},
        }

    def _load(self) -> None:
        try:
            with open(self._path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
        except (FileNotFoundError, OSError, json.JSONDecodeError, UnicodeDecodeError):
            loaded = None
        state = self._fresh_state()
        if isinstance(loaded, dict):
            state.update(loaded)
        for field, default in self._fresh_state().items():
            state.setdefault(field, default)
        self._state = state

    def _persist_locked(self) -> None:
        directory = os.path.dirname(self._path) or "."
        os.makedirs(directory, exist_ok=True)
        payload = json.dumps(self._state, ensure_ascii=False, indent=2, sort_keys=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".ledger-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp_path, self._path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def _transaction(self, handler: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        """写事务：失败回滚内存状态，成功才原子落盘，避免半截账本。"""
        with self._lock:
            backup = deepcopy(self._state)
            try:
                result = handler()
                self._persist_locked()
            except BaseException:
                self._state = backup
                raise
            return result

    def reset(self) -> None:
        """清空为初始账本；只给测试和本地演示用。"""
        with self._lock:
            self._state = self._fresh_state()
            self._persist_locked()

    def seed_demo(self, *, force: bool = False) -> dict[str, Any]:
        """预置一份可复现的两日账本，方便起服务后直接核对。

        第 1 日少记甲仓转乙仓 10 件（关账时没记），第 2 日两仓按旧账开账并各
        有经营变动。没有这个种子也能从头录，种子只是让演示有现成截面。
        """
        with self._lock:
            if not force and self._state["batches"]:
                return {"ok": True, "message": "账本已有数据，跳过预置", "seeded": False}
            self._state = self._fresh_state()
            self._persist_locked()

            self.close_day({"biz_date": "2026-09-01", "operator": "值班管理员"})
            self.record_movement({
                "warehouse": "WH-A", "sku": "SKU-1001", "biz_date": "2026-09-02",
                "qty": -4, "lot": "LOT-0902", "ref": "销-0902-01",
            })
            self.record_movement({
                "warehouse": "WH-B", "sku": "SKU-1001", "biz_date": "2026-09-02",
                "qty": 6, "lot": "LOT-0902", "ref": "采-0902-01",
            })
            self.close_day({"biz_date": "2026-09-02", "operator": "值班管理员"})
            return {
                "ok": True,
                "message": "已预置 2026-09-01、2026-09-02 两日关账账本",
                "seeded": True,
                "catalog": self.catalog(),
            }

    # ------------------------------------------------------------------
    # 基础查询
    # ------------------------------------------------------------------

    def catalog(self) -> dict[str, Any]:
        with self._lock:
            closed = self._closed_days_locked()
            return {
                "ok": True,
                "warehouses": deepcopy(self._state["warehouses"]),
                "skus": deepcopy(self._state["skus"]),
                "opening": deepcopy(self._state["opening"]),
                "closed_days": closed,
                "first_closed_day": closed[0] if closed else None,
                "latest_closed_day": closed[-1] if closed else None,
                "on_disk": os.path.exists(self._path),
            }

    def _warehouse_locked(self, code: str) -> dict[str, Any] | None:
        return next((w for w in self._state["warehouses"] if w["code"] == code), None)

    def _ensure_sku_locked(self, code: str) -> None:
        if not any(item["code"] == code for item in self._state["skus"]):
            self._state["skus"].append({"code": code, "name": code})

    def _opening_locked(self, wh: str, sku: str) -> int:
        return int(self._state["opening"].get(wh, {}).get(sku, 0))

    def _closed_days_locked(self) -> list[str]:
        return sorted({b["biz_date"] for b in self._state["batches"] if b["kind"] == "close"})

    def _latest_batch_locked(self, day: str) -> dict[str, Any] | None:
        items = [b for b in self._state["batches"] if b["biz_date"] == day]
        return max(items, key=lambda b: b["seq"]) if items else None

    def _latest_count_locked(self, wh: str, sku: str, day: str) -> dict[str, Any] | None:
        items = [c for c in self._state["counts"]
                 if c["warehouse"] == wh and c["sku"] == sku and c["biz_date"] == day]
        return max(items, key=lambda c: c["seq"]) if items else None

    # ------------------------------------------------------------------
    # 推导：期初 → 当天变动 → 盘盈亏 → 结余
    # ------------------------------------------------------------------

    def _event_days_locked(self, wh: str, sku: str) -> list[str]:
        days = {m["biz_date"] for m in self._state["movements"]
                if m["warehouse"] == wh and m["sku"] == sku}
        days |= {c["biz_date"] for c in self._state["counts"]
                 if c["warehouse"] == wh and c["sku"] == sku}
        return sorted(days)

    def _movements_for_locked(self, wh: str, sku: str, day: str) -> list[dict[str, Any]]:
        return [m for m in self._state["movements"]
                if m["warehouse"] == wh and m["sku"] == sku and m["biz_date"] == day]

    def _derive_rows_locked(self, wh: str, sku: str,
                            until_day: str | None = None) -> list[dict[str, Any]]:
        """从期初逐日推到 until_day（含），每天一行账页。"""
        days = self._event_days_locked(wh, sku)
        if until_day is not None:
            days = sorted(set(days) | {until_day})
        carry = self._opening_locked(wh, sku)
        rows: list[dict[str, Any]] = []
        for day in days:
            if until_day is not None and day > until_day:
                break
            movements = self._movements_for_locked(wh, sku, day)
            net = sum(int(m["qty"]) for m in movements)
            book = carry + net
            count = self._latest_count_locked(wh, sku, day)
            counted = int(count["counted_qty"]) if count else None
            diff = None if counted is None else counted - book
            closing = book if diff is None else counted
            versions = [{
                "version": c["version"],
                "counted_qty": c["counted_qty"],
                "lot": c["lot"],
                "recorded_at": c["recorded_at"],
                "applied": count is not None and c["seq"] == count["seq"],
            } for c in sorted(
                (c for c in self._state["counts"]
                 if c["warehouse"] == wh and c["sku"] == sku and c["biz_date"] == day),
                key=lambda c: c["seq"],
            )]
            lines = [{
                "line_id": m["movement_id"],
                "kind": m["kind"],
                "qty": int(m["qty"]),
                "lot": m["lot"],
                "ref": m["ref"],
                "transfer_id": m["transfer_id"],
                "counterparty": m["counterparty"],
                "recorded_at": m["recorded_at"],
            } for m in movements]
            if count is not None and diff:
                lines.append({
                    "line_id": f"CNT-{count['count_id']}",
                    "kind": "count_adjustment",
                    "qty": diff,
                    "lot": count["lot"],
                    "ref": f"盘点版本 {count['version']}",
                    "transfer_id": None,
                    "counterparty": None,
                    "recorded_at": count["recorded_at"],
                })
            rows.append({
                "warehouse": wh,
                "sku": sku,
                "biz_date": day,
                "opening_qty": carry,
                "net_movement": net,
                "count_adjustment": 0 if diff is None else diff,
                "book_qty": book,
                "counted_qty": counted,
                "count_diff": diff,
                "closing_qty": closing,
                "count_versions": versions,
                "lines": lines,
            })
            carry = closing
        return rows

    def _row_locked(self, wh: str, sku: str, day: str) -> dict[str, Any]:
        return self._derive_rows_locked(wh, sku, until_day=day)[-1]

    def _day_rows_locked(self, day: str) -> list[dict[str, Any]]:
        return [self._row_locked(wh["code"], sku["code"], day)
                for wh in self._state["warehouses"]
                for sku in self._state["skus"]]

    # ------------------------------------------------------------------
    # 批次发布
    # ------------------------------------------------------------------

    def _row_signature(self, row: dict[str, Any]) -> tuple[Any, ...]:
        return (
            row["warehouse"], row["sku"], row["opening_qty"], row["net_movement"],
            row["count_adjustment"], row["counted_qty"], row["count_diff"],
            row["closing_qty"],
            tuple((line["line_id"], line["qty"]) for line in row["lines"]),
            tuple((v["version"], v["counted_qty"]) for v in row["count_versions"]),
        )

    def _publish_locked(self, day: str, *, kind: str, operator: str = "",
                        note: str = "", reason: str = "") -> dict[str, Any] | None:
        """发布某业务日完整截面；内容与上一版一致时返回 None，不制造空批次。"""
        rows = self._day_rows_locked(day)
        previous = self._latest_batch_locked(day)
        prev_rows = {(r["warehouse"], r["sku"]): r for r in
                     (previous["rows"] if previous else [])}
        if previous is not None and [self._row_signature(r) for r in rows] == [
                self._row_signature(r) for r in previous["rows"]]:
            return None
        for row in rows:
            prior = prev_rows.get((row["warehouse"], row["sku"]))
            row["previous_batch_id"] = previous["batch_id"] if previous else None
            row["previous_opening_qty"] = prior["opening_qty"] if prior else None
            row["previous_closing_qty"] = prior["closing_qty"] if prior else None
            row["opening_delta"] = (
                row["opening_qty"] - prior["opening_qty"] if prior else None)
            row["closing_delta"] = (
                row["closing_qty"] - prior["closing_qty"] if prior else None)
            row["locked"] = True
        totals = {
            "opening_qty": sum(r["opening_qty"] for r in rows),
            "net_movement": sum(r["net_movement"] for r in rows),
            "count_adjustment": sum(r["count_adjustment"] for r in rows),
            "closing_qty": sum(r["closing_qty"] for r in rows),
            "count_diff": sum(r["count_diff"] for r in rows if r["count_diff"] is not None),
        }
        edition = (int(previous["edition"]) + 1) if previous else 1
        self._state["sequence"]["batch"] += 1
        seq = self._state["sequence"]["batch"]
        batch = {
            "batch_id": f"B{seq:06d}",
            "seq": seq,
            "biz_date": day,
            "edition": edition,
            "kind": kind,
            "supersedes": previous["batch_id"] if previous else None,
            "first_batch_id": previous["first_batch_id"] if previous else f"B{seq:06d}",
            "operator": operator,
            "note": note,
            "reason": reason,
            "published_at": _now(),
            "totals": totals,
            "rows": deepcopy(rows),
            "verified": self._verify_locked(rows, day),
        }
        self._state["batches"].append(batch)
        return batch

    def _verify_locked(self, rows: list[dict[str, Any]], day: str) -> dict[str, Any]:
        """批次自检：调拨守恒、逐行结余公式、与前一日结余衔接。"""
        transfers: dict[str, int] = {}
        for movement in self._state["movements"]:
            if movement["biz_date"] == day and movement["transfer_id"]:
                transfers[movement["transfer_id"]] = (
                    transfers.get(movement["transfer_id"], 0) + int(movement["qty"]))
        rows_ok = all(
            row["closing_qty"]
            == row["opening_qty"] + row["net_movement"] + row["count_adjustment"]
            for row in rows
        )
        carry_checks = []
        for row in rows:
            previous_days = [d for d in self._event_days_locked(row["warehouse"], row["sku"])
                             if d < day]
            if not previous_days:
                continue
            prior = self._row_locked(row["warehouse"], row["sku"], previous_days[-1])
            carry_checks.append({
                "warehouse": row["warehouse"],
                "sku": row["sku"],
                "previous_day": previous_days[-1],
                "previous_closing_qty": prior["closing_qty"],
                "opening_qty": row["opening_qty"],
                "ok": prior["closing_qty"] == row["opening_qty"],
            })
        return {
            "transfers_conserved": all(total == 0 for total in transfers.values()),
            "transfer_totals": transfers,
            "rows_consistent": rows_ok,
            "carry_checks": carry_checks,
        }

    def _restate_locked(self, from_day: str, *, reason: str) -> list[dict[str, Any]]:
        """补录后重述所有已关账且不早于 from_day 的日子，逐日追加修订批次。"""
        revisions = []
        for day in self._closed_days_locked():
            if day < from_day:
                continue
            batch = self._publish_locked(day, kind="revision", reason=reason)
            if batch is None:
                continue
            revisions.append({
                "biz_date": day,
                "batch_id": batch["batch_id"],
                "edition": batch["edition"],
                "supersedes": batch["supersedes"],
                "closing_qty": batch["totals"]["closing_qty"],
                "rows": [{
                    "warehouse": r["warehouse"],
                    "sku": r["sku"],
                    "opening_qty": r["opening_qty"],
                    "opening_delta": r["opening_delta"],
                    "closing_qty": r["closing_qty"],
                    "closing_delta": r["closing_delta"],
                } for r in batch["rows"]],
            })
        return revisions

    # ------------------------------------------------------------------
    # 幂等
    # ------------------------------------------------------------------

    def _replay_locked(self, key: str, fingerprint: Any, *,
                       strict: bool = False) -> dict[str, Any] | None:
        """命中幂等键就回放首次结果，绝不二次求和。

        ``strict`` 只用于显式幂等键：键相同但内容不同视为调用方出错；业务单据号
        （``transfer_id`` / ``count_version``）重复提交时放宽为回放并标注差异。
        """
        record = self._state["idempotency"].get(key)
        if record is None:
            return None
        mismatch = record.get("fingerprint") != fingerprint
        if mismatch and strict:
            raise LedgerError(
                "IDEMPOTENCY_CONFLICT",
                f"幂等键 {key} 已被另一笔内容不同的登记占用，请换一个键",
            )
        result = deepcopy(record["result"])
        result["replayed"] = True
        result["fingerprint_mismatch"] = mismatch
        result["message"] = f"幂等回放：{result.get('message', '')}"
        if mismatch:
            result["message"] += "（业务单号相同但内容有差异，按首次登记为准；确需另记请换单号或单据号）"
        return result

    def _remember_locked(self, key: str, fingerprint: Any, result: dict[str, Any]) -> None:
        self._state["idempotency"][key] = {
            "fingerprint": fingerprint,
            "recorded_at": _now(),
            "result": deepcopy(result),
        }

    # ------------------------------------------------------------------
    # 登记：出入库变动
    # ------------------------------------------------------------------

    def _validate_common_locked(self, values: dict[str, Any]) -> tuple[str, str, str, int, str]:
        wh = _text(values.get("warehouse"))
        sku = _text(values.get("sku"))
        day = _parse_day(values.get("biz_date"))
        qty = _as_int(values.get("qty"), "数量")
        lot = _text(values.get("lot"))
        if self._warehouse_locked(wh) is None:
            raise LedgerError("WH_UNKNOWN", f"仓库 {wh or '(空)'} 不在账本里")
        if not sku:
            raise LedgerError("SKU_REQUIRED", "商品编码不能为空")
        if not qty:
            raise LedgerError("QTY_INVALID", "数量不能为 0")
        if not lot:
            raise LedgerError("LOT_REQUIRED", "必须记录实际采用的批次 lot")
        return wh, sku, day, qty, lot

    def record_movement(self, values: dict[str, Any]) -> dict[str, Any]:
        return self._transaction(lambda: self._record_movement_locked(values))

    def _record_movement_locked(self, values: dict[str, Any]) -> dict[str, Any]:
        wh, sku, day, qty, lot = self._validate_common_locked(values)
        ref = _text(values.get("ref"))
        explicit = _text(values.get("idempotency_key"))
        key = f"movement:{explicit}" if explicit else "movement:auto:" + "|".join(
            [wh, sku, day, str(qty), lot, ref])
        fingerprint = ["movement", wh, sku, day, qty, lot, ref]
        replay = self._replay_locked(key, fingerprint, strict=bool(explicit))
        if replay is not None:
            return replay

        self._ensure_sku_locked(sku)
        self._state["sequence"]["movement"] += 1
        seq = self._state["sequence"]["movement"]
        movement = {
            "movement_id": f"M{seq:06d}",
            "kind": "in" if qty > 0 else "out",
            "warehouse": wh,
            "sku": sku,
            "biz_date": day,
            "qty": qty,
            "lot": lot,
            "ref": ref,
            "transfer_id": None,
            "counterparty": None,
            "recorded_at": _now(),
        }
        self._state["movements"].append(movement)
        revisions = self._restate_locked(day, reason=f"补录 {movement['movement_id']}")
        result = {
            "ok": True,
            "code": "RECORDED",
            "message": f"{wh}/{sku} {day} 登记 {qty:+d}，结余 {self._row_locked(wh, sku, day)['closing_qty']}",
            "replayed": False,
            "movement": deepcopy(movement),
            "day": self._published_row_locked(day, wh, sku),
            "restatements": revisions,
        }
        self._remember_locked(key, fingerprint, result)
        return result

    # ------------------------------------------------------------------
    # 登记：跨仓调拨
    # ------------------------------------------------------------------

    def record_transfer(self, values: dict[str, Any]) -> dict[str, Any]:
        return self._transaction(lambda: self._record_transfer_locked(values))

    def _record_transfer_locked(self, values: dict[str, Any]) -> dict[str, Any]:
        out_wh = _text(values.get("from_warehouse"))
        in_wh = _text(values.get("to_warehouse"))
        sku = _text(values.get("sku"))
        day = _parse_day(values.get("biz_date"))
        qty = _as_int(values.get("qty"), "调拨数量")
        lot = _text(values.get("lot"))
        ref = _text(values.get("ref"))
        transfer_id = _text(values.get("transfer_id"))
        if self._warehouse_locked(out_wh) is None:
            raise LedgerError("WH_UNKNOWN", f"出库仓 {out_wh or '(空)'} 不在账本里")
        if self._warehouse_locked(in_wh) is None:
            raise LedgerError("WH_UNKNOWN", f"入库仓 {in_wh or '(空)'} 不在账本里")
        if not sku:
            raise LedgerError("SKU_REQUIRED", "商品编码不能为空")
        if out_wh == in_wh or qty <= 0:
            raise LedgerError(
                "TRANSFER_UNBALANCED",
                "跨仓调拨必须两个不同仓库、数量为正，出库与入库绝对值相等",
            )
        if not lot:
            raise LedgerError("LOT_REQUIRED", "跨仓调拨必须记录实际采用的批次 lot")
        explicit = _text(values.get("idempotency_key"))
        if explicit:
            key = f"transfer:{explicit}"
        elif transfer_id:
            key = f"transfer:{transfer_id}"
        else:
            key = "transfer:auto:" + "|".join(
                [out_wh, in_wh, sku, day, str(qty), lot, ref])
        fingerprint = ["transfer", out_wh, in_wh, sku, day, qty, lot, ref]
        replay = self._replay_locked(key, fingerprint, strict=bool(explicit))
        if replay is not None:
            return replay

        self._ensure_sku_locked(sku)
        if not transfer_id:
            self._state["sequence"]["movement"] += 1
            transfer_id = f"TRF{self._state['sequence']['movement']:06d}"
        self._state["sequence"]["movement"] += 1
        out_seq = self._state["sequence"]["movement"]
        self._state["sequence"]["movement"] += 1
        in_seq = self._state["sequence"]["movement"]
        recorded_at = _now()
        out_line = {
            "movement_id": f"M{out_seq:06d}", "kind": "transfer_out",
            "warehouse": out_wh, "sku": sku, "biz_date": day, "qty": -qty,
            "lot": lot, "ref": ref, "transfer_id": transfer_id,
            "counterparty": in_wh, "recorded_at": recorded_at,
        }
        in_line = {
            "movement_id": f"M{in_seq:06d}", "kind": "transfer_in",
            "warehouse": in_wh, "sku": sku, "biz_date": day, "qty": qty,
            "lot": lot, "ref": ref, "transfer_id": transfer_id,
            "counterparty": out_wh, "recorded_at": recorded_at,
        }
        assert out_line["qty"] + in_line["qty"] == 0
        self._state["movements"].extend([out_line, in_line])
        revisions = self._restate_locked(day, reason=f"补录调拨 {transfer_id}")
        result = {
            "ok": True,
            "code": "RECORDED",
            "message": (
                f"调拨 {transfer_id} 已落账：{out_wh} {out_line['qty']:+d} / "
                f"{in_wh} {in_line['qty']:+d}，合计 0"
            ),
            "replayed": False,
            "transfer_id": transfer_id,
            "conserved": True,
            "lines": [deepcopy(out_line), deepcopy(in_line)],
            "days": [
                self._published_row_locked(day, out_wh, sku),
                self._published_row_locked(day, in_wh, sku),
            ],
            "restatements": revisions,
        }
        self._remember_locked(key, fingerprint, result)
        return result

    # ------------------------------------------------------------------
    # 登记：盘点合计
    # ------------------------------------------------------------------

    def record_count(self, values: dict[str, Any]) -> dict[str, Any]:
        return self._transaction(lambda: self._record_count_locked(values))

    def _record_count_locked(self, values: dict[str, Any]) -> dict[str, Any]:
        wh = _text(values.get("warehouse"))
        sku = _text(values.get("sku"))
        day = _parse_day(values.get("biz_date"))
        counted = _as_int(values.get("counted_qty"), "盘点合计")
        lot = _text(values.get("lot"))
        version = _text(values.get("count_version")) or "v1"
        if self._warehouse_locked(wh) is None:
            raise LedgerError("WH_UNKNOWN", f"仓库 {wh or '(空)'} 不在账本里")
        if not sku:
            raise LedgerError("SKU_REQUIRED", "商品编码不能为空")
        if counted < 0:
            raise LedgerError("COUNT_INVALID", "盘点合计不能为负数")
        explicit = _text(values.get("idempotency_key"))
        key = f"count:{explicit}" if explicit else "count:auto:" + "|".join(
            [wh, sku, day, version])
        fingerprint = ["count", wh, sku, day, version, counted, lot]
        replay = self._replay_locked(key, fingerprint, strict=bool(explicit))
        if replay is not None:
            return replay

        self._ensure_sku_locked(sku)
        self._state["sequence"]["count"] += 1
        seq = self._state["sequence"]["count"]
        count = {
            "count_id": f"C{seq:06d}",
            "warehouse": wh,
            "sku": sku,
            "biz_date": day,
            "version": version,
            "counted_qty": counted,
            "lot": lot,
            "recorded_at": _now(),
            "seq": seq,
        }
        self._state["counts"].append(count)
        revisions = self._restate_locked(day, reason=f"盘点 {count['count_id']} 版本 {version}")
        row = self._published_row_locked(day, wh, sku)
        result = {
            "ok": True,
            "code": "COUNTED",
            "message": (
                f"{wh}/{sku} {day} 盘点 {counted}（版本 {version}），"
                f"盘盈亏 {row['count_adjustment']:+d}，结余 {row['closing_qty']}"
            ),
            "replayed": False,
            "count": deepcopy(count),
            "day": row,
            "restatements": revisions,
        }
        self._remember_locked(key, fingerprint, result)
        return result

    # ------------------------------------------------------------------
    # 关账
    # ------------------------------------------------------------------

    def close_day(self, values: dict[str, Any]) -> dict[str, Any]:
        return self._transaction(lambda: self._close_day_locked(values))

    def _close_day_locked(self, values: dict[str, Any]) -> dict[str, Any]:
        day = _parse_day(values.get("biz_date"))
        operator = _text(values.get("operator")) or "值班管理员"
        note = _text(values.get("note"))
        existing = self._latest_batch_locked(day)
        if existing is not None:
            return {
                "ok": True,
                "code": "ALREADY_CLOSED",
                "message": (
                    f"{day} 已关账，最新批次 {existing['batch_id']}"
                    f"（第 {existing['edition']} 版）"
                ),
                "replayed": True,
                "batch": self._batch_summary(existing),
            }
        batch = self._publish_locked(day, kind="close", operator=operator, note=note)
        return {
            "ok": True,
            "code": "CLOSED",
            "message": f"{day} 已关账，批次 {batch['batch_id']}",
            "replayed": False,
            "batch": self._batch_summary(batch),
        }

    # ------------------------------------------------------------------
    # 读取：账页、批次、核对
    # ------------------------------------------------------------------

    def _batch_summary(self, batch: dict[str, Any]) -> dict[str, Any]:
        return {
            "batch_id": batch["batch_id"],
            "biz_date": batch["biz_date"],
            "edition": batch["edition"],
            "kind": batch["kind"],
            "supersedes": batch["supersedes"],
            "first_batch_id": batch["first_batch_id"],
            "published_at": batch["published_at"],
            "totals": deepcopy(batch["totals"]),
            "reason": batch.get("reason", ""),
            "note": batch.get("note", ""),
            "operator": batch.get("operator", ""),
        }

    def _published_row_locked(self, day: str, wh: str, sku: str) -> dict[str, Any]:
        """优先返回已发布批次的冻结行，避免读路径和写路径口径漂移。"""
        batch = self._latest_batch_locked(day)
        if batch is not None:
            for row in batch["rows"]:
                if row["warehouse"] == wh and row["sku"] == sku:
                    frozen = deepcopy(row)
                    frozen["batch_id"] = batch["batch_id"]
                    frozen["edition"] = batch["edition"]
                    frozen["locked"] = True
                    return frozen
        row = self._row_locked(wh, sku, day)
        row.update({"batch_id": None, "edition": None, "locked": False})
        return row

    def day(self, biz_date: Any, *, warehouse: str | None = None, sku: str | None = None,
            edition: int | None = None, batch_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            day = _parse_day(biz_date)
            batches = sorted((b for b in self._state["batches"] if b["biz_date"] == day),
                             key=lambda b: b["seq"])
            selected = None
            if batch_id:
                selected = next((b for b in batches if b["batch_id"] == batch_id), None)
                if selected is None:
                    raise LedgerError("BATCH_NOT_FOUND", f"批次 {batch_id} 不存在或不属于 {day}")
            elif edition is not None:
                selected = next((b for b in batches
                                 if int(b["edition"]) == int(edition)), None)
                if selected is None:
                    raise LedgerError("BATCH_NOT_FOUND", f"{day} 没有第 {edition} 版账本")
            elif batches:
                selected = batches[-1]

            if selected is not None:
                rows = [deepcopy(r) for r in selected["rows"]]
                if warehouse:
                    rows = [r for r in rows if r["warehouse"] == warehouse]
                if sku:
                    rows = [r for r in rows if r["sku"] == sku]
                for row in rows:
                    row["batch_id"] = selected["batch_id"]
                    row["edition"] = selected["edition"]
                    row["locked"] = True
                return {
                    "ok": True,
                    "biz_date": day,
                    "closed": True,
                    "source": "batch",
                    "batch_id": selected["batch_id"],
                    "edition": selected["edition"],
                    "kind": selected["kind"],
                    "supersedes": selected["supersedes"],
                    "first_batch_id": selected["first_batch_id"],
                    "published_at": selected["published_at"],
                    "operator": selected.get("operator", ""),
                    "note": selected.get("note", ""),
                    "reason": selected.get("reason", ""),
                    "verified": deepcopy(selected.get("verified", {})),
                    "rows": rows,
                    "totals": {
                        "opening_qty": sum(r["opening_qty"] for r in rows),
                        "net_movement": sum(r["net_movement"] for r in rows),
                        "count_adjustment": sum(r["count_adjustment"] for r in rows),
                        "closing_qty": sum(r["closing_qty"] for r in rows),
                        "count_diff": sum(r["count_diff"] for r in rows
                                          if r["count_diff"] is not None),
                    },
                    "batches": [self._batch_summary(b) for b in batches],
                }

            rows = self._day_rows_locked(day)
            if warehouse:
                rows = [r for r in rows if r["warehouse"] == warehouse]
            if sku:
                rows = [r for r in rows if r["sku"] == sku]
            return {
                "ok": True,
                "biz_date": day,
                "closed": False,
                "source": "live",
                "batch_id": None,
                "edition": None,
                "kind": None,
                "supersedes": None,
                "first_batch_id": None,
                "published_at": None,
                "operator": "",
                "note": "",
                "reason": "",
                "verified": {},
                "rows": rows,
                "totals": {
                    "opening_qty": sum(r["opening_qty"] for r in rows),
                    "net_movement": sum(r["net_movement"] for r in rows),
                    "count_adjustment": sum(r["count_adjustment"] for r in rows),
                    "closing_qty": sum(r["closing_qty"] for r in rows),
                    "count_diff": sum(r["count_diff"] for r in rows
                                      if r["count_diff"] is not None),
                },
                "batches": [],
            }

    def batches(self, *, biz_date: Any | None = None) -> dict[str, Any]:
        with self._lock:
            day = _parse_day(biz_date) if biz_date else None
            items = [self._batch_summary(b) for b in self._state["batches"]
                     if day is None or b["biz_date"] == day]
            return {"ok": True, "biz_date": day,
                    "items": sorted(items, key=lambda b: (b["biz_date"], b["edition"]))}

    def batch(self, batch_id: str) -> dict[str, Any] | None:
        with self._lock:
            batch = next((b for b in self._state["batches"]
                          if b["batch_id"] == batch_id), None)
            return deepcopy(batch) if batch else None

    def delta(self, earlier: Any, later: Any, *, warehouse: str | None = None,
              sku: str | None = None) -> dict[str, Any]:
        """两日关系：后一日期初等于前一日期末，差额逐仓可查。"""
        first = self.day(earlier, warehouse=warehouse, sku=sku)
        second = self.day(later, warehouse=warehouse, sku=sku)
        left = {(r["warehouse"], r["sku"]): r for r in first["rows"]}
        right = {(r["warehouse"], r["sku"]): r for r in second["rows"]}
        items = []
        for key in sorted(set(left) | set(right)):
            before, after = left.get(key), right.get(key)
            carry = None if before is None else before["closing_qty"]
            opening = None if after is None else after["opening_qty"]
            items.append({
                "warehouse": key[0],
                "sku": key[1],
                "earlier_closing_qty": carry,
                "later_opening_qty": opening,
                "carry_delta": None if carry is None or opening is None else opening - carry,
                "later_opening_delta": None if after is None else after.get("opening_delta"),
                "later_net_movement": None if after is None else after["net_movement"],
                "later_count_adjustment": None if after is None else after["count_adjustment"],
                "later_closing_qty": None if after is None else after["closing_qty"],
            })
        return {
            "ok": True,
            "earlier": {"biz_date": first["biz_date"], "batch_id": first["batch_id"],
                        "edition": first["edition"], "closed": first["closed"]},
            "later": {"biz_date": second["biz_date"], "batch_id": second["batch_id"],
                      "edition": second["edition"], "closed": second["closed"]},
            "items": items,
            "balanced": all(item["carry_delta"] in (0, None) for item in items),
        }

    def trace(self, *, transfer_id: str | None = None, idempotency_key: str | None = None,
              ref: str | None = None, count_id: str | None = None) -> dict[str, Any]:
        """追溯一笔补录：实际结余、当天调整、来源批号，以及它重述了哪几天。"""
        with self._lock:
            movement_ids: set[str] = set()
            count_ids: set[str] = {count_id} if count_id else set()
            if idempotency_key:
                for prefix in ("", "movement:", "transfer:", "count:"):
                    record = self._state["idempotency"].get(prefix + idempotency_key)
                    if record is None:
                        continue
                    result = record["result"]
                    if result.get("movement"):
                        movement_ids.add(result["movement"]["movement_id"])
                    for line in result.get("lines", []):
                        movement_ids.add(line["movement_id"])
                    if result.get("count"):
                        count_ids.add(result["count"]["count_id"])
            movements = [m for m in self._state["movements"]
                         if (transfer_id and m["transfer_id"] == transfer_id)
                         or (ref and m["ref"] == ref)
                         or m["movement_id"] in movement_ids]
            counts = [c for c in self._state["counts"] if c["count_id"] in count_ids]
            # 按业务单据追溯时，命中的行本身也要参与「批次是否包含该行」的判断
            movement_ids |= {m["movement_id"] for m in movements}
            count_ids |= {c["count_id"] for c in counts}
            if not movements and not counts:
                return {"ok": True, "found": False, "transfer_id": transfer_id,
                        "idempotency_key": idempotency_key, "ref": ref,
                        "count_id": count_id, "movements": [], "counts": [], "days": []}

            targets = sorted({(m["warehouse"], m["sku"], m["biz_date"]) for m in movements}
                             | {(c["warehouse"], c["sku"], c["biz_date"]) for c in counts})
            days = []
            for wh, sku, day in targets:
                row = self._published_row_locked(day, wh, sku)
                all_batches = sorted(
                    (b for b in self._state["batches"] if b["biz_date"] == day),
                    key=lambda b: b["seq"],
                )
                batch_items = []
                for batch in all_batches:
                    frozen_row = next(
                        (r for r in batch["rows"]
                         if r["warehouse"] == wh and r["sku"] == sku), None)
                    if frozen_row is None:
                        continue
                    contains = any(
                        line["line_id"] in movement_ids for line in frozen_row["lines"]
                    ) or any(
                        line["kind"] == "count_adjustment"
                        and line["line_id"].replace("CNT-", "") in count_ids
                        for line in frozen_row["lines"]
                    )
                    batch_items.append({
                        "batch_id": batch["batch_id"],
                        "edition": batch["edition"],
                        "kind": batch["kind"],
                        "published_at": batch["published_at"],
                        "reason": batch.get("reason", ""),
                        "supersedes": batch["supersedes"],
                        "contains_line": contains,
                        "opening_qty": frozen_row["opening_qty"],
                        "closing_qty": frozen_row["closing_qty"],
                        "opening_delta": frozen_row.get("opening_delta"),
                        "closing_delta": frozen_row.get("closing_delta"),
                    })
                matched_qty = sum(
                    int(m["qty"]) for m in movements
                    if (m["warehouse"], m["sku"], m["biz_date"]) == (wh, sku, day))
                matched_count_here = any(
                    (c["warehouse"], c["sku"], c["biz_date"]) == (wh, sku, day)
                    for c in counts)
                days.append({
                    "warehouse": wh,
                    "sku": sku,
                    "biz_date": day,
                    "opening_qty": row["opening_qty"],
                    "net_movement": row["net_movement"],
                    "count_adjustment": row["count_adjustment"],
                    "closing_qty": row["closing_qty"],
                    "counted_qty": row["counted_qty"],
                    "count_diff": row["count_diff"],
                    "locked": row["locked"],
                    "batch_id": row.get("batch_id"),
                    "edition": row.get("edition"),
                    "day_adjustment": matched_qty + (
                        int(row["count_adjustment"]) if matched_count_here else 0),
                    "affected_batches": [b["batch_id"] for b in batch_items
                                         if b["contains_line"]],
                    "batches": batch_items,
                })
            return {
                "ok": True,
                "found": True,
                "transfer_id": transfer_id,
                "idempotency_key": idempotency_key,
                "ref": ref,
                "count_id": count_id,
                "movements": deepcopy(movements),
                "counts": deepcopy(counts),
                "days": days,
            }

    def verify(self, biz_date: Any) -> dict[str, Any]:
        """独立核对：调拨守恒、逐行结余公式、日间衔接。"""
        with self._lock:
            day = _parse_day(biz_date)
            view = self.day(day)
            checks = deepcopy(view.get("verified", {}))
            checks.setdefault("transfers_conserved", True)
            checks.setdefault("rows_consistent", True)
            checks.setdefault("carry_checks", [])
            checks["ok"] = (
                bool(checks["transfers_conserved"])
                and bool(checks["rows_consistent"])
                and all(item["ok"] for item in checks["carry_checks"])
            )
            checks["biz_date"] = day
            checks["source"] = view["source"]
            checks["batch_id"] = view["batch_id"]
            checks["edition"] = view["edition"]
            return checks

    # ------------------------------------------------------------------
    # 数据集市读取：只认已关账批次
    # ------------------------------------------------------------------

    def balances(self, biz_date: Any, *, warehouse: str | None = None,
                 sku: str | None = None) -> dict[str, Any]:
        with self._lock:
            day = _parse_day(biz_date)
            if day not in self._closed_days_locked():
                return {
                    "ok": True,
                    "biz_date": day,
                    "available": False,
                    "reason": f"{day} 尚未关账，数据集市不提供可用结余",
                    "source_batch_id": None,
                    "edition": None,
                    "items": [],
                    "warehouse_totals": [],
                }
            view = self.day(day, warehouse=warehouse, sku=sku)
            items = [{
                "warehouse": row["warehouse"],
                "sku": row["sku"],
                "biz_date": day,
                "opening_qty": row["opening_qty"],
                "net_movement": row["net_movement"],
                "count_adjustment": row["count_adjustment"],
                "closing_qty": row["closing_qty"],
                "available_qty": row["closing_qty"],
                "counted_qty": row["counted_qty"],
                "count_diff": row["count_diff"],
                "source_batch_id": view["batch_id"],
                "edition": view["edition"],
            } for row in view["rows"]]
            totals: dict[str, dict[str, Any]] = {}
            for item in items:
                bucket = totals.setdefault(item["warehouse"], {
                    "warehouse": item["warehouse"],
                    "available_qty": 0,
                    "net_movement": 0,
                    "count_adjustment": 0,
                    "source_batch_id": view["batch_id"],
                    "edition": view["edition"],
                })
                bucket["available_qty"] += item["available_qty"]
                bucket["net_movement"] += item["net_movement"]
                bucket["count_adjustment"] += item["count_adjustment"]
            return {
                "ok": True,
                "biz_date": day,
                "available": True,
                "source_batch_id": view["batch_id"],
                "edition": view["edition"],
                "first_batch_id": view["first_batch_id"],
                "items": items,
                "warehouse_totals": [totals[key] for key in sorted(totals)],
            }

    def editions(self, biz_date: Any, *, warehouse: str | None = None,
                 sku: str | None = None) -> dict[str, Any]:
        """按版本列出某业务日的关账截面，用于核对首发与修订的差额。"""
        with self._lock:
            day = _parse_day(biz_date)
            items = []
            for batch in sorted((b for b in self._state["batches"]
                                 if b["biz_date"] == day), key=lambda b: b["seq"]):
                rows = [deepcopy(r) for r in batch["rows"]]
                if warehouse:
                    rows = [r for r in rows if r["warehouse"] == warehouse]
                if sku:
                    rows = [r for r in rows if r["sku"] == sku]
                items.append({
                    "batch_id": batch["batch_id"],
                    "edition": batch["edition"],
                    "kind": batch["kind"],
                    "supersedes": batch["supersedes"],
                    "published_at": batch["published_at"],
                    "reason": batch.get("reason", ""),
                    "totals": {
                        "opening_qty": sum(r["opening_qty"] for r in rows),
                        "net_movement": sum(r["net_movement"] for r in rows),
                        "count_adjustment": sum(r["count_adjustment"] for r in rows),
                        "closing_qty": sum(r["closing_qty"] for r in rows),
                    },
                    "rows": [{
                        "warehouse": r["warehouse"],
                        "sku": r["sku"],
                        "opening_qty": r["opening_qty"],
                        "net_movement": r["net_movement"],
                        "count_adjustment": r["count_adjustment"],
                        "closing_qty": r["closing_qty"],
                        "counted_qty": r["counted_qty"],
                        "count_diff": r["count_diff"],
                        "opening_delta": r.get("opening_delta"),
                        "closing_delta": r.get("closing_delta"),
                        "previous_batch_id": r.get("previous_batch_id"),
                    } for r in rows],
                })
            return {"ok": True, "biz_date": day, "items": items}


ledger_service = LedgerService()
