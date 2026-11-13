"""Versioned configuration use cases; no in-memory persistence fallback."""

import hashlib
import json
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_serializer
from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Mapped, mapped_column

from autoeval.models import Base, Record


class TestConfigVersion(Record, Base):
    __tablename__ = "test_config_version"
    config_key: Mapped[str] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(256))
    version: Mapped[int] = mapped_column(Integer)
    version_label: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16))
    content_hash: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(256))
    source_url: Mapped[str | None] = mapped_column(String(2048))
    source_revision: Mapped[str | None] = mapped_column(String(256))
    source_sha256: Mapped[str | None] = mapped_column(String(64))
    approved_by: Mapped[str | None] = mapped_column(String(256))
    __table_args__ = (
        UniqueConstraint("config_key", "version"),
        UniqueConstraint("config_key", "version_label"),
        CheckConstraint("version > 0"),
        CheckConstraint("kind IN ('standard','custom')"),
        CheckConstraint(
            "(kind = 'standard' AND source_url IS NOT NULL AND source_revision IS NOT NULL "
            "AND source_sha256 IS NOT NULL) OR "
            "(kind = 'custom' AND source_url IS NULL AND source_revision IS NULL "
            "AND source_sha256 IS NULL AND approved_by IS NULL)",
            name="ck_standard_config_metadata",
        ),
        CheckConstraint(
            "(source_url IS NULL OR source_url LIKE 'https://%') AND "
            "(source_revision IS NULL OR length(source_revision) > 0) AND "
            "(source_sha256 IS NULL OR source_sha256 ~ '^[0-9a-f]{64}$') AND "
            "(approved_by IS NULL OR length(approved_by) > 0)",
            name="ck_standard_config_source_values",
        ),
    )


class TestConfigItem(Record, Base):
    __tablename__ = "test_config_item"
    config_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("test_config_version.id", ondelete="RESTRICT")
    )
    dataset_key: Mapped[str] = mapped_column(String(128))
    parameter_values: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("config_version_id", "dataset_key"),)


class Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    output: int = Field(ge=1, le=1048576)
    concurrency: int = Field(ge=1, le=100000)
    temperature: float = Field(ge=0, le=2)
    top_p: float = Field(gt=0, le=1)
    scope: str = Field(pattern="^(全量|Lite)$")
    adapter_input: dict | None = None

    @field_validator("adapter_input")
    @classmethod
    def validate_adapter_input(cls, value):
        if value is not None:
            from autoeval.standard_inputs import validate_config

            validate_config(value)
        return value

    @model_serializer(mode="wrap")
    def serialize_parameters(self, handler):
        result = handler(self)
        if self.adapter_input is None:
            result.pop("adapter_input", None)
        return result


class CreateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config_key: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    display_name: str = Field(min_length=1, max_length=256)
    version_label: str = Field(min_length=1, max_length=128)
    items: dict[str, Parameters] = Field(min_length=1, max_length=100)
    expected_version: int = Field(ge=0)
    profile_bindings: dict[str, uuid.UUID] = Field(default_factory=dict, max_length=100)


router = APIRouter(prefix="/api/autoeval/v1/test-configs", tags=["configuration"])


