"""Customer SSH resource registration and explicit pinned-key preflight."""

import asyncio
import hashlib
import json
import uuid
from pathlib import Path
from typing import Literal

import paramiko
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint, and_, select
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Mapped, mapped_column

from autoeval.models import Base, Record
from autoeval.resources import ResourceProvider
from autoeval.secrets import FileSecretStore
from autoeval.ssh_executor import SSHConnection, SSHExecutor, load_private_key


class SSHResourceBinding(Record, Base):
    __tablename__ = "ssh_resource_binding"
    provider_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("resource_provider.id", ondelete="RESTRICT")
    )
    binding_key: Mapped[str] = mapped_column(String(128))
    version: Mapped[int]
    host: Mapped[str] = mapped_column(String(253))
    port: Mapped[int]
    username: Mapped[str] = mapped_column(String(64))
    fingerprint: Mapped[str] = mapped_column(String(64))
    workdir: Mapped[str] = mapped_column(String(512))
    auth_kind: Mapped[str] = mapped_column(String(16))
    credential_ref: Mapped[str] = mapped_column(String(32))
    native_tool_root: Mapped[str | None] = mapped_column(String(512))
    native_bridge_sha256: Mapped[str | None] = mapped_column(String(64))
    native_lock_sha256: Mapped[str | None] = mapped_column(String(64))
    native_tool_revision: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        UniqueConstraint("binding_key", "version"),
        CheckConstraint(
            "(native_bridge_sha256 IS NULL AND native_lock_sha256 IS NULL AND native_tool_revision IS NULL) OR "
            "(native_tool_root IS NOT NULL AND native_bridge_sha256 IS NOT NULL AND native_lock_sha256 IS NOT NULL AND native_tool_revision IS NOT NULL "
            "AND native_bridge_sha256 ~ '^[0-9a-f]{64}$' AND native_lock_sha256 ~ '^[0-9a-f]{64}$' AND native_tool_revision ~ '^([0-9a-f]{40}|[0-9a-f]{64})$')",
            name="ck_ssh_native_pins",
        ),
    )


class SSHCredential(BaseModel):
    model_config = ConfigDict(extra="forbid")
    auth_kind: Literal["password", "private_key"]
    value: SecretStr = Field(min_length=1, max_length=65536)


class SSHResourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    binding_key: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,128}$")
    host: str = Field(pattern=r"^[a-zA-Z0-9_.:-]{1,253}$")
    port: int = Field(default=22, ge=1, le=65535)
    username: str = Field(pattern=r"^[a-zA-Z_][a-zA-Z0-9_.-]{0,63}$")
    fingerprint: str = Field(pattern=r"^SHA256:[A-Za-z0-9+/]{43}$")
    workdir: str = Field(min_length=2, max_length=512)
    auth_kind: Literal["password", "private_key"]
    credential_ref: str = Field(pattern=r"^[0-9a-f]{32}$")
    native_tool_root: str | None = Field(default=None, min_length=2, max_length=512)


def connection_for(row, settings):
    secret = FileSecretStore(settings.secret_store_path).get(row.credential_ref)
    return SSHConnection(
        host=row.host,
        port=row.port,
        username=row.username,
        fingerprint=row.fingerprint,
        workdir=row.workdir,
        owner_id=row.id,
        credential=SecretStr(secret),
        auth_kind=row.auth_kind,
    )


def resource_data(row):
    return {
        "id": str(row.id),
        "binding_key": row.binding_key,
        "version": row.version,
        "host": row.host,
        "port": row.port,
        "username": row.username,
        "fingerprint": row.fingerprint,
        "workdir": row.workdir,
        "auth_kind": row.auth_kind,
        "provider_kind": "customer_ssh",
        "native_tool_root": row.native_tool_root,
        "native_bridge_sha256": row.native_bridge_sha256,
        "native_lock_sha256": row.native_lock_sha256,
        "native_tool_revision": row.native_tool_revision,
        "evaluation_ready": False,
    }


router = APIRouter(prefix="/api/autoeval/v1/ssh-resources", tags=["customer SSH resources"])


@router.post("/credentials", status_code=201)
async def save_credential(body: SSHCredential, request: Request):
    try:
        value = body.value.get_secret_value()
        if body.auth_kind == "private_key":
            load_private_key(value)
        reference = FileSecretStore(request.app.state.settings.secret_store_path).put(
            value, allow_multiline=body.auth_kind == "private_key"
        )
    except (OSError, ValueError, paramiko.SSHException) as exc:
        raise HTTPException(422, "Invalid SSH credential or protected store unavailable") from exc
    return {
        "data": {"credential_ref": reference, "auth_kind": body.auth_kind},
        "request_id": request.state.request_id,
    }


