"""Independent, immutable report analysis with server-owned evidence citations."""

import hashlib
import json
import re
import uuid
from datetime import UTC, datetime
from typing import Literal

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, mapped_column

from autoeval.datasets import canonical_manifest
from autoeval.models import Base, Outbox, Record
from autoeval.resources import ResourceBinding
from autoeval.targets import TargetBinding

HEX256 = re.compile(r"^[0-9a-f]{64}$")


class AnalysisConfigurationError(ValueError):
    """Analysis cannot be enabled without changing the original report outcome."""


class ReportAnalysis(Record, Base):
    __tablename__ = "report_analysis"
    report_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evaluation_report.id", ondelete="RESTRICT"), unique=True
    )
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evaluation_run.id", ondelete="RESTRICT"))
    state: Mapped[str] = mapped_column(String(16), server_default="queued")
    retry_history: Mapped[list] = mapped_column(JSONB, server_default="[]")
    trigger: Mapped[str] = mapped_column(String(24))
    retry_count: Mapped[int] = mapped_column(server_default="0")
    report_sha256: Mapped[str] = mapped_column(String(64))
    snapshot_sha256: Mapped[str] = mapped_column(String(64))
    baseline_comparison_sha256: Mapped[str | None] = mapped_column(String(64))
    baseline_comparison: Mapped[dict | None] = mapped_column(JSONB)
    target_snapshot: Mapped[dict] = mapped_column(JSONB)
    evidence_catalog: Mapped[dict] = mapped_column(JSONB, server_default="{}")
    input_object_key: Mapped[str | None] = mapped_column(String(256))
    input_sha256: Mapped[str | None] = mapped_column(String(64))
    object_key: Mapped[str | None] = mapped_column(String(256))
    sha256: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    usage: Mapped[dict | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint("state IN ('queued','running','succeeded','failed','unresolved')"),
        CheckConstraint("trigger IN ('report_created','manual_start','manual_retry')"),
        CheckConstraint("retry_count >= 0"),
        CheckConstraint("report_sha256 ~ '^[0-9a-f]{64}$'"),
        CheckConstraint("snapshot_sha256 ~ '^[0-9a-f]{64}$'"),
    )


def analysis_is_configured(settings) -> bool:
    return bool(
        getattr(settings, "analysis_target_id", None)
        and getattr(settings, "analysis_resource_id", None)
    )


def analysis_status_for(job, settings, *, execution_origin="real"):
    if execution_origin != "real":
        return "not_enabled"
    if job is not None:
        return job.state
    return "not_queued" if analysis_is_configured(settings) else "not_enabled"


async def current_baseline_comparison(session, report, snapshot):
    from autoeval.benchmarks import BenchmarkBaseline, compare_baseline

    baselines = (await session.scalars(select(BenchmarkBaseline))).all()
    return compare_baseline(report.summary, snapshot.canonical_body, baselines)


def _catalog_entry(kind, digest, source_id=None):
    return {"kind": kind, "sha256": digest, "source_id": source_id}


