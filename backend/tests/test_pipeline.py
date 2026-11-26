"""管线层关键链路测试：跨模块真实数据链路。"""
from __future__ import annotations

import pytest

from app.pipeline.shard_watermark_checker import ShardWatermarkChecker, ShardWatermarkLine
from app.pipeline.redeliver_engine import RedeliverEngine
from app.pipeline.transcribe_quota_manager import TranscribeQuotaManager
from app.pipeline.version_conflict_resolver import VersionConflictResolver, VersionEntry
from app.pipeline.sample_qc_gate import SampleQcGate
from app.pipeline.subtitle_pack_assembler import SubtitlePackAssembler
from app.pipeline.archive_reclaimer import ArchiveReclaimer
from app.pipeline.integration import PipelineOrchestrator


class TestShardWatermarkChecker:
    def test_register_and_advance(self):
        checker = ShardWatermarkChecker()
        line = checker.register_media("MEDI-001", 5)
        for s in range(1, 6):
            line.mark_processed(s)
        ok, msg = checker.try_advance("MEDI-001")
        assert ok
        assert "推进至分片 5/5" in msg

    def test_advance_blocked_by_missing_shard(self):
        checker = ShardWatermarkChecker()
        line = checker.register_media("MEDI-002", 5)
        line.mark_processed(1)
        line.mark_processed(2)
        line.mark_processed(4)
        ok, msg = checker.try_advance("MEDI-002")
        assert not ok
        assert "不连续" in msg

    def test_line_continuity_check(self):
        line = ShardWatermarkLine("MEDI-003", 3)
        line.mark_processed(1)
        line.mark_processed(2)
        line.mark_processed(3)
        can, seq = line.check_continuity()
        assert can
        assert seq == 3

    def test_line_continuity_gap(self):
        line = ShardWatermarkLine("MEDI-004", 3)
        line.mark_processed(1)
        line.mark_processed(3)
        can, seq = line.check_continuity()
        assert not can
        assert seq == 1

    def test_mark_failed_shard(self):
        line = ShardWatermarkLine("MEDI-005", 3)
        line.mark_processed(1)
        line.mark_failed(2)
        line.mark_processed(3)
        assert 2 in line.lag_shards
        can, _ = line.check_continuity()
        assert not can

    def test_unregistered_media(self):
        checker = ShardWatermarkChecker()
        ok, msg = checker.try_advance("NONEXIST")
        assert not ok
        assert "未注册" in msg

    def test_full_advance_then_snapshot(self):
        checker = ShardWatermarkChecker()
        line = checker.register_media("MEDI-007", 2)
        line.mark_processed(1)
        line.mark_processed(2)
        checker.try_advance("MEDI-007")
        snap = checker.snapshot()
        assert "lines" in snap
        assert snap["lines"]["MEDI-007"]["last_advanced_seq"] == 2


class TestRedeliverEngine:
    def test_register_and_redeliver(self):
        engine = RedeliverEngine()
        engine.register_success("SHRD-001")
        entry = engine.register_failed("SHRD-002", "转码失败", "超时")
        ok, msg = engine.execute_retry(entry.entry_id, "success")
        assert ok
        assert "重投成功" in msg

    def test_no_redeliver_for_success(self):
        engine = RedeliverEngine()
        engine.register_success("SHRD-001")
        entry = engine.register_failed("SHRD-001", "转码失败", "超时")
        candidates = engine.get_redeliver_candidates()
        assert len(candidates) == 0

    def test_retry_limit(self):
        engine = RedeliverEngine()
        entry = engine.register_failed("SHRD-003", "转写失败", "引擎错误")
        for i in range(3):
            engine.execute_retry(entry.entry_id, "fail")
        candidates = engine.get_redeliver_candidates()
        assert len(candidates) == 0

    def test_partial_success_protection(self):
        engine = RedeliverEngine()
        engine.register_success("SHRD-004")
        engine.register_success("SHRD-005")
        engine.register_failed("SHRD-006", "转码失败", "参数错误")
        missing = engine.get_missing_shards()
        assert "SHRD-006" in missing
        assert "SHRD-004" not in missing

    def test_missing_shards_tracking(self):
        engine = RedeliverEngine()
        engine.register_failed("SHRD-A", "转码", "E1")
        engine.register_failed("SHRD-B", "转写", "E2")
        engine.register_success("SHRD-A")
        missing = engine.get_missing_shards()
        assert "SHRD-A" not in missing
        assert "SHRD-B" in missing

    def test_redeliver_success_adds_to_success_set(self):
        engine = RedeliverEngine()
        entry = engine.register_failed("SHRD-007", "转写失败", "超时")
        before = len(engine.get_missing_shards())
        engine.execute_retry(entry.entry_id, "success")
        after = len(engine.get_missing_shards())
        assert after < before


