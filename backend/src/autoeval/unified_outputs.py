"""Pinned unified-result parsing and append-only, object-backed collection.

This module never launches a benchmark or approves a report. Expected case identities
must come from a frozen platform manifest, not be inferred from the collected output.
"""

import asyncio
import hashlib
import json
import math
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import ForeignKey, String, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from autoeval.adaptor_versions import CURRENT_ADAPTOR
from autoeval.case_history import CaseExecutionRecord
from autoeval.datasets import canonical_manifest, read_manifest, storage, write_manifest
from autoeval.models import Base, EvaluationRun, Record


class UnifiedCollection(Record, Base):
    __tablename__ = "unified_result_collection"
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evaluation_run.id", ondelete="RESTRICT"))
    collection_key: Mapped[str] = mapped_column(String(128))
    object_key: Mapped[str] = mapped_column(String(256))
    sha256: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("run_id", "collection_key"),)


def read_raw_output(settings, artifact):
    client = storage(settings)
    try:
        response = client.get_object(Bucket=settings.s3_bucket, Key=artifact["object_key"])
        try:
            raw = response["Body"].read(16 * 1024 * 1024 + 1)
        finally:
            response["Body"].close()
        if len(raw) > 16 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != artifact["sha256"]:
            raise ValueError("Unified output checksum mismatch")
        return raw
    finally:
        client.close()


def finite(value, name, integer=False):
    if value is None:
        return None
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or (integer and not isinstance(value, int))
    ):
        raise ValueError("Invalid numeric field: " + name)
    return value


def timestamp(value, issues, name):
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise ValueError("Timezone required")
        return parsed
    except (ValueError, AttributeError, TypeError):
        issues.append("invalid_" + name)
        return None


def parse_output(raw: bytes, expected: dict[str, list[str]], default_dataset=None):
    if len(raw) > 16 * 1024 * 1024:
        raise ValueError("Unified result exceeds 16 MB")

    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    body = json.loads(raw, object_pairs_hook=pairs)
    if (
        not isinstance(body, dict)
        or type(body.get("schema_version")) is not int
        or body["schema_version"] != 1
        or body.get("tool") != "ais_bench"
        or body.get("subcommand") not in {"llm_accuracy", "harbor_agent"}
    ):
        raise ValueError("Unsupported unified output contract")
    if not expected or any(
        not isinstance(ids, list) or not ids or len(set(ids)) != len(ids)
        for ids in expected.values()
    ):
        raise ValueError("Frozen expected case identities required")
    cases = body.get("cases")
    if (
        not isinstance(cases, list)
        or type(body.get("case_count")) is not int
        or body["case_count"] != len(cases)
    ):
        raise ValueError("Case count mismatch")
    normalized, identities = [], set()
    for case in cases:
        if not isinstance(case, dict):
            raise ValueError("Invalid case")  # noqa: TRY004 -- contract validation
        dataset, sample = case.get("dataset") or default_dataset, case.get("case_id")
        attempt = case.get("attempt")
        if (
            dataset not in expected
            or not isinstance(sample, str)
            or sample not in expected[dataset]
            or len(sample) > 256
            or len(dataset) > 128
            or type(attempt) is not int
            or attempt < 1
        ):
            raise ValueError("Case identity does not match frozen manifest")
        identity = (dataset, sample, attempt)
        if identity in identities:
            raise ValueError("Duplicate case attempt")
        identities.add(identity)
        status = case.get("status")
        if status not in {"success", "failed", "other"}:
            raise ValueError("Unknown case status")
        issues = []
        start = timestamp(case.get("started_at"), issues, "started_at")
        end = timestamp(case.get("finished_at"), issues, "finished_at")
        duration = finite(case.get("duration_sec"), "duration_sec")
        if start and end:
            if end < start:
                issues.append("reversed_tool_time")
            elif duration is not None and abs((end - start).total_seconds() - duration) > 0.01:
                issues.append("duration_mismatch")
        normalized.append(
            {
                "dataset_key": dataset,
                "sample_id": sample,
                "attempt_no": attempt,
                "status": status,
                "score": finite(case.get("score"), "score"),
                "tool_started_at": case.get("started_at"),
                "tool_finished_at": case.get("finished_at"),
                "duration_sec": duration,
                "input_tokens": finite(case.get("input_tokens"), "input_tokens", True),
                "output_tokens": finite(case.get("output_tokens"), "output_tokens", True),
                "statistics_source": "autoeval-adaptor.unified.v1",
                "diagnostics": issues,
                "raw": case,
            }
        )
    latest = {}
    for case in normalized:
        key = (case["dataset_key"], case["sample_id"])
        if key not in latest or latest[key]["attempt_no"] < case["attempt_no"]:
            latest[key] = case
    required = sum(len(ids) for ids in expected.values())
    failed = sum(c["status"] != "success" for c in latest.values())
    scored = sum(c["score"] is not None for c in latest.values())
    missing = required - len(latest)
    valid = not failed and not missing and scored == required
    summary = {
        "validity": "complete"
        if valid
        else "partial"
        if latest and failed < len(latest)
        else "failed",
        "expected_cases": required,
        "observed_cases": len(latest),
        "missing_cases": missing,
        "failed_cases": failed,
        "scored_cases": scored,
        "attempt_count": len(normalized),
        "token_coverage": {
            "known_attempts": sum(
                c["input_tokens"] is not None and c["output_tokens"] is not None for c in normalized
            ),
            "total_attempts": len(normalized),
            "input_tokens_known_sum": sum(
                c["input_tokens"] for c in normalized if c["input_tokens"] is not None
            )
            if any(c["input_tokens"] is not None for c in normalized)
            else None,
            "output_tokens_known_sum": sum(
                c["output_tokens"] for c in normalized if c["output_tokens"] is not None
            )
            if any(c["output_tokens"] is not None for c in normalized)
            else None,
        },
        "upstream_total_score": finite(body.get("total_score"), "total_score"),
        "score_source": body.get("score_source"),
        "metric_conversion": "not_verified",
        "gate_status": "not_applicable",
        "adaptor_revision": CURRENT_ADAPTOR,
    }
    return normalized, summary


