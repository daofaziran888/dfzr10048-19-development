"""服务与接口层测试：按阶段待办、领办开工回执、转写定位音轨的端到端链路。

当前依赖锁定里没有 httpx，测试直接调用路由函数（FastAPI 会做同样的请求模型
校验）；路由函数按模块单例取服务，这里用 monkeypatch 换成隔离实例。
"""
from __future__ import annotations

import pytest

from app.routers import redeliver_stage
from app.services.fail_redeliver import FailRedeliverService, _split_shard
from app.services.transcribe_task import TranscribeTaskService


@pytest.fixture()
def service() -> FailRedeliverService:
    svc = FailRedeliverService()
    svc.refresh_engine()
    return svc


@pytest.fixture()
def routes(service, monkeypatch):
    """把路由层的服务单例替换成隔离实例。"""
    monkeypatch.setattr(
        redeliver_stage, "get_fail_redeliver_service", lambda: service)
    return redeliver_stage


@pytest.fixture()
def linked(service, monkeypatch):
    """让转写任务的回执定位与当前隔离服务实例共用同一份工单。"""
    from app.services import fail_redeliver

    monkeypatch.setattr(fail_redeliver, "get_fail_redeliver_service", lambda: service)
    return service


class TestShardParsing:
    def test_split_standard_shard(self):
        assert _split_shard("MEDI-0001-S013") == ("MEDI-0001", 13)

    def test_split_without_seq(self):
        assert _split_shard("MEDI-0001") == ("MEDI-0001", 0)

    def test_split_bad_seq(self):
        assert _split_shard("MEDI-0001-SXX") == ("MEDI-0001-SXX", 0)


class TestTodoRoutes:
    def test_todos_shape(self, routes):
        payload = routes.list_todos(stage=None, page=1, size=20)
        assert set(payload) >= {"items", "total", "page", "size"}
        assert payload["total"] >= 1

    def test_todos_sorted_by_shard_no(self, routes):
        payload = routes.list_todos(stage=None, page=1, size=200)
        shard_nos = [item["shard_no"] for item in payload["items"]]
        assert shard_nos == sorted(shard_nos)

    def test_todos_filtered_by_stage(self, routes):
        payload = routes.list_todos(stage="转码", page=1, size=200)
        assert payload["total"] >= 1
        assert all(item["origin_stage"] == "转码" for item in payload["items"])

    def test_todos_pagination(self, routes):
        first = routes.list_todos(stage=None, page=1, size=1)
        second = routes.list_todos(stage=None, page=2, size=1)
        assert len(first["items"]) == 1
        assert len(second["items"]) == 1
        assert first["items"][0]["shard_no"] <= second["items"][0]["shard_no"]

    def test_summary(self, routes):
        payload = routes.todo_summary()
        assert set(payload) >= {"转码", "转写", "合计", "执行中"}


class TestSeedScenario:
    """种子数据里的 4 号片场景：无音轨先转码，迟到音轨后转办。"""

    def test_4th_shard_starts_without_audio(self, service):
        ok, _, detail = service.todo_detail("FRDL-0004")
        assert ok
        assert detail["shard_no"] == 4
        assert detail["can_claim_transcode"] is True
        assert detail["can_claim_transcribe"] is False

    def test_claim_transcode_then_late_audio_then_transfer(self, service, linked):
        ok, msg, payload = service.claim("FRDL-0004", "值班员")
        assert ok
        assert payload["receipt"]["stage"] == "转码"
        assert payload["receipt"]["audio_artifact_id"] is None

        # 重投未开工，原作业音轨迟到落盘。
        service.register_artifact(
            "BATCH-20260901", 4, artifact_id="ART-LATE-004",
            job_id="TRCD-OLD-004", audio_bytes=786432,
            audio_ref="s3://shard-audio/20260901/004.wav",
        )

        # 异批次同号不能代跑：BATCH-20260902 的 4 号音轨不该被选中。
        usable = service.engine.usable_artifact("BATCH-20260901", 4)
        assert usable is not None
        assert usable.artifact_id == "ART-LATE-004"
        other = service.engine.get_artifact("ART-20260902-S004")
        assert other is not None and other.batch_id != "BATCH-20260901"

        ok, msg, payload = service.transfer("FRDL-0004", "值班员")
        assert ok
        receipt = payload["receipt"]
        assert receipt["stage"] == "转写"
        assert receipt["audio_artifact_id"] == "ART-LATE-004"
        assert receipt["audio_source_job"] == "TRCD-OLD-004"

        # 转写任务凭回执能定位实际投入音轨。
        ok, msg, detail = TranscribeTaskService().locate_audio_receipt(receipt["receipt_id"])
        assert ok
        assert detail["audio_binding"]["音轨实物"] == "ART-LATE-004"
        assert detail["audio_binding"]["音轨字节"] == 786432

    def test_close_and_reclaim(self, service):
        service.claim("FRDL-0004", "值班员")
        service.register_artifact(
            "BATCH-20260901", 4, artifact_id="ART-LATE-004",
            job_id="TRCD-OLD-004", audio_bytes=786432,
            audio_ref="s3://shard-audio/20260901/004.wav",
        )
        ok, msg, payload = service.close_and_reclaim("FRDL-0004", "值班员")
        assert ok
        assert payload["closed_order"] == "FRDL-0004"
        assert payload["new_order"]["order_id"] == "FRDL-0004-T1"
        assert payload["receipt"]["stage"] == "转写"
        assert payload["receipt"]["audio_artifact_id"] == "ART-LATE-004"