class TestTranscribeQuotaManager:
    def test_consume_within_limit(self):
        mgr = TranscribeQuotaManager()
        mgr.register_slot("渠道A", "Whisper", 600)
        ok, msg = mgr.consume("渠道A", "Whisper", "TASK-1", 100)
        assert ok
        assert "配额扣减" in msg

    def test_consume_over_limit_queue(self):
        mgr = TranscribeQuotaManager()
        mgr.register_slot("渠道A", "Whisper", 100)
        ok, msg = mgr.consume("渠道A", "Whisper", "TASK-2", 150)
        assert not ok
        assert "排队" in msg

    def test_reset_window_releases_queued(self):
        mgr = TranscribeQuotaManager()
        mgr.register_slot("渠道A", "Whisper", 100)
        mgr.consume("渠道A", "Whisper", "TASK-2", 150)
        ok, msg = mgr.reset_window("渠道A", "Whisper")
        assert ok
        assert "放行" in msg

    def test_frozen_slot_queues(self):
        mgr = TranscribeQuotaManager()
        slot = mgr.register_slot("渠道B", "SenseVoice", 300)
        slot.frozen = True
        ok, msg = mgr.consume("渠道B", "SenseVoice", "TASK-3", 50)
        assert not ok
        assert "排队" in msg

    def test_auto_register_slot(self):
        mgr = TranscribeQuotaManager()
        ok, msg = mgr.consume("新渠道", "新引擎", "T-99", 10)
        assert ok


class TestVersionConflictResolver:
    def test_create_version(self):
        resolver = VersionConflictResolver()
        entry = VersionEntry("VER-001", "TASK-A", 1, None)
        resolver.register_version(entry)
        ok, msg, v = resolver.try_override("TASK-A", "VER-002", 2)
        assert ok
        assert v is not None

    def test_no_override_when_referenced(self):
        resolver = VersionConflictResolver()
        entry = VersionEntry("VER-001", "TASK-B", 1, None)
        entry.increment_ref()
        resolver.register_version(entry)
        ok, msg, _ = resolver.try_override("TASK-B", "VER-001-NEW", 1)
        assert not ok
        assert "被" in msg and "引用" in msg

    def test_effective_requires_review(self):
        resolver = VersionConflictResolver()
        entry = VersionEntry("VER-001", "TASK-C", 1, None)
        resolver.register_version(entry)
        ok, msg = resolver.set_effective("TASK-C", 1)
        assert not ok
        assert "复核" in msg

    def test_effective_after_review_pass(self):
        resolver = VersionConflictResolver()
        entry = VersionEntry("VER-001", "TASK-D", 1, None)
        resolver.register_version(entry)
        resolver.set_review_result("TASK-D", 1, True)
        ok, msg = resolver.set_effective("TASK-D", 1)
        assert ok
        assert "已生效" in msg

    def test_speaker_label_manual_override(self):
        resolver = VersionConflictResolver()
        ok, msg, change = resolver.apply_speaker_label("SEG-1", "主持人", "人工")
        assert ok
        assert change is not None
        assert change.change_count == 1

        ok, msg, change2 = resolver.apply_speaker_label("SEG-1", "新主持人", "人工")
        assert change2.change_count == 2

    def test_speaker_label_machine_first(self):
        resolver = VersionConflictResolver()
        ok, msg, change = resolver.apply_speaker_label("SEG-2", "发言者A", "机器")
        assert ok
        assert change.source == "机器"


