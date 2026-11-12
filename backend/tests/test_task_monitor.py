from datetime import UTC, datetime, timedelta

import pytest

from autoeval.task_monitor import freshness, normalize


def test_local_progress_mapping_and_missing_fields():
    now = datetime.now(UTC).isoformat()
    row = normalize(
        {
            "task_name": "m/d",
            "status": "running",
            "progress_detail": {"finish_count": 2, "total_count": 5, "description": "infer"},
            "raw": {"start_time": 123},
        },
        {"task_name": "m/d"},
        "local_monitor",
        now,
    )
    assert row["finish_count"] == 2 and row["total_count"] == 5
    assert row["tool_start_time"] == 123
    assert row["process_id"] is None
    assert not freshness(row)["stale"]
    assert freshness(row, datetime.now(UTC) + timedelta(seconds=11))["stale"]


def test_harbor_flattened_mapping_and_final_state():
    row = normalize(
        {
            "task_name": "m/d",
            "finish_count": 3,
            "total_count": 4,
            "harbor": {"n_running_trials": 1},
        },
        {"task_name": "m/d"},
        "harbor_monitor",
        datetime.now(UTC).isoformat(),
        snapshot_kind="final",
    )
    assert row["finish_count"] == 3 and row["harbor"]["n_running_trials"] == 1
    assert not freshness(row, datetime.now(UTC) + timedelta(days=1))["stale"]


def test_foreign_task_is_rejected():
    with pytest.raises(ValueError):
        normalize(
            {"task_name": "other/d"},
            {"task_name": "m/d"},
            "local_monitor",
            datetime.now(UTC).isoformat(),
        )


def test_invalid_time_counts_and_filtered_archive():
    from autoeval.task_monitor import safe_task

    task = {
        "task_name": "m/d",
        "api_key": "never archive",
        "other_kwargs": {"token": "secret"},
        "progress_detail": {"finish_count": 6, "total_count": 5},
        "raw": {"start_time": 1, "api_key": "secret"},
    }
    row = normalize(task, {"task_name": "m/d"}, "local_monitor", datetime.now(UTC).isoformat())
    assert "progress_count_inconsistent" in row["diagnostics"]
    assert "secret" not in str(safe_task(task))
    with pytest.raises(ValueError):
        normalize(task, {"task_name": "m/d"}, "local_monitor", "2026-01-01T00:00:00")


def test_unknown_status_future_time_and_missing_local_values_are_explicit():
    observed = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    row = normalize(
        {"task_name": "m/d", "status": "mystery"},
        {"task_name": "m/d"},
        "local_monitor",
        observed,
    )
    assert row["tool_status"] == "mystery"
    assert row["finish_count"] is None and row["total_count"] is None
    assert row["process_id"] is None and row["tool_start_time"] is None
    assert "unknown_tool_status" in row["diagnostics"]
    assert "observed_at_in_future" in row["diagnostics"]
    assert row["stale"] is False


def test_harbor_projection_is_bounded_and_nested_archive_is_sanitized():
    from autoeval.task_monitor import safe_task

    task = {
        "task_name": "m/d",
        "status": "running",
        "finish_count": 2,
        "total_count": 4,
        "job_dir": "/srv/private/job-7",
        "task_log_path": "/srv/private/job-7/output.log",
        "cases": [
            {"case_id": "case-1", "status": "completed", "api_key": "secret"},
            {"case_id": "case-2", "state": "running", "nested": {"token": "secret"}},
        ],
        "extensions": {"model": "fixture", "api_key": "secret"},
        "harbor": {"n_total_trials": 4, "n_running_trials": 2},
        "progress_detail": {"description": "safe", "kwargs": {"api_key": "secret"}},
        "raw": {"start_time": 10, "nested": {"api_key": "secret"}},
    }
    row = normalize(
        task,
        {"task_name": "m/d"},
        "harbor_monitor",
        datetime.now(UTC).isoformat(),
    )
    assert row["harbor"]["n_total_trials"] == 4
    assert row["harbor"]["job_dir"] == "job-7"
    assert row["harbor"]["cases"] == {
        "total": 2,
        "statuses": {"completed": 1, "running": 1},
    }
    assert row["harbor"]["extensions"] == {"model": "fixture"}
    archived = safe_task(task)
    assert archived["harbor"]["cases"][0] == {"case_id": "case-1", "status": "completed"}
    assert archived["task_log_path"] == "output.log"
    assert archived["progress_detail"] == {"description": "safe"}
    assert "secret" not in str(archived)
    assert "/srv/private" not in str(archived)