class TestFlagOnlyReceipt:
    """S002 的转码回执只有成功标记、没有录音。"""

    def test_claim_transcribe_falls_back_to_transcode(self, service):
        ok, msg, payload = service.claim("FRDL-0005", "值班员")
        assert ok
        assert payload["receipt"]["stage"] == "转码"
        assert "仅成功标记" in payload["receipt"]["note"]

    def test_failure_detail_keeps_reason(self, service):
        ok, _, detail = service.todo_detail("FRDL-0005")
        assert ok
        assert detail["fail_reason"] == "转码回执成功但未落录音"
        assert detail["can_claim_transcode"] is True
        assert "继续领取转码" in detail["advice"]


class TestClaimRoutes:
    def test_claim_route(self, routes):
        result = routes.claim_order("FRDL-0006", redeliver_stage.ClaimRequest(operator="值班员"))
        assert result.ok is True
        assert result.entry["receipt"]["stage"] == "转写"
        assert result.entry["audio_binding"]["音轨可用"] is True

    def test_claim_route_unknown_order(self, routes):
        result = routes.claim_order("NOPE", redeliver_stage.ClaimRequest(operator="值班员"))
        assert result.ok is False
        assert "不存在" in result.message

    def test_claim_route_requires_operator(self, routes):
        result = routes.claim_order("FRDL-0006", redeliver_stage.ClaimRequest(operator=""))
        assert result.ok is False
        assert "领办人" in result.message

    def test_artifacts_route(self, routes):
        result = routes.register_artifact(redeliver_stage.ArtifactRequest(
            batch_id="BATCH-20260901", shard_no=4,
            artifact_id="ART-ROUTE-004", job_id="TRCD-ROUTE",
            audio_bytes=1024, audio_ref="s3://x/4.wav",
        ))
        assert result.ok is True
        assert result.entry["usable"] is True

    def test_receipt_route_unknown_404(self, routes):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            routes.receipt_detail("NOPE")
        assert exc.value.status_code == 404

    def test_order_detail_route(self, routes):
        payload = routes.order_detail("FRDL-0006")
        assert payload["ok"] is True
        assert payload["detail"]["can_claim_transcribe"] is True

    def test_order_detail_unknown_404(self, routes):
        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            routes.order_detail("NOPE")
        assert exc.value.status_code == 404


class TestModuleListStageFilter:
    def test_module_list_accepts_stage_filter(self, service):
        rows, total = service.list_entries(stage="转码", page=1, size=200)
        assert total >= 1
        assert all("转码" in row["失败阶段"] for row in rows)

    def test_other_modules_ignore_stage(self):
        from app.services.media_source import MediaSourceService

        rows, total = MediaSourceService().list_entries(
            stage="转码", page=1, size=200)
        assert total == len(rows)


class TestGenericEntrySync:
    """通用登记/动作入口与领办工单保持同一口径。"""

    def test_create_entry_registers_claimable_order(self, service):
        entry, missing = service.create_entry({
            "重投编号": "FRDL-NEW", "关联分片": "BATCH-9-S007",
            "失败阶段": "转码失败", "失败原因": "编码器崩溃",
            "重投策略": "仅补缺",
        })
        assert entry is not None and not missing
        order = service.engine.get_order("FRDL-NEW")
        assert order is not None
        assert order.batch_id == "BATCH-9"
        assert order.shard_no == 7
        assert order.origin_stage == "转码"

        items, _ = service.list_todos("转码", 1, 200)
        assert "FRDL-NEW" in [item["order_id"] for item in items]

    def test_run_action_closes_order(self, service):
        entry, _ = service.create_entry({
            "重投编号": "FRDL-ACT", "关联分片": "BATCH-9-S008",
            "失败阶段": "转写失败", "失败原因": "超时", "重投策略": "仅补缺",
        })
        ok, _, _ = service.claim("FRDL-ACT", "值班员")
        assert ok
        entry, message = service.run_action(int(entry["id"]), "确认补齐")
        assert entry["status"] == "已补齐"
        order = service.engine.get_order("FRDL-ACT")
        assert order.closed is True
        assert order.active_stage is None

    def test_execute_redeliver_closes_order(self, service):
        entry, _ = service.register_redeliver(
            "FRDL-EXEC", "BATCH-9-S009", "转码", "超时", "仅补缺")
        service.claim("FRDL-EXEC", "值班员")
        ok, _ = service.execute_redeliver("FRDL-EXEC", True, "补齐完成")
        assert ok
        assert service.engine.get_order("FRDL-EXEC").closed is True


class TestTranscribeReceiptIntegration:
    def test_produce_requires_bound_receipt(self):
        svc = TranscribeTaskService()
        ok, msg, _ = svc.produce_transcription("TRCB-0001", 10, 0.9, 60, receipt_id="NOPE")
        assert not ok

    def test_produce_records_audio_binding(self, service, linked):
        ok, _, payload = service.claim("FRDL-0006", "值班员")
        assert ok
        receipt_id = payload["receipt"]["receipt_id"]
        svc = TranscribeTaskService()
        ok, msg, entry = svc.produce_transcription(
            "TRCB-0001", 30, 0.91, 120, receipt_id=receipt_id)
        assert ok
        assert entry["开工回执"] == receipt_id
        assert entry["投入音轨"] == payload["receipt"]["audio_artifact_id"]
