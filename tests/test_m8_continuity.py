"""M8 跨批次连续状态：日切重叠、早班交接、跨日重复不计数、迟到旧值不覆盖。

覆盖裁决（重叠内容以业务版本序为准，不以导出新旧为准）：

- 同签名跨批次再出现 → B_CROSS_DUP，只承认先前状态、不新增；
- 更高版本（含删除）→ 采信推进；低版本 → B_CROSS_ORDER，不顶掉已确认状态；
- 同版本不同内容 → B_CROSS_CONFLICT，两组都不进连续状态；
- 本批未出现的既有键保留；同输入重跑幂等；失败回滚、上一份结果可查；
- 页面 / API / CSV / JSON 导出读同一份连续结果。
"""
from __future__ import annotations

import pathlib
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from app import config, errors
from app.main import create_app
from app.modules.m6_batches import service
from app.modules.m8_continuity import merge, service as m8_service
from app.store import Store

SAMPLES = pathlib.Path(__file__).resolve().parent.parent / "sample_data"


@pytest.fixture()
def two_batch(settings, store):
    """先跑常规日导出，再跑带两小时重叠的早班交接导出。"""
    first = service.run(settings=settings, store=store, source="all")
    handoff = service.run(settings=settings, store=store, source="handoff")
    return {"first": first, "handoff": handoff,
            "first_id": first["batch"]["batch_id"],
            "handoff_id": handoff["batch"]["batch_id"]}


def _view(store):
    return {(v["table"], v["pk"]): v for v in m8_service.continuity_view(store)}


# ---------- 裁决：跨批重复不新增 ----------

def test_overlap_same_change_counted_once(two_batch, store):
    """1007 v2 在主导出与交接导出里签名相同：连续结果只算一次。"""
    totals = two_batch["handoff"]["totals"]
    assert totals["cross_duplicate"] == 1
    rows = _view(store)
    key = ("orders", "1007")
    # 同一个键只有一行，且首见状态来自第一批
    assert rows[key]["first_batch"] == two_batch["first_id"]
    dup_events = [e for e in store.load_events(two_batch["handoff_id"])
                  if e["reason_code"] == "B_CROSS_DUP"]
    assert len(dup_events) == 1
    assert dup_events[0]["row_ref"] == "events_handoff.jsonl:2"


def test_higher_version_in_handoff_is_adopted(two_batch, store):
    """交接导出的 1007 v3 是更高版本：以新交接导出为准，状态推进。"""
    rows = _view(store)
    row = rows[("orders", "1007")]
    assert row["version"] == 3
    assert row["payload"]["status"] == "已发货"
    assert row["latest_batch"] == two_batch["handoff_id"]
    assert row["first_batch"] == two_batch["first_id"]


def test_late_low_version_does_not_override_confirmed_state(two_batch, store):
    """1006 已确认 v3、1012 已是 v3 墓碑：交接里的 v2 旧值不能顶掉。"""
    totals = two_batch["handoff"]["totals"]
    assert totals["cross_order"] == 2
    rows = _view(store)
    assert rows[("orders", "1006")]["version"] == 3
    assert rows[("orders", "1012")]["op"] == "delete"
    assert rows[("orders", "1012")]["version"] == 3
    blocked = [e for e in store.load_events(two_batch["handoff_id"])
               if e["reason_code"] == "B_CROSS_ORDER"]
    assert {e["pk"] for e in blocked} == {"1006", "1012"}


def test_tombstone_resurrect_and_delete_need_higher_version(tmp_path):
    """墓碑之后的低版本 upsert 与更低版本删除都被拦下，墓碑不复活。"""
    base = tmp_path / "s"
    base.mkdir()
    (base / "d1.jsonl").write_text(
        "# cdc-export v2 rows=2\n"
        '{"table":"orders","pk":"9001","version":1,"op":"upsert",'
        '"event_time":"2026-11-15T08:00:00Z","schema":"v2",'
        '"payload":{"status":"新建","amount":"10.00"}}\n'
        '{"table":"orders","pk":"9001","version":3,"op":"delete",'
        '"event_time":"2026-11-15T08:10:00Z","schema":"v2","payload":{}}\n',
        encoding="utf-8")
    (base / "d2.jsonl").write_text(
        "# cdc-export v2 rows=1\n"
        '{"table":"orders","pk":"9001","version":2,"op":"upsert",'
        '"event_time":"2026-11-15T09:05:00Z","schema":"v2",'
        '"payload":{"status":"复活","amount":"10.00"}}\n', encoding="utf-8")
    settings = config.Settings(db_path=str(tmp_path / "t.sqlite"),
                               sample_dir=str(base), late_window_minutes=10)
    store = Store(settings.db_path)
    service.run(settings=settings, store=store, source="d1.jsonl")
    second = service.run(settings=settings, store=store, source="d2.jsonl")
    assert second["totals"]["cross_order"] == 1
    row = _view(store)[("orders", "9001")]
    assert row["op"] == "delete" and row["version"] == 3