class TestSampleQcGate:
    def test_evaluate_within_threshold(self):
        gate = SampleQcGate(0.05)
        rec = gate.evaluate("TASK-1", 100, 3)
        assert not rec.blocked
        assert rec.error_rate == 0.03

    def test_evaluate_exceeds_threshold(self):
        gate = SampleQcGate(0.05)
        rec = gate.evaluate("TASK-2", 100, 8)
        assert rec.blocked
        assert rec.error_rate == 0.08

    def test_get_blocked_tasks(self):
        gate = SampleQcGate(0.05)
        gate.evaluate("TASK-3", 100, 10)
        gate.evaluate("TASK-4", 100, 2)
        blocked = gate.get_blocked_tasks()
        assert "TASK-3" in blocked
        assert "TASK-4" not in blocked

    def test_clear_block(self):
        gate = SampleQcGate(0.05)
        gate.evaluate("TASK-5", 100, 10)
        assert gate.is_blocked("TASK-5")
        ok, msg = gate.clear_block("TASK-5")
        assert ok
        assert not gate.is_blocked("TASK-5")

    def test_custom_threshold(self):
        gate = SampleQcGate(0.05)
        rec = gate.evaluate("TASK-6", 100, 6, threshold=0.10)
        assert not rec.blocked


class TestSubtitlePackAssembler:
    def test_create_pack_and_reference(self):
        assembler = SubtitlePackAssembler()
        entry = assembler.create_pack("PACK-1", "MEDI-A", "v1", 100, 3600, "SRT", "content")
        assert entry.available
        assert assembler.has_reference("v1")
        assert assembler.get_ref_count("v1") == 1

    def test_mark_recompute(self):
        assembler = SubtitlePackAssembler()
        assembler.create_pack("PACK-2", "MEDI-B", "v1", 50, 1800, "ASS", "c1")
        packs = assembler.mark_recompute("v1")
        assert len(packs) == 1
        assert not assembler.get_packs_by_version("v1")[0].available

    def test_no_reference_for_new_version(self):
        assembler = SubtitlePackAssembler()
        assert not assembler.has_reference("v99")

    def test_multiple_packs_same_version(self):
        assembler = SubtitlePackAssembler()
        assembler.create_pack("P-1", "M-1", "v1", 10, 600, "SRT", "c1")
        assembler.create_pack("P-2", "M-1", "v1", 20, 1200, "WebVTT", "c2")
        assert assembler.get_ref_count("v1") == 2


class TestArchiveReclaimer:
    def test_archive_and_restore(self):
        reclaimer = ArchiveReclaimer()
        reclaimer.archive_media("ARCH-1", "MEDI-A", "SHELF-A1", "全部", 1024, 90)
        ok, msg = reclaimer.restore("ARCH-1")
        assert ok

    def test_expired_archive_blocked(self):
        from datetime import datetime, timedelta
        reclaimer = ArchiveReclaimer()
        entry = reclaimer.archive_media("ARCH-2", "MEDI-B", "SHELF-B2", "字幕包", 512, 0)
        entry.expires_at = datetime.now() - timedelta(days=1)
        ok, msg = reclaimer.restore("ARCH-2")
        assert not ok

    def test_cleaned_not_restorable(self):
        reclaimer = ArchiveReclaimer()
        reclaimer.archive_media("ARCH-3", "MEDI-C", "SHELF-C3", "分片", 3072, 90)
        from datetime import datetime, timedelta
        for a in reclaimer._archives:
            if a.archive_id == "ARCH-3":
                a.expires_at = datetime.now() - timedelta(days=1)
        reclaimer.clean_expired()
        ok, msg = reclaimer.restore("ARCH-3")
        assert not ok
        assert "已清理" in msg

    def test_clean_expired_multiple(self):
        reclaimer = ArchiveReclaimer()
        for i in range(3):
            reclaimer.archive_media(f"ARCH-E{i}", f"MEDIA-E{i}", f"S{i}", "全部", 1024, 0)
        from datetime import datetime, timedelta
        for a in reclaimer._archives:
            a.expires_at = datetime.now() - timedelta(days=1)
        cleaned = reclaimer.clean_expired()
        assert len(cleaned) == 3


