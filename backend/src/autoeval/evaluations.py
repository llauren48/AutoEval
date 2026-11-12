"""GPQA Lite exploration submission; Release validation remains unavailable."""

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Literal

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import ForeignKey, String, or_, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Mapped, mapped_column

from autoeval.configuration import TestConfigItem, TestConfigVersion
from autoeval.datasets import DatasetManifest, canonical_manifest, read_manifest
from autoeval.execution_transport import frozen_ssh_resource
from autoeval.models import Base, EvaluationRun, Outbox, Record, RunEvent, RunSnapshot
from autoeval.profiles import ConfigDatasetBinding, EvaluationProfile
from autoeval.registry import AssetVersion
from autoeval.resources import ResourceBinding
from autoeval.runs import ExecutionLease
from autoeval.ssh_resources import SSHResourceBinding
from autoeval.targets import TargetBinding


class EvaluationReport(Record, Base):
    __tablename__ = "evaluation_report"
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evaluation_run.id", ondelete="RESTRICT"), unique=True
    )
    validity: Mapped[str] = mapped_column(String(24))
    snapshot_sha256: Mapped[str] = mapped_column(String(64))
    summary: Mapped[dict] = mapped_column(JSONB)
    object_key: Mapped[str] = mapped_column(String(256))
    sha256: Mapped[str] = mapped_column(String(64))
    execution_origin: Mapped[str] = mapped_column(String(16), server_default="unverified")


class SubmitEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config_version_id: uuid.UUID
    target_id: uuid.UUID
    resource_id: uuid.UUID
    run_mode: Literal["exploration"]
    max_model_requests: int = Field(ge=1, le=5)
    resource_kind: Literal["docker_local", "customer_ssh"] = "docker_local"


router = APIRouter(prefix="/api/autoeval/v1/runs", tags=["evaluation runs"])


@router.get("")
async def list_runs(request: Request, offset: int = 0, limit: int = 50):
    if offset < 0 or not 1 <= limit <= 200:
        raise HTTPException(422, "Invalid pagination")
    try:
        async with request.app.state.sessions() as session:
            rows = (
                await session.execute(
                    select(EvaluationRun, RunSnapshot, EvaluationReport.id)
                    .join(RunSnapshot, RunSnapshot.run_id == EvaluationRun.id)
                    .outerjoin(EvaluationReport, EvaluationReport.run_id == EvaluationRun.id)
                    .where(EvaluationRun.trigger_scope == "local-operator:evaluation")
                    .where(
                        or_(
                            RunSnapshot.canonical_body["execution_origin"].astext
                            == request.app.state.settings.evaluation_origin,
                            EvaluationReport.execution_origin
                            == request.app.state.settings.evaluation_origin,
                        )
                    )
                    .order_by(EvaluationRun.created_at.desc(), EvaluationRun.id.desc())
                    .offset(offset)
                    .limit(limit + 1)
                )
            ).all()
            return {
                "data": [
                    {
                        "id": str(r.id),
                        "state": r.state,
                        "report_id": str(report_id) if report_id else None,
                        "created_at": r.created_at.isoformat(),
                        "remote_model": snapshot.canonical_body.get("remote_model"),
                        "resource_kind": snapshot.canonical_body.get(
                            "resource_kind", "docker_local"
                        ),
                    }
                    for r, snapshot, report_id in rows[:limit]
                ],
                "next_offset": offset + limit if len(rows) > limit else None,
                "request_id": request.state.request_id,
            }
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Run database unavailable") from exc