def build_analysis_input(
    *,
    report_id: uuid.UUID,
    report_sha256: str,
    snapshot_sha256: str,
    report_body: dict,
    snapshot_body: dict,
    attempts: list[dict],
    baseline_comparison: dict | None = None,
    baseline_comparison_sha256: str | None = None,
):
    """Build the exact model input and its closed citation registry."""
    if not HEX256.fullmatch(report_sha256) or not HEX256.fullmatch(snapshot_sha256):
        raise ValueError("Invalid report or snapshot digest")
    safe_snapshot = {
        key: snapshot_body[key]
        for key in ("config_version_id", "manifest_id", "manifest_sha256", "parameters", "tool")
        if key in snapshot_body
    }
    catalog = {
        "run.summary": _catalog_entry("run_summary", report_sha256, str(report_id)),
    }
    evidence = [
        {
            "evidence_id": "run.summary",
            "content": {"report": report_body, "execution": safe_snapshot},
        }
    ]
    seen_attempts = set()
    for attempt in attempts:
        body = attempt["body"]
        attempt_id = attempt["attempt_id"]
        try:
            parsed_id = uuid.UUID(attempt_id)
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError("Invalid attempt citation") from exc
        schema_version = body.get("schema_version")
        if (
            attempt_id in seen_attempts
            or not (
                (schema_version == "case-evidence.v1" and body.get("attempt_id") == attempt_id)
                # unified-case.v1 has no embedded attempt_id; CaseExecutionRecord id + sha256 bind it.
                or schema_version == "unified-case.v1"
            )
            or not HEX256.fullmatch(attempt.get("sha256", ""))
        ):
            raise ValueError("Attempt evidence identity is inconsistent")
        seen_attempts.add(attempt_id)
        alias = f"attempt:{parsed_id}"
        catalog[alias] = _catalog_entry("case_attempt", attempt["sha256"], attempt_id)
        evidence.append({"evidence_id": alias, "content": body})
    if baseline_comparison is not None:
        if not HEX256.fullmatch(baseline_comparison_sha256 or ""):
            raise ValueError("Baseline comparison requires a digest")
        catalog["baseline.comparison"] = _catalog_entry(
            "baseline_comparison", baseline_comparison_sha256
        )
        evidence.append({"evidence_id": "baseline.comparison", "content": baseline_comparison})
    return {
        "schema_version": "analysis-input.v1",
        "report_id": str(report_id),
        "report_sha256": report_sha256,
        "snapshot_sha256": snapshot_sha256,
        "baseline_comparison_sha256": baseline_comparison_sha256,
        "evidence_catalog": catalog,
        "evidence": evidence,
    }


def validate_analysis_output(raw, evidence_catalog: dict) -> dict:
    """Reject malformed output, foreign citations, and authority-expanding fields."""
    try:
        body = json.loads(raw) if isinstance(raw, str) else raw
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("Analysis output is not valid JSON") from exc
    if not isinstance(body, dict) or set(body) != {
        "summary",
        "evidence_ids",
        "facts",
        "inferences",
        "recommended_actions",
    }:
        raise ValueError("Analysis output schema is invalid")

    def citations(value, *, allow_empty=False):
        if (
            not isinstance(value, list)
            or (not allow_empty and not value)
            or len(value) > 1000
            or any(not isinstance(item, str) for item in value)
            or len(set(value)) != len(value)
            or any(item not in evidence_catalog for item in value)
        ):
            raise ValueError("Analysis contains an unknown or duplicate evidence citation")
        return value

    summary = body["summary"]
    if not isinstance(summary, str) or not summary.strip() or len(summary) > 20000:
        raise ValueError("Analysis summary is invalid")
    citations(body["evidence_ids"])
    sections = {}
    for name in ("facts", "inferences"):
        source = body[name]
        if not isinstance(source, list) or len(source) > 100:
            raise ValueError(f"Analysis {name} are invalid")
        section = []
        for item in source:
            if (
                not isinstance(item, dict)
                or set(item) != {"statement", "evidence_ids"}
                or not isinstance(item["statement"], str)
                or not item["statement"].strip()
                or len(item["statement"]) > 4000
            ):
                raise ValueError(f"Analysis {name} are invalid")
            citations(item["evidence_ids"], allow_empty=name == "inferences")
            if (
                name == "inferences"
                and not item["evidence_ids"]
                and not re.search(
                    r"证据不足|insufficient evidence", item["statement"], re.IGNORECASE
                )
            ):
                raise ValueError("Uncited inference must state insufficient evidence")
            section.append(item)
        sections[name] = section
    actions = body["recommended_actions"]
    if (
        not isinstance(actions, list)
        or len(actions) > 100
        or any(
            not isinstance(item, str) or not item.strip() or len(item) > 2000 for item in actions
        )
    ):
        raise ValueError("Analysis recommended actions are invalid")
    return {
        "summary": summary,
        "evidence_ids": body["evidence_ids"],
        **sections,
        "recommended_actions": actions,
    }


