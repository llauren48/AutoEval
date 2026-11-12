"""Versioned evaluation profiles. Registration does not approve or verify a tool."""

import hashlib
import uuid
from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import ForeignKey, String, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Mapped, mapped_column

from autoeval.datasets import DatasetManifest, canonical_manifest
from autoeval.models import Base, Record


class EvaluationProfile(Record, Base):
    __tablename__ = "evaluation_profile"
    profile_key: Mapped[str] = mapped_column(String(128))
    version: Mapped[str] = mapped_column(String(128))
    dataset_name: Mapped[str] = mapped_column(String(128))
    full_manifest_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_manifest.id", ondelete="RESTRICT")
    )
    lite_manifest_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dataset_manifest.id", ondelete="RESTRICT")
    )
    contract: Mapped[dict] = mapped_column(JSONB)
    sha256: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("profile_key", "version"),)


class ConfigDatasetBinding(Record, Base):
    __tablename__ = "config_dataset_binding"
    config_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("test_config_item.id", ondelete="RESTRICT"), unique=True
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("evaluation_profile.id", ondelete="RESTRICT")
    )
    manifest_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("dataset_manifest.id", ondelete="RESTRICT")
    )


class ToolContract(BaseModel):
    model_config = ConfigDict(extra="forbid")
    family: Literal["aisbench", "harbor"]
    source_url: str = Field(pattern=r"^https://[^\s]+$", max_length=1024)
    revision: str = Field(pattern=r"^[0-9a-f]{40}([0-9a-f]{24})?$")
    scorer_reference: str = Field(min_length=1, max_length=1024)
    scorer_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    parallelism_owner: Literal["tool", "platform"]


class ProfileInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile_key: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,128}$")
    version: str = Field(min_length=1, max_length=128)
    dataset_name: Literal["GPQA", "MMMU-Pro", "DeepSWE", "AutomationBench"]
    full_manifest_id: uuid.UUID
    lite_manifest_id: uuid.UUID | None = None
    tool: ToolContract


def profile_data(row):
    return {
        "id": str(row.id),
        "profile_key": row.profile_key,
        "version": row.version,
        "dataset_name": row.dataset_name,
        "full_manifest_id": str(row.full_manifest_id),
        "lite_manifest_id": str(row.lite_manifest_id) if row.lite_manifest_id else None,
        "tool": row.contract,
        "sha256": row.sha256,
        "tool_verified": False,
    }


router = APIRouter(prefix="/api/autoeval/v1/profiles", tags=["evaluation profiles"])


@router.post("", status_code=201)
async def register_profile(body: ProfileInput, request: Request):
    digest = hashlib.sha256(canonical_manifest(body.model_dump(mode="json"))).hexdigest()
    try:
        async with request.app.state.sessions() as session, session.begin():
            old = await session.scalar(
                select(EvaluationProfile).where(
                    EvaluationProfile.profile_key == body.profile_key,
                    EvaluationProfile.version == body.version,
                )
            )
            if old:
                if old.sha256 != digest:
                    raise HTTPException(409, "Profile is immutable; create a new version")
                return {"data": profile_data(old), "request_id": request.state.request_id}
            full = await session.get(DatasetManifest, body.full_manifest_id)
            if full is None or full.scope != "full":
                raise HTTPException(422, "Full manifest is required")
            if body.lite_manifest_id:
                lite = await session.get(DatasetManifest, body.lite_manifest_id)
                if lite is None or lite.scope != "lite" or lite.parent_id != full.id:
                    raise HTTPException(422, "Lite must belong to the selected full manifest")
            if (body.dataset_name in {"DeepSWE", "AutomationBench"}) != (body.tool.family == "harbor"):
                raise HTTPException(422, "Dataset and tool family do not match")
            row = EvaluationProfile(
                profile_key=body.profile_key,
                version=body.version,
                dataset_name=body.dataset_name,
                full_manifest_id=full.id,
                lite_manifest_id=body.lite_manifest_id,
                contract=body.tool.model_dump(),
                sha256=digest,
            )
            session.add(row)
            await session.flush()
            data = profile_data(row)
    except IntegrityError as exc:
        raise HTTPException(
            409, "Concurrent profile registration; retry identical content"
        ) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Profile database unavailable") from exc
    return {"data": data, "request_id": request.state.request_id}


@router.get("")
async def list_profiles(request: Request, offset: int = 0, limit: int = 50):
    if offset < 0 or not 1 <= limit <= 200:
        raise HTTPException(422, "Invalid pagination")
    try:
        async with request.app.state.sessions() as session:
            rows = (
                await session.scalars(
                    select(EvaluationProfile)
                    .order_by(EvaluationProfile.created_at.desc(), EvaluationProfile.id.desc())
                    .offset(offset)
                    .limit(limit + 1)
                )
            ).all()
            return {
                "data": [profile_data(row) for row in rows[:limit]],
                "next_offset": offset + limit if len(rows) > limit else None,
                "request_id": request.state.request_id,
            }
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Profile database unavailable") from exc