@router.post("", status_code=202)
async def submit(
    body: SubmitEvaluation,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=256),
):
    # Preserve existing Docker idempotency hashes when the new selector is omitted.
    request_body = body.model_dump(
        mode="json", exclude={"resource_kind"} if body.resource_kind == "docker_local" else set()
    )
    digest = hashlib.sha256(canonical_manifest(request_body)).hexdigest()
    try:
        async with request.app.state.sessions() as session, session.begin():
            old = await session.scalar(
                select(EvaluationRun).where(
                    EvaluationRun.trigger_scope == "local-operator:evaluation",
                    EvaluationRun.idempotency_key == idempotency_key,
                )
            )
            if old:
                if old.request_hash != digest:
                    raise HTTPException(409, "Idempotency content conflict")
                return {
                    "data": {"run_id": str(old.id), "state": old.state},
                    "request_id": request.state.request_id,
                }
            config = await session.get(TestConfigVersion, body.config_version_id)
            if config is None:
                raise HTTPException(404, "Configuration not found")
            items = (
                await session.scalars(
                    select(TestConfigItem).where(TestConfigItem.config_version_id == config.id)
                )
            ).all()
            if len(items) != 1 or items[0].dataset_key != "GPQA":
                raise HTTPException(422, "Pilot supports a GPQA-only configuration")
            item = items[0]
            params = item.parameter_values
            if params.get("adapter_input") is not None:
                raise HTTPException(
                    422, "Full adaptor configurations are not enabled for pilot execution"
                )
            if (
                params["scope"] != "Lite"
                or params["concurrency"] != 1
                or not 1 <= params["output"] <= 64
                or params["temperature"] != 0
                or params["top_p"] != 1
            ):
                raise HTTPException(
                    422, "Pilot requires Lite, concurrency=1, output<=64, temperature=0, top_p=1"
                )
            binding = await session.scalar(
                select(ConfigDatasetBinding).where(ConfigDatasetBinding.config_item_id == item.id)
            )
            if binding is None:
                raise HTTPException(422, "Configuration has no execution profile")
            profile = await session.get(EvaluationProfile, binding.profile_id)
            lock = json.loads(
                request.app.state.settings.aisbench_lock_path.read_text(encoding="utf-8")
            )
            contract = profile.contract
            scorer_path = "ais_bench/benchmark/datasets/gpqa.py"
            if (
                profile.dataset_name != "GPQA"
                or contract["family"] != "aisbench"
                or contract["revision"] != lock["revision"]
                or contract["source_url"].rstrip("/") != lock["source_url"]
                or contract["scorer_reference"] != scorer_path
                or contract["scorer_sha256"] != lock["files"][scorer_path]
                or contract["parallelism_owner"] != "tool"
            ):
                raise HTTPException(422, "Profile does not match the pinned native GPQA adapter")
            manifest = await session.get(DatasetManifest, binding.manifest_id)
            if (
                manifest.scope != "lite"
                or manifest.id != profile.lite_manifest_id
                or manifest.sample_count > body.max_model_requests
            ):
                raise HTTPException(
                    422, "Manifest exceeds explicit request budget or does not match Lite profile"
                )
            version = await session.get(AssetVersion, manifest.dataset_version_id)
            content = version.metadata_json
            if (
                content.get("schema_version") != "dataset-content.v1"
                or content.get("tool_revision") != lock["revision"]
            ):
                raise HTTPException(422, "Native dataset content is not registered for this tool")
            manifest_body = await asyncio.to_thread(
                read_manifest, request.app.state.settings, manifest
            )
            bundle = await asyncio.to_thread(
                read_manifest, request.app.state.settings, SimpleNamespace(**content)
            )
            samples = {s["sample_id"]: s for s in bundle["samples"]}
            if any(
                s["sample_id"] not in samples or samples[s["sample_id"]]["sha256"] != s["sha256"]
                for s in manifest_body["samples"]
            ):
                raise HTTPException(422, "Native sample bundle differs from manifest")
            target = await session.get(TargetBinding, body.target_id)
            resource_type = (
                SSHResourceBinding if body.resource_kind == "customer_ssh" else ResourceBinding
            )
            resource = await session.get(resource_type, body.resource_id)
            if target is None or resource is None:
                raise HTTPException(404, "Target or resource not found")
            if (
                target.base_url.rstrip("/") != "https://api.deepseek.com"
                or target.remote_model != "deepseek-flash"
            ):
                raise HTTPException(422, "Pilot currently supports official deepseek-flash")
            if body.resource_kind == "customer_ssh":
                if not resource.native_tool_root:
                    raise HTTPException(422, "SSH resource has no approved native tool root")
                if (
                    not resource.native_bridge_sha256
                    or resource.native_tool_revision != contract["revision"]
                ):
                    raise HTTPException(
                        422, "SSH resource tool version is unpinned or differs from Profile"
                    )
                resource_spec = {
                    "resource_kind": "customer_ssh",
                    "ssh": frozen_ssh_resource(resource, request.app.state.settings),
                }
                allocation_key = (
                    "ssh:"
                    + hashlib.sha256(
                        f"{resource.host}:{resource.port}:{resource.workdir}".encode()
                    ).hexdigest()[:60]
                )
            else:
                resource_spec = {
                    "resource_kind": "docker_local",
                    "container_id": resource.container_id,
                    "image_id": resource.image_id,
                }
                allocation_key = resource.container_id
            frozen = {
                "execution_origin": request.app.state.settings.evaluation_origin,
                "schema_version": "gpqa-run.v1",
                "config_version_id": str(config.id),
                "config_sha256": config.content_hash,
                "parameters": params,
                "profile_id": str(profile.id),
                "profile_sha256": profile.sha256,
                "tool": contract,
                "manifest_id": str(manifest.id),
                "manifest_sha256": manifest.sha256,
                "samples": manifest_body["samples"],
                "dataset_content": content,
                "target_id": str(target.id),
                "model_version_id": str(target.model_version_id),
                "base_url": target.base_url,
                "remote_model": target.remote_model,
                "resource_id": str(resource.id),
                **resource_spec,
                "allocation_key": allocation_key,
                "max_model_requests": body.max_model_requests,
                "generation": {"thinking": {"type": "disabled"}},
                "gate_status": "not_applicable",
            }
            run = EvaluationRun(
                trigger_scope="local-operator:evaluation",
                idempotency_key=idempotency_key,
                request_hash=digest,
                run_mode="exploration",
                created_by="local-operator",
            )
            session.add(run)
            await session.flush()
            session.add(ExecutionLease(container_id=allocation_key, run_id=run.id))
            session.add(
                RunSnapshot(
                    run_id=run.id,
                    schema_version="gpqa-run.v1",
                    canonical_body=frozen,
                    sha256=hashlib.sha256(canonical_manifest(frozen)).hexdigest(),
                )
            )
            session.add(
                RunEvent(
                    run_id=run.id, seq=1, type="accepted", body={"gate_status": "not_applicable"}
                )
            )
            session.add(
                Outbox(
                    run_id=run.id,
                    kind="START_RUN",
                    dedup_key=f"start:{run.id}",
                    payload={
                        "workflow_kind": "gpqa_exploration",
                        "activity_queue": request.app.state.settings.gpqa_activity_queue,
                        "workflow_queue": request.app.state.settings.gpqa_workflow_queue,
                    },
                )
            )
            data = {
                "run_id": str(run.id),
                "state": "accepted",
                "expected_samples": len(frozen["samples"]),
                "gate_status": "not_applicable",
            }
    except IntegrityError as exc:
        raise HTTPException(409, "Resource busy or concurrent submission; retry same key") from exc
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Run database unavailable") from exc
    except (BotoCoreError, ClientError, ValueError) as exc:
        raise HTTPException(503, "Dataset evidence unavailable or inconsistent") from exc
    return {"data": data, "request_id": request.state.request_id}


