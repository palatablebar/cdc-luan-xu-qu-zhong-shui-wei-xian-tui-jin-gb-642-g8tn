"""连续状态编排：合并、落库、读取，以及给页面/导出的统一视图。

持久化与批次结果共用同一个事务（由 ``Store.save_run`` 一起提交）：
合并阶段抛错则整批回滚，上一份连续结果与上一份批次结果都保持可查询。
"""
from __future__ import annotations

from ..m2_schema.coerce import parse_iso
from . import merge, view


def apply_continuity(store, *, incoming, batch_id: str) -> dict:
    """把本批 applied 事件合并进全局连续状态，返回 M8 计数与拦截明细。

    ``incoming`` 是 M3 判为 applied 的 Norm 列表；被跨批次规则拦下的事件
    在这里就地改写 row_class/reason_code，随后随整批事件一起落库留证。
    """
    states = store.load_continuity_states()
    sig_index = store.load_continuity_signatures()
    grouped = merge.signatures_by_key(store.load_continuity_signature_rows())
    result = merge.merge(incoming, states=states, sig_index=sig_index,
                         grouped=grouped, batch_id=batch_id)
    return {
        "result": result,
        "counters": result.counters,
        "skipped": result.skipped,
        "rows": result.rows,
        "state_rows": [row.to_state() for row in result.rows.values()],
        "signatures": result.signatures,
    }


def _load_rows(store):
    return merge.load_rows(
        store.load_continuity_states(),
        merge.signatures_by_key(store.load_continuity_signature_rows()))


def continuity_summary(store) -> dict:
    """页面/API/导出共用的连续结果摘要；无批次时给空态。"""
    batch = store.latest_batch()
    rows = _load_rows(store)
    as_of = parse_iso(batch["watermark_utc"]) if batch else None
    return view.summarize(rows, as_of)


def continuity_view(store) -> list[dict]:
    batch = store.latest_batch()
    rows = _load_rows(store)
    as_of = parse_iso(batch["watermark_utc"]) if batch else None
    return view.view_rows(rows, as_of)


def continuity_document(store) -> dict:
    batch = store.latest_batch() or {}
    summary = continuity_summary(store)
    counters = store.load_continuity_counters()
    rec = view.reconcile(
        summary,
        cross_duplicate=int(counters.get("cross_duplicate", 0) or 0),
        cross_conflict=int(counters.get("cross_order", 0) or 0)
        + int(counters.get("cross_conflict", 0) or 0))
    return {"summary": summary, "reconcile": rec, "counters": counters,
            "batch_id": batch.get("batch_id"),
            "watermark_utc": batch.get("watermark_utc")}
