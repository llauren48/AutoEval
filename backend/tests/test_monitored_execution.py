import hashlib
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from autoeval import monitored_execution, task_monitor
from autoeval.cleanup import ExecutionCleanup
from autoeval.config import Settings
from autoeval.datasets import canonical_manifest
from autoeval.evaluations import EvaluationReport
from autoeval.models import EvaluationRun, RunEvent, RunSnapshot
from autoeval.registry import Asset, AssetVersion
from autoeval.runs import ExecutionLease
from autoeval.secrets import FileSecretStore
from autoeval.targets import TargetBinding
from autoeval.task_monitor import MonitorBinding, normalize


class FakeGateway:
    submit_state: ClassVar[dict] = {"state": "succeeded"}
    status_state: ClassVar[dict] = {"state": "succeeded"}
    final_error: ClassVar[bool] = False
    instances: ClassVar[list] = []

    def __init__(self, settings, frozen, run_id):
        self.env = frozen["monitor_environment"]
        self.identity = frozen["monitor_identity"]
        self.control = SimpleNamespace(connection=object())
        self.calls = []
        self.payloads = []
        self.native = {
            "score_source": "ais_bench_results_json",
            "total_score": 1.0,
            "cases": [{"score": 1}],
        }
        FakeGateway.instances.append(self)

    def operation(self, action, payload=None):
        self.calls.append(action)
        if action == "submit":
            self.payloads.append(payload)
            return dict(self.submit_state)
        if action == "status":
            return dict(self.status_state)
        if action == "collect":
            return {"state": "collected"}
        if action == "bundle":
            return {"state": "bundled"}
        if action == "cleanup":
            return {"state": "cleaned"}
        if action == "cancel":
            return {"state": "cancelled"}
        raise AssertionError(action)

    def read(self, relative):
        if relative == "monitor-final.json":
            if self.final_error:
                raise ValueError("final unavailable")
            return canonical_manifest(
                {
                    "tasks": [
                        {
                            "task_name": self.identity["task_name"],
                            "status": "finish",
                            "process_id": 123,
                            "progress_detail": {"finish_count": 1, "total_count": 1},
                        }
                    ]
                }
            )
        if relative == "unified_result.json":
            return canonical_manifest(self.native)
        if relative == "raw-artifacts.json":
            content = "fixture log"
            return canonical_manifest(
                {
                    "files": [
                        {
                            "path": "job.log",
                            "sha256": hashlib.sha256(content.encode()).hexdigest(),
                            "size": len(content),
                            "content": content,
                        }
                    ]
                }
            )
        raise AssertionError(relative)


