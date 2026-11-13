"""Development curation, independent of immutable evaluation reports."""

import hashlib
import json
import uuid
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator
from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from autoeval.audit import AuditEvent
from autoeval.evaluations import EvaluationReport
from autoeval.models import Base, Record, RunSnapshot
from autoeval.report_integrity import report_evidence_status


class LeaderboardRecord(Record, Base):
    __tablename__ = "leaderboard_record"
    source_key: Mapped[str] = mapped_column(String(128), unique=True)
    report_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("evaluation_report.id"))
    state: Mapped[str] = mapped_column(String(16))
    revision: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (
        CheckConstraint("state IN ('draft','approved','rejected','withdrawn')"),
        CheckConstraint("revision > 0"),
        Index("ix_leaderboard_record_state", "state", "created_at", "id"),
    )


class EntryInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, str_strip_whitespace=True)
    source_key: str = Field(pattern=r"^[a-zA-Z0-9_.:-]{1,128}$")
    source_type: Literal["report", "official", "manual"]
    model_name: str = Field(min_length=1, max_length=256)
    dataset: str = Field(min_length=1, max_length=128)
    metric: str = Field(min_length=1, max_length=64)
    value: float = Field(ge=0, strict=True)
    unit: str = Field(min_length=1, max_length=32)
    direction: Literal["higher_is_better", "lower_is_better"]
    scope: Literal["full", "lite"]
    conditions: dict[str, str | int | float] = Field(min_length=1, max_length=30)
    source_name: str = Field(min_length=1, max_length=128)
    source_url: HttpUrl | None = None
    source_version: str = Field(min_length=1, max_length=128)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_id: uuid.UUID | None = None
    notes: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def coherent(self):
        if self.unit == "%" and self.value > 100:
            raise ValueError("Percentage must be between zero and one hundred")
        if (self.source_type == "report") != (self.report_id is not None):
            raise ValueError("Report sources require a report reference")
        if self.source_type == "official" and (
            self.source_url is None or self.source_url.scheme != "https"
        ):
            raise ValueError("Official sources require an HTTPS source URL")
        if len(json.dumps(self.conditions)) > 8192:
            raise ValueError("Conditions too large")
        return self


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_revision: int = Field(ge=1)
    action: Literal["approve", "reject", "withdraw"]
    reason: str = Field(min_length=1, max_length=1000)


class EditInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    expected_revision: int = Field(ge=1)
    entry: EntryInput
    reason: str = Field(min_length=1, max_length=1000)


def development(request: Request):
    if request.app.state.settings.environment not in {"development", "test"}:
        raise HTTPException(403, "Data workbench is development-only")


router = APIRouter(
    prefix="/api/autoeval/v1/data-workbench",
    tags=["data workbench"],
    dependencies=[Depends(development)],
)


def view(row):
    return {
        "id": str(row.id),
        "state": row.state,
        "revision": row.revision,
        "created_at": row.created_at.isoformat(),
        "entry": row.payload,
    }


def audit(session, request, row, action, reason):
    session.add(
        AuditEvent(
            actor="local-operator",
            action=action,
            resource_type="leaderboard_record",
            resource_id=str(row.id),
            run_id=None,
            outcome=row.state,
            request_id=request.state.request_id,
            details={"revision": row.revision, "reason": reason, "entry": row.payload},
        )
    )


async def validate_report(request, session, entry):
    if entry.source_type != "report":
        return
    from autoeval.models import EvaluationRun

    report = await session.get(EvaluationReport, entry.report_id)
    if report is None:
        raise HTTPException(404, "Report not found")
    run = await session.get(EvaluationRun, report.run_id)
    snapshot = await session.scalar(select(RunSnapshot).where(RunSnapshot.run_id == report.run_id))
    if (
        report.validity != "complete"
        or report.execution_origin != "real"
        or snapshot is None
        or run.trigger_scope != "local-operator:evaluation"
        or await report_evidence_status(request.app.state.settings, report, run, snapshot)
        != "verified"
    ):
        raise HTTPException(409, "Report is incomplete or original evidence is unavailable")
    frozen = snapshot.canonical_body
    expected_conditions = {
        "manifest_sha256": frozen["manifest_sha256"],
        "tool_revision": frozen["tool"]["revision"],
        **frozen["parameters"],
    }
    if (
        entry.source_key != "report-" + str(report.id)
        or entry.source_name != "AutoEval"
        or entry.model_name != frozen["remote_model"]
        or entry.dataset != "GPQA"
        or entry.metric != "accuracy"
        or entry.unit != "%"
        or entry.scope != "lite"
        or entry.direction != "higher_is_better"
        or entry.value != report.summary.get("accuracy")
        or entry.conditions != expected_conditions
        or entry.source_sha256 != report.sha256
        or entry.source_version != str(report.id)
    ):
        raise HTTPException(422, "Report entry differs from original score or frozen conditions")
    return frozen


@router.get("/entries")
async def entries(request: Request, offset: int = 0, limit: int = 20, include_test: bool = False):
    if offset < 0 or not 1 <= limit <= 100:
        raise HTTPException(422, "Invalid pagination")
    async with request.app.state.sessions() as session:
        rows = (
            await session.scalars(
                select(LeaderboardRecord)
                .where(
                    True
                    if include_test
                    else ~LeaderboardRecord.source_key.op("~")("^test-[a-f0-9]{32}$")
                )
                .order_by(LeaderboardRecord.created_at.desc(), LeaderboardRecord.id.desc())
                .offset(offset)
                .limit(limit + 1)
            )
        ).all()
        return {
            "data": [view(row) for row in rows[:limit]],
            "next_offset": offset + limit if len(rows) > limit else None,
        }


