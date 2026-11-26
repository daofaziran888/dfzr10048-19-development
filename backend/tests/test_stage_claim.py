"""按失败阶段领办的现场流程测试：转码/转写分离、音轨匹配、转办与改领。"""
from __future__ import annotations

import pytest

from app.pipeline.stage_claim_engine import (
    STAGE_TRANSCODE,
    STAGE_TRANSCRIBE,
    StageClaimEngine,
)


def make_engine() -> StageClaimEngine:
    engine = StageClaimEngine()
    engine.register_order("FRDL-S004", "BATCH-A", 4, "转码失败", "转码进程被杀")
    engine.register_order("FRDL-S002", "BATCH-A", 2, "转写失败", "引擎超时")
    engine.register_order("FRDL-S003", "BATCH-B", 3, "转写失败", "音频静音")
    engine.register_order("FRDL-S001", "BATCH-B", 1, "转码失败", "参数不兼容")
    return engine


class TestTodoListing:
    def test_sorted_by_shard_no(self):
        engine = make_engine()
        items, total = engine.list_todos()
        assert total == 4
        assert [item["shard_no"] for item in items] == [1, 2, 3, 4]

    def test_filter_by_stage(self):
        engine = make_engine()
        items, total = engine.list_todos("转码")
        assert total == 2
        assert {item["origin_stage"] for item in items} == {"转码"}
        assert [item["shard_no"] for item in items] == [1, 4]

    def test_filter_accepts_failure_text(self):
        engine = make_engine()
        _, total = engine.list_todos("转写失败")
        assert total == 2

    def test_pagination(self):
        engine = make_engine()
        first, total = engine.list_todos(page=1, size=3)
        second, _ = engine.list_todos(page=2, size=3)
        assert total == 4
        assert [i["shard_no"] for i in first] == [1, 2, 3]
        assert [i["shard_no"] for i in second] == [4]

    def test_todo_payload_marks_suggestion(self):
        engine = make_engine()
        items, _ = engine.list_todos("转码")
        by_no = {item["shard_no"]: item for item in items}
        assert by_no[4]["suggestion"] == "领取转码"
        assert by_no[4]["usable_audio"] is None

    def test_closed_orders_leave_todo(self):
        engine = make_engine()
        engine.register_artifact(
            "ART-A2", "BATCH-A", 2, audio_bytes=4096, audio_ref="s3://a/2.wav")
        engine.claim("FRDL-S002", "值班员")
        engine.confirm_output("FRDL-S002", "ART-A2")
        _, total = engine.list_todos()
        assert total == 3


class TestSingleActiveStage:
    def test_second_claim_rejected(self):
        engine = make_engine()
        ok, _, _ = engine.claim("FRDL-S004", "值班员")
        assert ok
        ok2, msg2, receipt2 = engine.claim("FRDL-S004", "另一值班员")
        assert not ok2
        assert "正在执行的阶段" in msg2
        assert receipt2 is not None

    def test_transfer_rejected_after_start(self):
        engine = make_engine()
        engine.claim("FRDL-S004", "值班员")
        engine.start_work("FRDL-S004")
        engine.register_artifact(
            "ART-A4", "BATCH-A", 4, audio_bytes=1024, audio_ref="s3://a/4.wav")
        ok, msg, _ = engine.transfer("FRDL-S004", "值班员")
        assert not ok
        assert "已开工" in msg

    def test_claim_requires_operator(self):
        engine = make_engine()
        ok, msg, _ = engine.claim("FRDL-S004", "  ")
        assert not ok
        assert "领办人" in msg


