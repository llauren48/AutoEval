"""Durable multi-dataset execution with capacity leases and isolated child jobs."""

import asyncio
import copy
import uuid
from datetime import UTC, datetime

from fastapi import HTTPException
from sqlalchemy import DateTime, ForeignKey, String, UniqueConstraint, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from temporalio import activity

from autoeval.config import get_settings
from autoeval.contracts import JobRef
from autoeval.datasets import canonical_manifest, write_manifest
from autoeval.db import create_database
from autoeval.models import Base, EvaluationRun, Outbox, Record, RunEvent, RunSnapshot
from autoeval.planning import (
    PlanningArtifact,
    PlanningJob,
    artifact,
    current_preflight,
    resolve_inputs,
)
from autoeval.planning_transport import transport
from autoeval.secrets import FileSecretStore
from autoeval.standard_inputs import digest, leaves
from autoeval.targets import TargetBinding, validate_url
from autoeval.unified_outputs import archive_output

TERMINAL = {"succeeded", "failed", "cancelled"}


class DatasetExecution(Record, Base):
    __tablename__ = "dataset_execution"
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evaluation_run.id"))
    task_id: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(24), default="pending")
    payload: Mapped[dict] = mapped_column(JSONB)
    summary: Mapped[dict | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (UniqueConstraint("run_id", "task_id"),)


class CapacityLease(Record, Base):
    __tablename__ = "planning_capacity_lease"
    owner_id: Mapped[uuid.UUID] = mapped_column(unique=True)
    node_id: Mapped[str] = mapped_column(String(128), index=True)
    demand: Mapped[dict] = mapped_column(JSONB)
    released: Mapped[bool] = mapped_column(default=False)


def demand_for(item):
    count = item["concurrency"]
    params = item["compilation"]["config"]["params"]
    if item["dataset"] == "DeepSWE":
        return {
            "cpu": 0.5 + count * max(2, float(params.get("override_cpus") or 2)),
            "mem": 256 + count * max(8192, int(params.get("override_memory_mb") or 8192)),
            "disk": count * max(20, float(params.get("override_storage_mb") or 20480) / 1024),
            "networks": count,
        }
    return {"cpu": 1 + count * 0.02, "mem": 1024 + count * 16, "disk": 0, "networks": 0}


def fits(demand, used, resource):
    limits = {
        "cpu": resource["cpu_cores"] * 0.8,
        "mem": resource["mem_mb"] * 0.8,
        "disk": resource["disk_gb"] * 0.8,
        "networks": resource.get("network_available") or 0,
    }
    return all(demand[k] + used.get(k, 0) <= limits[k] for k in limits)


async def acquire(session, owner, resource, demand):
    node = resource["node_id"]
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": "capacity:" + node}
    )
    existing = await session.scalar(select(CapacityLease).where(CapacityLease.owner_id == owner))
    if existing:
        return not existing.released
    leases = (
        await session.scalars(
            select(CapacityLease).where(
                CapacityLease.node_id == node,
                CapacityLease.released.is_(False),
            )
        )
    ).all()
    used = {k: sum(row.demand.get(k, 0) for row in leases) for k in demand}
    if not fits(demand, used, resource):
        return False
    session.add(CapacityLease(owner_id=owner, node_id=node, demand=demand))
    return True


async def release(session, owner):
    lease = await session.scalar(select(CapacityLease).where(CapacityLease.owner_id == owner))
    if lease:
        lease.released = True