@router.post("/entries", status_code=201)
async def create(body: EntryInput, request: Request):
    async with request.app.state.sessions() as session, session.begin():
        # Serialize source identity to make import retries safe across processes.
        from sqlalchemy import text

        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": "leaderboard:" + body.source_key},
        )
        payload = body.model_dump(mode="json")
        existing = await session.scalar(
            select(LeaderboardRecord).where(LeaderboardRecord.source_key == body.source_key)
        )
        if existing:
            if existing.payload != payload:
                raise HTTPException(409, "Source key already exists with different content")
            return {"data": view(existing)}
        await validate_report(request, session, body)
        row = LeaderboardRecord(
            source_key=body.source_key,
            report_id=body.report_id,
            state="draft",
            revision=1,
            payload=payload,
        )
        session.add(row)
        await session.flush()
        audit(session, request, row, "leaderboard_created", "录入待评审数据")
        return {"data": view(row)}


@router.post("/entries/{entry_id}/edit")
async def edit(entry_id: uuid.UUID, body: EditInput, request: Request):
    async with request.app.state.sessions() as session, session.begin():
        row = await session.scalar(
            select(LeaderboardRecord).where(LeaderboardRecord.id == entry_id).with_for_update()
        )
        if row is None:
            raise HTTPException(404, "Entry not found")
        if row.revision != body.expected_revision or row.state == "approved":
            raise HTTPException(409, "Version conflict or approved record: withdraw before editing")
        if body.entry.source_key != row.source_key or body.entry.report_id != row.report_id:
            raise HTTPException(422, "Source identity cannot be changed")
        await validate_report(request, session, body.entry)
        row.payload = body.entry.model_dump(mode="json")
        row.state = "draft"
        row.revision += 1
        audit(session, request, row, "leaderboard_edited", body.reason)
        return {"data": view(row)}


@router.post("/entries/{entry_id}/review")
async def review(entry_id: uuid.UUID, body: Decision, request: Request):
    async with request.app.state.sessions() as session, session.begin():
        row = await session.scalar(
            select(LeaderboardRecord).where(LeaderboardRecord.id == entry_id).with_for_update()
        )
        if row is None:
            raise HTTPException(404, "Entry not found")
        if row.revision != body.expected_revision:
            raise HTTPException(409, "Version conflict; reload before review")
        allowed = {
            "approve": ("draft", "approved"),
            "reject": ("draft", "rejected"),
            "withdraw": ("approved", "withdrawn"),
        }
        before, after = allowed[body.action]
        if row.state != before:
            raise HTTPException(409, "Invalid review transition")
        if after == "approved":
            await validate_report(request, session, EntryInput.model_validate(row.payload))
        row.state = after
        row.revision += 1
        audit(session, request, row, "leaderboard_" + body.action, body.reason)
        return {"data": view(row)}


def best_per_model(rows, direction):
    """Input is newest first; equal scores retain the newest immutable source."""
    selected = {}
    for row in rows:
        key = row.get("model_id") or row["entry"]["model_name"]
        previous = selected.get(key)
        value = row["entry"]["value"]
        if previous is None or (
            value > previous["entry"]["value"]
            if direction == "higher_is_better"
            else value < previous["entry"]["value"]
        ):
            selected[key] = row
    return list(selected.values())


@router.get("/published")
async def published(request: Request, selection: Literal["best", "all"] = "best"):
    async with request.app.state.sessions() as session:
        rows = (
            await session.scalars(
                select(LeaderboardRecord)
                .where(LeaderboardRecord.state == "approved")
                .order_by(LeaderboardRecord.created_at.desc(), LeaderboardRecord.id.desc())
            )
        ).all()
        groups = {}
        for row in rows:
            entry = EntryInput.model_validate(row.payload)
            frozen = None
            if entry.source_type == "report":
                try:
                    frozen = await validate_report(request, session, entry)
                except HTTPException:
                    continue
            identity = {
                key: row.payload[key]
                for key in (
                    "dataset",
                    "metric",
                    "unit",
                    "direction",
                    "scope",
                    "conditions",
                    "source_type",
                    "source_name",
                    "source_version",
                )
            }
            # Report IDs are provenance, not comparison groups.
            if entry.source_type == "report":
                identity.pop("source_version")
                identity["config_version_id"] = frozen.get("config_version_id") if frozen else None
            if any(
                str(value).strip().lower() in {"unknown", "未知", "未提供", "待确认"}
                for value in entry.conditions.values()
            ):
                identity["unconfirmed_condition_record"] = str(row.id)
            digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            group = groups.setdefault(digest, {"id": digest, "comparison": identity, "entries": []})
            item = view(row)
            item["model_id"] = frozen.get("model_version_id") if frozen else entry.model_name
            item["config_version_id"] = frozen.get("config_version_id") if frozen else None
            group["entries"].append(item)
        for group in groups.values():
            group["total_records"] = len(group["entries"])
            if selection == "best":
                group["entries"] = best_per_model(
                    group["entries"], group["comparison"]["direction"]
                )
            group["entries"].sort(
                key=lambda v: v["entry"]["value"],
                reverse=group["comparison"]["direction"] == "higher_is_better",
            )
            previous, rank = None, 0
            for position, item in enumerate(group["entries"], 1):
                if item["entry"]["value"] != previous:
                    rank = position
                item["rank"] = rank
                previous = item["entry"]["value"]
        return {"data": list(groups.values())}