@pytest.mark.asyncio
async def test_postgres_monitor_ordering_and_immutable_s3_evidence():
    import os
    import uuid
    from types import SimpleNamespace

    from sqlalchemy import delete, select, update
    from sqlalchemy.exc import DBAPIError

    from autoeval.config import Settings
    from autoeval.datasets import canonical_manifest, read_manifest, write_manifest
    from autoeval.db import create_database
    from autoeval.models import EvaluationRun
    from autoeval.task_monitor import MonitorBinding, MonitorSnapshot, save_observation

    if os.getenv("AE_INFRA_TEST") != "1":
        pytest.skip("Requires real PostgreSQL and persistent S3")
    settings = Settings(environment="test")
    engine, sessions = create_database(settings)
    identifier = uuid.uuid4()
    try:
        async with sessions() as session, session.begin():
            session.add(
                EvaluationRun(
                    id=identifier,
                    trigger_scope="test-monitor",
                    idempotency_key=str(identifier),
                    request_hash="0" * 64,
                    run_mode="exploration",
                    created_by="test",
                )
            )
            await session.flush()
            binding = MonitorBinding(run_id=identifier, attempt=1, identity={"task_name": "test/d"})
            session.add(binding)
            await session.flush()
            binding_id = binding.id
        task = {
            "task_name": "test/d",
            "status": "running",
            "progress_detail": {"finish_count": 1, "total_count": 5},
        }
        stamp = datetime.now(UTC)
        await save_observation(
            sessions, settings, binding_id, task, "local_monitor", stamp.isoformat()
        )
        await save_observation(
            sessions, settings, binding_id, task, "local_monitor", stamp.isoformat()
        )
        late = await save_observation(
            sessions,
            settings,
            binding_id,
            {**task, "status": "old"},
            "local_monitor",
            (stamp - timedelta(seconds=1)).isoformat(),
        )
        assert late["tool_status"] == "running"
        final = await save_observation(
            sessions,
            settings,
            binding_id,
            {**task, "status": "finish"},
            "local_monitor",
            (stamp + timedelta(seconds=1)).isoformat(),
            "final",
        )
        repeated = await save_observation(
            sessions,
            settings,
            binding_id,
            task,
            "local_monitor",
            (stamp + timedelta(seconds=2)).isoformat(),
        )
        assert repeated == final
        async with sessions() as session:
            rows = (
                await session.scalars(
                    select(MonitorSnapshot).where(MonitorSnapshot.binding_id == binding_id)
                )
            ).all()
            assert len(rows) == 2
            payload = read_manifest(
                settings, SimpleNamespace(object_key=rows[-1].object_key, sha256=rows[-1].sha256)
            )
            assert payload["raw"]["task_name"] == "test/d"
        with pytest.raises(DBAPIError):
            async with sessions() as session, session.begin():
                await session.execute(
                    update(MonitorSnapshot)
                    .where(MonitorSnapshot.binding_id == binding_id)
                    .values(kind="modified")
                )
        with pytest.raises(DBAPIError):
            async with sessions() as session, session.begin():
                await session.execute(
                    delete(MonitorSnapshot).where(MonitorSnapshot.binding_id == binding_id)
                )
        with pytest.raises(DBAPIError):
            async with sessions() as session, session.begin():
                session.add(
                    MonitorBinding(run_id=identifier, attempt=1, identity={"task_name": "test/d"})
                )
                await session.flush()

        from fastapi import HTTPException

        from autoeval import task_monitor
        from autoeval.unified_artifacts import UnifiedArtifact

        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(sessions=sessions, settings=settings)),
            state=SimpleNamespace(request_id="infra-monitor"),
        )
        first = await task_monitor.snapshots(identifier, request, offset=0, limit=1)
        second = await task_monitor.snapshots(identifier, request, offset=1, limit=1)
        assert len(first["data"]) == 1 and first["next_offset"] == 1
        assert len(second["data"]) == 1 and second["next_offset"] is None

        other = uuid.uuid4()
        async with sessions() as session, session.begin():
            session.add(
                EvaluationRun(
                    id=other,
                    trigger_scope="test-monitor",
                    idempotency_key=str(other),
                    request_hash="0" * 64,
                    run_mode="exploration",
                    created_by="test",
                )
            )
        with pytest.raises(HTTPException) as missing:
            await task_monitor.evidence(other, rows[-1].id, request)
        assert missing.value.status_code == 404
        with pytest.raises(HTTPException) as missing:
            await task_monitor.snapshots(uuid.uuid4(), request)
        assert missing.value.status_code == 404
        old = await task_monitor.latest(other, request)
        assert old["data"]["collection_status"] == "not_collected"
        with pytest.raises(HTTPException) as missing:
            await task_monitor.archived_log(identifier, request)
        assert missing.value.status_code == 404

        log_body = canonical_manifest(
            {"files": [{"path": "job.log", "sha256": "a" * 64, "content": "fixture"}]}
        )
        log_sha = __import__("hashlib").sha256(log_body).hexdigest()
        log_key = f"native-artifacts/{identifier}/{log_sha}.json"
        write_manifest(settings, log_key, log_body)
        async with sessions() as session, session.begin():
            session.add(
                UnifiedArtifact(
                    run_id=identifier,
                    collection_key="cli-native-v1",
                    object_key=log_key,
                    sha256=log_sha,
                    manifest={"files": [{"path": "job.log", "sha256": "a" * 64}]},
                )
            )
        current = await task_monitor.latest(identifier, request)
        assert current["data"]["log"] == {
            "endpoint": f"/api/autoeval/v1/runs/{identifier}/monitor/log",
            "sha256": "a" * 64,
        }
        archived = await task_monitor.archived_log(identifier, request)
        assert archived["data"]["content"] == "fixture"
        with pytest.raises(HTTPException) as missing:
            await task_monitor.archived_log(other, request)
        assert missing.value.status_code == 404
    finally:
        await engine.dispose()