async def create_run(session, settings, body, execution_plan_id, key):
    if not execution_plan_id:
        body = body.model_copy(update={"recommend": False, "throughput_profile_id": None})
    request_hash = digest(
        {
            "request": body.model_dump(mode="json"),
            "execution_plan_id": str(execution_plan_id) if execution_plan_id else None,
        }
    )
    old = await session.scalar(
        select(EvaluationRun).where(
            EvaluationRun.trigger_scope == "local-operator:orchestration",
            EvaluationRun.idempotency_key == key,
        )
    )
    if old:
        if old.request_hash != request_hash:
            raise HTTPException(409, "Idempotency content conflict")
        return {"run_id": str(old.id), "state": old.state}
    if settings.evaluation_origin != "real":
        raise HTTPException(
            409, "General model execution requires an explicit real execution origin"
        )
    inputs = await resolve_inputs(session, settings, body)
    fresh = await current_preflight(session, inputs)
    if not fresh or not fresh.payload.get("execution_ready"):
        raise HTTPException(409, "Fresh resource preflight required")
    report = None
    if execution_plan_id:
        execution = await session.get(PlanningArtifact, execution_plan_id)
        if not execution or execution.kind != "execution":
            raise HTTPException(404, "Execution plan missing")
        original = await session.get(PlanningJob, execution.payload["source_plan_id"])
        if {k: v for k, v in original.payload.items() if k != "intent_key"} != inputs:
            raise HTTPException(409, "Selected inputs changed; regenerate plan")
        if execution.payload["resource_sha256"] != fresh.sha256:
            raise HTTPException(409, "Resource snapshot changed; regenerate plan")
        inputs = copy.deepcopy(execution.payload["inputs"])
        report = execution.payload["report"]
    else:
        # Direct execution freezes the supplied settings without optimizer rewrites.
        inputs = copy.deepcopy(inputs)
        for item in inputs["tasks"]:
            if int(item["compilation"]["config"]["params"].get("max_num_workers", 1)) != 1:
                raise HTTPException(
                    409, "Platform owns dataset parallelism; configure max_num_workers=1"
                )
    for item in inputs["tasks"]:
        if item["dataset"] not in fresh.payload["supported_datasets"] or not fits(
            demand_for(item), {}, fresh.payload
        ):
            raise HTTPException(
                409, "Selected dataset/concurrency exceeds execution resource capability"
            )
        compiled = item["compilation"]
        if any(
            leaves(compiled["config"]).get(k) != v
            for k, v in compiled.get("locked_fields", {}).items()
        ):
            raise HTTPException(409, "Selected sample range conflicts with standard locks")
    target = await session.get(TargetBinding, body.target_id)
    validate_url(target.base_url, settings.allowed_model_hosts)
    run = EvaluationRun(
        trigger_scope="local-operator:orchestration",
        idempotency_key=key,
        request_hash=request_hash,
        run_mode="exploration",
        created_by="local-operator",
    )
    session.add(run)
    await session.flush()
    frozen = {
        "schema_version": "planned-run.v1",
        "inputs": inputs,
        "estimate": report,
        "execution_mode": "optimized" if execution_plan_id else "direct",
        "resource": fresh.payload,
        "resource_sha256": fresh.sha256,
        "target_id": str(target.id),
        "resource_id": inputs["resource_id"],
        "resource_kind": inputs["resource_kind"],
        "remote_model": target.remote_model,
        "execution_origin": settings.evaluation_origin,
    }
    session.add(
        RunSnapshot(
            run_id=run.id,
            schema_version="planned-run.v1",
            canonical_body=frozen,
            sha256=digest(frozen),
        )
    )
    from autoeval.task_monitor import MonitorBinding

    for index, item in enumerate(inputs["tasks"], 1):
        child_id = uuid.uuid4()
        session.add(
            DatasetExecution(id=child_id, run_id=run.id, task_id=item["task_id"], payload=item)
        )
        session.add(
            MonitorBinding(
                run_id=run.id,
                attempt=index,
                identity={
                    "task_name": str(child_id),
                    "dataset": item["dataset"],
                    "child_id": str(child_id),
                },
            )
        )
    session.add(
        Outbox(
            run_id=run.id,
            kind="START_RUN",
            dedup_key="planned:" + str(run.id),
            payload={"workflow_kind": "planned_evaluation"},
        )
    )
    return {"run_id": str(run.id), "state": "accepted"}


async def update_child(sessions, child_id, **values):
    async with sessions() as session, session.begin():
        child = await session.get(DatasetExecution, child_id, with_for_update=True)
        for key, value in values.items():
            setattr(child, key, value)


async def advance_run(session, run, state, details=None):
    if run.state != state:
        run.state = state
        run.projection_version += 1
        session.add(
            RunEvent(run_id=run.id, seq=run.projection_version, type=state, body=details or {})
        )