class TestAudioMatching:
    def test_cross_batch_same_number_rejected(self):
        engine = make_engine()
        # 异批次的 4 号音轨不能给 BATCH-A 的 4 号片用。
        engine.register_artifact(
            "ART-B4", "BATCH-B", 4, audio_bytes=2048, audio_ref="s3://b/4.wav")
        assert engine.usable_artifact("BATCH-A", 4) is None
        ok, msg, _ = engine.claim("FRDL-S004", "值班员")
        assert ok
        assert engine.get_order("FRDL-S004").open_receipt.stage == STAGE_TRANSCODE

    def test_success_flag_without_audio_not_usable(self):
        engine = make_engine()
        engine.register_artifact(
            "ART-A4-FLAG", "BATCH-A", 4, audio_bytes=0, audio_ref="",
            success_flag=True)
        assert engine.usable_artifact("BATCH-A", 4) is None
        latest = engine.latest_artifact("BATCH-A", 4)
        assert latest.status_text() == "仅成功标记无录音"

    def test_usable_requires_ref(self):
        engine = make_engine()
        engine.register_artifact(
            "ART-A4-NOREF", "BATCH-A", 4, audio_bytes=4096, audio_ref="")
        assert engine.usable_artifact("BATCH-A", 4) is None


class TestClaimTranscode:
    def test_claim_transcode_without_audio(self):
        engine = make_engine()
        ok, msg, receipt = engine.claim("FRDL-S004", "值班员")
        assert ok
        assert receipt.stage == STAGE_TRANSCODE
        assert receipt.audio_artifact_id is None
        assert receipt.audio_usable is False
        order = engine.get_order("FRDL-S004")
        assert order.active_stage == STAGE_TRANSCODE
        assert order.open_receipt.receipt_id == receipt.receipt_id

    def test_no_repeat_transcode_when_usable(self):
        engine = make_engine()
        engine.register_artifact(
            "ART-A4", "BATCH-A", 4, audio_bytes=8192, audio_ref="s3://a/4.wav")
        ok, msg, _ = engine.claim("FRDL-S004", "值班员")
        assert not ok
        assert "无需重复转码" in msg

    def test_claim_reports_last_failure(self):
        engine = make_engine()
        engine.register_artifact(
            "ART-A4-FLAG", "BATCH-A", 4, audio_bytes=0, audio_ref="",
            success_flag=True)
        ok, msg, receipt = engine.claim("FRDL-S004", "值班员")
        assert ok
        assert receipt.stage == STAGE_TRANSCODE
        assert "仅成功标记" in receipt.note

    def test_claim_transcribe_order_without_audio_falls_back_to_transcode(self):
        engine = make_engine()
        engine.register_artifact(
            "ART-A2-FLAG", "BATCH-A", 2, audio_bytes=0, audio_ref="")
        ok, msg, receipt = engine.claim("FRDL-S002", "值班员")
        assert ok
        assert receipt.stage == STAGE_TRANSCODE
        assert "不能领取转写" in msg
        # 失败阶段保持原值，便于事后查为何失败。
        assert engine.get_order("FRDL-S002").origin_stage == STAGE_TRANSCRIBE


class TestTransfer:
    def test_transfer_after_late_audio(self):
        engine = make_engine()
        ok, _, first = engine.claim("FRDL-S004", "值班员")
        assert ok
        assert first.stage == STAGE_TRANSCODE

        # 重投未开工，原作业的音轨才迟到落盘。
        engine.register_artifact(
            "ART-A4", "BATCH-A", 4, job_id="TRCD-OLD", audio_bytes=5120,
            audio_ref="s3://batch-a/4.wav")

        ok, msg, second = engine.transfer("FRDL-S004", "值班员")
        assert ok
        assert second.stage == STAGE_TRANSCRIBE
        assert second.audio_artifact_id == "ART-A4"
        assert second.audio_source_job == "TRCD-OLD"
        assert second.audio_bytes == 5120
        assert second.transferred_from == first.receipt_id

        order = engine.get_order("FRDL-S004")
        assert order.transfer_count == 1
        assert order.stage == STAGE_TRANSCRIBE
        assert order.active_stage == STAGE_TRANSCRIBE
        # 转办不新开单。
        _, total = engine.list_todos()
        assert total == 4

    def test_transfer_without_audio_rejected(self):
        engine = make_engine()
        engine.claim("FRDL-S004", "值班员")
        ok, msg, _ = engine.transfer("FRDL-S004", "值班员")
        assert not ok
        assert "尚无可验证转码音轨" in msg
        assert engine.get_order("FRDL-S004").active_stage == STAGE_TRANSCODE

    def test_close_and_reclaim_opens_new_order(self):
        engine = make_engine()
        engine.claim("FRDL-S004", "值班员")
        engine.register_artifact(
            "ART-A4", "BATCH-A", 4, job_id="TRCD-OLD", audio_bytes=5120,
            audio_ref="s3://batch-a/4.wav")
        ok, msg, new_order, receipt = engine.close_and_reclaim("FRDL-S004", "值班员")
        assert ok
        assert new_order is not None
        assert new_order.order_id == "FRDL-S004-T1"
        assert new_order.source_order_id == "FRDL-S004"
        assert receipt.stage == STAGE_TRANSCRIBE
        assert receipt.audio_artifact_id == "ART-A4"

        old = engine.get_order("FRDL-S004")
        assert old.closed is True
        assert old.active_stage is None
        # 旧单结掉后只剩新的转写工单。
        items, total = engine.list_todos()
        assert total == 4
        assert "FRDL-S004-T1" in [i["order_id"] for i in items]


