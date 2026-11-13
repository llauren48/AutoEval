"""Immutable dataset manifests: sample identifiers and checksums, never sample bodies."""

import asyncio
import hashlib
import json
import uuid
from typing import Literal

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import CheckConstraint, ForeignKey, Index, String, UniqueConstraint, select, text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Mapped, mapped_column

from autoeval.models import Base, Record
from autoeval.registry import Asset, AssetVersion


class DatasetManifest(Record, Base):
    __tablename__ = "dataset_manifest"
    dataset_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("asset_version.id", ondelete="RESTRICT")
    )
    manifest_key: Mapped[str] = mapped_column(String(128))
    scope: Mapped[str] = mapped_column(String(8))
    parent_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("dataset_manifest.id", ondelete="RESTRICT")
    )
    sha256: Mapped[str] = mapped_column(String(64))
    object_key: Mapped[str] = mapped_column(String(256))
    sample_count: Mapped[int]
    __table_args__ = (
        UniqueConstraint("dataset_version_id", "manifest_key"),
        Index(
            "uq_dataset_full_manifest",
            "dataset_version_id",
            unique=True,
            postgresql_where=text("scope = 'full'"),
        ),
        CheckConstraint("sample_count > 0"),
        CheckConstraint(
            "(scope = 'full' AND parent_id IS NULL) OR (scope = 'lite' AND parent_id IS NOT NULL)"
        ),
    )


class SampleRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sample_id: str = Field(min_length=1, max_length=256, pattern=r"^[^\x00-\x1f\x7f]+$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ManifestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset_key: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,128}$")
    name: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=128)
    manifest_key: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,128}$")
    scope: Literal["full", "lite"]
    parent_id: uuid.UUID | None = None
    samples: list[SampleRef] = Field(min_length=1, max_length=20000)

    @model_validator(mode="after")
    def validate_members(self):
        if (self.scope == "lite") != (self.parent_id is not None):
            raise ValueError("Lite requires a full parent; full cannot have a parent")
        if len({sample.sample_id for sample in self.samples}) != len(self.samples):
            raise ValueError("Duplicate sample identifiers")
        return self


def canonical_manifest(body):
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()


def storage(settings):
    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key.get_secret_value(),
        aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
        region_name="us-east-1",
        config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1}),
    )


def read_manifest(settings, row):
    client = storage(settings)
    try:
        response = client.get_object(Bucket=settings.s3_bucket, Key=row.object_key)
        try:
            raw = response["Body"].read(16 * 1024 * 1024 + 1)
        finally:
            response["Body"].close()
        if len(raw) > 16 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != row.sha256:
            raise ValueError("Manifest integrity mismatch")
        return json.loads(raw)
    finally:
        client.close()


def write_manifest(settings, key, raw):
    client = storage(settings)
    try:
        client.put_object(
            Bucket=settings.s3_bucket, Key=key, Body=raw, ContentType="application/json"
        )
    finally:
        client.close()


def summary(row):
    return {
        "id": str(row.id),
        "dataset_version_id": str(row.dataset_version_id),
        "manifest_key": row.manifest_key,
        "scope": row.scope,
        "sample_count": row.sample_count,
        "parent_id": str(row.parent_id) if row.parent_id else None,
        "sha256": row.sha256,
    }


router = APIRouter(prefix="/api/autoeval/v1/dataset-manifests", tags=["dataset manifests"])


@router.get("/{manifest_id}")
async def manifest_detail(manifest_id: uuid.UUID, request: Request):
    try:
        async with request.app.state.sessions() as session:
            row = await session.get(DatasetManifest, manifest_id)
            if row is None:
                raise HTTPException(404, "Manifest not found")
            version = await session.get(AssetVersion, row.dataset_version_id)
            asset = await session.get(Asset, version.asset_id)
            metadata = version.metadata_json
            data = {
                **summary(row),
                "dataset_key": asset.asset_key,
                "dataset_name": asset.name,
                "version": version.version,
                "native_content_registered": metadata.get("schema_version") == "dataset-content.v1",
                "bundle_sha256": metadata.get("sha256"),
                "source": metadata.get("source"),
                "tool_revision": metadata.get("tool_revision"),
                "approval_status": metadata.get("approval_status", "not_approved"),
            }
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Dataset database unavailable") from exc
    return {"data": data, "request_id": request.state.request_id}


