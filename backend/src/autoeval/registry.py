"""Single-workspace model registry and optimistic default configuration pointer."""

import uuid

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import ForeignKey, Integer, String, UniqueConstraint, func, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Mapped, mapped_column

from autoeval.configuration import TestConfigItem, TestConfigVersion
from autoeval.models import Base, Record


class Asset(Record, Base):
    __tablename__ = "asset"
    asset_key: Mapped[str] = mapped_column(String(128), unique=True)
    kind: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(256))


class AssetVersion(Record, Base):
    __tablename__ = "asset_version"
    asset_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("asset.id", ondelete="RESTRICT"))
    version: Mapped[str] = mapped_column(String(128))
    metadata_json: Mapped[dict] = mapped_column("metadata", JSONB)
    __table_args__ = (UniqueConstraint("asset_id", "version"),)


class ModelConfigBinding(Record, Base):
    __tablename__ = "model_config_binding"
    model_asset_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("asset.id", ondelete="RESTRICT"), unique=True
    )
    default_config_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("test_config_version.id", ondelete="RESTRICT")
    )
    row_version: Mapped[int] = mapped_column(Integer, server_default="1")


class ModelCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_id: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_.-]+$")
    name: str = Field(min_length=1, max_length=256)
    version: str = Field(min_length=1, max_length=128)


class DefaultConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config_version_id: uuid.UUID


router = APIRouter(prefix="/api/autoeval/v1/models", tags=["models"])


@router.get("/{model_id}/versions")
async def model_versions(model_id: uuid.UUID, request: Request):
    try:
        async with request.app.state.sessions() as session:
            model = await session.get(Asset, model_id)
            if model is None or model.kind != "model":
                raise HTTPException(404, "Model not found")
            rows = (
                await session.scalars(
                    select(AssetVersion)
                    .where(AssetVersion.asset_id == model_id)
                    .order_by(AssetVersion.created_at.desc())
                )
            ).all()
            data = [{"id": str(v.id), "version": v.version} for v in rows]
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Model database unavailable") from exc
    return {"data": data, "request_id": request.state.request_id}


@router.get("/{model_id}/default-test-config")
async def get_default(model_id: uuid.UUID, request: Request):
    try:
        async with request.app.state.sessions() as session:
            model = await session.get(Asset, model_id)
            if model is None or model.kind != "model":
                raise HTTPException(404, "Model not found")
            binding = await session.scalar(
                select(ModelConfigBinding).where(ModelConfigBinding.model_asset_id == model_id)
            )
            if binding is None:
                raise HTTPException(404, "No default configuration configured")
            config = await session.get(TestConfigVersion, binding.default_config_version_id)
            items = (
                await session.scalars(
                    select(TestConfigItem).where(TestConfigItem.config_version_id == config.id)
                )
            ).all()
            data = {
                "model_id": model.asset_key,
                "config_version_id": str(config.id),
                "config_key": config.config_key,
                "version_label": config.version_label,
                "binding_revision": binding.row_version,
                "items": {i.dataset_key: i.parameter_values for i in items},
            }
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Model database unavailable or migration required") from exc
    return {"data": data, "request_id": request.state.request_id}


class CatalogRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    catalog_id: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=128)


@router.post("/from-catalog")
async def register_catalog_model(body: CatalogRegistration, request: Request):
    """Register identity from persisted official evidence, never invent a service."""
    from autoeval.data_workbench import LeaderboardRecord, development

    development(request)
    async with request.app.state.sessions() as session, session.begin():
        records = (
            await session.scalars(
                select(LeaderboardRecord).where(
                    LeaderboardRecord.payload["source_type"].astext == "official",
                    LeaderboardRecord.state == "approved",
                    func.lower(
                        LeaderboardRecord.payload["conditions"]["modelscope_model_id"].astext
                    )
                    == body.catalog_id.lower(),
                )
            )
        ).all()
        if not records:
            raise HTTPException(422, "No reviewed official catalog evidence for this model")
        canonical = records[0].payload["conditions"]["modelscope_model_id"]
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": "catalog-model:" + canonical.lower()},
        )
        matches = (
            await session.scalars(
                select(Asset).where(func.lower(Asset.asset_key) == canonical.lower())
            )
        ).all()
        if len(matches) > 1 or (matches and matches[0].kind != "model"):
            raise HTTPException(409, "Ambiguous model identity; select explicitly")
        asset = (
            matches[0]
            if matches
            else Asset(asset_key=canonical, kind="model", name=canonical.split("/")[-1])
        )
        if not matches:
            session.add(asset)
            await session.flush()
        version = await session.scalar(
            select(AssetVersion).where(
                AssetVersion.asset_id == asset.id, AssetVersion.version == "catalog-identity.v1"
            )
        )
        if version is None:
            version = AssetVersion(
                asset_id=asset.id,
                version="catalog-identity.v1",
                metadata_json={
                    "identity_only": True,
                    "catalog_id": canonical,
                    "source_record_ids": sorted(str(r.id) for r in records),
                },
            )
            session.add(version)
            await session.flush()
        return {
            "data": {
                "id": str(asset.id),
                "model_id": asset.asset_key,
                "version_id": str(version.id),
                "connection_state": "not_configured",
                "execution_enabled": False,
            },
            "request_id": request.state.request_id,
        }


