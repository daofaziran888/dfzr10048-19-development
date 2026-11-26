"""失败重投回填领域服务：登记转码或转写失败分片的重投单，只处理仍缺失分片不覆盖已成功产出。

同时承接「按失败阶段领办」的现场流程：待办按失败阶段筛选、按分片序号排序分页；
转码与转写分开领办；开工回执绑定实际投入的音轨，转写任务凭回执定位。
"""
from __future__ import annotations

from typing import Any

from app.pipeline.stage_claim_engine import (
    STAGE_TRANSCODE,
    STAGE_TRANSCRIBE,
    StageClaimEngine,
    normalize_stage,
)
from app.services.base import BaseModuleService
from app.store import store


def _split_shard(shard_id: str) -> tuple[str, int]:
    """把 MEDI-0001-S013 这类分片标识拆成批次与分片序号。"""
    text = str(shard_id or "").strip()
    if "-S" in text:
        batch, _, seq = text.rpartition("-S")
        try:
            return batch, int(seq)
        except ValueError:
            return text, 0
    return text, 0


class FailRedeliverService(BaseModuleService):
    MODULE = "fail_redeliver"
    ENTITY = "重投单"
    OPERATION_TERM = "回填"
    LIST_FIELDS = ["重投编号", "关联分片", "失败阶段", "失败原因", "重投次数", "重投策略", "重投结论", "重投时间"]
    REQUIRED_FIELDS = ["重投编号", "关联分片", "失败阶段"]
    STATUS_ORDER = ["待重投", "重投中", "已补齐", "重投放弃"]
    ACTION_RULES = {"启动重投": "重投中", "确认补齐": "已补齐", "放弃重投": "重投放弃"}
    NEGATIVE_ACTIONS = []

    def __init__(self) -> None:
        self._engine: StageClaimEngine | None = None

    # ---------- 引擎与种子 ----------

    @property
    def engine(self) -> StageClaimEngine:
        if self._engine is None:
            engine = StageClaimEngine()
            self._seed_engine(engine)
            self._engine = engine
        return self._engine

    def _seed_engine(self, engine: StageClaimEngine) -> None:
        """把重投单种子数据映射成带阶段语义的工单，并补登示例转码实物。"""
        for row in store.rows(self.MODULE):
            stage = normalize_stage(str(row.get("失败阶段", "")))
            if stage is None:
                continue
            shard_id = str(row.get("关联分片", ""))
            batch, shard_no = _split_shard(shard_id)
            order_id = str(row.get("重投编号") or f"ORDER-{row.get('id')}")
            if engine.get_order(order_id) is not None:
                continue
            order = engine.register_order(
                order_id, batch, shard_no, stage,
                fail_reason=str(row.get("失败原因", "")),
                shard_id=shard_id,
            )
            if row.get("status") in ("已补齐", "重投放弃"):
                order.status = "已结单"
                order.closed = True
            # 「重投中」在重投单数据集里表示重试正在进行；领办工单以开工回执为准，
            # 没有回执就不占用执行中阶段，避免历史脏状态把工单永久卡死。

        # 示例转码实物：
        # - S002 只有成功标记，没有录音，领取转写时必须继续领取转码；
        # - S006 有可验证音轨，可以直接领取转写；
        # - 异批次 BATCH-20260902 的 4 号音轨不能给 BATCH-20260901 的 4 号片代跑。
        engine.register_artifact(
            "ART-20260901-S002", "BATCH-20260901", 2, job_id="TRCD-OLD-002",
            audio_bytes=0, audio_ref="", success_flag=True,
            reason="转码完成但录音未落盘",
        )
        engine.register_artifact(
            "ART-20260901-S006", "BATCH-20260901", 6, job_id="TRCD-OK-006",
            audio_bytes=655360, audio_ref="s3://shard-audio/20260901/006.wav",
        )
        engine.register_artifact(
            "ART-20260902-S004", "BATCH-20260902", 4, job_id="TRCD-OTHER-004",
            audio_bytes=524288, audio_ref="s3://shard-audio/20260902/004.wav",
        )

    def refresh_engine(self) -> None:
        """丢弃当前引擎并按当前重投单重新计算，用于数据被外部改动后的重算。"""
        self._engine = None

    # ---------- 待办 ----------

    def list_todos(
        self, stage: str | None = None, page: int = 1, size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """按失败阶段筛选待办，按分片序号排序并分页。"""
        return self.engine.list_todos(stage, page, size)

    def todo_detail(self, order_id: str) -> tuple[bool, str, dict[str, Any]]:
        """单张工单的现场判断依据：原失败原因、当前音轨、可否转办或改领。"""
        return self.engine.failure_detail(order_id)

    def stage_summary(self) -> dict[str, Any]:
        """按失败阶段汇总待办数量，供前端选项卡显示。"""
        _, tc_total = self.engine.list_todos(STAGE_TRANSCODE, 1, 1_000_000)
        _, tr_total = self.engine.list_todos(STAGE_TRANSCRIBE, 1, 1_000_000)
        running = sum(
            1 for order in self.engine.snapshot()["orders"]
            if order.get("active_stage") and not order.get("closed")
        )
        return {
            "转码": tc_total,
            "转写": tr_total,
            "合计": tc_total + tr_total,
            "执行中": running,
        }

    # ---------- 音轨登记与查询 ----------

    def register_artifact(
        self,
        batch_id: str,
        shard_no: int,
        *,
        artifact_id: str | None = None,
        job_id: str = "",
        audio_bytes: int = 0,
        audio_ref: str = "",
        success_flag: bool = True,
        reason: str = "",
    ) -> dict[str, Any]:
        """登记转码实物；迟到落盘的音轨也走这里，领办时实时重算是否可用。"""
        engine = self.engine
        if not artifact_id:
            artifact_id = (
                f"ART-{batch_id}-S{int(shard_no):03d}-"
                f"{len(self.engine.snapshot()['artifacts']) + 1:03d}"
            )
        # 同一音轨编号重复登记视为补录迟到信息，覆盖旧值而不是并存。
        artifact = engine.register_artifact(
            artifact_id, batch_id, shard_no,
            job_id=job_id, audio_bytes=audio_bytes, audio_ref=audio_ref,
            success_flag=success_flag, reason=reason,
        )
        return artifact.to_dict()

    def latest_artifact(self, batch_id: str, shard_no: int) -> dict[str, Any] | None:
        artifact = self.engine.latest_artifact(batch_id, shard_no)
        return artifact.to_dict() if artifact else None

    # ---------- 领办动作 ----------

    def claim(self, order_id: str, operator: str) -> tuple[bool, str, dict[str, Any] | None]:
        """按工单当前阶段领办并返回开工回执。"""
        ok, message, receipt = self.engine.claim(order_id, operator)
        self._sync_order(order_id)
        return ok, message, self._order_payload(order_id, receipt)

    def start_work(self, order_id: str) -> tuple[bool, str, dict[str, Any] | None]:
        ok, message = self.engine.start_work(order_id)
        self._sync_order(order_id)
        order = self.engine.get_order(order_id)
        return ok, message, self._order_payload(
            order_id, order.open_receipt if order and order.open_receipt else None)

    def transfer(self, order_id: str, operator: str) -> tuple[bool, str, dict[str, Any] | None]:
        """原单转办：同一张工单从转码切到转写。"""
        ok, message, receipt = self.engine.transfer(order_id, operator)
        self._sync_order(order_id)
        return ok, message, self._order_payload(order_id, receipt)

    def close_and_reclaim(
        self, order_id: str, operator: str,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """结掉当前转码工单，另开一张转写工单并领办。"""
        ok, message, new_order, receipt = self.engine.close_and_reclaim(order_id, operator)
        self._sync_order(order_id)
        if new_order is not None:
            self._sync_order(new_order.order_id)
        payload = {
            "closed_order": order_id,
            "new_order": new_order.to_dict() if new_order else None,
            "receipt": receipt.to_dict() if receipt else None,
            "audio_binding": receipt.audio_binding() if receipt else None,
        }
        return ok, message, payload

    def confirm_output(
        self, order_id: str, artifact_id: str | None = None, operator: str = "",
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """确认产出并核对实际投入音轨。"""
        ok, message, receipt = self.engine.confirm_output(order_id, artifact_id, operator)
        self._sync_order(order_id)
        return ok, message, self._order_payload(order_id, receipt)

    def _order_payload(
        self, order_id: str, receipt,
    ) -> dict[str, Any] | None:
        """统一领办类接口的返回结构：工单、回执与音轨绑定。"""
        if receipt is None:
            return None
        order = self.engine.get_order(order_id)
        return {
            "order": order.to_dict() if order else None,
            "receipt": receipt.to_dict(),
            "audio_binding": receipt.audio_binding(),
        }

    def receipt_detail(self, receipt_id: str) -> tuple[bool, str, dict[str, Any]]:
        """按开工回执取音轨定位信息，供转写任务核对实际投入。"""
        receipt = self.engine.find_receipt(receipt_id)
        if receipt is None:
            return False, f"开工回执 {receipt_id} 不存在", {}
        return True, f"回执 {receipt_id} 已绑定音轨", {
            "receipt": receipt.to_dict(),
            "audio_binding": receipt.audio_binding(),
        }

    def _sync_order(self, order_id: str) -> None:
        """把工单状态回写到重投单数据集，保持列表口径与工单一致。"""
        order = self.engine.get_order(order_id)
        if order is None:
            return
        if order.closed:
            status = "已补齐"
        elif order.active_stage:
            status = "重投中"
        else:
            status = "待重投"
        for row in store.rows(self.MODULE):
            if str(row.get("重投编号")) != order_id:
                continue
            row["status"] = status
            row["pending"] = status != "已补齐"
            row["失败阶段"] = f"{order.origin_stage}失败"
            row["重投次数"] = str(max(0, len(order.receipts) - 1))
            if order.closed:
                row["重投时间"] = order.closed_at or "-"
                if row.get("重投结论") in (None, "-"):
                    row["重投结论"] = "已结单"
            break

    # ---------- 原有重投动作 ----------

    def create_entry(
        self, values: dict[str, Any],
    ) -> tuple[dict[str, Any] | None, list[str]]:
        """通用登记入口：登记重投单的同时建立可领办的阶段工单。"""
        entry, missing = super().create_entry(values)
        if entry is None:
            return None, missing
        stage = normalize_stage(str(entry.get("失败阶段", "")))
        if stage is not None:
            batch, shard_no = _split_shard(str(entry.get("关联分片", "")))
            self.engine.register_order(
                str(entry.get("重投编号")), batch, shard_no, stage,
                fail_reason=str(entry.get("失败原因", "")),
                shard_id=str(entry.get("关联分片", "")),
            )
        return entry, missing

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        """通用动作入口：同步工单状态，避免列表与领办视图各说各话。"""
        entry, message = super().run_action(entry_id, action)
        if entry is None:
            return entry, message
        order_id = str(entry.get("重投编号", ""))
        order = self.engine.get_order(order_id)
        if order is not None:
            if entry.get("status") in ("已补齐", "重投放弃"):
                order.active_stage = None
                order.closed = True
            elif entry.get("status") == "待重投":
                order.active_stage = None
                order.closed = False
        return entry, message

    def register_redeliver(
        self, redeliver_id: str, shard_id: str, fail_stage: str,
        fail_reason: str, strategy: str,
    ) -> tuple[dict[str, Any] | None, str]:
        """登记失败重投单。"""
        rows = store.rows(self.MODULE)
        entry = {
            "id": max((int(r.get("id", 0)) for r in rows), default=0) + 1,
            "重投编号": redeliver_id, "关联分片": shard_id,
            "失败阶段": fail_stage, "失败原因": fail_reason,
            "重投次数": "0", "重投策略": strategy,
            "重投结论": "-", "重投时间": "-",
            "status": "待重投", "pending": True, "abnormal": False,
        }
        rows.append(entry)
        stage = normalize_stage(fail_stage)
        if stage is not None:
            batch, shard_no = _split_shard(shard_id)
            self.engine.register_order(
                redeliver_id, batch, shard_no, stage,
                fail_reason=fail_reason, shard_id=shard_id,
            )
        return entry, f"重投单 {redeliver_id} 已登记"

    def execute_redeliver(
        self, redeliver_id: str, success: bool, conclusion: str,
    ) -> tuple[bool, str]:
        """执行重投并记录结论，同步工单执行中阶段。"""
        rows = store.rows(self.MODULE)
        for r in rows:
            if r.get("重投编号") == redeliver_id:
                count = int(r.get("重投次数", "0")) + 1
                r["重投次数"] = str(count)
                r["重投时间"] = "2026-11-01"
                if success:
                    r["重投结论"] = conclusion
                    r["status"] = "已补齐"
                    r["pending"] = False
                    self._close_order(redeliver_id)
                    return True, f"重投 {redeliver_id} 已补齐"
                else:
                    r["重投结论"] = conclusion
                    r["status"] = "待重投" if count < 3 else "重投放弃"
                    r["pending"] = count < 3
                    if count >= 3:
                        self._close_order(redeliver_id)
                    return False, f"重投 {redeliver_id} 第 {count} 次失败"
        return False, f"重投单 {redeliver_id} 不存在"

    def _close_order(self, order_id: str) -> None:
        order = self.engine.get_order(order_id)
        if order is not None:
            order.active_stage = None
            order.closed = True

    def get_by_shard(self, shard_id: str) -> list[dict[str, Any]]:
        """获取指定分片的所有重投单。"""
        rows = store.rows(self.MODULE)
        return [r for r in rows if r.get("关联分片") == shard_id]

    def get_pending(self) -> list[dict[str, Any]]:
        """获取所有待重投的单据。"""
        rows = store.rows(self.MODULE)
        return [r for r in rows if r.get("status") == "待重投"]

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        snapshot["stage_claim"] = self.engine.snapshot()
        snapshot["stage_summary"] = self.stage_summary()
        return snapshot


def get_fail_redeliver_service() -> FailRedeliverService:
    """给跨模块调用方（转写任务）取同一个服务实例。"""
    from app.services import service_for

    service = service_for("fail_redeliver")
    assert isinstance(service, FailRedeliverService)
    return service