def analysis_prompt(evidence_input: dict) -> str:
    return (
        "Analyze only the supplied immutable evaluation evidence. Do not invent measurements, "
        "assume missing samples passed, change scores, or make release/gate decisions. Return one "
        'JSON object with exactly "summary", "evidence_ids", "facts", "inferences", and '
        '"recommended_actions". Separate directly reported facts from model inferences. Facts and '
        'inferences are lists of {"statement", "evidence_ids"} objects; use an empty evidence_ids '
        'list only for an inference whose statement contains "insufficient evidence" or "证据不足". '
        "Every citation must be copied from evidence_catalog without duplicates. recommended_actions must be a "
        "list of concise strings. "
        "请使用简体中文撰写所有面向用户的分析内容：summary、facts 和 inferences 中的 statement，"
        "以及 recommended_actions 中的每一条建议。不要输出整段英文叙述。"
        "JSON 字段名、evidence_ids、模型与数据集标识、错误码、原始数值及单位必须保持原样，"
        "不要翻译或改写证据标识。证据不足的推断必须明确写出‘证据不足’。\n"
        + canonical_manifest(evidence_input).decode()
    )


async def enqueue_analysis(
    session,
    settings,
    report,
    snapshot,
    *,
    trigger: Literal["report_created", "manual_start", "manual_retry"],
    baseline_comparison: dict | None = None,
):
    """Enqueue once inside the caller's transaction; report row lock serializes retries."""
    from autoeval.evaluations import EvaluationReport

    locked = await session.scalar(
        select(EvaluationReport).where(EvaluationReport.id == report.id).with_for_update()
    )
    if locked is None or locked.sha256 != report.sha256:
        raise ValueError("Report is missing or changed")
    if snapshot.sha256 != report.snapshot_sha256:
        raise ValueError("Report snapshot digest is inconsistent")
    if report.execution_origin != "real":
        raise AnalysisConfigurationError("Report analysis requires real execution evidence")
    existing = await session.scalar(
        select(ReportAnalysis).where(ReportAnalysis.report_id == report.id)
    )
    if existing is not None:
        return existing, False
    if not analysis_is_configured(settings):
        raise AnalysisConfigurationError("Independent analysis target and resource are required")
    if trigger not in ("report_created", "manual_start", "manual_retry"):
        raise ValueError("Invalid analysis trigger")
    target = await session.get(TargetBinding, settings.analysis_target_id)
    resource = await session.get(ResourceBinding, settings.analysis_resource_id)
    if target is None or resource is None:
        raise AnalysisConfigurationError(
            "Independent analysis target or resource is not registered"
        )
    evaluated = snapshot.canonical_body
    evaluated_target = await session.get(TargetBinding, uuid.UUID(evaluated["target_id"]))
    if evaluated_target is None or (
        str(target.id) == evaluated["target_id"]
        or str(target.model_version_id) == evaluated["model_version_id"]
        or target.remote_model == evaluated["remote_model"]
        or target.credential_ref == evaluated_target.credential_ref
        or str(resource.id) == evaluated["resource_id"]
        or (
            evaluated.get("resource_kind") == "docker_local"
            and resource.container_id == evaluated.get("container_id")
        )
    ):
        raise AnalysisConfigurationError(
            "Independent analysis target, model, credential, and resource are required"
        )
    job = ReportAnalysis(
        id=uuid.uuid4(),
        report_id=report.id,
        run_id=report.run_id,
        state="queued",
        trigger=trigger,
        retry_count=1 if trigger == "manual_retry" else 0,
        report_sha256=report.sha256,
        snapshot_sha256=snapshot.sha256,
        baseline_comparison_sha256=(
            hashlib.sha256(canonical_manifest(baseline_comparison)).hexdigest()
            if baseline_comparison is not None
            else None
        ),
        baseline_comparison=baseline_comparison,
        target_snapshot={
            "target_id": str(target.id),
            "model_version_id": str(target.model_version_id),
            "base_url": target.base_url,
            "remote_model": target.remote_model,
            "resource_id": str(resource.id),
            "container_id": resource.container_id,
            "image_id": resource.image_id,
        },
        evidence_catalog={},
    )
    session.add(job)
    session.add(
        Outbox(
            run_id=report.run_id,
            kind="ANALYZE_REPORT",
            dedup_key=f"analysis:{job.id}",
            payload={
                "workflow_kind": "report_analysis",
                "analysis_id": str(job.id),
                "retry_count": job.retry_count,
                "activity_queue": settings.analysis_activity_queue,
                "workflow_queue": settings.analysis_workflow_queue,
            },
        )
    )
    await session.flush()
    return job, True