@router.get("/{config_id}/execution-spec")
async def execution_spec(config_id: uuid.UUID, request: Request):
    from autoeval.datasets import DatasetManifest
    from autoeval.profiles import ConfigDatasetBinding, EvaluationProfile

    try:
        async with request.app.state.sessions() as session:
            config = await session.get(TestConfigVersion, config_id)
            if config is None:
                raise HTTPException(404, "Configuration not found")
            items = (
                await session.scalars(
                    select(TestConfigItem)
                    .where(TestConfigItem.config_version_id == config_id)
                    .order_by(TestConfigItem.dataset_key)
                )
            ).all()
            evidence = []
            missing = []
            for item in items:
                binding = await session.scalar(
                    select(ConfigDatasetBinding).where(
                        ConfigDatasetBinding.config_item_id == item.id
                    )
                )
                if binding is None:
                    missing.append(item.dataset_key)
                    continue
                profile = await session.get(EvaluationProfile, binding.profile_id)
                manifest = await session.get(DatasetManifest, binding.manifest_id)
                evidence.append(
                    {
                        "dataset_name": item.dataset_key,
                        "parameters": item.parameter_values,
                        "profile_id": str(profile.id),
                        "profile_sha256": profile.sha256,
                        "tool": profile.contract,
                        "manifest_id": str(manifest.id),
                        "manifest_sha256": manifest.sha256,
                        "sample_count": manifest.sample_count,
                        "scope": manifest.scope,
                    }
                )
            frozen = {
                "schema_version": "config-spec.v1",
                "config_version_id": str(config.id),
                "config_sha256": config.content_hash,
                "items": evidence,
            }
            return {
                "data": {
                    "spec": frozen,
                    "sha256": hashlib.sha256(
                        json.dumps(
                            frozen, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                        ).encode()
                    ).hexdigest(),
                    "bindings_complete": not missing,
                    "missing_profiles": missing,
                    "execution_enabled": False,
                    "blocking_reasons": (["missing_profile_bindings"] if missing else [])
                    + ["tool_adapter_not_verified"],
                    "gate_status": "not_applicable",
                },
                "request_id": request.state.request_id,
            }
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Configuration database unavailable") from exc


@router.get("")
async def list_configs(
    request: Request, offset: int = 0, limit: int = 100, config_id: uuid.UUID | None = None
):
    from autoeval.profiles import ConfigDatasetBinding

    if offset < 0 or not 1 <= limit <= 200:
        raise HTTPException(422, "Invalid pagination")
    try:
        async with request.app.state.sessions() as session:
            rows = (
                await session.scalars(
                    select(TestConfigVersion)
                    .where(TestConfigVersion.id == config_id if config_id else True)
                    .order_by(TestConfigVersion.created_at.desc(), TestConfigVersion.id.desc())
                    .offset(offset)
                    .limit(limit + 1)
                )
            ).all()
            versions = rows[:limit]
            ids = [v.id for v in versions]
            items = (
                (
                    await session.scalars(
                        select(TestConfigItem).where(TestConfigItem.config_version_id.in_(ids))
                    )
                ).all()
                if ids
                else []
            )
            data = [
                {
                    "id": str(v.id),
                    "config_key": v.config_key,
                    "display_name": v.display_name,
                    "version": v.version,
                    "version_label": v.version_label,
                    "kind": v.kind,
                    "content_hash": v.content_hash,
                    "source_url": v.source_url,
                    "source_revision": v.source_revision,
                    "source_sha256": v.source_sha256,
                    "approved_by": v.approved_by,
                    "approval_status": "not_required",
                    "items": {
                        i.dataset_key: i.parameter_values
                        for i in items
                        if i.config_version_id == v.id
                    },
                }
                for v in versions
            ]
            item_lookup = {item.id: item for item in items}
            binding_rows = (
                (
                    await session.scalars(
                        select(ConfigDatasetBinding).where(
                            ConfigDatasetBinding.config_item_id.in_(list(item_lookup))
                        )
                    )
                ).all()
                if item_lookup
                else []
            )
            by_version = {}
            for binding in binding_rows:
                item = item_lookup[binding.config_item_id]
                by_version.setdefault(str(item.config_version_id), {})[item.dataset_key] = str(
                    binding.profile_id
                )
            for entry in data:
                entry["profile_bindings"] = by_version.get(entry["id"], {})
    except SQLAlchemyError as exc:
        raise HTTPException(
            503, "Configuration database unavailable or migration required"
        ) from exc
    return {
        "data": data,
        "next_offset": offset + limit if len(rows) > limit else None,
        "request_id": request.state.request_id,
    }