async def execute(sessions, settings, run_id, heartbeat=lambda: None):
    async with sessions() as session:
        existing_run = await session.get(EvaluationRun, run_id)
        if existing_run and existing_run.state in {"completed", "failed", "cancelled"}:
            return
        snapshot = await session.scalar(select(RunSnapshot).where(RunSnapshot.run_id == run_id))
        if not snapshot or snapshot.sha256 != digest(snapshot.canonical_body):
            raise ValueError("Frozen snapshot integrity mismatch")
        frozen = snapshot.canonical_body
        inputs = frozen["inputs"]
        adapter = await transport(session, settings, inputs)
        target = await session.get(TargetBinding, inputs["target_id"])
        secret = FileSecretStore(settings.secret_store_path).get(target.credential_ref)
    while True:
        heartbeat()
        async with sessions() as session:
            run = await session.get(EvaluationRun, run_id)
            cancelled = run.cancel_requested_at is not None
            children = list(
                await session.scalars(
                    select(DatasetExecution).where(DatasetExecution.run_id == run_id)
                )
            )
        by_id = {c.task_id: c for c in children}
        children = [by_id[t["task_id"]] for t in inputs["tasks"]]
        for child in children:
            if child.state == "pending" and cancelled:
                await update_child(
                    sessions, child.id, state="cancelled", finished_at=datetime.now(UTC)
                )
                child.state = "cancelled"
                child.finished_at = datetime.now(UTC)
            if child.state not in {"submitting", "running", "unresolved"}:
                continue
            job = JobRef(external_job_id=str(child.id), submission_key=str(child.id))
            observation = (
                await adapter.cancel(job, "cancel:" + str(child.id))
                if cancelled
                else await adapter.status(job)
            )
            if observation.state in {"unknown", "cleaned"}:
                await update_child(
                    sessions,
                    child.id,
                    state="unresolved",
                    summary={
                        "reason": "Remote state uncertain; lease retained, no automatic resubmission"
                    },
                )
                child.state = "unresolved"
                continue
            if observation.state not in TERMINAL:
                continue
            summary = None
            state = observation.state
            if state == "succeeded":
                result = await adapter.collect(job)
                unified = result["result"]["unified_result"]
                async with sessions() as session, session.begin():
                    await artifact(
                        session,
                        settings,
                        "raw_result",
                        {"run_id": str(run_id), "task_id": child.task_id, "result": unified},
                    )
                try:
                    aliases = {s["tool_case_id"]: s["sample_id"] for s in child.payload["samples"]}
                    allowed_ids = {s["sample_id"] for s in child.payload["samples"]}
                    params = child.payload["compilation"]["config"]["params"]
                    result_aliases = {
                        "GPQA": "GPQA_diamond",
                        "MMMU-Pro": "mmmu_pro",
                        "DeepSWE": "harbor_deepswe-v1.1",
                    }
                    allowed_datasets = {
                        child.payload["dataset"],
                        result_aliases[child.payload["dataset"]],
                        params.get("dataset_abbr"),
                        *(params.get("datasets") or []),
                    }
                    for case in unified.get("cases", []):
                        if case.get("dataset") not in allowed_datasets:
                            raise ValueError("Unexpected result dataset")
                        case["dataset"] = child.payload["dataset"]
                        if case.get("case_id") in aliases:
                            case["case_id"] = aliases[case["case_id"]]
                        elif case.get("case_id") not in allowed_ids:
                            raise ValueError("Unexpected result sample")
                    raw = canonical_manifest(unified)
                    summary = await archive_output(
                        sessions,
                        settings,
                        run_id,
                        child.task_id,
                        raw,
                        {
                            child.payload["dataset"]: [
                                s["sample_id"] for s in child.payload["samples"]
                            ]
                        },
                        child.payload["dataset"],
                    )
                except ValueError:
                    summary = {
                        "validity": "failed",
                        "reason": "Result identity or evidence validation failed; raw result preserved",
                    }
                if result["result"].get("tool_exit_code", 0) != 0:
                    summary = {
                        **summary,
                        "validity": "partial",
                        "tool_exit_code": result["result"]["tool_exit_code"],
                    }
                if summary["validity"] != "complete":
                    state = "failed"
            await update_child(
                sessions, child.id, state=state, summary=summary, finished_at=datetime.now(UTC)
            )
            child.state = state
            child.summary = summary
            child.finished_at = datetime.now(UTC)
            # Release capacity only after the transport confirms cleanup. Harbor cancellation
            # can leave daemon-owned trial containers; retain ownership instead of overbooking.
            cleanup = await adapter.cleanup(job)
            if cleanup.get("cleaned"):
                async with sessions() as session, session.begin():
                    await release(session, child.id)
            else:
                child.summary = {**(child.summary or {}), "cleanup_pending": True}
                await update_child(sessions, child.id, summary=child.summary)
        if all(c.state in TERMINAL for c in children):
            await finish(sessions, settings, run_id, frozen, children, cancelled)
            return
        active = sum(c.state in {"submitting", "running", "unresolved"} for c in children)
        for child in children:
            if cancelled or active >= inputs["dataset_concurrency"]:
                break
            if child.state != "pending":
                continue
            async with sessions() as session, session.begin():
                if not await acquire(
                    session, child.id, frozen["resource"], demand_for(child.payload)
                ):
                    break
                stored = await session.get(DatasetExecution, child.id, with_for_update=True)
                stored.state = "submitting"
                stored.started_at = datetime.now(UTC)
            # The durable submitting intent precedes network I/O. A lost reply is reconciled
            # by lookup, never by issuing a new model-generating submission.
            try:
                await adapter.submit(
                    {
                        "kind": "adaptor",
                        "compilation": child.payload["compilation"],
                        "tool": child.payload["tool"],
                        "credential": secret,
                    },
                    str(child.id),
                )
                await update_child(sessions, child.id, state="running")
            except Exception:  # noqa: BLE001 -- isolate third-party errors without leaking credentials
                await update_child(sessions, child.id, state="unresolved")
            active += 1
        async with sessions() as session, session.begin():
            run = await session.get(EvaluationRun, run_id, with_for_update=True)
            await advance_run(
                session, run, "cancelling" if cancelled else "running" if active else "queued"
            )
        await asyncio.sleep(2)


