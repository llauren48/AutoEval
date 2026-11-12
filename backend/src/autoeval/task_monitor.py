"""Versioned local/Harbor monitor projection, independent from scoring."""

import hashlib
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import ForeignKey, String, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from autoeval.datasets import canonical_manifest, read_manifest, write_manifest
from autoeval.models import Base, EvaluationRun, Record
from autoeval.unified_artifacts import UnifiedArtifact

KNOWN_TOOL_STATES = {
    "waiting",
    "pending",
    "running",
    "inferencing",
    "evaluating",
    "postprocessing",
    "finish",
    "finished",
    "completed",
    "succeeded",
    "failed",
    "error",
    "cancelled",
}
TRIAL_FIELDS = [
    "n_total_trials",
    "n_running_trials",
    "n_completed_trials",
    "n_errored_trials",
    "n_pending_trials",
    "n_cancelled_trials",
    "n_retries",
]
CASE_FIELDS = {"case_id", "trial_name", "status", "state", "score"}
EXTENSION_FIELDS = {"dataset", "model", "status", "created_at", "updated_at"}


def safe_scalar(value):
    return value if value is None or type(value) in {str, int, float, bool} else None


def safe_cases(value):
    if not isinstance(value, list):
        return []
    return [
        {key: safe_scalar(case.get(key)) for key in CASE_FIELDS if key in case}
        for case in value[:1000]
        if isinstance(case, dict)
    ]


def safe_extensions(value):
    if not isinstance(value, dict):
        return {}
    return {
        key: safe_scalar(value[key])
        for key in EXTENSION_FIELDS
        if key in value and (not isinstance(value[key], str) or len(value[key]) <= 512)
    }


def safe_job_dir(value):
    if not isinstance(value, str) or "\\" in value or ".." in value.split("/"):
        return None
    name = value.rstrip("/").rsplit("/", 1)[-1]
    return name[:256] if name else None


def safe_harbor(value):
    value = value if isinstance(value, dict) else {}
    return {
        **{key: value.get(key) if type(value.get(key)) is int else None for key in TRIAL_FIELDS},
        "job_dir": safe_job_dir(value.get("job_dir")),
        "cases": safe_cases(value.get("cases")),
        "extensions": safe_extensions(value.get("extensions")),
    }


class MonitorBinding(Record, Base):
    __tablename__ = "run_monitor_binding"
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evaluation_run.id"))
    attempt: Mapped[int]
    identity: Mapped[dict] = mapped_column(JSONB)
    latest: Mapped[dict | None] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("run_id", "attempt"),)


class MonitorSnapshot(Record, Base):
    __tablename__ = "run_monitor_snapshot"
    binding_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("run_monitor_binding.id"))
    sha256: Mapped[str] = mapped_column(String(64))
    object_key: Mapped[str] = mapped_column(String(256))
    kind: Mapped[str] = mapped_column(String(24))
    __table_args__ = (UniqueConstraint("binding_id", "sha256"),)


def normalize(task, identity, source, observed_at, *, snapshot_kind="live"):
    stamp = datetime.fromisoformat(observed_at)
    if stamp.utcoffset() is None:
        raise ValueError("Timezone required")
    observed_at = stamp.astimezone(UTC).isoformat()
    if source not in {"local_monitor", "harbor_monitor"} or snapshot_kind not in {
        "live",
        "final",
        "last_observed",
    }:
        raise ValueError("Unknown monitor contract")
    if task.get("task_name") != identity["task_name"]:
        raise ValueError("Monitor task does not belong to run")
    detail = task.get("progress_detail") or {} if source == "local_monitor" else task

    def count(name):
        value = detail.get(name)
        return value if type(value) is int and value >= 0 else None

    finished, total = count("finish_count"), count("total_count")
    issues = []
    if finished is not None and total is not None and finished > total:
        issues.append("progress_count_inconsistent")
    status = task.get("status")
    if status is not None and status not in KNOWN_TOOL_STATES:
        issues.append("unknown_tool_status")
    if stamp > datetime.now(UTC):
        issues.append("observed_at_in_future")
    harbor_source = dict(task.get("harbor") or {})
    for key in ["job_dir", "cases", "extensions"]:
        if key in task:
            harbor_source[key] = task[key]
    harbor = safe_harbor(harbor_source)
    if source == "harbor_monitor":
        statuses = {}
        for case in harbor["cases"]:
            label = case.get("status") or case.get("state") or "unknown"
            if isinstance(label, str):
                statuses[label] = statuses.get(label, 0) + 1
        harbor["cases"] = {"total": len(harbor["cases"]), "statuses": statuses}
    # No inference from status strings, container PID, or elapsed wall time.
    return {
        "source": source,
        "task_name": identity["task_name"],
        "process_id": task.get("process_id")
        if type(task.get("process_id")) is int and task["process_id"] > 0
        else None,
        "tool_status": status,
        "finish_count": finished,
        "total_count": total,
        "progress_description": detail.get("description")
        if source == "local_monitor"
        else task.get("progress_description"),
        "progress_text": task.get("progress") if source == "local_monitor" else None,
        "tool_start_time": task.get("raw", {}).get("start_time")
        if source == "local_monitor"
        else task.get("start_time"),
        "time_cost": task.get("time_cost"),
        "observed_at": observed_at,
        "stale": snapshot_kind != "final" and (datetime.now(UTC) - stamp).total_seconds() > 10,
        "snapshot_kind": snapshot_kind,
        "collection_status": "available",
        "harbor": harbor if source == "harbor_monitor" else None,
        "diagnostics": issues,
        "log_available": bool(task.get("task_log_path")),
        "job_dir_available": bool(task.get("job_dir") or harbor_source.get("job_dir")),
    }