class TestConfirmOutput:
    def test_confirm_checks_actual_artifact(self):
        engine = make_engine()
        engine.register_artifact(
            "ART-A2", "BATCH-A", 2, audio_bytes=4096, audio_ref="s3://a/2.wav")
        engine.claim("FRDL-S002", "值班员")
        ok, msg, _ = engine.confirm_output("FRDL-S002", "ART-OTHER")
        assert not ok
        assert "不一致" in msg

    def test_confirm_without_audio_rejected(self):
        engine = make_engine()
        engine.claim("FRDL-S004", "值班员")
        ok, msg, _ = engine.confirm_output("FRDL-S004", None)
        assert not ok
        assert "转写阶段" in msg

    def test_confirm_after_transfer(self):
        engine = make_engine()
        engine.claim("FRDL-S004", "值班员")
        engine.register_artifact(
            "ART-A4", "BATCH-A", 4, job_id="TRCD-OLD", audio_bytes=5120,
            audio_ref="s3://batch-a/4.wav")
        engine.transfer("FRDL-S004", "值班员")
        ok, msg, receipt = engine.confirm_output("FRDL-S004", "ART-A4", "值班员")
        assert ok
        assert receipt.confirmed is True
        assert "5120 字节" in msg
        assert engine.get_order("FRDL-S004").closed is True


class TestFailureDetail:
    def test_detail_explains_flag_only(self):
        engine = make_engine()
        engine.register_artifact(
            "ART-A4-FLAG", "BATCH-A", 4, audio_bytes=0, audio_ref="",
            success_flag=True)
        ok, _, detail = engine.failure_detail("FRDL-S004")
        assert ok
        assert detail["fail_reason"] == "转码进程被杀"
        assert detail["can_claim_transcode"] is True
        assert detail["can_claim_transcribe"] is False
        assert "继续领取转码" in detail["advice"]

    def test_detail_after_late_audio(self):
        engine = make_engine()
        engine.register_artifact(
            "ART-A4", "BATCH-A", 4, audio_bytes=5120, audio_ref="s3://a/4.wav")
        _, _, detail = engine.failure_detail("FRDL-S004")
        assert detail["can_claim_transcribe"] is True
        assert "转办" in detail["advice"]


class TestReceiptBinding:
    def test_receipt_binding_locates_audio(self):
        engine = make_engine()
        engine.register_artifact(
            "ART-A2", "BATCH-A", 2, job_id="TRCD-OLD", audio_bytes=5120,
            audio_ref="s3://batch-a/2.wav")
        _, _, receipt = engine.claim("FRDL-S002", "值班员")
        found = engine.find_receipt(receipt.receipt_id)
        binding = found.audio_binding()
        assert binding["音轨实物"] == "ART-A2"
        assert binding["音轨来源作业"] == "TRCD-OLD"
        assert binding["音轨字节"] == 5120
        assert binding["音轨可用"] is True

    def test_transcode_receipt_has_no_audio(self):
        engine = make_engine()
        _, _, receipt = engine.claim("FRDL-S004", "值班员")
        binding = receipt.audio_binding()
        assert binding["音轨可用"] is False
        assert binding["音轨实物"] == "-"
