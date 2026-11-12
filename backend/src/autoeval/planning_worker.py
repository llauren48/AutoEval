"""Durable planning worker: python -m autoeval.planning_worker."""

import asyncio
import tempfile
from datetime import UTC, datetime

from sqlalchemy import select, text

from autoeval.case_history import CaseExecutionRecord
from autoeval.config import get_settings
from autoeval.db import create_database
from autoeval.models import RunSnapshot
from autoeval.planning import PlanningArtifact, PlanningJob, artifact, current_preflight
from autoeval.planning_engine import calculate
from autoeval.planning_transport import transport
from autoeval.secrets import FileSecretStore
from autoeval.targets import TargetBinding, validate_url


async def set_job(sessions, identifier, **values):
    async with sessions() as session, session.begin():
        row = await session.get(PlanningJob, identifier, with_for_update=True)
        if values.get("state") == "completed" and row.cancel_requested:
            values["state"] = "cancelled"
        for key, value in values.items():
            setattr(row, key, value)
        row.updated_at = datetime.now(UTC)


async def execute_job(sessions, settings, identifier):
    async with sessions() as session:
        row = await session.get(PlanningJob, identifier)
        inputs, previous, kind, cancelled = row.payload, row.state, row.kind, row.cancel_requested
        if previous not in {"pending", "running"} and not (previous == "unresolved" and cancelled):
            return
        if cancelled and previous == "pending":
            await set_job(sessions, identifier, state="cancelled")
            return
        adapter = await transport(session, settings, inputs)
    await set_job(sessions, identifier, state="running")
    if kind == "preflight":
        checks = [
            {key: item[key] for key in ("task_id", "dataset", "compilation", "tool")}
            for item in inputs["tasks"]
        ]
        snapshot = await adapter.preflight(checks)
        async with sessions() as session, session.begin():
            saved = await artifact(session, settings, "resource", {**inputs, **snapshot})
            result = {"resource_snapshot_id": str(saved.id), **saved.payload}
        await set_job(sessions, identifier, state="completed", result=result)
        return
    if kind == "plan":
        async with sessions() as session:
            curve = await session.get(PlanningArtifact, inputs["throughput_profile_id"])
            snapshot = await current_preflight(session, inputs)
            observations = (
                await session.execute(
                    select(CaseExecutionRecord, RunSnapshot)
                    .join(
                        RunSnapshot,
                        RunSnapshot.run_id == CaseExecutionRecord.run_id,
                    )
                    .where(
                        CaseExecutionRecord.dataset_key.in_(
                            [t["dataset"] for t in inputs["tasks"]]
                        ),
                        CaseExecutionRecord.finished_at.is_not(None),
                        CaseExecutionRecord.state != "started",
                    )
                )
            ).all()
            history = [
                {
                    "run_id": str(c.run_id),
                    "dataset": c.dataset_key,
                    "sample_id": c.sample_id,
                    "attempt": c.attempt_no,
                    "state": c.state,
                    "metadata": c.tool_metadata or {},
                    "finished_at": c.finished_at.isoformat(),
                }
                for c, s in observations
                if s.canonical_body.get("execution_origin") == "real"
            ]
            resource = snapshot.payload if snapshot else None
            resource_sha = snapshot.sha256 if snapshot else None
            profile = curve.payload["curve"]
        # Each calculation owns a fresh snapshot rebuilt from authoritative archived evidence.
        # No shared mutable SQLite file or multiple-writer race exists.
        with tempfile.TemporaryDirectory(prefix="autoeval-plan-") as directory:
            report = await asyncio.to_thread(
                calculate,
                inputs,
                profile,
                resource,
                history,
                directory,
                recommend=inputs.get("recommend", True),
            )
        async with sessions() as session, session.begin():
            saved = await artifact(session, settings, "plan", {"inputs": inputs, "report": report})
        await set_job(
            sessions,
            identifier,
            state="completed",
            result={
                "artifact_id": str(saved.id),
                "report": report,
                "resource_sha256": resource_sha,
            },
        )
        return
    # Remote probe submission is never automatically repeated after an uncertain response.
    async with sessions() as session:
        snapshot = await current_preflight(session, inputs, require_selection=False)
        if not snapshot or not snapshot.payload.get("planner_ready"):
            raise ValueError("Fresh resource preflight with pinned Planner required")
        if max(inputs["concurrency"]) > snapshot.payload["max_probe_concurrency"]:
            raise ValueError("Probe exceeds configured service concurrency")
        target = await session.get(TargetBinding, inputs["target_id"])
        validate_url(target.base_url, settings.allowed_model_hosts)
        spec = {
            "kind": "planner_probe",
            "credential": FileSecretStore(settings.secret_store_path).get(target.credential_ref),
            "options": {
                "base_url": target.base_url,
                "model": target.remote_model,
                "concurrency_levels": inputs["concurrency"],
                **{k: inputs[k] for k in ("requests_per_level", "prompt_tokens", "max_tokens")},
            },
        }
    key = str(identifier)
    if previous == "pending":
        job = await adapter.submit(spec, key)
    else:
        from autoeval.contracts import JobRef

        job = JobRef(external_job_id=key, submission_key=key)
    while True:
        async with sessions() as session:
            cancelled = (await session.get(PlanningJob, identifier)).cancel_requested
        observation = (
            await adapter.cancel(job, "cancel:" + key) if cancelled else await adapter.status(job)
        )
        if observation.state in {"succeeded", "failed", "cancelled"}:
            break
        if observation.state in {"unknown", "cleaned"}:
            await set_job(
                sessions,
                identifier,
                state="unresolved",
                result={
                    "reason": "Remote submission uncertain; no generation request was repeated"
                },
            )
            return
        await asyncio.sleep(2)
    if observation.state == "succeeded" and not cancelled:
        result = await adapter.collect(job)
        async with sessions() as session, session.begin():
            saved = await artifact(
                session,
                settings,
                "throughput",
                {
                    **{k: inputs[k] for k in ("target_id", "resource_id", "resource_kind")},
                    "curve": result["result"]["curve"],
                    "source": "probe",
                    "measured_at": datetime.now(UTC).isoformat(),
                },
            )
        await set_job(
            sessions, identifier, state="completed", result={"throughput_profile_id": str(saved.id)}
        )
    else:
        await set_job(
            sessions,
            identifier,
            state="cancelled" if cancelled else "failed",
            result={
                "reason": "probe_cancelled"
                if cancelled
                else observation.reason or "remote_probe_failed"
            },
        )
    await adapter.cleanup(job)