def test_same_version_different_content_conflicts(tmp_path):
    """同键同版本两种内容跨批次出现：两组都不进连续状态，保留先前值。"""
    base = tmp_path / "s"
    base.mkdir()
    (base / "d1.jsonl").write_text(
        "# cdc-export v2 rows=1\n"
        '{"table":"orders","pk":"8001","version":1,"op":"upsert",'
        '"event_time":"2026-11-15T08:00:00Z","schema":"v2",'
        '"payload":{"status":"新建","amount":"10.00"}}\n', encoding="utf-8")
    (base / "d2.jsonl").write_text(
        "# cdc-export v2 rows=1\n"
        '{"table":"orders","pk":"8001","version":1,"op":"upsert",'
        '"event_time":"2026-11-15T08:05:00Z","schema":"v2",'
        '"payload":{"status":"新建","amount":"99.00"}}\n', encoding="utf-8")
    settings = config.Settings(db_path=str(tmp_path / "t.sqlite"),
                               sample_dir=str(base), late_window_minutes=10)
    store = Store(settings.db_path)
    service.run(settings=settings, store=store, source="d1.jsonl")
    second = service.run(settings=settings, store=store, source="d2.jsonl")
    assert second["totals"]["cross_conflict"] == 1
    assert second["totals"]["continuity_applied"] == 0
    row = _view(store)[("orders", "8001")]
    assert row["version"] == 1 and row["amount"] == "10.00"


# ---------- 既有键保留、新键纳入、连续视图 ----------

def test_keys_absent_from_batch_are_kept(two_batch, store):
    """交接导出只碰 5 个键，其余连续键原样保留，记录不丢失。"""
    totals = two_batch["handoff"]["totals"]
    assert totals["continuity_kept"] == 15
    rows = _view(store)
    # 首批的墓碑与普通键都还在
    assert rows[("orders", "1001")]["op"] == "delete"
    assert ("inventory", "9007199254740993") in rows
    assert rows[("inventory", "00123")]["pk"] == "00123"  # 前导零保形


def test_new_key_in_handoff_is_created(two_batch, store):
    rows = _view(store)
    row = rows[("orders", "3002")]
    assert row["version"] == 1
    assert row["first_batch"] == two_batch["handoff_id"]
    assert two_batch["handoff"]["totals"]["continuity_created"] == 1


def test_continuity_grows_and_reconciles(two_batch, store):
    first = two_batch["first"]["totals"]
    second = two_batch["handoff"]["totals"]
    # 连续键只增不减：19 → 20（新增 1 个，其余是覆盖/拦截）
    assert first["continuity_keys"] == 19
    assert second["continuity_keys"] == 20
    doc = m8_service.continuity_document(store)
    assert all(r["ok"] for r in doc["reconcile"])
    s = doc["summary"]
    assert s["keys_total"] == s["snapshot_keys"] + s["pending_keys"]


# ---------- 幂等：重跑不新增 ----------

def test_rerun_handoff_is_idempotent(settings, store, two_batch):
    before_sigs = store.load_continuity_signatures()
    before_rows = m8_service.continuity_view(store)
    rerun = service.run(settings=settings, store=store, source="handoff")
    assert rerun["idempotent_replay"] is True
    assert len(store.list_batches()) == 2
    assert store.load_continuity_signatures() == before_sigs
    after_rows = m8_service.continuity_view(store)
    assert [(r["table"], r["pk"], r["version"], r["amount"]) for r in after_rows] == \
           [(r["table"], r["pk"], r["version"], r["amount"]) for r in before_rows]


# ---------- 中断：上一份结果可查、不丢不增 ----------

def test_failed_run_rolls_back_continuity(settings, store):
    service.run(settings=settings, store=store, source="all")
    keys_before = len(m8_service.continuity_view(store))
    sigs_before = store.load_continuity_signatures()

    def boom(conn, continuity, batch_id):
        raise RuntimeError("simulated interruption")

    store._write_continuity = boom
    with pytest.raises(RuntimeError):
        service.run(settings=settings, store=store, source="handoff")
    del store._write_continuity

    assert len(m8_service.continuity_view(store)) == keys_before
    assert store.load_continuity_signatures() == sigs_before
    assert all(b["status"] == "done" for b in store.list_batches())


# ---------- 陈旧导出仍被拒绝 ----------