@pytest.fixture
async def harness(monkeypatch, tmp_path):
    settings = Settings(environment="test", evaluation_origin="real", secret_store_path=tmp_path)
    engine = create_async_engine(settings.database_url.get_secret_value())
    connection = await engine.connect()
    transaction = await connection.begin()
    sessions = async_sessionmaker(
        connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    objects = {}

    def read_manifest(_settings, row):
        raw = objects.get(row.object_key)
        if raw is None or hashlib.sha256(raw).hexdigest() != row.sha256:
            raise ValueError("Evidence missing or corrupt")
        return json.loads(raw)

    def write_manifest(_settings, key, raw):
        objects[key] = raw

    activity_engine = SimpleNamespace(dispose=AsyncMock())
    monkeypatch.setattr(monitored_execution, "get_settings", lambda: settings)
    monkeypatch.setattr(
        monitored_execution, "create_database", lambda _settings: (activity_engine, sessions)
    )
    monkeypatch.setattr(monitored_execution, "read_manifest", read_manifest)
    monkeypatch.setattr(monitored_execution, "write_manifest", write_manifest)
    monkeypatch.setattr(task_monitor, "read_manifest", read_manifest)
    monkeypatch.setattr(task_monitor, "write_manifest", write_manifest)
    monkeypatch.setattr(monitored_execution, "Gateway", FakeGateway)
    FakeGateway.instances = []
    FakeGateway.submit_state = {"state": "succeeded"}
    FakeGateway.status_state = {"state": "succeeded"}
    FakeGateway.final_error = False

    async def summary(*_args):
        return {
            "validity": "complete",
            "attempt_count": 1,
            "observed_cases": 1,
            "failed_cases": 0,
            "missing_cases": 0,
        }

    monkeypatch.setattr(monitored_execution, "archive_output", summary)
    try:
        yield SimpleNamespace(settings=settings, sessions=sessions, objects=objects)
    finally:
        await transaction.rollback()
        await connection.close()
        await engine.dispose()


async def make_run(harness, *, budget=1, samples=1, cancelled=False, intent=False, observed=False):
    credential_ref = FileSecretStore(harness.settings.secret_store_path).put("fixture-only")
    async with harness.sessions() as session, session.begin():
        asset = Asset(asset_key="model-" + uuid.uuid4().hex, kind="model", name="Fixture")
        session.add(asset)
        await session.flush()
        version = AssetVersion(asset_id=asset.id, version="v1", metadata_json={})
        session.add(version)
        await session.flush()
        target = TargetBinding(
            model_version_id=version.id,
            base_url="https://api.deepseek.com",
            remote_model="deepseek-flash",
            credential_ref=credential_ref,
        )
        session.add(target)
        await session.flush()
        run = EvaluationRun(
            trigger_scope="local-operator:evaluation",
            idempotency_key=uuid.uuid4().hex,
            request_hash="a" * 64,
            run_mode="exploration",
            created_by="monitor-test",
            state="running",
            cancel_requested_at=datetime.now(UTC) if cancelled else None,
        )
        session.add(run)
        await session.flush()
        identity = {
            "container": "fixture-container",
            "container_id": "c" * 64,
            "port": 18480,
            "work_dir": "/fixture/jobs/" + str(run.id) + "/output",
            "task_name": "ae-test/GPQA_diamond",
        }
        frozen = {
            "execution_origin": "real",
            "execution_path": "aisbench_cli_monitor",
            "remote_model": target.remote_model,
            "base_url": target.base_url,
            "target_id": str(target.id),
            "parameters": {"output": 64, "concurrency": 1, "temperature": 0, "top_p": 1},
            "samples": [{"sample_id": str(index)} for index in range(samples)],
            "model_abbr": "ae-test",
            "max_model_requests": budget,
            "monitor_environment": {"root": "/fixture", "pins": {}},
            "monitor_identity": identity,
        }
        session.add(
            RunSnapshot(
                run_id=run.id,
                schema_version="gpqa-cli.v1",
                canonical_body=frozen,
                sha256=hashlib.sha256(canonical_manifest(frozen)).hexdigest(),
            )
        )
        binding = MonitorBinding(run_id=run.id, attempt=1, identity=identity)
        if observed:
            binding.latest = normalize(
                {
                    "task_name": identity["task_name"],
                    "status": "running",
                    "process_id": 123,
                    "progress_detail": {"finish_count": 0, "total_count": samples},
                },
                identity,
                "local_monitor",
                datetime.now(UTC).isoformat(),
            )
        session.add(binding)
        session.add(ExecutionLease(container_id=identity["container_id"], run_id=run.id))
        session.add(RunEvent(run_id=run.id, seq=1, type="accepted", body={}))
        if intent:
            run.projection_version += 1
            session.add(
                RunEvent(
                    run_id=run.id,
                    seq=run.projection_version,
                    type="monitor_submit_intent",
                    body={"operation_key": str(run.id)},
                )
            )
    return run, binding


async def test_budget_is_rejected_before_model_secret_or_submission(harness, monkeypatch):
    run, _ = await make_run(harness, budget=1, samples=2)

    def forbidden_secret(*_args):
        raise AssertionError("Model credential must not be read before budget validation")

    monkeypatch.setattr(FileSecretStore, "get", forbidden_secret)
    with pytest.raises(ValueError, match="exceeds the model budget"):
        await monitored_execution.execute_monitored_gpqa(str(run.id))
    assert FakeGateway.instances[0].payloads == []


async def test_lost_submission_is_queried_and_never_resent(harness):
    run, _ = await make_run(harness, intent=True)
    gateway = FakeGateway(None, {"monitor_environment": {}, "monitor_identity": {}}, str(run.id))
    gateway.status_state = {"state": "unknown"}
    with pytest.raises(ValueError, match="uncertain"):
        await monitored_execution.submit_once(harness.sessions, run.id, gateway, {})
    assert gateway.calls == ["status"]
    assert gateway.payloads == []


async def test_success_final_archival_and_terminal_replay_are_idempotent(harness):
    run, binding = await make_run(harness)
    result = await monitored_execution.execute_monitored_gpqa(str(run.id))
    assert result["accuracy"] == 100
    gateway = FakeGateway.instances[-1]
    assert gateway.payloads[0]["config"]["params"]["retry"] == 0
    assert gateway.payloads[0]["budget"]["max_model_requests"] == 1
    assert gateway.calls.count("submit") == 1
    async with harness.sessions() as session:
        current = await session.get(MonitorBinding, binding.id)
        assert current.latest["snapshot_kind"] == "final"
        report = await session.scalar(
            select(EvaluationReport).where(EvaluationReport.run_id == run.id)
        )
        assert report.validity == "complete"
        assert (
            await session.scalar(select(ExecutionCleanup).where(ExecutionCleanup.run_id == run.id))
            is not None
        )
    assert await monitored_execution.execute_monitored_gpqa(str(run.id)) == result
    assert gateway.calls.count("submit") == 1


async def test_confirmed_cancellation_archives_without_scoring_or_report(harness, monkeypatch):
    run, binding = await make_run(harness, cancelled=True, intent=True, observed=True)
    FakeGateway.status_state = {"state": "cancelled"}

    async def forbidden_scoring(*_args):
        raise AssertionError("Cancelled run must not be scored")

    monkeypatch.setattr(monitored_execution, "archive_output", forbidden_scoring)
    assert await monitored_execution.execute_monitored_gpqa(str(run.id)) == {"state": "cancelled"}
    gateway = FakeGateway.instances[-1]
    assert gateway.payloads == []
    assert "bundle" in gateway.calls and "cleanup" in gateway.calls
    async with harness.sessions() as session:
        current = await session.get(MonitorBinding, binding.id)
        assert current.latest["snapshot_kind"] == "final"
        assert (
            await session.scalar(select(EvaluationReport).where(EvaluationReport.run_id == run.id))
            is None
        )
        assert (
            await session.scalar(select(ExecutionLease).where(ExecutionLease.run_id == run.id))
            is None
        )
        assert (await session.get(EvaluationRun, run.id)).state == "cancelled"


async def test_missing_final_uses_last_observed_and_overbudget_report_is_invalid(harness):
    run, binding = await make_run(harness, observed=True)
    FakeGateway.final_error = True

    async def overbudget(*_args):
        return {
            "validity": "complete",
            "attempt_count": 2,
            "observed_cases": 1,
            "failed_cases": 0,
            "missing_cases": 0,
        }

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(monitored_execution, "archive_output", overbudget)
    try:
        result = await monitored_execution.execute_monitored_gpqa(str(run.id))
    finally:
        monkeypatch.undo()
    assert result["accuracy"] is None
    async with harness.sessions() as session:
        current = await session.get(MonitorBinding, binding.id)
        assert current.latest["snapshot_kind"] == "last_observed"
        report = await session.scalar(
            select(EvaluationReport).where(EvaluationReport.run_id == run.id)
        )
        assert report.validity == "failed"
        assert report.summary["budget_exceeded"] is True
