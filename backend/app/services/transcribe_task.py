"""转写任务转换领域服务：对转码完成的分片执行语音转文本转换，产出转写产出数据集供语音分段与置信复核消费。"""
from __future__ import annotations

from typing import Any

from app.services.base import BaseModuleService
from app.store import store


class TranscribeTaskService(BaseModuleService):
    MODULE = "transcribe_task"
    ENTITY = "转写任务"
    OPERATION_TERM = "转换"
    LIST_FIELDS = ["任务编号", "关联分片", "转写引擎", "语言设定", "输出文本行数", "平均置信度", "转写耗时", "任务批次"]
    REQUIRED_FIELDS = ["任务编号", "关联分片", "转写引擎"]
    STATUS_ORDER = ["待转写", "转写中", "已产出", "产出异常"]
    ACTION_RULES = {"启动转写": "转写中", "确认产出": "已产出", "登记异常": "产出异常"}
    NEGATIVE_ACTIONS = []

    def produce_transcription(
        self, task_id: str, line_count: int, avg_confidence: float,
        elapsed_seconds: int,
        receipt_id: str | None = None,
    ) -> tuple[bool, str, dict[str, Any] | None]:
        """记录转写产出：更新文本行数、置信度与产出状态。

        传了开工回执时，必须能凭回执定位到实际投入的可用音轨，否则不允许产出。
        """
        binding: dict[str, Any] | None = None
        if receipt_id:
            ok, message, detail = self.locate_audio_receipt(receipt_id)
            if not ok:
                return False, message, None
            binding = detail["audio_binding"]
        rows = store.rows(self.MODULE)
        for r in rows:
            if r.get("任务编号") == task_id:
                r["输出文本行数"] = str(line_count)
                r["平均置信度"] = str(avg_confidence)
                r["转写耗时"] = str(elapsed_seconds)
                r["status"] = "已产出"
                r["pending"] = False
                if binding is not None:
                    r["开工回执"] = binding["开工回执"]
                    r["投入音轨"] = binding["音轨实物"]
                    r["音轨来源作业"] = binding["音轨来源作业"]
                return True, f"任务 {task_id} 已产出 {line_count} 行", dict(r)
        return False, f"任务 {task_id} 不存在", None

    def locate_audio_receipt(self, receipt_id: str) -> tuple[bool, str, dict[str, Any]]:
        """凭开工回执定位实际投入的音轨；回执未绑定可用音轨时返回失败。"""
        from app.services.fail_redeliver import get_fail_redeliver_service

        ok, message, detail = get_fail_redeliver_service().receipt_detail(receipt_id)
        if not ok:
            return False, message, {}
        binding = detail.get("audio_binding") or {}
        if not binding.get("音轨可用"):
            return False, (
                f"开工回执 {receipt_id} 未绑定可用音轨（{binding.get('音轨实物', '-')}），"
                "不能据此定位转写投入"
            ), {}
        return True, message, detail

    def get_by_batch(self, batch_id: str) -> list[dict[str, Any]]:
        """获取指定批次的所有转写任务。"""
        rows = store.rows(self.MODULE)
        return [r for r in rows if r.get("任务批次") == batch_id]

    def batch_avg_confidence(self, batch_id: str) -> float:
        """计算指定批次的平均置信度。"""
        tasks = self.get_by_batch(batch_id)
        confs = [
            float(t.get("平均置信度", "0"))
            for t in tasks
            if t.get("平均置信度") and float(t.get("平均置信度", "0")) > 0
        ]
        return sum(confs) / len(confs) if confs else 0.0

    def get_low_confidence_tasks(self, threshold: float) -> list[dict[str, Any]]:
        """获取低于指定置信度阈值的已产出任务。"""
        rows = store.rows(self.MODULE)
        return [
            r for r in rows
            if r.get("status") == "已产出"
            and float(r.get("平均置信度", "0")) < threshold
        ]