async def serve():
    settings = get_settings()
    engine, sessions = create_database(settings)
    try:
        while True:
            async with sessions() as session:
                ids = list(
                    await session.scalars(
                        select(PlanningJob.id)
                        .where(
                            (
                                PlanningJob.state.in_(["pending", "running"])
                                | (
                                    (PlanningJob.state == "unresolved")
                                    & PlanningJob.cancel_requested.is_(True)
                                )
                            ),
                        )
                        .order_by(PlanningJob.created_at)
                        .limit(20)
                    )
                )
            for identifier in ids:
                # Session-level lock survives commits and is released if this worker dies.
                async with engine.connect() as connection:
                    lock = "planning:" + str(identifier)
                    owned = await connection.scalar(
                        text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": lock}
                    )
                    if not owned:
                        continue
                    try:
                        await execute_job(sessions, settings, identifier)
                    except Exception:  # noqa: BLE001 -- isolate third-party errors without leaking credentials
                        async with sessions() as session:
                            failed = await session.get(PlanningJob, identifier)
                            uncertain = failed.kind == "probe" and failed.state == "running"
                        await set_job(
                            sessions,
                            identifier,
                            state="unresolved" if uncertain else "failed",
                            result={
                                "reason": "Planning operation failed; check resource preflight and pinned inputs"
                            },
                        )
                    finally:
                        await connection.execute(
                            text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": lock}
                        )
            await asyncio.sleep(settings.poll_seconds)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(serve())