@router.post("", status_code=201)
async def register_model(body: ModelCreate, request: Request):
    try:
        async with request.app.state.sessions() as session, session.begin():
            asset = Asset(asset_key=body.model_id, kind="model", name=body.name)
            session.add(asset)
            await session.flush()
            version = AssetVersion(asset_id=asset.id, version=body.version, metadata_json={})
            session.add(version)
            await session.flush()
            data = {
                "id": str(asset.id),
                "model_id": asset.asset_key,
                "version_id": str(version.id),
                "connection_state": "not_configured",
            }
    except IntegrityError as exc:
        raise HTTPException(409, "Model ID already exists") from exc
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Model database unavailable or migration required") from exc
    return {"data": data, "request_id": request.state.request_id}


@router.get("")
async def list_models(
    request: Request, offset: int = 0, limit: int = 200, include_test: bool = False
):
    if offset < 0 or not 1 <= limit <= 200:
        raise HTTPException(422, "Invalid pagination")
    try:
        async with request.app.state.sessions() as session:
            rows = (
                await session.execute(
                    select(Asset, ModelConfigBinding)
                    .where(
                        True
                        if include_test
                        else ~Asset.asset_key.op("~")(
                            "^(fixture-|browser-|model-browser-|navigation-|pipeline-|TEST-)"
                        )
                    )
                    .outerjoin(ModelConfigBinding, ModelConfigBinding.model_asset_id == Asset.id)
                    .where(Asset.kind == "model")
                    .order_by(Asset.created_at.desc(), Asset.id.desc())
                    .offset(offset)
                    .limit(limit + 1)
                )
            ).all()
            data = [
                {
                    "id": str(a.id),
                    "model_id": a.asset_key,
                    "name": a.name,
                    "default_config_version_id": str(b.default_config_version_id) if b else None,
                    "binding_revision": b.row_version if b else 0,
                    "connection_state": "not_configured",
                }
                for a, b in rows[:limit]
            ]
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Model database unavailable or migration required") from exc
    return {
        "data": data,
        "next_offset": offset + limit if len(rows) > limit else None,
        "request_id": request.state.request_id,
    }


@router.put("/{model_id}/default-test-config")
async def set_default(
    model_id: uuid.UUID, body: DefaultConfig, request: Request, if_match: int = Header(ge=0)
):
    try:
        async with request.app.state.sessions() as session, session.begin():
            # Lock the stable parent even when no pointer exists yet.
            model = await session.scalar(
                select(Asset).where(Asset.id == model_id, Asset.kind == "model").with_for_update()
            )
            if model is None:
                raise HTTPException(404, "Model not found")
            config = await session.get(TestConfigVersion, body.config_version_id)
            if config is None:
                raise HTTPException(404, "Configuration version not found")
            binding = await session.scalar(
                select(ModelConfigBinding).where(ModelConfigBinding.model_asset_id == model_id)
            )
            current = binding.row_version if binding else 0
            if current != if_match:
                raise HTTPException(412, "Default configuration changed; refresh before saving")
            if binding is None:
                binding = ModelConfigBinding(
                    model_asset_id=model_id, default_config_version_id=config.id, row_version=1
                )
                session.add(binding)
            else:
                binding.default_config_version_id = config.id
                binding.row_version += 1
            data = {
                "model_id": str(model_id),
                "config_version_id": str(config.id),
                "binding_revision": binding.row_version,
            }
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Model database unavailable or migration required") from exc
    return {"data": data, "request_id": request.state.request_id}