@router.get("/{run_id}")
async def detail(run_id: uuid.UUID, request: Request):
    from autoeval.cleanup import ExecutionCleanup, cleanup_view

    try:
        async with request.app.state.sessions() as session:
            row = await session.get(EvaluationRun, run_id)
            if row is None or (row.trigger_scope != "local-operator:evaluation" and not (row.trigger_scope == "local-operator:unified-fixture" and request.app.state.settings.environment in {"development", "test"})):
                raise HTTPException(404, "Evaluation run not found")
            events = (
                await session.scalars(
                    select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.seq)
                )
            ).all()
            report = await session.scalar(
                select(EvaluationReport).where(EvaluationReport.run_id == run_id)
            )
            cleanup = await session.scalar(
                select(ExecutionCleanup).where(ExecutionCleanup.run_id == run_id)
            )
            snapshot = await session.scalar(select(RunSnapshot).where(RunSnapshot.run_id == run_id))
            data = {
                "id": str(row.id),
                "state": row.state,
                "created_at": row.created_at.isoformat(),
                "updated_at": events[-1].created_at.isoformat()
                if events
                else row.created_at.isoformat(),
                "cancel_requested_at": row.cancel_requested_at.isoformat()
                if row.cancel_requested_at
                else None,
                "run_mode": row.run_mode,
                "snapshot": {
                    "sha256": snapshot.sha256,
                    **{
                        key: snapshot.canonical_body.get(key)
                        for key in (
                            "config_version_id",
                            "config_sha256",
                            "parameters",
                            "tool",
                            "profile_id",
                            "profile_sha256",
                            "max_model_requests",
                            "target_id",
                            "model_version_id",
                            "remote_model",
                            "resource_id",
                            "resource_kind",
                            "manifest_id",
                            "manifest_sha256",
                            "execution_origin",
                        )
                    },
                    "sample_count": len(snapshot.canonical_body.get("samples", [])),
                }
                if snapshot
                else None,
                "gate_status": "not_applicable",
                "cleanup": cleanup_view(
                    cleanup,
                    required=bool(
                        snapshot and snapshot.canonical_body.get("resource_kind") == "customer_ssh"
                    ),
                ),
                "execution_unresolved": bool(events and events[-1].type == "execution_unresolved"),
                "events": [
                    {"seq": e.seq, "type": e.type, "created_at": e.created_at.isoformat()}
                    for e in events
                ],
                "report": {
                    "id": str(report.id),
                    "validity": report.validity,
                    "summary": report.summary,
                    "sha256": report.sha256,
                }
                if report
                else None,
            }
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Run database unavailable") from exc
    return {"data": data, "request_id": request.state.request_id}


