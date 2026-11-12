"""Append-only case attempts with object-backed evidence, independent of scoring."""

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime
from typing import Literal

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, mapped_column

from autoeval.datasets import canonical_manifest, read_manifest, write_manifest
from autoeval.models import Base, EvaluationRun, Record


class CaseExecutionRecord(Record, Base):
    __tablename__ = "case_execution_record"
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("evaluation_run.id", ondelete="RESTRICT"))
    dataset_key: Mapped[str] = mapped_column(String(128))
    sample_id: Mapped[str] = mapped_column(String(256))
    attempt_no: Mapped[int]
    operation_key: Mapped[str] = mapped_column(String(256))
    state: Mapped[str] = mapped_column(String(16))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    object_key: Mapped[str | None] = mapped_column(String(256))
    sha256: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(64))
    tool_metadata: Mapped[dict | None] = mapped_column(JSONB)
    __table_args__ = (
        UniqueConstraint("run_id", "operation_key"),
        UniqueConstraint("run_id", "dataset_key", "sample_id", "attempt_no"),
        CheckConstraint("attempt_no > 0"),
        CheckConstraint("state IN ('started','succeeded','failed')"),
        CheckConstraint(
            "(state = 'started' AND finished_at IS NULL AND object_key IS NULL AND sha256 IS NULL) OR (state <> 'started' AND finished_at IS NOT NULL AND object_key IS NOT NULL AND sha256 IS NOT NULL)"
        ),
        Index("ix_case_history_run_sample", "run_id", "dataset_key", "sample_id", "attempt_no"),
    )


class AttemptInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: uuid.UUID
    dataset_key: str = Field(min_length=1, max_length=128)
    sample_id: str = Field(min_length=1, max_length=256)
    attempt_no: int = Field(ge=1)
    operation_key: str = Field(min_length=1, max_length=256)


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    input_tokens: int = Field(ge=0, le=1000000000)
    output_tokens: int = Field(ge=0, le=1000000000)


class AttemptEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    state: Literal["succeeded", "failed"]
    prompt: str = Field(max_length=4 * 1024 * 1024)
    response: str | None = Field(default=None, max_length=4 * 1024 * 1024)
    finish_reason: str | None = Field(default=None, max_length=128)
    error_code: str | None = Field(default=None, pattern=r"^[A-Z0-9_]{1,64}$")
    usage: TokenUsage | None = None
    # Do not accept headers, credentials, arbitrary upstream exception bodies or URLs.

    @model_validator(mode="after")
    def validate_outcome(self):
        if self.state == "succeeded" and (self.response is None or self.error_code is not None):
            raise ValueError(
                "Successful execution requires a response, including an empty string, and no execution error"
            )
        if self.state == "failed" and self.error_code is None:
            raise ValueError("Failed execution requires a categorized error code")
        return self


async def begin_attempt(sessions, body: AttemptInput):
    async with sessions() as session, session.begin():
        run = await session.scalar(
            select(EvaluationRun).where(EvaluationRun.id == body.run_id).with_for_update()
        )
        if run is None:
            raise ValueError("Run not found")
        old = await session.scalar(
            select(CaseExecutionRecord).where(
                CaseExecutionRecord.run_id == body.run_id,
                CaseExecutionRecord.operation_key == body.operation_key,
            )
        )
        if old:
            if (old.dataset_key, old.sample_id, old.attempt_no) != (
                body.dataset_key,
                body.sample_id,
                body.attempt_no,
            ):
                raise ValueError("Attempt operation key conflicts with existing execution")
            return old.id
        old_number = await session.scalar(
            select(CaseExecutionRecord.id).where(
                CaseExecutionRecord.run_id == body.run_id,
                CaseExecutionRecord.dataset_key == body.dataset_key,
                CaseExecutionRecord.sample_id == body.sample_id,
                CaseExecutionRecord.attempt_no == body.attempt_no,
            )
        )
        if old_number:
            raise ValueError("Attempt number already registered")
        row = CaseExecutionRecord(**body.model_dump(), state="started")
        session.add(row)
        await session.flush()
        return row.id


async def finish_attempt(sessions, settings, identifier: uuid.UUID, evidence: AttemptEvidence):
    raw = canonical_manifest(
        {
            "schema_version": "case-evidence.v1",
            "attempt_id": str(identifier),
            **evidence.model_dump(),
        }
    )
    digest = hashlib.sha256(raw).hexdigest()
    async with sessions() as session, session.begin():
        row = await session.scalar(
            select(CaseExecutionRecord)
            .where(CaseExecutionRecord.id == identifier)
            .with_for_update()
        )
        if row is None:
            raise ValueError("Attempt not found")
        if row.state != "started":
            if row.sha256 != digest:
                raise ValueError("Attempt result is immutable")
            await asyncio.to_thread(read_manifest, settings, row)
            return
        key = f"case-evidence/{row.run_id}/{row.id}/{digest}.json"
        await asyncio.to_thread(write_manifest, settings, key, raw)
        row.state, row.error_code = evidence.state, evidence.error_code
        row.finished_at = datetime.now(UTC)
        row.object_key, row.sha256 = key, digest


router = APIRouter(prefix="/api/autoeval/v1", tags=["case execution history"])


@router.get("/runs/{run_id}/case-executions")
async def history(
    run_id: uuid.UUID,
    request: Request,
    dataset_key: str | None = None,
    sample_id: str | None = None,
    offset: int = 0,
    limit: int = 50,
):
    if offset < 0 or not 1 <= limit <= 200:
        raise HTTPException(422, "Invalid pagination")
    try:
        async with request.app.state.sessions() as session:
            if await session.get(EvaluationRun, run_id) is None:
                raise HTTPException(404, "Run not found")
            query = select(CaseExecutionRecord).where(CaseExecutionRecord.run_id == run_id)
            if dataset_key is not None:
                query = query.where(CaseExecutionRecord.dataset_key == dataset_key)
            if sample_id is not None:
                query = query.where(CaseExecutionRecord.sample_id == sample_id)
            rows = (
                await session.scalars(
                    query.order_by(CaseExecutionRecord.created_at, CaseExecutionRecord.id)
                    .offset(offset)
                    .limit(limit + 1)
                )
            ).all()
            return {
                "data": [
                    {
                        "id": str(r.id),
                        "dataset_key": r.dataset_key,
                        "sample_id": r.sample_id,
                        "attempt_no": r.attempt_no,
                        "state": r.state,
                        "error_code": r.error_code,
                        "evidence_sha256": r.sha256,
                        "evidence_available": r.sha256 is not None,
                        "created_at": r.created_at.isoformat(),
                        "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                        "tool_metadata": r.tool_metadata,
                    }
                    for r in rows[:limit]
                ],
                "next_offset": offset + limit if len(rows) > limit else None,
                "request_id": request.state.request_id,
            }
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Case history database unavailable") from exc


@router.get("/case-executions/{attempt_id}/evidence")
async def evidence(attempt_id: uuid.UUID, request: Request):
    try:
        async with request.app.state.sessions() as session:
            row = await session.get(CaseExecutionRecord, attempt_id)
            if row is None:
                raise HTTPException(404, "Attempt not found")
            if row.sha256 is None:
                raise HTTPException(409, "Attempt has no confirmed outcome evidence")
            data = await asyncio.to_thread(read_manifest, request.app.state.settings, row)
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Case history database unavailable") from exc
    except (BotoCoreError, ClientError, ValueError) as exc:
        raise HTTPException(503, "Evidence missing or integrity check failed") from exc
    return {"data": data, "request_id": request.state.request_id}
