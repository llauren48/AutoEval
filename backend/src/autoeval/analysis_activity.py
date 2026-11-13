"""One-shot report analysis activity; execution evidence never mutates the report."""

import asyncio
import hashlib
import re
import subprocess
import uuid
from types import SimpleNamespace

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from temporalio import activity

from autoeval.analysis import (
    ReportAnalysis,
    analysis_prompt,
    build_analysis_input,
    utcnow,
    validate_analysis_output,
)
from autoeval.case_history import CaseExecutionRecord
from autoeval.config import get_settings
from autoeval.datasets import canonical_manifest, read_manifest, write_manifest
from autoeval.db import create_database
from autoeval.evaluations import EvaluationReport
from autoeval.models import RunSnapshot
from autoeval.native_smoke import invoke
from autoeval.resources import ResourceBinding, inspect_executor
from autoeval.secrets import FileSecretStore
from autoeval.targets import TargetBinding, validate_url

SAFE_ERROR = re.compile(r"^[A-Z0-9_]{1,64}$")


def _response(state, job, **extra):
    return {"analysis_id": str(job.id), "state": state, **extra}


def build_analysis_result(
    native,
    evidence_input,
    *,
    analysis_id,
    report_id,
    input_sha256,
    target_snapshot,
):
    """Validate and serialize the complete immutable native outcome without credentials."""
    if not isinstance(native, dict):
        raise TypeError("Native analysis response is invalid")
    error_code = None
    parsed = None
    if native.get("state") == "succeeded":
        try:
            parsed = validate_analysis_output(
                native.get("response"), evidence_input["evidence_catalog"]
            )
        except ValueError:
            error_code = "INVALID_MODEL_OUTPUT"
    else:
        candidate = native.get("error_code")
        error_code = (
            candidate
            if isinstance(candidate, str) and SAFE_ERROR.fullmatch(candidate)
            else "ANALYSIS_INFERENCE_FAILED"
        )
    return {
        "schema_version": "analysis-result.v1",
        "analysis_id": str(analysis_id),
        "report_id": str(report_id),
        "input_sha256": input_sha256,
        "state": "succeeded" if parsed is not None else "failed",
        "analysis": parsed,
        "raw_response": native.get("response"),
        "error_code": error_code,
        "finish_reason": native.get("finish_reason"),
        "usage": native.get("usage") if isinstance(native.get("usage"), dict) else None,
        "model": {
            key: target_snapshot[key]
            for key in ("target_id", "model_version_id", "remote_model", "base_url")
        },
    }


async def _mark_unresolved(sessions, analysis_id, error_code):
    try:
        async with sessions() as session, session.begin():
            job = await session.scalar(
                select(ReportAnalysis).where(ReportAnalysis.id == analysis_id).with_for_update()
            )
            if job is not None and job.state in {"queued", "running"}:
                job.state = "unresolved"
                job.error_code = error_code
                job.finished_at = utcnow()
    except SQLAlchemyError:
        # Preserve the original execution uncertainty; a running row is never auto-replayed.
        pass