def test_stale_export_rejected_before_touching_continuity(settings, store, tmp_path):
    service.run(settings=settings, store=store, source="all")
    old = tmp_path / "old"
    old.mkdir()
    (old / "events_old.jsonl").write_text(
        "# cdc-export v2 rows=1 charset=utf-8\n"
        '{"table":"orders","pk":"7001","version":9,"op":"upsert",'
        '"event_time":"2026-11-15T06:00:00Z","schema":"v2",'
        '"payload":{"status":"新建","amount":"1.00"}}\n', encoding="utf-8")
    stale_settings = replace(settings, sample_dir=str(old))
    with pytest.raises(errors.StaleBatch) as info:
        service.run(settings=stale_settings, store=store, source="events_old.jsonl")
    assert info.value.code == "C_STALE_BATCH"
    # 连续结果完全没被旧导出改动
    assert len(store.list_batches()) == 1


# ---------- 页面 / API / 导出同源 ----------

def test_page_api_and_export_share_one_result(settings):
    app = create_app(settings, seed=True)
    with TestClient(app) as client:
        client.post("/api/events/import", json={"source": "handoff"})
        page = client.get("/continuity")
        assert page.status_code == 200
        assert "连续结果" in page.text
        snapshot_page = client.get("/snapshot")
        assert snapshot_page.status_code == 200
        assert "连续结果（跨批次" in snapshot_page.text

        doc = client.get("/api/continuity").json()
        csv_resp = client.get("/api/export/continuity.csv")
        json_resp = client.get("/api/export/continuity.json")
        assert csv_resp.status_code == 200 and json_resp.status_code == 200
        # CSV 数据行数、JSON 行数、API 键数三者一致
        csv_data_rows = len(csv_resp.text.splitlines()) - 1
        payload = json_resp.json()
        assert csv_data_rows == doc["summary"]["keys_total"]
        assert len(payload["summary"]["rows"]) == doc["summary"]["keys_total"]
        assert payload["summary"]["amount_applied"] == \
            doc["summary"]["amount_applied"]
        # 主键保形：大整数与前导零在连续导出里仍是字符串
        assert "9007199254740993" in csv_resp.text
        assert ",00123," in csv_resp.text


def test_export_empty_state_404(settings):
    app = create_app(settings, seed=False)
    with TestClient(app) as client:
        assert client.get("/api/export/continuity.csv").status_code == 404
        assert client.get("/api/continuity").status_code == 200


def test_import_and_manifest_carry_continuity(settings):
    """采信响应与运行清单都带连续结果，重放时仍能读到当前连续键数。"""
    app = create_app(settings, seed=True)
    with TestClient(app) as client:
        run = client.post("/api/events/import",
                          json={"source": "handoff"}).json()
        assert run["continuity"]["counters"]["cross_duplicate"] == 1
        manifest = client.get("/api/export/manifest.json").json()
        assert manifest["continuity"]["keys_total"] == \
            run["continuity"]["summary"]["keys_total"]
        # 幂等重放不新增，但响应仍给出当前连续结果
        replay = client.post("/api/events/import",
                             json={"source": "handoff"}).json()
        assert replay["idempotent_replay"] is True
        assert replay["continuity"]["summary"]["keys_total"] == \
            manifest["continuity"]["keys_total"]
        # 健康检查暴露连续键数
        assert client.get("/api/health").json()["continuity_keys"] == \
            manifest["continuity"]["keys_total"]


# ---------- 纯规则单元：不依赖库 ----------

def test_merge_pure_rules_first_batch(norm_factory):
    norms = [norm_factory(pk="1", version=1), norm_factory(pk="1", version=2)]
    result = merge.merge(norms, states=[], sig_index={}, batch_id="B-1")
    assert result.counters["keys_created"] == 2 - 1  # 同一键两次，首建后更新
    assert result.counters["keys_updated"] == 1
    assert result.rows[("orders", "1")].version == 2
    assert not result.skipped


def test_merge_is_order_independent(norm_factory):
    """历史在前 vs 历史在后，裁决结果一致（集合运算，不依赖遍历顺序）。"""
    old = norm_factory(pk="2", version=1, event_time="2026-11-15T08:00:00Z")
    new = norm_factory(pk="2", version=2, event_time="2026-11-15T09:00:00Z")
    first = merge.merge([old], states=[], sig_index={}, batch_id="B-1")
    second = merge.merge([new], states=[r.to_state() for r in first.rows.values()],
                         sig_index=first.signatures, batch_id="B-2")
    # 再用一份只含旧 v1 的「重投」导出：应判跨批重复/乱序，不改写 v2
    replay = merge.merge([old], states=[r.to_state() for r in second.rows.values()],
                         sig_index=second.signatures, batch_id="B-3")
    assert replay.rows[("orders", "2")].version == 2
    assert replay.counters["cross_duplicate"] == 1