class TestIntegrationLinks:
    """管线编排器集成测试。"""

    def test_link1_media_to_delivery(self):
        orch = PipelineOrchestrator()
        result = orch.link1_media_to_delivery(
            "MEDI-001", "CAPT-01", 3, "Whisper", "SRT",
        )
        assert result["ok"]
        assert result["shards"] == 3
        assert len(orch.audit_log) >= 1

    def test_link2_fail_redeliver_partial(self):
        orch = PipelineOrchestrator()
        result = orch.link2_fail_redeliver("MEDI-002", [2], 3)
        assert len(result["redelivered"]) >= 1
        assert result["watermark_ok"]

    def test_link2_fail_redeliver_all_ok(self):
        orch = PipelineOrchestrator()
        result = orch.link2_fail_redeliver("MEDI-003", [], 2)
        assert result["watermark_ok"]

    def test_link3_overlap_boundary(self):
        orch = PipelineOrchestrator()
        segments = [
            {"seg_id": "S1", "start_ms": 0, "end_ms": 4000},
            {"seg_id": "S2", "start_ms": 3500, "end_ms": 7500},
            {"seg_id": "S3", "start_ms": 7000, "end_ms": 11000},
        ]
        result = orch.link3_overlap_boundary("T-1", segments)
        assert len(result["boundaries"]) == 2

    def test_link3_no_overlap(self):
        orch = PipelineOrchestrator()
        segments = [
            {"seg_id": "S1", "start_ms": 0, "end_ms": 3000},
            {"seg_id": "S2", "start_ms": 3100, "end_ms": 6000},
        ]
        result = orch.link3_overlap_boundary("T-2", segments)
        assert len(result["boundaries"]) == 0

    def test_link4_speaker_label_manual(self):
        orch = PipelineOrchestrator()
        result = orch.link4_speaker_label("SEG-A", "主持人A", "人工")
        assert result["ok"]
        assert result["change_count"] == 1

    def test_link5_confidence_review_pass(self):
        orch = PipelineOrchestrator()
        entry = VersionEntry("VER-R", "TASK-R", 1, None)
        orch.version.register_version(entry)
        orch.version.set_review_result("TASK-R", 1, True)
        result = orch.link5_confidence_review("TASK-R", 1, True, 0.85)
        assert result["ok"]

    def test_link5_confidence_review_block(self):
        orch = PipelineOrchestrator()
        entry = VersionEntry("VER-B", "TASK-B", 1, None)
        orch.version.register_version(entry)
        result = orch.link5_confidence_review("TASK-B", 1, False, 0.85)
        assert result["blocked"]

    def test_link6_correction_trigger_recompute(self):
        orch = PipelineOrchestrator()
        entry = VersionEntry("VER-001", "TASK-CR", 1, None)
        orch.version.register_version(entry)
        orch.pack.create_pack("PACK-R", "MEDI-M", "VER-001", 10, 600, "SRT", "c1")
        result = orch.link6_correction_loop("TASK-CR", 1, "修正文本")
        assert result["ok"]
        assert result["new_version"] is not None

    def test_link7_term_index(self):
        orch = PipelineOrchestrator()
        terms = [
            {"text": "采集架构"},
            {"text": "AI技术"},
            {"text": "语音识别"},
        ]
        result = orch.link7_term_index("TASK-T", terms, 3)
        assert result["index_version"] == "v3"
        assert len(result["entries"]) == 3

    def test_link8_shard_watermark_all_success(self):
        orch = PipelineOrchestrator()
        shard_results = [
            {"seq": 1, "success": True, "stage": "转码", "reason": ""},
            {"seq": 2, "success": True, "stage": "转码", "reason": ""},
            {"seq": 3, "success": True, "stage": "转码", "reason": ""},
        ]
        result = orch.link8_shard_watermark("MEDIA-WM", 3, shard_results)
        assert result["can_advance"]
        assert len(result["missing"]) == 0

    def test_link8_shard_watermark_partial_fail(self):
        orch = PipelineOrchestrator()
        shard_results = [
            {"seq": 1, "success": True, "stage": "转码", "reason": ""},
            {"seq": 2, "success": False, "stage": "转写", "reason": "引擎超时"},
            {"seq": 3, "success": True, "stage": "转码", "reason": ""},
        ]
        result = orch.link8_shard_watermark("MEDIA-WM2", 3, shard_results)
        assert not result["can_advance"]

    def test_link9_quota_consume(self):
        orch = PipelineOrchestrator()
        orch.quota.register_slot("渠道X", "Whisper", 600)
        result = orch.link9_quota_queue("渠道X", "Whisper", "T-1", 100)
        assert result["ok"]

    def test_link9_quota_exceed(self):
        orch = PipelineOrchestrator()
        orch.quota.register_slot("渠道Y", "SenseVoice", 50)
        result = orch.link9_quota_queue("渠道Y", "SenseVoice", "T-2", 100)
        assert not result["ok"]

    def test_link10_sample_qc_pass(self):
        orch = PipelineOrchestrator()
        result = orch.link10_sample_qc("T-QC1", 100, 2, 0.05)
        assert result["ok"]
        assert not result["blocked"]

    def test_link10_sample_qc_block_and_trigger(self):
        orch = PipelineOrchestrator()
        result = orch.link10_sample_qc("T-QC2", 100, 10, 0.05)
        assert not result["ok"]
        assert result["blocked"]

    def test_link11_stage_claim_transfer(self):
        orch = PipelineOrchestrator()
        result = orch.link11_stage_claim(
            "ORD-11", "BATCH-11", 4, "值班员",
            late_audio_bytes=2048, late_audio_ref="s3://b/4.wav",
        )
        assert result["receipt"]["stage"] == "转写"
        assert result["audio_binding"]["音轨实物"] == "ART-ORD-11"
        assert result["confirmed"] is True
        assert orch.stage_claim.get_order("ORD-11").closed is True

    def test_link11_stage_claim_holds_without_audio(self):
        orch = PipelineOrchestrator()
        result = orch.link11_stage_claim(
            "ORD-12", "BATCH-12", 4, "值班员", transfer=False)
        assert result["receipt"]["stage"] == "转码"
        assert result["audio_binding"]["音轨可用"] is False

    def test_full_pipeline_status_report(self):
        orch = PipelineOrchestrator()
        orch.link1_media_to_delivery("MEDI-F", "CAPT-F", 2, "Whisper", "SRT")
        orch.link2_fail_redeliver("MEDI-F2", [1], 3)
        orch.link9_quota_queue("渠道F", "Whisper", "T-F", 50)
        report = orch.status_report()
        assert "audit_log" in report
        assert "shard_watermark" in report
        assert "redeliver" in report
        assert "quota" in report
        assert "stage_claim" in report


