"""Read-only immutable report catalog, separate from prototype report data."""

import asyncio
import uuid

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import SQLAlchemyError

from autoeval.analysis import ReportAnalysis, analysis_status_for
from autoeval.benchmarks import BenchmarkBaseline, compare_baseline
from autoeval.datasets import read_manifest
from autoeval.evaluations import EvaluationReport
from autoeval.models import EvaluationRun, RunSnapshot
from autoeval.report_integrity import report_evidence_status

router = APIRouter(prefix="/api/autoeval/v1/reports", tags=["reports"])


def report_data(
    row,
    run,
    snapshot,
    baselines=(),
    *,
    analysis_status="not_enabled",
    analysis_id=None,
    evidence_status="not_checked",
):
    comparison = compare_baseline(row.summary, snapshot.canonical_body, baselines)
    if evidence_status != "verified":
        comparison = {
            **comparison,
            "status": "incomparable",
            "measured_value": None,
            "difference": None,
            "reason": "report_evidence_unverified",
        }
    return {
        "id": str(row.id),
        "run_id": str(row.run_id),
        "execution_kind": "planned"
        if snapshot.canonical_body.get("schema_version") == "planned-run.v1"
        else "evaluation",
        "created_at": row.created_at.isoformat(),
        "validity": row.validity,
        "evidence_status": evidence_status,
        "summary": row.summary,
        "sha256": row.sha256,
        "snapshot_sha256": row.snapshot_sha256,
        "run_mode": run.run_mode,
        "remote_model": snapshot.canonical_body["remote_model"],
        "config_version_id": snapshot.canonical_body["config_version_id"],
        "manifest_id": snapshot.canonical_body["manifest_id"],
        "nomination_conditions": {
            **snapshot.canonical_body["parameters"],
            "manifest_sha256": snapshot.canonical_body["manifest_sha256"],
            "tool_revision": snapshot.canonical_body["tool"]["revision"],
        },
        "execution_origin": row.execution_origin,
        "gate_status": "not_applicable",
        "analysis_status": analysis_status,
        "analysis_id": str(analysis_id) if analysis_id else None,
        "baseline_comparison": comparison,
    }


@router.get("")
async def catalog(request: Request, offset: int = 0, limit: int = 20):
    if offset < 0 or not 1 <= limit <= 200:
        raise HTTPException(422, "Invalid pagination")
    try:
        async with request.app.state.sessions() as session:
            rows = (
                await session.execute(
                    select(EvaluationReport, EvaluationRun, RunSnapshot)
                    .join(EvaluationRun, EvaluationRun.id == EvaluationReport.run_id)
                    .join(RunSnapshot, RunSnapshot.run_id == EvaluationRun.id)
                    .where(
                        EvaluationRun.trigger_scope.in_(
                            ["local-operator:evaluation", "local-operator:orchestration"]
                        ),
                        EvaluationReport.execution_origin == "real",
                    )
                    .order_by(EvaluationReport.created_at.desc(), EvaluationReport.id.desc())
                    .offset(offset)
                    .limit(limit + 1)
                )
            ).all()
            baselines = (await session.scalars(select(BenchmarkBaseline))).all()
            selected = rows[:limit]
            analyses = (
                (
                    await session.scalars(
                        select(ReportAnalysis).where(
                            ReportAnalysis.report_id.in_([row[0].id for row in selected])
                        )
                    )
                ).all()
                if selected
                else []
            )
            jobs = {job.report_id: job for job in analyses}
            statuses = await asyncio.gather(
                *(report_evidence_status(request.app.state.settings, *row) for row in selected)
            )
            data = [
                report_data(
                    *row,
                    baselines,
                    analysis_status=analysis_status_for(
                        jobs.get(row[0].id),
                        request.app.state.settings,
                        execution_origin=row[0].execution_origin,
                    ),
                    analysis_id=jobs.get(row[0].id).id if jobs.get(row[0].id) else None,
                    evidence_status=status,
                )
                for row, status in zip(selected, statuses, strict=True)
            ]
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Report catalog unavailable") from exc
    return {
        "data": data,
        "next_offset": offset + limit if len(rows) > limit else None,
        "request_id": request.state.request_id,
    }


@router.get("/{report_id}")
async def detail(report_id: uuid.UUID, request: Request):
    try:
        async with request.app.state.sessions() as session:
            row = (
                await session.execute(
                    select(EvaluationReport, EvaluationRun, RunSnapshot)
                    .join(EvaluationRun, EvaluationRun.id == EvaluationReport.run_id)
                    .join(RunSnapshot, RunSnapshot.run_id == EvaluationRun.id)
                    .where(
                        EvaluationReport.id == report_id,
                        or_(
                            and_(
                                EvaluationRun.trigger_scope.in_(
                                    ["local-operator:evaluation", "local-operator:orchestration"]
                                ),
                                EvaluationReport.execution_origin == "real",
                            ),
                            and_(
                                EvaluationRun.trigger_scope == "local-operator:unified-fixture",
                                EvaluationReport.execution_origin == "simulation",
                                request.app.state.settings.environment in {"development", "test"},
                            ),
                        ),
                    )
                )
            ).one_or_none()
            if row is None:
                raise HTTPException(404, "Report not found")
            baselines = (await session.scalars(select(BenchmarkBaseline))).all()
            job = await session.scalar(
                select(ReportAnalysis).where(ReportAnalysis.report_id == report_id)
            )
            data = report_data(
                *row,
                baselines,
                analysis_status=analysis_status_for(
                    job,
                    request.app.state.settings,
                    execution_origin=row[0].execution_origin,
                ),
                analysis_id=job.id if job else None,
                evidence_status=await report_evidence_status(request.app.state.settings, *row),
            )
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Report database unavailable") from exc
    return {"data": data, "request_id": request.state.request_id}


@router.get("/{report_id}/evidence")
async def evidence(report_id: uuid.UUID, request: Request):
    try:
        async with request.app.state.sessions() as session:
            row = await session.scalar(
                select(EvaluationReport)
                .join(EvaluationRun, EvaluationRun.id == EvaluationReport.run_id)
                .where(
                    EvaluationReport.id == report_id,
                    EvaluationRun.trigger_scope.in_(
                        ["local-operator:evaluation", "local-operator:orchestration"]
                    ),
                    EvaluationReport.execution_origin == "real",
                )
            )
            if row is None:
                raise HTTPException(404, "Report not found")
            data = await asyncio.to_thread(read_manifest, request.app.state.settings, row)
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Report database unavailable") from exc
    except (BotoCoreError, ClientError, ValueError) as exc:
        raise HTTPException(503, "Report evidence unavailable or integrity check failed") from exc
    return {"data": data, "request_id": request.state.request_id}