@router.post("", status_code=201)
async def register(body: ManifestInput, request: Request):
    frozen = body.model_dump(mode="json")
    # Ordering is part of execution identity, including the full parent identifier.
    raw = canonical_manifest(frozen)
    digest = hashlib.sha256(raw).hexdigest()
    object_key = f"dataset-manifests/{digest}.json"
    settings = request.app.state.settings
    try:
        async with request.app.state.sessions() as session, session.begin():
            asset = await session.scalar(
                select(Asset).where(Asset.asset_key == body.dataset_key).with_for_update()
            )
            if asset is None:
                asset = Asset(asset_key=body.dataset_key, name=body.name, kind="dataset")
                session.add(asset)
                await session.flush()
            elif asset.kind != "dataset" or asset.name != body.name:
                raise HTTPException(409, "Dataset identity conflict")
            version = await session.scalar(
                select(AssetVersion).where(
                    AssetVersion.asset_id == asset.id, AssetVersion.version == body.version
                )
            )
            if version is None:
                version = AssetVersion(asset_id=asset.id, version=body.version, metadata_json={})
                session.add(version)
                await session.flush()
            old = await session.scalar(
                select(DatasetManifest).where(
                    DatasetManifest.dataset_version_id == version.id,
                    DatasetManifest.manifest_key == body.manifest_key,
                )
            )
            if old:
                if old.sha256 != digest:
                    raise HTTPException(
                        409, "Manifest revision is immutable; use a new key or version"
                    )
                await asyncio.to_thread(read_manifest, settings, old)
                return {"data": summary(old), "request_id": request.state.request_id}
            if body.scope == "lite":
                parent = await session.get(DatasetManifest, body.parent_id)
                if (
                    parent is None
                    or parent.scope != "full"
                    or parent.dataset_version_id != version.id
                ):
                    raise HTTPException(
                        422, "Lite parent must be a full manifest of the same dataset version"
                    )
                parent_data = await asyncio.to_thread(read_manifest, settings, parent)
                members = {s["sample_id"]: s["sha256"] for s in parent_data["samples"]}
                if any(members.get(s.sample_id) != s.sha256 for s in body.samples):
                    raise HTTPException(422, "Lite contains a missing or changed sample")
            # Object precedes metadata commit. Failure can leave an unreferenced object;
            # never delete by hash on rollback because another committed row may use it.
            await asyncio.to_thread(write_manifest, settings, object_key, raw)
            row = DatasetManifest(
                dataset_version_id=version.id,
                manifest_key=body.manifest_key,
                scope=body.scope,
                parent_id=body.parent_id,
                sha256=digest,
                object_key=object_key,
                sample_count=len(body.samples),
            )
            session.add(row)
            await session.flush()
            data = summary(row)
    except IntegrityError as exc:
        raise HTTPException(409, "Concurrent registration; retry identical content") from exc
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Dataset database unavailable") from exc
    except (BotoCoreError, ClientError, ValueError) as exc:
        raise HTTPException(503, "Manifest storage unavailable or integrity check failed") from exc
    return {"data": data, "request_id": request.state.request_id}


@router.get("")
async def list_manifests(
    request: Request, offset: int = 0, limit: int = 50, include_test: bool = False
):
    if offset < 0 or not 1 <= limit <= 200:
        raise HTTPException(422, "Invalid pagination")
    try:
        async with request.app.state.sessions() as session:
            rows = (
                await session.scalars(
                    select(DatasetManifest)
                    .where(
                        True
                        if include_test
                        else DatasetManifest.dataset_version_id.in_(
                            select(AssetVersion.id)
                            .join(Asset, Asset.id == AssetVersion.asset_id)
                            .where(
                                ~Asset.asset_key.op("~")(
                                    r"^(fixture-|browser-|model-browser-|navigation-|pipeline-|TEST-)"
                                )
                            )
                        )
                    )
                    .order_by(DatasetManifest.created_at.desc(), DatasetManifest.id.desc())
                    .offset(offset)
                    .limit(limit + 1)
                )
            ).all()
            return {
                "data": [summary(row) for row in rows[:limit]],
                "next_offset": offset + limit if len(rows) > limit else None,
                "request_id": request.state.request_id,
            }
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Dataset database unavailable") from exc