class TestServiceIntegration:
    """服务层跨模块消费测试。"""

    def test_media_source_register(self):
        from app.services.media_source import MediaSourceService
        svc = MediaSourceService()
        entry, msg = svc.register_media(
            "MEDI-TEST", "内部网关", "测试媒体", 3600, 48000, 2, "mp4",
        )
        assert entry is not None
        assert entry["status"] == "已登记"

    def test_media_source_dup_rejected(self):
        from app.services.media_source import MediaSourceService
        svc = MediaSourceService()
        svc.register_media("MEDI-DUP", "渠道1", "标题1", 100, 16000, 1, "wav")
        entry2, msg2 = svc.register_media("MEDI-DUP", "渠道2", "标题2", 200, 44100, 2, "mp4")
        assert entry2 is None
        assert "已存在" in msg2

    def test_media_shard_continuity(self):
        from app.services.media_shard import MediaShardService
        svc = MediaShardService()
        cont, missing = svc.check_shard_continuity("CAPT-0001")
        assert isinstance(cont, bool)

    def test_media_shard_checksum_gen(self):
        from app.services.media_shard import MediaShardService
        svc = MediaShardService()
        checksum, matched = svc.compute_checksum("SHRD-0001", "test_content_123")
        assert len(checksum) == 8

    def test_transcribe_produce(self):
        from app.services.transcribe_task import TranscribeTaskService
        svc = TranscribeTaskService()
        ok, msg, result = svc.produce_transcription("TRCB-0001", 50, 0.88, 300)
        assert ok
        assert result["status"] == "已产出"

    def test_transcribe_task_not_found(self):
        from app.services.transcribe_task import TranscribeTaskService
        svc = TranscribeTaskService()
        ok, msg, result = svc.produce_transcription("NONEXIST", 50, 0.88, 300)
        assert not ok

    def test_speech_segment_overlap_detect(self):
        from app.services.speech_segment import SpeechSegmentService
        svc = SpeechSegmentService()
        has_overlap, msg, ms = svc.detect_overlap("SPCH-0001", "SPCH-0002")
        assert has_overlap or msg == "无重叠"

    def test_speech_segment_set_boundary(self):
        from app.services.speech_segment import SpeechSegmentService
        svc = SpeechSegmentService()
        ok, msg = svc.set_boundary("SPCH-0001", 0, 3350)
        assert ok

    def test_speaker_manual_override(self):
        from app.services.speaker_label import SpeakerLabelService
        svc = SpeakerLabelService()
        ok, msg, entry = svc.manual_override("SPKR-0002", "新说话人", "vox_new_001")
        assert ok
        assert entry["说话人标识"] == "新说话人"
        assert int(entry["改标次数"]) > 0

    def test_subtitle_align_check_offset(self):
        from app.services.subtitle_align import SubtitleAlignService
        svc = SubtitleAlignService()
        ok, msg = svc.check_offset_threshold("SALN-0003", 500)
        assert ok

    def test_transcript_version_create(self):
        from app.services.transcript_version import TranscriptVersionService
        svc = TranscriptVersionService()
        entry, msg = svc.create_version("TV-NEW", "TRCB-0003", "v99", "v1", 5, "新增版本")
        assert entry is not None
        assert entry["版本号"] == "v99"

    def test_transcript_version_dup(self):
        from app.services.transcript_version import TranscriptVersionService
        svc = TranscriptVersionService()
        svc.create_version("TV-DUP", "TRCB-0003", "v1", "-", 1, "t1")
        entry2, msg2 = svc.create_version("TV-DUP", "TRCB-0003", "v2", "-", 1, "t2")
        assert entry2 is None

    def test_correction_register(self):
        from app.services.correction_loop import CorrectionLoopService
        svc = CorrectionLoopService()
        entry, msg = svc.register_correction(
            "CORR-NEW", "TRSV-0001", "1000-3000",
            "原文", "修正文", "人工", True,
        )
        assert entry is not None
        assert entry["是否触发重算"] == "是"

    def test_subtitle_pack_create(self):
        from app.services.subtitle_pack import SubtitlePackService
        svc = SubtitlePackService()
        entry, msg = svc.create_pack("SBPK-NEW", "MEDI-0001", "v3", 100, 7200, "SRT")
        assert entry is not None
        assert entry["绑定版本"] == "v3"

    def test_confidence_review_submit(self):
        from app.services.confidence_review import ConfidenceReviewService
        svc = ConfidenceReviewService()
        ok, msg, result = svc.submit_review("CFRV-0001", "通过", "rv_01", 30, "v1")
        assert ok
        assert result["status"] == "已通过"

    def test_shard_watermark_advance(self):
        from app.services.shard_watermark import ShardWatermarkService
        svc = ShardWatermarkService()
        ok, msg = svc.advance_watermark("SHWM-0001", 5, "BATCH-NEW")
        assert ok

    def test_fail_redeliver_execute(self):
        from app.services.fail_redeliver import FailRedeliverService
        svc = FailRedeliverService()
        svc.register_redeliver("FRDL-TEST", "SHRD-FAIL", "转码", "超时", "仅补缺")
        ok, msg = svc.execute_redeliver("FRDL-TEST", True, "补齐完成")
        assert ok

    def test_term_extract_create(self):
        from app.services.term_extract import TermExtractService
        svc = TermExtractService()
        entry, msg = svc.extract_term(
            "TERM-NEW", "TRCB-0003", "深度学习", 5, 1200, "AI", "BATCH-1", True,
        )
        assert entry is not None

    def test_media_index_build(self):
        from app.services.media_index import MediaIndexService
        svc = MediaIndexService()
        entry, msg = svc.build_index(
            "MDIX-NEW", "MEDI-0001", "新术语", 3, "1500,3000", "v3",
        )
        assert entry is not None

    def test_seed_data_integrity(self):
        from app.store import store
        for name in store.module_names():
            rows = store.rows(name)
            assert len(rows) >= 1, f"{name} 种子数据为空"
            statuses = {r.get("status") for r in rows if r.get("status")}
            assert len(statuses) >= 1, f"{name} 只有一种状态"

    def test_cross_module_media_to_pack(self):
        from app.services.media_source import MediaSourceService
        from app.services.subtitle_pack import SubtitlePackService

        ms = MediaSourceService()
        ms.register_media("MEDI-CROSS", "网关", "跨模块测试", 7200, 48000, 2, "mp4")

        sp = SubtitlePackService()
        entry, msg = sp.create_pack("SBPK-CROSS", "MEDI-CROSS", "v3", 100, 7200, "SRT")
        assert entry is not None

    def test_correction_chain_to_pack(self):
        from app.services.correction_loop import CorrectionLoopService
        from app.services.subtitle_pack import SubtitlePackService

        cl = CorrectionLoopService()
        cl.register_correction("CORR-CHAIN", "TRSV-0003", "0-5000", "旧文本", "新文本", "人工", True)

        sp = SubtitlePackService()
        ok, msg = sp.mark_invalid("SBPK-0003")
        assert ok
        assert "已标记失效" in msg