@router.post("/{run_id}/cleanup", status_code=202)
async def retry_cleanup(run_id: uuid.UUID, request: Request):
    from autoeval.cleanup import schedule_cleanup

    async with request.app.state.sessions() as session, session.begin():
        row = await session.scalar(
            select(EvaluationRun).where(EvaluationRun.id == run_id).with_for_update()
        )
        snapshot = await session.scalar(select(RunSnapshot).where(RunSnapshot.run_id == run_id))
        if row is None or row.trigger_scope != "local-operator:evaluation":
            raise HTTPException(404, "Evaluation run not found")
        if (
            row.state not in {"completed", "failed", "cancelled"}
            or snapshot is None
            or snapshot.canonical_body.get("resource_kind") != "customer_ssh"
            or snapshot.canonical_body.get("execution_origin")
            != request.app.state.settings.evaluation_origin
        ):
            raise HTTPException(
                409, "Only terminal SSH runs from this execution origin may be reclaimed"
            )
        cleanup = await schedule_cleanup(session, run_id)
        if cleanup.state != "cleaned":
            cleanup.next_retry_at = None
    return {"data": {"accepted": True}, "request_id": request.state.request_id}


@router.post("/{run_id}/cancel", status_code=202)
async def cancel(run_id: uuid.UUID, request: Request):
    try:
        async with request.app.state.sessions() as session, session.begin():
            row = await session.scalar(
                select(EvaluationRun)
                .where(
                    EvaluationRun.id == run_id,
                    EvaluationRun.trigger_scope.in_(["local-operator:evaluation", "local-operator:unified-fixture"] if request.app.state.settings.environment in {"development", "test"} else ["local-operator:evaluation"]),
                )
                .with_for_update()
            )
            if row is None:
                raise HTTPException(404, "Evaluation run not found")
            accepted = row.state not in {"completed", "failed", "cancelled"}
            if accepted:
                row.cancel_requested_at = datetime.now(UTC)
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Run database unavailable") from exc
    return {"data": {"accepted": accepted}, "request_id": request.state.request_id}


@router.get("/{run_id}/report")
async def report_evidence(run_id: uuid.UUID, request: Request):
    try:
        async with request.app.state.sessions() as session:
            report = await session.scalar(
                select(EvaluationReport).where(EvaluationReport.run_id == run_id)
            )
            if report is None:
                raise HTTPException(404, "Report not available")
            data = await asyncio.to_thread(read_manifest, request.app.state.settings, report)
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Report database unavailable") from exc
    except (BotoCoreError, ClientError, ValueError) as exc:
        raise HTTPException(503, "Report evidence unavailable or integrity check failed") from exc
    return {"data": data, "request_id": request.state.request_id}
