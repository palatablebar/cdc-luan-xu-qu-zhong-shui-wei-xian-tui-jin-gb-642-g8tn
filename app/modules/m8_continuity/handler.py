"""M8 入口：跨批次连续结果的只读 API、页面与导出。

页面（/snapshot 顶部连续结果卡片）、JSON API 与 CSV/JSON 导出都从
``service.continuity_view`` 取同一份视图行，数字逐格一致。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response

from ... import errors
from ...web import Ctx, get_ctx, render
from ..m7_export import csv_out, json_out
from . import service

router = APIRouter(tags=["M8 跨批次连续状态"])

CSV_MEDIA = "text/csv; charset=utf-8"
JSON_MEDIA = "application/json"


@router.get("/api/continuity")
def api_continuity(ctx: Ctx = Depends(get_ctx)) -> dict:
    doc = service.continuity_document(ctx.store)
    return doc


@router.get("/api/export/continuity.csv")
def export_continuity_csv(ctx: Ctx = Depends(get_ctx)) -> Response:
    rows = service.continuity_view(ctx.store)
    if not rows:
        raise errors.NotFound("还没有连续结果，先采信一次", code="E_NO_BATCH")
    text = csv_out.to_csv(csv_out.CONTINUITY_COLUMNS,
                          csv_out.continuity_rows(rows))
    data = csv_out.encode_csv(text)
    batch = ctx.store.latest_batch() or {}
    filename = f"{batch.get('batch_id', 'continuity')}-continuity.csv"
    return Response(content=data, media_type=CSV_MEDIA,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/api/export/continuity.json")
def export_continuity_json(ctx: Ctx = Depends(get_ctx)) -> Response:
    doc = service.continuity_document(ctx.store)
    if not doc["summary"]["rows"]:
        raise errors.NotFound("还没有连续结果，先采信一次", code="E_NO_BATCH")
    data = json_out.dumps(doc).encode("utf-8")
    batch = ctx.store.latest_batch() or {}
    filename = f"{batch.get('batch_id', 'continuity')}-continuity.json"
    return Response(content=data, media_type=JSON_MEDIA,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/continuity")
def page_continuity(request: Request, ctx: Ctx = Depends(get_ctx)):
    doc = service.continuity_document(ctx.store)
    batch = ctx.current_batch()
    return render(request, "continuity.html", batch=batch, page="continuity",
                  summary=doc["summary"], reconcile=doc["reconcile"],
                  counters=doc["counters"], rows=doc["summary"]["rows"][:400],
                  total_rows=len(doc["summary"]["rows"]),
                  page_title="连续结果")