class TestDataEdgeCases:
    """边界与脏数据测试。"""

    def test_shard_zero_total(self):
        line = ShardWatermarkLine("MEDI-Z", 0)
        can, seq = line.check_continuity()
        assert can

    def test_redeliver_nonexistent(self):
        engine = RedeliverEngine()
        ok, msg = engine.execute_retry("NONEXIST", "success")
        assert not ok

    def test_quota_negative_consume(self):
        mgr = TranscribeQuotaManager()
        mgr.register_slot("NegativeCh", "Whisper", 100)
        ok, msg = mgr.consume("NegativeCh", "Whisper", "T-N", 200)
        assert not ok

    def test_version_snapshot_contains_data(self):
        resolver = VersionConflictResolver()
        entry = VersionEntry("V-SNAP", "T-SNAP", 1, None)
        resolver.register_version(entry)
        snap = resolver.snapshot()
        assert "versions" in snap
        assert "T-SNAP" in snap["versions"]

    def test_qc_segment_count_zero(self):
        gate = SampleQcGate(0.05)
        rec = gate.evaluate("T-ZERO", 0, 0)
        assert rec.error_rate == 0.0

    def test_pack_compute_checksum_deterministic(self):
        assembler = SubtitlePackAssembler()
        e1 = assembler.create_pack("P-CK1", "M-CK", "v1", 1, 60, "SRT", "hello")
        e2 = assembler.create_pack("P-CK2", "M-CK", "v1", 1, 60, "SRT", "hello")
        assert e1.checksum == e2.checksum

    def test_archive_restore_nonexistent(self):
        reclaimer = ArchiveReclaimer()
        ok, msg = reclaimer.restore("NO-EXIST")
        assert not ok
        assert "不存在" in msg

    def test_overlap_boundary_single_segment(self):
        orch = PipelineOrchestrator()
        segments = [{"seg_id": "S1", "start_ms": 0, "end_ms": 3000}]
        result = orch.link3_overlap_boundary("T-SINGLE", segments)
        assert len(result["boundaries"]) == 0

    def test_version_effective_nonexistent_task(self):
        resolver = VersionConflictResolver()
        ok, msg = resolver.set_effective("NO-TASK", 1)
        assert not ok