async def finish(sessions, settings, run_id, frozen, children, cancelled):
    async with sessions() as session, session.begin():
        run = await session.get(EvaluationRun, run_id, with_for_update=True)
        result = {
            "schema_version": "planned-report.v1",
            "run_id": str(run_id),
            "estimate": frozen.get("estimate"),
            "datasets": [child_view(c) for c in children],
            "actual_duration_sec": (
                max(c.finished_at for c in children if c.finished_at)
                - min(c.started_at for c in children if c.started_at)
            ).total_seconds()
            if any(c.started_at for c in children)
            else None,
            "gate_status": "not_applicable",
            "execution_origin": frozen["execution_origin"],
        }
        key = f"planned-reports/{run_id}/{digest(result)}.json"
        await asyncio.to_thread(write_manifest, settings, key, canonical_manifest(result))
        session.add(
            PlanningArtifact(
                kind="run_report", sha256=digest(result), object_key=key, payload=result
            )
        )
        state = (
            "cancelled"
            if cancelled
            else "completed"
            if all(c.state == "succeeded" for c in children)
            else "failed"
        )
        await advance_run(session, run, state, {"report_object_key": key})


def child_view(child):
    return {
        "id": str(child.id),
        "task_id": child.task_id,
        "dataset": child.payload["dataset"],
        "state": child.state,
        "summary": child.summary,
        "started_at": child.started_at.isoformat() if child.started_at else None,
        "finished_at": child.finished_at.isoformat() if child.finished_at else None,
        "actual_duration_sec": (child.finished_at - child.started_at).total_seconds()
        if child.started_at and child.finished_at
        else None,
    }


@activity.defn
async def execute_planned_evaluation(run_id: str):
    settings = get_settings()
    engine, sessions = create_database(settings)
    try:
        # Prevent duplicate activity deliveries from controlling the same run concurrently.
        async with engine.connect() as connection:
            key = "execute:" + run_id
            owned = await connection.scalar(
                text("SELECT pg_try_advisory_lock(hashtext(:key))"), {"key": key}
            )
            if not owned:
                raise RuntimeError("Run is already controlled")
            try:
                await execute(
                    sessions, settings, uuid.UUID(run_id), lambda: activity.heartbeat(run_id)
                )
            finally:
                await connection.execute(
                    text("SELECT pg_advisory_unlock(hashtext(:key))"), {"key": key}
                )
    finally:
        await engine.dispose()