def safe_task(task):
    """Archive a bounded monitor contract, never credentials or arbitrary server paths."""
    result = {
        key: safe_scalar(task[key])
        for key in [
            "task_name",
            "process_id",
            "status",
            "progress",
            "finish_count",
            "total_count",
            "progress_description",
            "time_cost",
            "start_time",
        ]
        if key in task
    }
    detail = task.get("progress_detail") or {}
    if isinstance(detail, dict):
        result["progress_detail"] = {
            key: safe_scalar(detail[key])
            for key in ["finish_count", "total_count", "description"]
            if key in detail
        }
    result["raw"] = {
        key: safe_scalar(task.get("raw", {})[key])
        for key in ["start_time", "end_time", "finish_count", "total_count", "status"]
        if isinstance(task.get("raw"), dict) and key in task["raw"]
    }
    harbor_source = dict(task.get("harbor") or {})
    for key in ["job_dir", "cases", "extensions"]:
        if key in task:
            harbor_source[key] = task[key]
    result["harbor"] = safe_harbor(harbor_source)
    if "task_log_path" in task:
        result["task_log_path"] = safe_job_dir(task["task_log_path"])
    if "job_dir" in task:
        result["job_dir"] = safe_job_dir(task["job_dir"])
    return result


def freshness(projection, now=None):
    result = dict(projection)
    stamp = datetime.fromisoformat(result["observed_at"])
    if stamp.utcoffset() is None:
        raise ValueError("Monitor observation requires timezone")
    result["stale"] = (
        result.get("snapshot_kind") != "final"
        and ((now or datetime.now(UTC)) - stamp).total_seconds() > 10
    )
    return result


async def save_observation(sessions, settings, binding_id, task, source, observed_at, kind="live"):
    async with sessions() as session, session.begin():
        binding = await session.scalar(
            select(MonitorBinding).where(MonitorBinding.id == binding_id).with_for_update()
        )
        if binding is None:
            raise ValueError("Monitor binding missing")
        projection = normalize(task, binding.identity, source, observed_at, snapshot_kind=kind)
        if binding.latest and (
            binding.latest["snapshot_kind"] == "final"
            or datetime.fromisoformat(projection["observed_at"])
            < datetime.fromisoformat(binding.latest["observed_at"])
        ):
            return binding.latest
        # Persist changes/terminal evidence, not a full JSON row every poll.
        changed = not binding.latest or any(
            projection[k] != binding.latest.get(k)
            for k in [
                "tool_status",
                "finish_count",
                "total_count",
                "snapshot_kind",
                "harbor",
                "diagnostics",
            ]
        )
        if changed:
            raw = canonical_manifest(
                {
                    "projection": projection,
                    "raw": safe_task(task),
                    "identity": {"task_name": binding.identity["task_name"]},
                }
            )
            sha = hashlib.sha256(raw).hexdigest()
            key = f"monitor/{binding.run_id}/{binding.id}/{sha}.json"
            import asyncio
            from types import SimpleNamespace

            await asyncio.to_thread(write_manifest, settings, key, raw)
            await asyncio.to_thread(
                read_manifest, settings, SimpleNamespace(object_key=key, sha256=sha)
            )
            session.add(
                MonitorSnapshot(binding_id=binding.id, object_key=key, sha256=sha, kind=kind)
            )
        binding.latest = projection
        return projection