@router.post("", status_code=201)
async def create_version(body: CreateConfig, request: Request):
    from autoeval.datasets import DatasetManifest
    from autoeval.profiles import ConfigDatasetBinding, EvaluationProfile

    # The first configuration API writes custom revisions only. Standard source
    # import will be a separate privileged command; never trust client kind.
    values = {k: v.model_dump() for k, v in body.items.items()}
    if any(
        k not in {"GPQA", "GPQA_Diamond", "MMMU-Pro", "DeepSWE", "AutomationBench"} for k in values
    ):
        raise HTTPException(422, "Unknown dataset")
    if body.profile_bindings and body.profile_bindings.keys() != values.keys():
        raise HTTPException(422, "Profile bindings must cover every configured dataset")
    digest = hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    try:
        async with request.app.state.sessions() as session, session.begin():
            bindings = {}
            for dataset, profile_id in body.profile_bindings.items():
                profile = await session.get(EvaluationProfile, profile_id)
                if profile is None or profile.dataset_name != dataset:
                    raise HTTPException(422, "Profile dataset mismatch or missing profile")
                manifest_id = (
                    profile.lite_manifest_id
                    if values[dataset]["scope"] == "Lite"
                    else profile.full_manifest_id
                )
                if manifest_id is None:
                    raise HTTPException(422, "Lite manifest is not configured")
                manifest = await session.get(DatasetManifest, manifest_id)
                bindings[dataset] = {
                    "profile_id": str(profile.id),
                    "profile_sha256": profile.sha256,
                    "manifest_id": str(manifest.id),
                    "manifest_sha256": manifest.sha256,
                }
            if bindings:
                digest = hashlib.sha256(
                    json.dumps(
                        {"parameters": values, "bindings": bindings},
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode()
                ).hexdigest()
            latest = await session.scalar(
                select(TestConfigVersion)
                .where(TestConfigVersion.config_key == body.config_key)
                .order_by(TestConfigVersion.version.desc())
                .limit(1)
                .with_for_update()
            )
            if latest and latest.kind == "standard":
                raise HTTPException(403, "Standard configurations are read-only")
            if latest and not body.profile_bindings:
                has_binding = await session.scalar(
                    select(ConfigDatasetBinding.id)
                    .join(TestConfigItem, TestConfigItem.id == ConfigDatasetBinding.config_item_id)
                    .where(TestConfigItem.config_version_id == latest.id)
                    .limit(1)
                )
                if has_binding:
                    raise HTTPException(
                        409,
                        "This configuration has execution bindings; explicitly preserve or replace all profiles",
                    )
            if (latest.version if latest else 0) != body.expected_version:
                raise HTTPException(409, "Configuration version changed; refresh before saving")
            version = TestConfigVersion(
                config_key=body.config_key,
                display_name=body.display_name,
                version=body.expected_version + 1,
                version_label=body.version_label,
                kind="custom",
                content_hash=digest,
                created_by="local-operator",
            )
            session.add(version)
            await session.flush()
            for dataset, params in values.items():
                item = TestConfigItem(
                    config_version_id=version.id, dataset_key=dataset, parameter_values=params
                )
                session.add(item)
                await session.flush()
                if dataset in bindings:
                    session.add(
                        ConfigDatasetBinding(
                            config_item_id=item.id,
                            profile_id=uuid.UUID(bindings[dataset]["profile_id"]),
                            manifest_id=uuid.UUID(bindings[dataset]["manifest_id"]),
                        )
                    )
            result = {
                "id": str(version.id),
                "config_key": version.config_key,
                "version": version.version,
                "content_hash": digest,
            }
    except IntegrityError as exc:
        raise HTTPException(
            409, "Version or label already exists; existing version was preserved"
        ) from exc
    except SQLAlchemyError as exc:
        raise HTTPException(
            503, "Configuration database unavailable or migration required"
        ) from exc
    return {"data": result, "request_id": request.state.request_id}