async def retry_existing_analysis(session, settings, job: ReportAnalysis):
    """Explicitly requeue failed or uncertain analysis, retaining prior attempt metadata."""
    locked = await session.scalar(
        select(ReportAnalysis).where(ReportAnalysis.id == job.id).with_for_update()
    )
    if locked is None:
        raise ValueError("Report analysis is missing")
    if locked.state not in {"failed", "unresolved"} or not analysis_is_configured(settings):
        return locked, False
    locked.retry_history = [
        *(locked.retry_history or []),
        {
            **analysis_data(locked),
            "object_key": locked.object_key,
            "input_object_key": locked.input_object_key,
        },
    ]
    locked.state = "queued"
    locked.trigger = "manual_retry"
    locked.retry_count += 1
    locked.started_at = None
    locked.finished_at = None
    locked.object_key = None
    locked.sha256 = None
    locked.error_code = None
    locked.usage = None
    session.add(
        Outbox(
            run_id=locked.run_id,
            kind="ANALYZE_REPORT",
            dedup_key=f"analysis:{locked.id}:manual-retry:{locked.retry_count}",
            payload={
                "workflow_kind": "report_analysis",
                "analysis_id": str(locked.id),
                "retry_count": locked.retry_count,
                "activity_queue": settings.analysis_activity_queue,
                "workflow_queue": settings.analysis_workflow_queue,
            },
        )
    )
    await session.flush()
    return locked, True


def analysis_data(job: ReportAnalysis, result: dict | None = None) -> dict:
    return {
        "id": str(job.id),
        "report_id": str(job.report_id),
        "run_id": str(job.run_id),
        "state": job.state,
        "trigger": job.trigger,
        "retry_count": job.retry_count,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "report_sha256": job.report_sha256,
        "snapshot_sha256": job.snapshot_sha256,
        "baseline_comparison_sha256": job.baseline_comparison_sha256,
        "input_sha256": job.input_sha256,
        "result_sha256": job.sha256,
        "error_code": job.error_code,
        "usage": job.usage,
        "target": job.target_snapshot,
        "evidence_catalog": job.evidence_catalog,
        "result": result,
        "retry_creates_new_analysis": False,
    }


def unfinished(job: ReportAnalysis) -> bool:
    return job.state in {"queued", "running", "unresolved"}


def utcnow():
    return datetime.now(UTC)


router = APIRouter(prefix="/api/autoeval/v1/reports", tags=["report analysis"])