@router.post("", status_code=201)
async def register(body: SSHResourceInput, request: Request):
    try:
        if body.native_tool_root is not None:
            SSHConnection.absolute_directory(body.native_tool_root)
        SSHConnection(
            **body.model_dump(exclude={"binding_key", "credential_ref", "native_tool_root"}),
            owner_id=uuid.uuid4(),
            credential=SecretStr("validation-only"),
        )
        secret = FileSecretStore(request.app.state.settings.secret_store_path).get(
            body.credential_ref
        )
        if body.auth_kind == "private_key":
            load_private_key(secret)
        async with request.app.state.sessions() as session, session.begin():
            provider = await session.scalar(
                select(ResourceProvider).where(ResourceProvider.name == "customer-ssh")
            )
            if provider is None:
                provider = ResourceProvider(name="customer-ssh", kind="customer_ssh")
                session.add(provider)
                await session.flush()
            pins = {}
            if body.native_tool_root:
                lock_bytes = request.app.state.settings.aisbench_lock_path.read_bytes()
                pins = {
                    "native_bridge_sha256": hashlib.sha256(
                        Path(__file__).with_name("aisbench_native.py").read_bytes()
                    ).hexdigest(),
                    "native_lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
                    "native_tool_revision": json.loads(lock_bytes)["revision"],
                }
            row = SSHResourceBinding(
                provider_id=provider.id, version=1, **body.model_dump(), **pins
            )
            session.add(row)
            await session.flush()
            data = resource_data(row)
    except IntegrityError as exc:
        raise HTTPException(409, "SSH binding already exists; existing version preserved") from exc
    except SQLAlchemyError as exc:
        raise HTTPException(503, "SSH resource database unavailable") from exc
    except (OSError, ValueError) as exc:
        raise HTTPException(422, "Invalid SSH binding or missing credential") from exc
    return {"data": data, "request_id": request.state.request_id}


@router.get("")
async def list_resources(
    request: Request, offset: int = 0, limit: int = 50, include_test: bool = False
):
    if offset < 0 or not 1 <= limit <= 200:
        raise HTTPException(422, "Invalid pagination")
    try:
        async with request.app.state.sessions() as session:
            rows = (
                await session.scalars(
                    select(SSHResourceBinding)
                    .where(
                        True
                        if include_test
                        else ~and_(
                            SSHResourceBinding.host.in_(["127.0.0.1", "localhost", "::1"]),
                            SSHResourceBinding.port.in_([1, 2222]),
                            SSHResourceBinding.binding_key.op("~")(
                                r"^(ssh-registry-[a-f0-9]{32}|browser-ssh-[a-f0-9]{12}|ssh-native-pilot-[a-f0-9]{8})$"
                            ),
                        )
                    )
                    .order_by(SSHResourceBinding.created_at.desc(), SSHResourceBinding.id.desc())
                    .offset(offset)
                    .limit(limit + 1)
                )
            ).all()
        return {
            "data": [resource_data(r) for r in rows[:limit]],
            "next_offset": offset + limit if len(rows) > limit else None,
            "request_id": request.state.request_id,
        }
    except SQLAlchemyError as exc:
        raise HTTPException(503, "SSH resource database unavailable") from exc


@router.post("/{binding_id}/validate")
async def validate(binding_id: uuid.UUID, request: Request):
    try:
        async with request.app.state.sessions() as session:
            row = await session.get(SSHResourceBinding, binding_id)
            if row is None:
                raise HTTPException(404, "SSH resource not found")
        adapter = SSHExecutor(connection_for(row, request.app.state.settings))

        def preflight():
            with adapter.connect() as client:
                adapter.prepare(client)
            ready = False
            if row.native_tool_root:
                from autoeval.execution_transport import frozen_ssh_resource
                from autoeval.ssh_native import SSHNativeTransport

                spec = frozen_ssh_resource(row, request.app.state.settings)
                native = SSHNativeTransport(
                    connection_for(row, request.app.state.settings),
                    spec["tool_root"],
                    spec["bridge_sha256"],
                    spec["lock_sha256"],
                )
                result = native.invoke_local(
                    {
                        "operation": "prompt_gpqa_0shot",
                        "sample": {
                            "question": "Preflight fixture only",
                            "A": "one",
                            "B": "two",
                            "C": "three",
                            "D": "four",
                            "answer": "A",
                        },
                    }
                )
                if result.get("tool_revision") != row.native_tool_revision or not result.get(
                    "prompt"
                ):
                    raise ValueError("Native tool preflight identity mismatch")
                ready = True
            return {
                "environment_ready": True,
                "host_key_verified": True,
                "sftp_ready": True,
                "evaluation_ready": ready,
                "supported_jobs": ["framework_smoke", "gpqa_lite"]
                if ready
                else ["framework_smoke"],
            }

        data = await asyncio.to_thread(preflight)
    except SQLAlchemyError as exc:
        raise HTTPException(503, "SSH resource database unavailable") from exc
    except (OSError, ValueError, RuntimeError, paramiko.SSHException) as exc:
        raise HTTPException(
            409,
            "SSH preflight failed: check approved host key, credentials, Python3 and owned directory",
        ) from exc
    return {"data": data, "request_id": request.state.request_id}
