"""Real SQL/S3/Temporal, explicitly simulated model and scoring boundary. No paid calls."""

import asyncio
import json
import os
import uuid
from pathlib import Path

import httpx
import pytest
from sqlalchemy import select
from temporalio.client import Client
from temporalio.worker import Worker

from autoeval import analysis_activity, evaluation_activity
from autoeval.analysis import ReportAnalysis
from autoeval.analysis_activity import execute_report_analysis
from autoeval.api import create_app
from autoeval.config import get_settings
from autoeval.datasets import DatasetManifest
from autoeval.dispatcher import deliver_once
from autoeval.registry import Asset, AssetVersion
from autoeval.resources import ResourceBinding
from autoeval.targets import TargetBinding
from autoeval.workflows import GpqaExplorationWorkflow, ReportAnalysisWorkflow

pytestmark = pytest.mark.skipif(
    not os.environ.get("AE_GPQA_PIPELINE_TEST"),
    reason="Imported native data and live services required",
)


async def test_submit_deduplicate_partial_report_and_cancel(monkeypatch):
    settings = get_settings().model_copy(
        update={
            "evaluation_origin": "simulation",
            "analysis_target_id": None,
            "analysis_resource_id": None,
            "gpqa_workflow_queue": "test-wf-" + uuid.uuid4().hex,
            "gpqa_activity_queue": "test-io-" + uuid.uuid4().hex,
            "analysis_activity_queue": "test-analysis-" + uuid.uuid4().hex,
            "analysis_max_output_tokens": 64,
        }
    )
    settings.analysis_workflow_queue = settings.gpqa_workflow_queue
    app = create_app(settings)
    monkeypatch.setattr(evaluation_activity, "get_settings", lambda: settings)
    lock = json.loads((Path(__file__).parents[1] / "tool-locks/aisbench.json").read_text())
    calls = []

    def fake_invoke(settings, container_id, payload):
        if payload["operation"] == "prompt_gpqa_0shot":
            return {"prompt": "Simulated native prompt", "tool_revision": lock["revision"]}
        if payload["operation"] == "score_gpqa_0shot":
            if scenario == "scoring_failure":
                raise ValueError("Simulated scorer failure")
            return {"native_result": {"accuracy": 50}, "simulated": True}
        calls.append(payload["operation"])
        success = len(calls) == 1 or scenario == "scoring_failure"
        return {
            "state": "succeeded" if success else "failed",
            "response": "A" if success else None,
            "finish_reason": "stop" if success else None,
            "error_code": None if success else "UPSTREAM_ERROR",
            "usage": {"input_tokens": 1, "output_tokens": 1},
        }

    monkeypatch.setattr(evaluation_activity, "invoke", fake_invoke)
    monkeypatch.setattr(evaluation_activity, "get_settings", lambda: settings)
    monkeypatch.setattr(evaluation_activity.FileSecretStore, "get", lambda *args: "fixture-only")
    analysis_calls = []

    def fake_analysis(settings, container_id, payload):
        analysis_calls.append(payload)
        if scenario == "cancel":
            raise OSError("Simulated uncertain analysis transport")
        invalid = scenario == "partial" and len(analysis_calls) == 1
        return {
            "state": "succeeded",
            "response": json.dumps(
                {
                    "summary": "Evidence-bound analysis",
                    "evidence_ids": ["foreign" if invalid else "run.summary"],
                    "facts": [{"statement": "Report archived", "evidence_ids": ["run.summary"]}],
                    "inferences": [],
                    "recommended_actions": [],
                }
            ),
            "finish_reason": "stop",
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }

    monkeypatch.setattr(analysis_activity, "invoke", fake_analysis)
    monkeypatch.setattr(analysis_activity, "get_settings", lambda: settings)
    monkeypatch.setattr(analysis_activity.FileSecretStore, "get", lambda *args: "fixture-only")
    client = await Client.connect(settings.temporal_address)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api,
        Worker(
            client,
            task_queue=settings.gpqa_workflow_queue,
            workflows=[GpqaExplorationWorkflow, ReportAnalysisWorkflow],
        ),
        Worker(
            client,
            task_queue=settings.gpqa_activity_queue,
            activities=[evaluation_activity.execute_gpqa_exploration],
        ),
        Worker(
            client,
            task_queue=settings.analysis_activity_queue,
            activities=[execute_report_analysis],
        ),
    ):
        async with app.state.sessions() as session:
            full = await session.scalar(
                select(DatasetManifest)
                .join(AssetVersion, AssetVersion.id == DatasetManifest.dataset_version_id)
                .join(Asset, Asset.id == AssetVersion.asset_id)
                .where(Asset.asset_key == "gpqa-diamond", DatasetManifest.scope == "full")
            )
            assert full
            version = await session.get(AssetVersion, full.dataset_version_id)
            target = await session.scalar(
                select(TargetBinding).where(TargetBinding.remote_model == "deepseek-flash").limit(1)
            )
            resource = await session.scalar(select(ResourceBinding).limit(1))
            settings.analysis_target_id = target.id
            settings.analysis_resource_id = resource.id
        from autoeval.datasets import read_manifest

        members = read_manifest(settings, full)["samples"][:2]
        prefix = "/api/autoeval/v1"
        key = "pipeline-" + uuid.uuid4().hex
        lite = await api.post(
            prefix + "/dataset-manifests",
            json={
                "dataset_key": "gpqa-diamond",
                "name": "GPQA Diamond",
                "version": version.version,
                "manifest_key": key,
                "scope": "lite",
                "parent_id": str(full.id),
                "samples": members,
            },
        )
        assert lite.status_code == 201
        profile = await api.post(
            prefix + "/profiles",
            json={
                "profile_key": key,
                "version": "v1",
                "dataset_name": "GPQA",
                "full_manifest_id": str(full.id),
                "lite_manifest_id": lite.json()["data"]["id"],
                "tool": {
                    "family": "aisbench",
                    "source_url": lock["source_url"],
                    "revision": lock["revision"],
                    "scorer_reference": "ais_bench/benchmark/datasets/gpqa.py",
                    "scorer_sha256": lock["files"]["ais_bench/benchmark/datasets/gpqa.py"],
                    "parallelism_owner": "tool",
                },
            },
        )
        assert profile.status_code == 201
        config = await api.post(
            prefix + "/test-configs",
            json={
                "config_key": key,
                "display_name": key,
                "version_label": "v1",
                "expected_version": 0,
                "items": {
                    "GPQA": {
                        "output": 32,
                        "concurrency": 1,
                        "temperature": 0,
                        "top_p": 1,
                        "scope": "Lite",
                    }
                },
                "profile_bindings": {"GPQA": profile.json()["data"]["id"]},
            },
        )
        assert config.status_code == 201
        body = {
            "config_version_id": config.json()["data"]["id"],
            "target_id": str(target.id),
            "resource_id": str(resource.id),
            "run_mode": "exploration",
            "max_model_requests": 2,
        }
        assert (
            await api.post(
                prefix + "/runs",
                json={**body, "max_model_requests": 1},
                headers={"Idempotency-Key": uuid.uuid4().hex},
            )
        ).status_code == 422
        for scenario in ("partial", "cancel", "scoring_failure"):
            cancelled = scenario == "cancel"
            headers = {"Idempotency-Key": uuid.uuid4().hex}
            response = await api.post(prefix + "/runs", json=body, headers=headers)
            assert response.status_code == 202, response.text
            run_id = response.json()["data"]["run_id"]
            assert (await api.post(prefix + "/runs", json=body, headers=headers)).json()["data"][
                "run_id"
            ] == run_id
            if cancelled:
                assert (await api.post(f"{prefix}/runs/{run_id}/cancel")).status_code == 202
            for _ in range(20):
                if not await deliver_once(app.state.sessions, client):
                    break
            for _ in range(100):
                detail = (await api.get(f"{prefix}/runs/{run_id}")).json()["data"]
                if detail["state"] in {"completed", "failed", "cancelled"}:
                    break
                await asyncio.sleep(0.2)
            assert detail["state"] == ("cancelled" if cancelled else "completed"), detail
            assert detail["report"]["validity"] == (
                "failed_summary"
                if cancelled
                else "invalid"
                if scenario == "scoring_failure"
                else "partial"
            )
            if scenario == "scoring_failure":
                assert detail["report"]["summary"]["accuracy"] is None
                assert detail["report"]["summary"]["scoring_status"] == "failed"
            assert detail["report"]["summary"]["formal_score_eligible"] is False
            assert detail["gate_status"] == "not_applicable"
            assert len(calls) == (4 if scenario == "scoring_failure" else 2)
            assert await evaluation_activity.execute_gpqa_exploration(run_id) == {
                "state": detail["state"]
            }
            assert len(calls) == (4 if scenario == "scoring_failure" else 2)

            start = await api.post(f"{prefix}/reports/{detail['report']['id']}/analysis:start")
            assert start.status_code == 404
            async with app.state.sessions() as session:
                job = await session.scalar(
                    select(ReportAnalysis).where(
                        ReportAnalysis.report_id == uuid.UUID(detail["report"]["id"])
                    )
                )
            assert job is None, "Simulated execution reports must not enqueue analysis"
            assert analysis_calls == []
        assert len(analysis_calls) == 0
