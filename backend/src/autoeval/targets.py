"""Explicit, bounded model-list probes; never sends generation requests."""

import uuid
from datetime import UTC, datetime
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import DateTime, ForeignKey, String, select
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, mapped_column

from autoeval.models import Base, Record
from autoeval.registry import Asset, AssetVersion, ModelConfigBinding
from autoeval.secrets import FileSecretStore


class TargetBinding(Record, Base):
    __tablename__ = "target_binding_version"
    model_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("asset_version.id", ondelete="RESTRICT")
    )
    base_url: Mapped[str] = mapped_column(String(2048))
    remote_model: Mapped[str] = mapped_column(String(128))
    credential_ref: Mapped[str] = mapped_column(String(32))


class ConnectionProbe(Record, Base):
    __tablename__ = "connection_probe"
    target_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("target_binding_version.id", ondelete="RESTRICT")
    )
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str] = mapped_column(String(32))
    details: Mapped[dict] = mapped_column(JSONB)


class CredentialInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: SecretStr = Field(min_length=1, max_length=65536)


class TargetInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model_version_id: uuid.UUID
    base_url: str = Field(max_length=2048)
    remote_model: str = Field(min_length=1, max_length=128)
    credential_ref: str = Field(pattern=r"^[0-9a-f]{32}$")


def validate_url(url: str, allowed_hosts: list[str]) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in allowed_hosts
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.port not in (None, 443)
    ):
        raise ValueError("Only explicitly allowed HTTPS model endpoints are accepted")
    return url.rstrip("/")


async def probe_model_list(
    base_url: str, remote_model: str, key: str, client: httpx.AsyncClient
) -> tuple[str, dict]:
    # No redirects: an upstream must never redirect Authorization to another host.
    async with client.stream(
        "GET", base_url + "/models", headers={"Authorization": f"Bearer {key}"}
    ) as response:
        if response.status_code != 200:
            return "upstream_error", {"http_status": response.status_code}
        raw = bytearray()
        async for part in response.aiter_bytes():
            raw.extend(part)
            if len(raw) > 1048576:
                return "invalid_response", {"reason": "response_too_large"}
    import json

    try:
        body = json.loads(raw)
        models = [m["id"] for m in body["data"] if isinstance(m.get("id"), str)]
    except (ValueError, KeyError, TypeError, AttributeError):
        return "invalid_response", {"reason": "invalid_model_list"}
    return ("matched" if remote_model in models else "model_not_found"), {"models": models}


router = APIRouter(prefix="/api/autoeval/v1", tags=["model services"])


@router.post("/credentials", status_code=201)
async def put_credential(body: CredentialInput, request: Request):
    store = FileSecretStore(request.app.state.settings.secret_store_path)
    try:
        reference = store.put(body.value.get_secret_value())
    except ValueError as exc:
        raise HTTPException(422, "Invalid credential") from exc
    return {"data": {"credential_ref": reference}, "request_id": request.state.request_id}


@router.post("/target-bindings", status_code=201)
async def create_target(body: TargetInput, request: Request):
    settings = request.app.state.settings
    try:
        base_url = validate_url(body.base_url, settings.allowed_model_hosts)
        FileSecretStore(settings.secret_store_path).get(body.credential_ref)
    except (ValueError, OSError) as exc:
        raise HTTPException(422, "Invalid endpoint or credential reference") from exc
    try:
        async with request.app.state.sessions() as session, session.begin():
            if await session.get(AssetVersion, body.model_version_id) is None:
                raise HTTPException(404, "Model version not found")
            target = TargetBinding(
                model_version_id=body.model_version_id,
                base_url=base_url,
                remote_model=body.remote_model,
                credential_ref=body.credential_ref,
            )
            session.add(target)
            await session.flush()
            result = {"id": str(target.id), "connection_state": "untested"}
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Target database unavailable") from exc
    return {"data": result, "request_id": request.state.request_id}


@router.post("/targets/{target_id}/connection-tests")
async def check_connection(target_id: uuid.UUID, request: Request):
    settings = request.app.state.settings
    try:
        async with request.app.state.sessions() as session:
            target = await session.get(TargetBinding, target_id)
            if target is None:
                raise HTTPException(404, "Target not found")
            validate_url(target.base_url, settings.allowed_model_hosts)
            try:
                key = FileSecretStore(settings.secret_store_path).get(target.credential_ref)
                async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                    outcome, details = await probe_model_list(
                        target.base_url, target.remote_model, key, client
                    )
            except (OSError, ValueError):
                outcome, details = "credential_unavailable", {}
            except httpx.HTTPError:
                outcome, details = "connection_error", {}
            record = ConnectionProbe(
                target_id=target.id, checked_at=datetime.now(UTC), outcome=outcome, details=details
            )
            session.add(record)
            await session.commit()
            result = {
                "probe_id": str(record.id),
                "outcome": outcome,
                "details": details,
                "generation_tested": False,
            }
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Target database unavailable") from exc
    return {"data": result, "request_id": request.state.request_id}


@router.get("/target-bindings")
async def list_targets(
    request: Request,
    offset: int = 0,
    limit: int = 200,
    target_id: uuid.UUID | None = None,
    include_test: bool = False,
):
    if offset < 0 or not 1 <= limit <= 200:
        raise HTTPException(422, "Invalid pagination")
    try:
        async with request.app.state.sessions() as session:
            rows = (
                await session.execute(
                    select(TargetBinding, Asset, AssetVersion, ModelConfigBinding)
                    .join(AssetVersion, AssetVersion.id == TargetBinding.model_version_id)
                    .join(Asset, Asset.id == AssetVersion.asset_id)
                    .outerjoin(ModelConfigBinding, ModelConfigBinding.model_asset_id == Asset.id)
                    .where(TargetBinding.id == target_id if target_id else True)
                    .where(
                        True
                        if include_test or target_id
                        else ~Asset.asset_key.op("~")(
                            r"^(navigation-[a-f0-9]{32}|model-browser-[a-f0-9]{10})$"
                        )
                    )
                    .order_by(TargetBinding.created_at.desc(), TargetBinding.id.desc())
                    .offset(offset)
                    .limit(limit + 1)
                )
            ).all()
            data = [
                {
                    "id": str(t.id),
                    "model_id": str(model.id),
                    "model_key": model.asset_key,
                    "model_name": model.name,
                    "model_version_id": str(version.id),
                    "model_version": version.version,
                    "default_config_version_id": str(binding.default_config_version_id)
                    if binding
                    else None,
                    "base_url": t.base_url,
                    "remote_model": t.remote_model,
                }
                for t, model, version, binding in rows[:limit]
            ]
    except SQLAlchemyError as exc:
        raise HTTPException(503, "Target database unavailable") from exc
    return {
        "data": data,
        "next_offset": offset + limit if len(rows) > limit else None,
        "request_id": request.state.request_id,
    }