@router.get("/{report_id}/analysis")
async def get_analysis(report_id: uuid.UUID, request: Request):
    from autoeval.analysis_activity import read_analysis_result
    from autoeval.evaluations import EvaluationReport
    from autoeval.models import EvaluationRun

    try:
        async with request.app.state.sessions() as session:
            job = await session.scalar(
                select(ReportAnalysis)
                .join(EvaluationReport, EvaluationReport.id == ReportAnalysis.report_id)
                .join(EvaluationRun, EvaluationRun.id == ReportAnalysis.run_id)
                .where(
                    ReportAnalysis.report_id == report_id,
                    EvaluationReport.execution_origin == "real",
                    EvaluationRun.trigger_scope == "local-operator:evaluation",
                )
            )
            if job is None:
                raise HTTPException(404, "Report analysis not found")
        result = await read_analysis_result(request.app.state.settings, job)
        data = analysis_data(job, result)
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Analysis database unavailable") from exc
    except (BotoCoreError, ClientError, ValueError) as exc:
        raise HTTPException(503, "Analysis result unavailable or integrity check failed") from exc
    return {"data": data, "request_id": request.state.request_id}


@router.post("/{report_id}/analysis:start", status_code=202)
async def start_analysis(report_id: uuid.UUID, request: Request):
    from autoeval.evaluations import EvaluationReport
    from autoeval.models import EvaluationRun, RunSnapshot

    settings = request.app.state.settings
    if not analysis_is_configured(settings):
        raise HTTPException(503, "Independent analysis target and resource are not configured")
    try:
        async with request.app.state.sessions() as session, session.begin():
            report = await session.scalar(
                select(EvaluationReport)
                .join(EvaluationRun, EvaluationRun.id == EvaluationReport.run_id)
                .where(
                    EvaluationReport.id == report_id,
                    EvaluationReport.execution_origin == "real",
                    EvaluationRun.trigger_scope == "local-operator:evaluation",
                )
            )
            if report is None:
                raise HTTPException(404, "Report not found")
            snapshot = await session.scalar(
                select(RunSnapshot).where(RunSnapshot.run_id == report.run_id)
            )
            if snapshot is None:
                raise HTTPException(409, "Report snapshot is unavailable")
            existing = await session.scalar(
                select(ReportAnalysis).where(ReportAnalysis.report_id == report.id)
            )
            if existing is not None:
                raise HTTPException(409, "Analysis has already been queued")
            comparison = await current_baseline_comparison(session, report, snapshot)
            job, _ = await enqueue_analysis(
                session,
                settings,
                report,
                snapshot,
                trigger="manual_start",
                baseline_comparison=comparison,
            )
            data = analysis_data(job)
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Analysis database unavailable") from exc
    except ValueError as exc:
        raise HTTPException(409, "Report analysis cannot be queued") from exc
    return {"data": data, "request_id": request.state.request_id}


@router.post("/{report_id}/analysis:retry", status_code=202)
async def retry_analysis(report_id: uuid.UUID, request: Request):
    from autoeval.evaluations import EvaluationReport
    from autoeval.models import EvaluationRun

    settings = request.app.state.settings
    if not analysis_is_configured(settings):
        raise HTTPException(503, "Independent analysis target and resource are not configured")
    try:
        async with request.app.state.sessions() as session, session.begin():
            report = await session.scalar(
                select(EvaluationReport)
                .join(EvaluationRun, EvaluationRun.id == EvaluationReport.run_id)
                .where(
                    EvaluationReport.id == report_id,
                    EvaluationReport.execution_origin == "real",
                    EvaluationRun.trigger_scope == "local-operator:evaluation",
                )
            )
            if report is None:
                raise HTTPException(404, "Report not found")
            existing = await session.scalar(
                select(ReportAnalysis).where(ReportAnalysis.report_id == report.id)
            )
            if existing is None:
                raise HTTPException(409, "Analysis has not been queued")
            job, created = await retry_existing_analysis(session, settings, existing)
            if not created:
                raise HTTPException(409, "Analysis is active or complete")
            data = analysis_data(job)
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Analysis database unavailable") from exc
    except ValueError as exc:
        raise HTTPException(409, "Report analysis cannot be queued") from exc
    return {"data": data, "request_id": request.state.request_id}