@activity.defn
async def execute_report_analysis(analysis_id: str):
    identifier = uuid.UUID(analysis_id)
    settings = get_settings()
    engine, sessions = create_database(settings)
    try:
        async with sessions() as session, session.begin():
            job = await session.scalar(
                select(ReportAnalysis).where(ReportAnalysis.id == identifier).with_for_update()
            )
            if job is None:
                return {"analysis_id": analysis_id, "state": "not_found"}
            if job.state in {"succeeded", "failed", "unresolved"}:
                return _response(job.state, job, retry_sent=False)
            if job.state != "queued":
                job.state = "unresolved"
                job.error_code = "ANALYSIS_REDELIVERY_UNRESOLVED"
                job.finished_at = utcnow()
                return _response("unresolved", job, retry_sent=False)
            report = await session.get(EvaluationReport, job.report_id)
            snapshot = await session.scalar(
                select(RunSnapshot).where(RunSnapshot.run_id == job.run_id)
            )
            target = await session.get(TargetBinding, uuid.UUID(job.target_snapshot["target_id"]))
            resource = await session.get(
                ResourceBinding, uuid.UUID(job.target_snapshot["resource_id"])
            )
            if report is None or snapshot is None or target is None or resource is None:
                raise ValueError("Analysis input binding is missing")
            if (
                report.sha256 != job.report_sha256
                or report.snapshot_sha256 != job.snapshot_sha256
                or snapshot.sha256 != job.snapshot_sha256
                or hashlib.sha256(canonical_manifest(snapshot.canonical_body)).hexdigest()
                != snapshot.sha256
            ):
                raise ValueError("Analysis report or snapshot identity changed")
            frozen = job.target_snapshot
            if (
                str(target.id) != frozen["target_id"]
                or str(target.model_version_id) != frozen["model_version_id"]
                or target.base_url != frozen["base_url"]
                or target.remote_model != frozen["remote_model"]
                or str(resource.id) != frozen["resource_id"]
                or resource.container_id != frozen["container_id"]
                or resource.image_id != frozen["image_id"]
            ):
                raise ValueError("Analysis target or resource identity changed")
            validate_url(target.base_url, settings.allowed_model_hosts)
            attempts = (
                await session.scalars(
                    select(CaseExecutionRecord)
                    .where(
                        CaseExecutionRecord.run_id == job.run_id,
                        CaseExecutionRecord.sha256.is_not(None),
                    )
                    .order_by(CaseExecutionRecord.created_at, CaseExecutionRecord.id)
                )
            ).all()

        report_body = await asyncio.to_thread(read_manifest, settings, report)
        attempt_evidence = []
        for attempt in attempts:
            body = await asyncio.to_thread(read_manifest, settings, attempt)
            attempt_evidence.append(
                {"attempt_id": str(attempt.id), "sha256": attempt.sha256, "body": body}
            )
        baseline = job.baseline_comparison
        if job.baseline_comparison_sha256 is not None and (
            baseline is None
            or hashlib.sha256(canonical_manifest(baseline)).hexdigest()
            != job.baseline_comparison_sha256
        ):
            raise ValueError("Baseline comparison identity changed")
        evidence_input = build_analysis_input(
            report_id=report.id,
            report_sha256=job.report_sha256,
            snapshot_sha256=job.snapshot_sha256,
            report_body=report_body,
            snapshot_body=snapshot.canonical_body,
            attempts=attempt_evidence,
            baseline_comparison=baseline if job.baseline_comparison_sha256 else None,
            baseline_comparison_sha256=job.baseline_comparison_sha256,
        )
        input_raw = canonical_manifest(evidence_input)
        input_digest = hashlib.sha256(input_raw).hexdigest()
        input_key = f"analysis-inputs/{job.id}/{input_digest}.json"
        await asyncio.to_thread(write_manifest, settings, input_key, input_raw)
        async with sessions() as session, session.begin():
            locked = await session.scalar(
                select(ReportAnalysis).where(ReportAnalysis.id == identifier).with_for_update()
            )
            if locked.state == "running":
                locked.state = "unresolved"
                locked.error_code = "ANALYSIS_REDELIVERY_UNRESOLVED"
                locked.finished_at = utcnow()
                return _response("unresolved", locked, retry_sent=False)
            if locked.state != "queued":
                return _response(locked.state, locked, retry_sent=False)
            locked.input_object_key = input_key
            locked.input_sha256 = input_digest
            locked.evidence_catalog = evidence_input["evidence_catalog"]
            locked.state = "running"
            locked.started_at = utcnow()
            locked.error_code = None

        identity = await asyncio.to_thread(
            inspect_executor, settings.docker_executable, resource.container_id
        )
        if not identity["running"] or identity["image_id"] != resource.image_id:
            raise ValueError("Analysis executor identity changed")
        credential = await asyncio.to_thread(
            FileSecretStore(settings.secret_store_path).get, target.credential_ref
        )
        activity.heartbeat(analysis_id)
        native = await asyncio.to_thread(
            invoke,
            settings,
            resource.container_id,
            {
                "operation": "analyze_report",
                "prompt": analysis_prompt(evidence_input),
                "base_url": target.base_url.rstrip("/"),
                "model": target.remote_model,
                "max_tokens": settings.analysis_max_output_tokens,
                "api_key": credential,
            },
        )
        result_body = build_analysis_result(
            native,
            evidence_input,
            analysis_id=job.id,
            report_id=report.id,
            input_sha256=input_digest,
            target_snapshot=frozen,
        )
        result_raw = canonical_manifest(result_body)
        result_digest = hashlib.sha256(result_raw).hexdigest()
        result_key = f"analysis-results/{job.id}/{result_digest}.json"
        await asyncio.to_thread(write_manifest, settings, result_key, result_raw)
        async with sessions() as session, session.begin():
            locked = await session.scalar(
                select(ReportAnalysis).where(ReportAnalysis.id == identifier).with_for_update()
            )
            if locked.state != "running" or locked.input_sha256 != input_digest:
                return _response("unresolved", locked, retry_sent=False)
            locked.state = result_body["state"]
            locked.object_key = result_key
            locked.sha256 = result_digest
            locked.error_code = result_body["error_code"]
            locked.usage = result_body["usage"]
            locked.finished_at = utcnow()
        return _response(locked.state, locked, result_sha256=result_digest, retry_sent=False)
    except (
        OSError,
        ValueError,
        RuntimeError,
        KeyError,
        TypeError,
        BotoCoreError,
        ClientError,
        SQLAlchemyError,
        subprocess.SubprocessError,
    ):
        await _mark_unresolved(sessions, identifier, "ANALYSIS_EXECUTION_UNRESOLVED")
        return {"analysis_id": analysis_id, "state": "unresolved", "retry_sent": False}
    finally:
        await engine.dispose()


async def read_analysis_result(settings, job):
    if not job.object_key or not job.sha256:
        return None
    body = await asyncio.to_thread(
        read_manifest,
        settings,
        SimpleNamespace(object_key=job.object_key, sha256=job.sha256),
    )
    if (
        body.get("schema_version") != "analysis-result.v1"
        or body.get("analysis_id") != str(job.id)
        or body.get("report_id") != str(job.report_id)
        or body.get("input_sha256") != job.input_sha256
    ):
        raise ValueError("Analysis result identity is inconsistent")
    return body
