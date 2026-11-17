"""连续结果视图：全局键状态按当前水位切快照窗口与待定窗口。

与 M5 的批内快照同一套半开区间口径（``event_time ≤ as_of`` 进快照、
``(as_of, +inf)`` 待定），但数据源是 M8 的全局连续状态，因此：

- 跨日重叠的同一业务变更只出现一次（签名去重在合并阶段已完成）；
- 本批未出现的既有键仍在视图里，不会因为换了导出文件而消失；
- 页面与导出读同一份视图行，保证两处数字逐格一致。
"""
from __future__ import annotations

from decimal import Decimal

from ..m2_schema.coerce import parse_iso
from . import merge


def view_rows(rows: dict[tuple, merge.ContinuityRow], as_of) -> list[dict]:
    """把连续状态切成页面/导出共用的行，pending 由事件时间与水位比较得出。"""
    out: list[dict] = []
    for key in sorted(rows):
        row = rows[key]
        when = parse_iso(row.event_time)
        pending = bool(as_of is not None and when is not None and when > as_of)
        out.append({
            "table": row.table, "pk": row.pk, "version": row.version,
            "op": row.op,
            "kind": "pending" if pending else ("tombstone" if row.tombstone else "active"),
            "amount": None if row.amount is None else str(row.amount),
            "qty": row.qty, "event_time": row.event_time, "row_ref": row.row_ref,
            "late": row.late, "pending": pending,
            "first_batch": row.first_batch, "latest_batch": row.latest_batch,
            "payload": dict(row.payload),
        })
    return out


def summarize(rows: dict[tuple, merge.ContinuityRow], as_of) -> dict:
    view = view_rows(rows, as_of)
    active = [r for r in view if not r["pending"] and r["op"] != "delete"]
    tomb = [r for r in view if not r["pending"] and r["op"] == "delete"]
    pending = [r for r in view if r["pending"]]
    snapshot_amount = sum(
        (Decimal(r["amount"]) for r in active if r["amount"] is not None),
        Decimal("0.00"))
    pending_amount = sum(
        (Decimal(r["amount"]) for r in pending
         if r["op"] != "delete" and r["amount"] is not None),
        Decimal("0.00"))
    return {
        "as_of": as_of.isoformat().replace("+00:00", "Z") if as_of else None,
        "keys_total": len(view),
        "snapshot_keys": len(active) + len(tomb),
        "active_keys": len(active),
        "tombstone_keys": len(tomb),
        "pending_keys": len(pending),
        "amount_snapshot": snapshot_amount,
        "amount_pending": pending_amount,
        "amount_applied": merge.amounts(rows),
        "rows": view,
    }


def reconcile(summary: dict, *, cross_duplicate: int, cross_conflict: int) -> list[dict]:
    """连续结果自己的对账：键账与金额账；跨批次拦截单列，不进状态。"""
    amount_ok = (summary["amount_snapshot"] + summary["amount_pending"]
                 == summary["amount_applied"])
    return [
        {"name": "连续键账", "kind": "count",
         "left": "连续键总数", "left_value": summary["keys_total"],
         "right": "快照键 + 待定键",
         "right_value": summary["snapshot_keys"] + summary["pending_keys"],
         "ok": summary["keys_total"] == summary["snapshot_keys"] + summary["pending_keys"]},
        {"name": "连续金额账", "kind": "money",
         "left": "连续金额", "left_value": summary["amount_applied"],
         "right": "快照金额 + 待定金额",
         "right_value": summary["amount_snapshot"] + summary["amount_pending"],
         "ok": amount_ok},
        {"name": "重叠拦截账", "kind": "count",
         "left": "跨批次重复（不新增）", "left_value": cross_duplicate,
         "right": "跨批次冲突（不覆盖）", "right_value": cross_conflict,
         "ok": True},
    ]
