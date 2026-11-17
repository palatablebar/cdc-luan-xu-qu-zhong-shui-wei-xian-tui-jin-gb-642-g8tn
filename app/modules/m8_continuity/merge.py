"""跨批次连续状态的纯规则：把本批已应用事件折叠进历史连续状态。

裁决原则（重叠内容以业务版本序为准，不以导出新旧为准）：

1. 同签名（规范化全行签名）已被任一历史批次采信 → 跨批次重复
   （``B_CROSS_DUP``）：跳过，不新增、不覆盖，连续状态只承认首见；
2. 同键同版本但签名不同 → 跨批次版本冲突（``B_CROSS_CONFLICT``）：
   新到的一条不进连续状态，也不改写既有状态；
3. 低版本 upsert 晚于已确认的更高版本（含墓碑）→ 跨批次乱序
   （``B_CROSS_ORDER``）：迟到旧值不顶掉已确认状态；
4. 版本更高（upsert/delete 都算）→ 采信并覆盖该键，delete 写墓碑；
5. 本批没有触及的既有键原样保留，连续状态只增不减。

这里的全部判定都是集合运算，只依赖 (历史状态, 历史签名, 本批事件)，
与调用顺序无关，同一份输入重跑得到完全相同的结果（幂等）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from ..m2_schema.service import Norm
from ..m3_dedup import dedup

# 跨批次拦截原因码 → 落库 row_class 的映射
CROSS_DUP = "B_CROSS_DUP"
CROSS_ORDER = "B_CROSS_ORDER"
CROSS_CONFLICT = "B_CROSS_CONFLICT"
SKIP_CLASSES = {CROSS_DUP: "cross_duplicate",
                CROSS_ORDER: "cross_conflict",
                CROSS_CONFLICT: "cross_conflict"}


@dataclass
class ContinuityRow:
    """一个 (table, pk) 在连续结果里的唯一状态（全局，跨批次）。"""

    table: str
    pk: str
    version: int
    op: str
    payload: dict
    amount: Decimal | None
    qty: int | None
    event_time: str                 # ISO UTC
    row_ref: str
    first_batch: str                # 首见批次
    latest_batch: str               # 最近一次更新该状态的批次
    late: bool = False
    signatures: list[str] = field(default_factory=list)  # 该键已采信的全部签名

    @property
    def key(self) -> tuple[str, str]:
        return (self.table, self.pk)

    @property
    def tombstone(self) -> bool:
        return self.op == "delete"

    def to_state(self) -> dict:
        return {
            "table": self.table, "pk": self.pk, "version": self.version,
            "op": self.op,
            "payload": dict(self.payload) if self.op != "delete" else {},
            "amount": None if self.amount is None else str(self.amount),
            "qty": self.qty, "event_time": self.event_time,
            "row_ref": self.row_ref, "first_batch": self.first_batch,
            "latest_batch": self.latest_batch, "late": 1 if self.late else 0,
        }


@dataclass
class MergeResult:
    rows: dict[tuple[str, str], ContinuityRow]
    signatures: dict[str, str]      # 签名 → 首见行引用（全局去重索引）
    skipped: list[Norm] = field(default_factory=list)
    applied: list[Norm] = field(default_factory=list)
    counters: dict = field(default_factory=dict)

    @property
    def cross_duplicates(self) -> list[Norm]:
        return [n for n in self.skipped if n.reason_code == CROSS_DUP]

    @property
    def cross_conflicts(self) -> list[Norm]:
        return [n for n in self.skipped if n.reason_code in (CROSS_ORDER, CROSS_CONFLICT)]


def _row_from_state(state: dict, signatures: list[str]) -> ContinuityRow:
    # 兼容两种来源：持久层行（table_name）与 to_state() 内存行（table）
    amount = state.get("amount")
    return ContinuityRow(
        table=state.get("table", state.get("table_name")), pk=state["pk"],
        version=int(state["version"]),
        op=state["op"], payload=dict(state.get("payload") or {}),
        amount=None if amount is None else Decimal(str(amount)),
        qty=state.get("qty"),
        event_time=state.get("event_time", state.get("event_time_utc")),
        row_ref=state["row_ref"], first_batch=state["first_batch"],
        latest_batch=state["latest_batch"], late=bool(state.get("late")),
        signatures=list(signatures))


def load_rows(states: list[dict], sig_index: dict[str, str]) -> dict[tuple[str, str], ContinuityRow]:
    """把库里的连续状态行还原成内存结构。

    签名本身是 sha256，无法反解出键；签名按键分组所需的 (table, pk) 在
    持久层 ``continuity_signatures`` 里单独存了列，由 :func:`signatures_by_key`
    预先分组后通过 ``sig_index`` 传入（这里的入参是「键 → 签名列表」映射）。
    """
    grouped = sig_index if sig_index and isinstance(next(iter(sig_index.values())), list) else {}
    rows: dict[tuple[str, str], ContinuityRow] = {}
    for state in states:
        key = (state.get("table", state.get("table_name")), state["pk"])
        rows[key] = _row_from_state(state, grouped.get(key, []))
    return rows


def signatures_by_key(rows: list[dict]) -> dict[tuple[str, str], list[str]]:
    """持久层签名行（含 table_name/pk 列）→ 按键分组的签名列表。"""
    out: dict[tuple[str, str], list[str]] = {}
    for row in rows:
        out.setdefault((row["table_name"], row["pk"]), []).append(row["signature"])
    return out


def _mark_skip(norm: Norm, code: str, detail: str) -> None:
    norm.row_class = SKIP_CLASSES[code]
    norm.reason_code = code
    norm.detail = detail


def merge(incoming: list[Norm], *, states: list[dict], sig_index: dict[str, str],
          batch_id: str, grouped: dict[tuple[str, str], list[str]] | None = None) -> MergeResult:
    """把本批 ``applied`` 事件合并进历史连续状态。

    ``states`` / ``sig_index`` 来自持久层（首轮为空）；``grouped`` 是按键
    分组的历史签名（用于重建每个键的签名清单）。返回新的连续状态、全局
    签名索引，以及被跨批次规则拦下的事件（带原因码与 row_class）。
    """
    rows = load_rows(states, grouped or {})
    signatures = dict(sig_index)
    skipped: list[Norm] = []
    applied: list[Norm] = []
    counters = {"incoming": len(incoming), "cross_duplicate": 0,
                "cross_order": 0, "cross_conflict": 0, "continuity_applied": 0,
                "keys_kept": 0, "keys_updated": 0, "keys_created": 0}

    for norm in sorted(incoming, key=lambda n: n.seq):
        sig = dedup.signature(norm)
        current = rows.get(norm.key())

        # 规则 1：同一变更已被历史批次采信 → 跨批次重复，承认先前状态
        if sig in signatures:
            _mark_skip(norm, CROSS_DUP,
                       f"与已采信变更 {signatures[sig]} 同签名，跨批次重复不新增")
            counters["cross_duplicate"] += 1
            skipped.append(norm)
            continue

        # 规则 2：同键同版本、内容不同 → 跨批次版本冲突，两组都不进连续状态
        if current is not None and norm.version == current.version and not current.tombstone:
            _mark_skip(norm, CROSS_CONFLICT,
                       f"版本 {norm.version} 与已确认状态 {current.row_ref} 内容不同")
            counters["cross_conflict"] += 1
            skipped.append(norm)
            continue

        # 规则 3：低版本晚于已确认的更高版本/墓碑 → 乱序旧值，不顶掉既有状态
        if current is not None and norm.version < current.version:
            if norm.op == "delete":
                # 对更低版本的删除同样拒绝：不能用旧删除抹掉新高版本
                _mark_skip(norm, CROSS_ORDER,
                           f"删除版本 {norm.version} 低于已确认版本 {current.version}")
            else:
                _mark_skip(norm, CROSS_ORDER,
                           f"版本 {norm.version} 低于已确认版本 {current.version}，"
                           "迟到旧值不覆盖")
            counters["cross_order"] += 1
            skipped.append(norm)
            continue

        # 规则 4：版本更高（或首见）→ 采信并覆盖；delete 写墓碑
        is_new = current is None
        payload = {} if norm.op == "delete" else dict(norm.payload)
        amount = None if norm.op == "delete" else norm.amount
        row = ContinuityRow(
            table=norm.table, pk=norm.pk, version=int(norm.version or 0),
            op=norm.op or "upsert", payload=payload, amount=amount, qty=norm.qty,
            event_time=norm.time_iso, row_ref=norm.row_ref,
            first_batch=current.first_batch if current else batch_id,
            latest_batch=batch_id, late=norm.late,
            signatures=(current.signatures if current else []) + [sig])
        rows[norm.key()] = row
        signatures[sig] = norm.row_ref
        counters["continuity_applied"] += 1
        if is_new:
            counters["keys_created"] += 1
        else:
            counters["keys_updated"] += 1
        applied.append(norm)

    # 规则 5：本批未触及的键原样保留（rows 本来就没动它们），只做计数
    touched = {n.key() for n in incoming}
    counters["keys_kept"] = sum(1 for key in rows if key not in touched)
    return MergeResult(rows=rows, signatures=signatures, skipped=skipped,
                       applied=applied, counters=counters)


def amounts(rows: dict[tuple[str, str], ContinuityRow]) -> Decimal:
    """连续结果金额合计：墓碑与空金额不计，Decimal 精确累加。"""
    total = Decimal("0.00")
    for row in rows.values():
        if row.tombstone or row.amount is None:
            continue
        total += row.amount
    return total


def classify_counts(rows: dict[tuple[str, str], ContinuityRow]) -> dict:
    active = sum(1 for r in rows.values() if not r.tombstone)
    tombstone = sum(1 for r in rows.values() if r.tombstone)
    return {"keys_total": len(rows), "active_keys": active,
            "tombstone_keys": tombstone}