router = APIRouter(prefix="/api/autoeval/v1/runs", tags=["run monitoring"])


@router.get("/{run_id}/monitor")
async def latest(run_id: uuid.UUID, request: Request):
    async with request.app.state.sessions() as session:
        if await session.get(EvaluationRun, run_id) is None:
            raise HTTPException(404, "Run not found")
        binding = await session.scalar(
            select(MonitorBinding)
            .where(MonitorBinding.run_id == run_id)
            .order_by(MonitorBinding.attempt.desc())
            .limit(1)
        )
        projection = (
            {**freshness(binding.latest), "run_id": str(run_id), "attempt": binding.attempt}
            if binding and binding.latest
            else {"collection_status": "not_collected", "stale": False}
        )
        artifact = await session.scalar(
            select(UnifiedArtifact).where(
                UnifiedArtifact.run_id == run_id,
                UnifiedArtifact.collection_key == "cli-native-v1",
            )
        )
        if artifact:
            logs = [
                row for row in artifact.manifest.get("files", []) if row.get("path") == "job.log"
            ]
            if len(logs) == 1:
                projection["log"] = {
                    "endpoint": f"/api/autoeval/v1/runs/{run_id}/monitor/log",
                    "sha256": logs[0]["sha256"],
                }
        projection.setdefault("log", None)
        return {"data": projection, "request_id": request.state.request_id}


@router.get("/{run_id}/monitor/snapshots")
async def snapshots(run_id: uuid.UUID, request: Request, offset: int = 0, limit: int = 20):
    if offset < 0 or not 1 <= limit <= 100:
        raise HTTPException(422, "Invalid pagination")
    async with request.app.state.sessions() as session:
        if await session.get(EvaluationRun, run_id) is None:
            raise HTTPException(404, "Run not found")
        rows = (
            await session.scalars(
                select(MonitorSnapshot)
                .join(MonitorBinding)
                .where(MonitorBinding.run_id == run_id)
                .order_by(MonitorSnapshot.created_at, MonitorSnapshot.id)
                .offset(offset)
                .limit(limit + 1)
            )
        ).all()
        return {
            "data": [
                {
                    "id": str(r.id),
                    "kind": r.kind,
                    "sha256": r.sha256,
                    "created_at": r.created_at.isoformat(),
                }
                for r in rows[:limit]
            ],
            "next_offset": offset + limit if len(rows) > limit else None,
            "request_id": request.state.request_id,
        }


@router.get("/{run_id}/monitor/snapshots/{snapshot_id}")
async def evidence(run_id: uuid.UUID, snapshot_id: uuid.UUID, request: Request):
    import asyncio

    async with request.app.state.sessions() as session:
        row = await session.scalar(
            select(MonitorSnapshot)
            .join(MonitorBinding)
            .where(MonitorBinding.run_id == run_id, MonitorSnapshot.id == snapshot_id)
        )
        if not row:
            raise HTTPException(404, "Monitor snapshot not found")
        try:
            data = await asyncio.to_thread(read_manifest, request.app.state.settings, row)
        except Exception as exc:
            raise HTTPException(503, "Monitor evidence unavailable") from exc
        return {"data": data, "request_id": request.state.request_id}


@router.get("/{run_id}/monitor/log")
async def archived_log(run_id: uuid.UUID, request: Request):
    import asyncio

    from autoeval.unified_artifacts import UnifiedArtifact

    async with request.app.state.sessions() as session:
        artifact = await session.scalar(
            select(UnifiedArtifact).where(
                UnifiedArtifact.run_id == run_id, UnifiedArtifact.collection_key == "cli-native-v1"
            )
        )
        if artifact is None:
            raise HTTPException(404, "Run log has not been archived")
        try:
            bundle = await asyncio.to_thread(read_manifest, request.app.state.settings, artifact)
        except Exception as exc:
            raise HTTPException(503, "Archived log failed integrity verification") from exc
        files = [row for row in bundle["files"] if row["path"] == "job.log"]
        if len(files) != 1:
            raise HTTPException(404, "Archived run log unavailable")
        return {
            "data": {
                "content": files[0]["content"],
                "sha256": files[0]["sha256"],
                "source": "archived_job_log",
            },
            "request_id": request.state.request_id,
        }
