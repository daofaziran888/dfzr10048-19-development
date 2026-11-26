"""失败分片按阶段领办引擎：把「转码/转写」失败分片当作工单，按失败阶段派发待办。

关键约束（缺一不可）：
- 一张工单同一时刻最多只有一个正在执行的阶段；
- 音轨实物必须按「批次 + 分片序号」严格匹配，异批次的同号录音不能代跑；
- 手上已有可验证的转码实物（有成功标记且有录音）时，不再重复转码；
- 转码回执只有成功标记、没有录音时，不能据此领取转写，继续操作仍要领取转码；
- 开工回执必须绑定实际投入的音轨，转写任务凭回执定位音轨。

典型现场流程：
1. 4 号片还没有音轨 → 值班员领取一次转码（阶段=转码，回执无音轨）；
2. 重投尚未开工，原转码作业的音轨迟到落盘 → 重新计算可用音轨；
3. 沿刚领的工单继续，二选一：
   - 原单转办：同一张工单从转码阶段切到转写阶段，回执记录转办来源；
   - 结掉改领：原单结单，另开一张转写工单并领办；
4. 领取转写后，回执绑定音轨；确认产出时核对实际投入与绑定音轨一致。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

STAGE_TRANSCODE = "转码"
STAGE_TRANSCRIBE = "转写"
STAGE_LABELS = (STAGE_TRANSCODE, STAGE_TRANSCRIBE)

STATUS_PENDING = "待领办"
STATUS_RUNNING = "执行中"
STATUS_CLOSED = "已结单"


def normalize_stage(value: str | None) -> str | None:
    """把「转码失败」「转写待办」等文本归一成阶段类别。"""
    text = str(value or "").strip()
    if not text:
        return None
    if "转写" in text:
        return STAGE_TRANSCRIBE
    if "转码" in text:
        return STAGE_TRANSCODE
    return None


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class AudioArtifact:
    """转码实物：只有同时具备成功标记、录音字节与音轨引用才算可验证。"""

    artifact_id: str
    batch_id: str
    shard_no: int
    job_id: str = ""
    audio_bytes: int = 0
    audio_ref: str = ""
    success_flag: bool = True
    reason: str = ""

    def is_usable(self) -> bool:
        return (
            self.success_flag
            and self.audio_bytes > 0
            and bool(str(self.audio_ref).strip())
        )

    def status_text(self) -> str:
        if self.is_usable():
            return "可验证音轨"
        if self.success_flag:
            return "仅成功标记无录音"
        return "转码未成功"

    def to_dict(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "batch_id": self.batch_id,
            "shard_no": self.shard_no,
            "shard_id": f"{self.batch_id}-S{self.shard_no:03d}",
            "job_id": self.job_id,
            "audio_bytes": self.audio_bytes,
            "audio_ref": self.audio_ref,
            "success_flag": self.success_flag,
            "usable": self.is_usable(),
            "status_text": self.status_text(),
            "reason": self.reason,
        }


@dataclass
class ClaimReceipt:
    """开工回执：领办阶段、工单、绑定音轨与转办来源都在这里，转写任务据此定位实际投入。"""

    receipt_id: str
    order_id: str
    batch_id: str
    shard_no: int
    stage: str
    operator: str
    claimed_at: str
    fail_reason: str
    audio_usable: bool = False
    audio_artifact_id: str | None = None
    audio_source_job: str | None = None
    audio_bytes: int = 0
    audio_ref: str = ""
    note: str = ""
    transferred_from: str | None = None
    started: bool = False
    confirmed: bool = False
    confirmed_at: str | None = None

    def audio_binding(self) -> dict[str, Any]:
        """转写任务需要凭回执定位的音轨信息。"""
        return {
            "开工回执": self.receipt_id,
            "阶段类别": self.stage,
            "批次": self.batch_id,
            "分片序号": self.shard_no,
            "音轨实物": self.audio_artifact_id or "-",
            "音轨来源作业": self.audio_source_job or "-",
            "音轨字节": self.audio_bytes,
            "音轨引用": self.audio_ref or "-",
            "音轨可用": self.audio_usable,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "receipt_id": self.receipt_id,
            "order_id": self.order_id,
            "batch_id": self.batch_id,
            "shard_no": self.shard_no,
            "stage": self.stage,
            "operator": self.operator,
            "claimed_at": self.claimed_at,
            "fail_reason": self.fail_reason,
            "audio_usable": self.audio_usable,
            "audio_artifact_id": self.audio_artifact_id,
            "audio_source_job": self.audio_source_job,
            "audio_bytes": self.audio_bytes,
            "audio_ref": self.audio_ref,
            "note": self.note,
            "transferred_from": self.transferred_from,
            "started": self.started,
            "confirmed": self.confirmed,
            "confirmed_at": self.confirmed_at,
        }
        payload["audio_binding"] = self.audio_binding()
        return payload


@dataclass
class WorkOrder:
    """一张失败分片工单：origin_stage 是失败阶段，stage 是当前要执行（领办）的阶段。"""

    order_id: str
    batch_id: str
    shard_no: int
    origin_stage: str
    fail_reason: str = ""
    shard_id: str = ""
    stage: str = ""
    status: str = STATUS_PENDING
    active_stage: str | None = None
    claimed_by: str | None = None
    open_receipt: ClaimReceipt | None = None
    receipts: list[ClaimReceipt] = field(default_factory=list)
    transfer_count: int = 0
    closed: bool = False
    closed_at: str | None = None
    confirmed_artifact_id: str | None = None
    source_order_id: str | None = None

    def __post_init__(self) -> None:
        if not self.stage:
            self.stage = self.origin_stage

    def has_active_stage(self) -> bool:
        return self.active_stage is not None and not self.closed

    def to_dict(self) -> dict[str, Any]:
        return {
            "order_id": self.order_id,
            "batch_id": self.batch_id,
            "shard_no": self.shard_no,
            "shard_id": self.shard_id,
            "origin_stage": self.origin_stage,
            "stage": self.stage,
            "fail_reason": self.fail_reason,
            "status": self.status,
            "active_stage": self.active_stage,
            "claimed_by": self.claimed_by,
            "open_receipt": self.open_receipt.to_dict() if self.open_receipt else None,
            "transfer_count": self.transfer_count,
            "closed": self.closed,
            "closed_at": self.closed_at,
            "confirmed_artifact_id": self.confirmed_artifact_id,
            "source_order_id": self.source_order_id,
            "receipts": [r.to_dict() for r in self.receipts],
        }


class StageClaimEngine:
    """按阶段领办引擎：维护工单、转码实物与开工回执，负责全部阶段流转判断。"""

    def __init__(self) -> None:
        self._orders: dict[str, WorkOrder] = {}
        self._artifacts: dict[str, AudioArtifact] = {}
        self._receipt_seq = 0

    # ---------- 登记 ----------

    def register_order(
        self,
        order_id: str,
        batch_id: str,
        shard_no: int,
        stage: str,
        fail_reason: str = "",
        shard_id: str = "",
        *,
        source_order_id: str | None = None,
    ) -> WorkOrder:
        normalized = normalize_stage(stage)
        if normalized is None:
            raise ValueError(f"未知阶段：{stage}")
        order = WorkOrder(
            order_id=order_id,
            batch_id=str(batch_id),
            shard_no=int(shard_no),
            origin_stage=normalized,
            stage=normalized,
            fail_reason=fail_reason,
            shard_id=shard_id or f"{batch_id}-S{int(shard_no):03d}",
            source_order_id=source_order_id,
        )
        self._orders[order_id] = order
        return order

    def register_artifact(
        self,
        artifact_id: str,
        batch_id: str,
        shard_no: int,
        *,
        job_id: str = "",
        audio_bytes: int = 0,
        audio_ref: str = "",
        success_flag: bool = True,
        reason: str = "",
    ) -> AudioArtifact:
        artifact = AudioArtifact(
            artifact_id=artifact_id,
            batch_id=str(batch_id),
            shard_no=int(shard_no),
            job_id=job_id or artifact_id,
            audio_bytes=int(audio_bytes),
            audio_ref=audio_ref,
            success_flag=success_flag,
            reason=reason,
        )
        self._artifacts[artifact_id] = artifact
        return artifact

    def get_order(self, order_id: str) -> WorkOrder | None:
        return self._orders.get(order_id)

    def get_artifact(self, artifact_id: str) -> AudioArtifact | None:
        return self._artifacts.get(artifact_id)

    # ---------- 音轨查询 ----------

    def latest_artifact(self, batch_id: str, shard_no: int) -> AudioArtifact | None:
        """取同批次同分片序号的最新转码实物；异批次同号不参与匹配。"""
        matched = [
            a for a in self._artifacts.values()
            if a.batch_id == str(batch_id) and a.shard_no == int(shard_no)
        ]
        return matched[-1] if matched else None

    def usable_artifact(self, batch_id: str, shard_no: int) -> AudioArtifact | None:
        """取同批次同分片序号、可验证（有成功标记且有录音）的转码实物。"""
        matched = [
            a for a in self._artifacts.values()
            if a.batch_id == str(batch_id) and a.shard_no == int(shard_no) and a.is_usable()
        ]
        return matched[-1] if matched else None

    # ---------- 待办 ----------

    def list_todos(
        self, stage: str | None = None, page: int = 1, size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        """按失败阶段筛选待办，按分片序号升序（同号按批次、工单号）排序后分页。"""
        wanted = normalize_stage(stage) if stage else None
        rows = [o for o in self._orders.values() if not o.closed]
        if wanted:
            rows = [o for o in rows if o.origin_stage == wanted]
        rows.sort(key=lambda o: (o.shard_no, o.batch_id, o.order_id))
        total = len(rows)
        page = max(1, int(page))
        size = max(1, int(size))
        start = (page - 1) * size
        return [self.todo_payload(o) for o in rows[start:start + size]], total

    def todo_payload(self, order: WorkOrder) -> dict[str, Any]:
        usable = self.usable_artifact(order.batch_id, order.shard_no)
        latest = self.latest_artifact(order.batch_id, order.shard_no)
        if order.has_active_stage():
            if order.active_stage == STAGE_TRANSCODE:
                suggestion = "继续转码；音轨可用后可原单转办或结单改领转写"
            else:
                suggestion = "继续转写"
        elif order.stage == STAGE_TRANSCODE:
            suggestion = "改领转写" if usable else "领取转码"
        else:
            suggestion = "领取转写" if usable else "先领取转码"
        return {
            **order.to_dict(),
            "failed_stage": order.origin_stage,
            "claim_stage": order.stage,
            "usable_audio": usable.to_dict() if usable else None,
            "latest_artifact": latest.to_dict() if latest else None,
            "suggestion": suggestion,
        }

    # ---------- 领办 ----------

    def _new_receipt(
        self, order: WorkOrder, stage: str, operator: str, *,
        artifact: AudioArtifact | None = None,
        note: str = "",
        transferred_from: str | None = None,
    ) -> ClaimReceipt:
        self._receipt_seq += 1
        return ClaimReceipt(
            receipt_id=f"RC-{order.order_id}-{self._receipt_seq:03d}",
            order_id=order.order_id,
            batch_id=order.batch_id,
            shard_no=order.shard_no,
            stage=stage,
            operator=operator,
            claimed_at=_now(),
            fail_reason=order.fail_reason,
            audio_usable=bool(artifact and artifact.is_usable()),
            audio_artifact_id=artifact.artifact_id if artifact else None,
            audio_source_job=artifact.job_id if artifact else None,
            audio_bytes=artifact.audio_bytes if artifact else 0,
            audio_ref=artifact.audio_ref if artifact else "",
            note=note,
            transferred_from=transferred_from,
        )

    def _issue_transcode_receipt(
        self, order: WorkOrder, operator: str, note: str,
    ) -> ClaimReceipt:
        latest = self.latest_artifact(order.batch_id, order.shard_no)
        if latest is not None:
            note = f"{note}；上次转码{latest.status_text()}".strip("；")
        receipt = self._new_receipt(order, STAGE_TRANSCODE, operator, note=note)
        order.stage = STAGE_TRANSCODE
        order.active_stage = STAGE_TRANSCODE
        order.status = STATUS_RUNNING
        order.claimed_by = operator
        order.open_receipt = receipt
        order.receipts.append(receipt)
        return receipt

    def _issue_transcribe_receipt(
        self, order: WorkOrder, operator: str, artifact: AudioArtifact,
        note: str = "", transferred_from: str | None = None,
    ) -> ClaimReceipt:
        receipt = self._new_receipt(
            order, STAGE_TRANSCRIBE, operator, artifact=artifact,
            note=note, transferred_from=transferred_from,
        )
        order.stage = STAGE_TRANSCRIBE
        order.active_stage = STAGE_TRANSCRIBE
        order.status = STATUS_RUNNING
        order.claimed_by = operator
        order.open_receipt = receipt
        order.receipts.append(receipt)
        return receipt

    def claim(
        self, order_id: str, operator: str,
    ) -> tuple[bool, str, ClaimReceipt | None]:
        """按工单当前阶段领办。

        - 转码工单：已有可验证转码实物时拒绝重复转码，提示改领转写；
        - 转写工单：必须绑定同批次、同分片序号、可验证的音轨；没有音轨或
          回执只有成功标记时，改按转码阶段领办，并保留原失败原因备查。
        """
        order = self._orders.get(order_id)
        if order is None:
            return False, f"工单 {order_id} 不存在", None
        operator = str(operator or "").strip()
        if not operator:
            return False, "领办人不能为空", None
        if order.closed:
            return False, f"工单 {order_id} 已结单，不能重复领办", None
        if order.has_active_stage():
            return False, (
                f"工单 {order_id} 已有正在执行的阶段（{order.active_stage}），"
                "一张工单最多只有一个执行中阶段"
            ), order.open_receipt

        if order.stage == STAGE_TRANSCODE:
            usable = self.usable_artifact(order.batch_id, order.shard_no)
            if usable is not None:
                return False, (
                    f"分片 {order.shard_no} 已有可验证转码实物 {usable.artifact_id}"
                    f"（{usable.audio_bytes} 字节），无需重复转码；"
                    "可原单转办到转写，或结单后改领转写"
                ), None
            receipt = self._issue_transcode_receipt(order, operator, "领取转码")
            return True, (
                f"工单 {order_id} 已由 {operator} 领办转码阶段，"
                f"开工回执 {receipt.receipt_id}"
            ), receipt

        latest = self.latest_artifact(order.batch_id, order.shard_no)
        usable = latest if latest is not None and latest.is_usable() else None
        if usable is not None:
            receipt = self._issue_transcribe_receipt(order, operator, usable, "领取转写")
            return True, (
                f"工单 {order_id} 已由 {operator} 领办转写阶段，"
                f"音轨 {usable.artifact_id}（{usable.audio_bytes} 字节），"
                f"开工回执 {receipt.receipt_id}"
            ), receipt

        if latest is not None and latest.success_flag:
            why = f"转码回执 {latest.artifact_id} 仅有成功标记、没有录音"
        elif latest is not None:
            why = f"转码回执 {latest.artifact_id} 未标记成功"
        else:
            why = "尚未产出任何转码音轨"
        receipt = self._issue_transcode_receipt(
            order, operator,
            f"原为{order.origin_stage}失败，{why}，先领取转码",
        )
        return True, (
            f"分片 {order.shard_no} {why}，不能领取转写；"
            f"已按转码阶段领办，开工回执 {receipt.receipt_id}。"
            f"原失败原因：{order.fail_reason or '未记录'}"
        ), receipt

    def start_work(self, order_id: str) -> tuple[bool, str]:
        """把已领办的阶段标记为已开工；开工后不能转办，只能结单后改领。"""
        order = self._orders.get(order_id)
        if order is None:
            return False, f"工单 {order_id} 不存在"
        if order.open_receipt is None:
            return False, f"工单 {order_id} 尚未领办"
        if order.open_receipt.started:
            return False, f"工单 {order_id} 已开工，无需重复标记"
        order.open_receipt.started = True
        return True, f"工单 {order_id} 已开工（{order.open_receipt.stage}阶段）"

    def transfer(
        self, order_id: str, operator: str,
    ) -> tuple[bool, str, ClaimReceipt | None]:
        """原单转办：同一张工单从转码阶段切到转写阶段，不新开单。"""
        order = self._orders.get(order_id)
        if order is None:
            return False, f"工单 {order_id} 不存在", None
        operator = str(operator or "").strip()
        if not operator:
            return False, "领办人不能为空", None
        if order.closed:
            return False, f"工单 {order_id} 已结单，不能转办", None
        if order.active_stage != STAGE_TRANSCODE:
            return False, f"工单 {order_id} 当前没有执行中的转码阶段，不能转办", None
        if order.open_receipt is not None and order.open_receipt.started:
            return False, "原单已开工，不能转办；请结单后改领转写", None
        usable = self.usable_artifact(order.batch_id, order.shard_no)
        if usable is None:
            return False, (
                f"分片 {order.shard_no} 尚无可验证转码音轨，不能转办到转写；"
                "请继续转码或核对原失败原因"
            ), None

        previous = order.open_receipt
        # 先清空当前阶段再切到转写，保证同一时刻只有一个执行中阶段。
        order.active_stage = None
        receipt = self._issue_transcribe_receipt(
            order, operator, usable,
            note=f"原单转办：{previous.receipt_id if previous else '-'} 转码→转写",
            transferred_from=previous.receipt_id if previous else None,
        )
        order.transfer_count += 1
        return True, (
            f"工单 {order_id} 已从转码转办到转写，音轨 {usable.artifact_id}"
            f"（{usable.audio_bytes} 字节），开工回执 {receipt.receipt_id}"
        ), receipt

    def close_and_reclaim(
        self, order_id: str, operator: str,
    ) -> tuple[bool, str, WorkOrder | None, ClaimReceipt | None]:
        """结掉当前转码工单，另开一张转写工单并领办。"""
        order = self._orders.get(order_id)
        if order is None:
            return False, f"工单 {order_id} 不存在", None, None
        operator = str(operator or "").strip()
        if not operator:
            return False, "领办人不能为空", None, None
        if order.closed:
            return False, f"工单 {order_id} 已结单", None, None
        if order.active_stage != STAGE_TRANSCODE:
            return False, f"工单 {order_id} 当前没有执行中的转码阶段，不能结单改领", None, None
        usable = self.usable_artifact(order.batch_id, order.shard_no)
        if usable is None:
            return False, (
                f"分片 {order.shard_no} 尚无可验证转码音轨，不能改领转写"
            ), None, None

        order.active_stage = None
        order.closed = True
        order.status = STATUS_CLOSED
        order.closed_at = _now()
        if order.open_receipt is not None:
            order.open_receipt.note = (
                f"{order.open_receipt.note}；已结单改领转写".strip("；")
            )

        suffix = 1
        new_order_id = f"{order_id}-T{suffix}"
        while new_order_id in self._orders:
            suffix += 1
            new_order_id = f"{order_id}-T{suffix}"
        new_order = self.register_order(
            new_order_id, order.batch_id, order.shard_no, STAGE_TRANSCRIBE,
            order.fail_reason, shard_id=order.shard_id, source_order_id=order_id,
        )
        ok, msg, receipt = self.claim(new_order_id, operator)
        if not ok:
            return False, msg, new_order, receipt
        return True, (
            f"原工单 {order_id} 已结单，改领转写工单 {new_order_id}，"
            f"音轨 {usable.artifact_id}，开工回执 {receipt.receipt_id if receipt else '-'}"
        ), new_order, receipt

    def confirm_output(
        self, order_id: str, artifact_id: str | None, operator: str = "",
    ) -> tuple[bool, str, ClaimReceipt | None]:
        """确认产出：核对转写任务实际投入的音轨与开工回执绑定音轨是否一致。"""
        order = self._orders.get(order_id)
        if order is None:
            return False, f"工单 {order_id} 不存在", None
        if order.closed and order.confirmed_artifact_id:
            return False, f"工单 {order_id} 已确认产出，无需重复核对", None
        receipt = order.open_receipt
        if receipt is None:
            return False, f"工单 {order_id} 尚未领办，无开工回执可核对", None
        if receipt.stage != STAGE_TRANSCRIBE:
            return False, "只有转写阶段的产出可以核对实际投入音轨", None
        if not receipt.audio_artifact_id:
            return False, "开工回执未绑定音轨，无法核对实际投入", None
        actual_id = str(artifact_id or "").strip() or receipt.audio_artifact_id
        if actual_id != receipt.audio_artifact_id:
            return False, (
                f"实际投入音轨 {actual_id} 与开工回执绑定音轨 "
                f"{receipt.audio_artifact_id} 不一致"
            ), None
        artifact = self._artifacts.get(actual_id)
        if artifact is None or not artifact.is_usable():
            return False, f"音轨实物 {actual_id} 不可验证，不能确认产出", None
        if artifact.batch_id != order.batch_id or artifact.shard_no != order.shard_no:
            return False, (
                f"音轨 {actual_id} 属于批次 {artifact.batch_id} 分片 {artifact.shard_no}，"
                f"与工单 {order.batch_id} 分片 {order.shard_no} 不一致，异批次同号不得代跑"
            ), None

        receipt.confirmed = True
        receipt.confirmed_at = _now()
        if operator:
            receipt.note = f"{receipt.note}；产出由 {operator} 核对".strip("；")
        order.confirmed_artifact_id = actual_id
        order.active_stage = None
        order.closed = True
        order.status = STATUS_CLOSED
        order.closed_at = receipt.confirmed_at
        return True, (
            f"工单 {order_id} 产出已核对：实际投入音轨 {actual_id}"
            f"（{artifact.audio_bytes} 字节，来源作业 {artifact.job_id}）"
        ), receipt

    # ---------- 查询 ----------

    def failure_detail(self, order_id: str) -> tuple[bool, str, dict[str, Any]]:
        """查看原失败原因与当前音轨状态，供现场决定是原单转办还是结单改领。"""
        order = self._orders.get(order_id)
        if order is None:
            return False, f"工单 {order_id} 不存在", {}
        latest = self.latest_artifact(order.batch_id, order.shard_no)
        usable = self.usable_artifact(order.batch_id, order.shard_no)
        if usable is not None:
            advice = "已有可验证转码音轨，可原单转办或结单改领转写"
        elif latest is not None and latest.success_flag:
            advice = "转码回执仅有成功标记、没有录音，需继续领取转码"
        elif latest is not None:
            advice = "转码未成功，需继续领取转码"
        else:
            advice = "尚无转码音轨，需领取转码"
        return True, f"原失败原因：{order.fail_reason or '未记录'}", {
            "order_id": order.order_id,
            "batch_id": order.batch_id,
            "shard_no": order.shard_no,
            "origin_stage": order.origin_stage,
            "claim_stage": order.stage,
            "active_stage": order.active_stage,
            "status": order.status,
            "fail_reason": order.fail_reason or "未记录失败原因",
            "latest_artifact": latest.to_dict() if latest else None,
            "usable_audio": usable.to_dict() if usable else None,
            "can_claim_transcode": usable is None,
            "can_claim_transcribe": usable is not None,
            "advice": advice,
        }

    def find_receipt(self, receipt_id: str) -> ClaimReceipt | None:
        for order in self._orders.values():
            for receipt in order.receipts:
                if receipt.receipt_id == receipt_id:
                    return receipt
        return None

    def snapshot(self) -> dict[str, Any]:
        by_stage = {
            stage: sum(
                1 for o in self._orders.values()
                if o.origin_stage == stage and not o.closed
            )
            for stage in STAGE_LABELS
        }
        return {
            "orders": [o.to_dict() for o in self._orders.values()],
            "todos": len([o for o in self._orders.values() if not o.closed]),
            "by_stage": by_stage,
            "running": sum(1 for o in self._orders.values() if o.has_active_stage()),
            "artifacts": [a.to_dict() for a in self._artifacts.values()],
        }