async def archive_output(
    sessions, settings, run_id, collection_key, raw, expected, default_dataset=None
):
    cases, summary = parse_output(raw, expected, default_dataset)
    sha = hashlib.sha256(raw).hexdigest()
    run_id = uuid.UUID(str(run_id))
    if not collection_key or len(collection_key) > 128:
        raise ValueError("Invalid collection key")
    async with sessions() as session, session.begin():
        run = await session.scalar(
            select(EvaluationRun).where(EvaluationRun.id == run_id).with_for_update()
        )
        if run is None:
            raise ValueError("Run not found")
        existing = await session.scalar(
            select(UnifiedCollection).where(
                UnifiedCollection.run_id == run_id,
                UnifiedCollection.collection_key == collection_key,
            )
        )
        if existing:
            if existing.sha256 != sha or existing.payload["expected"] != expected:
                raise ValueError("Collection identity conflicts with archived content")
            await asyncio.to_thread(read_manifest, settings, existing)
            return existing.payload["summary"]
        pending = []
        for case in cases:
            evidence = canonical_manifest({"schema_version": "unified-case.v1", **case})
            evidence_sha = hashlib.sha256(evidence).hexdigest()
            old = await session.scalar(
                select(CaseExecutionRecord).where(
                    CaseExecutionRecord.run_id == run_id,
                    CaseExecutionRecord.dataset_key == case["dataset_key"],
                    CaseExecutionRecord.sample_id == case["sample_id"],
                    CaseExecutionRecord.attempt_no == case["attempt_no"],
                )
            )
            if old:
                if old.sha256 != evidence_sha:
                    raise ValueError("Existing attempt evidence conflicts")
                await asyncio.to_thread(read_manifest, settings, old)
                continue
            pending.append((case, evidence, evidence_sha))
        key = f"unified-results/{run_id}/{sha}.json"
        await asyncio.to_thread(write_manifest, settings, key, raw)
        for case, evidence, evidence_sha in pending:
            identifier = uuid.uuid4()
            evidence_key = f"case-evidence/{run_id}/{identifier}/{evidence_sha}.json"
            await asyncio.to_thread(write_manifest, settings, evidence_key, evidence)
            session.add(
                CaseExecutionRecord(
                    id=identifier,
                    run_id=run_id,
                    dataset_key=case["dataset_key"],
                    sample_id=case["sample_id"],
                    attempt_no=case["attempt_no"],
                    operation_key="unified:"
                    + hashlib.sha256(
                        canonical_manifest(
                            [case["dataset_key"], case["sample_id"], case["attempt_no"]]
                        )
                    ).hexdigest(),
                    state="succeeded" if case["status"] == "success" else "failed",
                    finished_at=datetime.now(UTC),
                    object_key=evidence_key,
                    sha256=evidence_sha,
                    error_code=None
                    if case["status"] == "success"
                    else "UPSTREAM_EXECUTION_FAILURE",
                    tool_metadata={k: v for k, v in case.items() if k != "raw"},
                )
            )
        session.add(
            UnifiedCollection(
                run_id=run_id,
                collection_key=collection_key,
                object_key=key,
                sha256=sha,
                payload={"expected": expected, "summary": summary},
            )
        )
    return summary


router = APIRouter(prefix="/api/autoeval/v1", tags=["unified output evidence"])


@router.get("/runs/{run_id}/unified-output")
async def output_summary(run_id: uuid.UUID, request: Request):
    async with request.app.state.sessions() as session:
        if await session.get(EvaluationRun, run_id) is None:
            raise HTTPException(404, "Run not found")
        rows = (
            await session.scalars(
                select(UnifiedCollection)
                .where(UnifiedCollection.run_id == run_id)
                .order_by(UnifiedCollection.created_at.desc(), UnifiedCollection.id.desc())
                .limit(1)
            )
        ).all()
        if not rows:
            return {"data": None, "request_id": request.state.request_id}
        row = rows[0]
        try:
            await asyncio.to_thread(read_manifest, request.app.state.settings, row)
        except Exception as exc:
            raise HTTPException(503, "Unified evidence unavailable or invalid") from exc
        return {
            "data": {
                **row.payload["summary"],
                "sha256": row.sha256,
                "scope": "latest_collection",
                "observed_at": row.created_at.isoformat(),
            },
            "request_id": request.state.request_id,
        }
