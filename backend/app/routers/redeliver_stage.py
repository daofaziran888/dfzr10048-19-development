"""失败重投按阶段领办接口：待办、领办、开工、转办、结单改领、确认产出与音轨登记。

路由放在通用资源路由之前注册：`/todos` 这类固定路径必须先于
`/api/{module}/{entry_id}` 命中，否则会被 entry_id 的整数校验拦下。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.schemas import ActionResult
from app.services.fail_redeliver import get_fail_redeliver_service

router = APIRouter(prefix="/api/fail_redeliver", tags=["失败重投领办"])


class ClaimRequest(BaseModel):
    operator: str


class ArtifactRequest(BaseModel):
    batch_id: str
    shard_no: int
    artifact_id: str | None = None
    job_id: str = ""
    audio_bytes: int = 0
    audio_ref: str = ""
    success_flag: bool = True
    reason: str = ""


class ConfirmRequest(BaseModel):
    artifact_id: str | None = None
    operator: str = ""


@router.get("/todos")
def list_todos(
    stage: str | None = Query(default=None, description="失败阶段：转码 / 转写"),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=200),
) -> dict[str, object]:
    """按失败阶段筛选待办，按分片序号排序并分页。"""
    service = get_fail_redeliver_service()
    items, total = service.list_todos(stage, page, size)
    return {"items": items, "total": total, "page": page, "size": size, "stage": stage or ""}


@router.get("/todos/summary")
def todo_summary() -> dict[str, object]:
    """按阶段汇总待办数量。"""
    return get_fail_redeliver_service().stage_summary()


@router.get("/orders/{order_id}")
def order_detail(order_id: str) -> dict[str, object]:
    """查看工单现场判断依据：原失败原因、当前音轨、可否转办或改领。"""
    ok, message, detail = get_fail_redeliver_service().todo_detail(order_id)
    if not ok:
        raise HTTPException(status_code=404, detail=message)
    return {"ok": True, "message": message, "detail": detail}


@router.post("/orders/{order_id}/claim", response_model=ActionResult)
def claim_order(order_id: str, payload: ClaimRequest) -> ActionResult:
    """按工单当前阶段领办并签发开工回执。"""
    ok, message, result = get_fail_redeliver_service().claim(order_id, payload.operator)
    return ActionResult(ok=ok, message=message, entry=result)


@router.post("/orders/{order_id}/start", response_model=ActionResult)
def start_order(order_id: str) -> ActionResult:
    """把已领办阶段标记为已开工。"""
    ok, message, receipt = get_fail_redeliver_service().start_work(order_id)
    entry = None
    if receipt is not None:
        entry = {
            "receipt": receipt,
            "audio_binding": receipt.get("audio_binding"),
        }
    return ActionResult(ok=ok, message=message, entry=entry)


@router.post("/orders/{order_id}/transfer", response_model=ActionResult)
def transfer_order(order_id: str, payload: ClaimRequest) -> ActionResult:
    """原单转办：同一张工单从转码切到转写。"""
    ok, message, result = get_fail_redeliver_service().transfer(order_id, payload.operator)
    return ActionResult(ok=ok, message=message, entry=result)


@router.post("/orders/{order_id}/close-reclaim", response_model=ActionResult)
def close_and_reclaim(order_id: str, payload: ClaimRequest) -> ActionResult:
    """结掉当前转码工单并另开转写工单领办。"""
    ok, message, result = get_fail_redeliver_service().close_and_reclaim(
        order_id, payload.operator)
    return ActionResult(ok=ok, message=message, entry=result)


@router.post("/orders/{order_id}/confirm", response_model=ActionResult)
def confirm_output(order_id: str, payload: ConfirmRequest) -> ActionResult:
    """确认产出并核对实际投入音轨。"""
    ok, message, result = get_fail_redeliver_service().confirm_output(
        order_id, payload.artifact_id, payload.operator)
    return ActionResult(ok=ok, message=message, entry=result)


@router.post("/artifacts", response_model=ActionResult)
def register_artifact(payload: ArtifactRequest) -> ActionResult:
    """登记转码音轨实物；迟到落盘的音轨也在此补登记。"""
    artifact = get_fail_redeliver_service().register_artifact(
        payload.batch_id, payload.shard_no,
        artifact_id=payload.artifact_id, job_id=payload.job_id,
        audio_bytes=payload.audio_bytes, audio_ref=payload.audio_ref,
        success_flag=payload.success_flag, reason=payload.reason,
    )
    return ActionResult(
        ok=True,
        message=f"音轨 {artifact['artifact_id']} 已登记：{artifact['status_text']}",
        entry=artifact,
    )


@router.get("/receipts/{receipt_id}")
def receipt_detail(receipt_id: str) -> dict[str, object]:
    """按开工回执取音轨定位信息。"""
    ok, message, detail = get_fail_redeliver_service().receipt_detail(receipt_id)
    if not ok:
        raise HTTPException(status_code=404, detail=message)
    return {"ok": True, "message": message, **detail}
