"""Privileged import of approved, immutable standard configurations."""

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from autoeval.config import get_settings
from autoeval.configuration import Parameters, TestConfigItem, TestConfigVersion
from autoeval.db import create_database


class StandardConfigInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config_key: str = Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9_-]+$")
    display_name: str = Field(min_length=1, max_length=256)
    version_label: str = Field(min_length=1, max_length=128)
    items: dict[str, Parameters] = Field(min_length=1, max_length=100)
    source_url: str = Field(max_length=2048)
    source_revision: str = Field(min_length=1, max_length=256)
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved_by: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("items")
    @classmethod
    def known_datasets(cls, value):
        if any(key not in {"GPQA", "MMMU-Pro", "AutomationBench"} for key in value):
            raise ValueError("Unknown dataset")
        return value

    @field_validator("source_url")
    @classmethod
    def https_source(cls, value):
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("Source URL must use HTTPS")
        return value


class ImportConflict(ValueError):
    pass


def result_for(version):
    return {
        "id": str(version.id),
        "config_key": version.config_key,
        "version": version.version,
        "content_hash": version.content_hash,
    }


async def import_config(session, body: StandardConfigInput):
    values = {key: value.model_dump() for key, value in body.items.items()}
    digest = hashlib.sha256(
        json.dumps(values, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()
    existing = await session.scalar(
        select(TestConfigVersion)
        .where(
            TestConfigVersion.config_key == body.config_key,
            TestConfigVersion.version_label == body.version_label,
        )
        .with_for_update()
    )
    if existing is not None:
        expected = (
            "standard",
            body.display_name,
            digest,
            body.source_url,
            body.source_revision,
            body.source_sha256,
            body.approved_by,
        )
        actual = (
            existing.kind,
            existing.display_name,
            existing.content_hash,
            existing.source_url,
            existing.source_revision,
            existing.source_sha256,
            existing.approved_by,
        )
        if actual != expected:
            raise ImportConflict("Configuration version label already has different content")
        return result_for(existing)

    latest = await session.scalar(
        select(TestConfigVersion)
        .where(TestConfigVersion.config_key == body.config_key)
        .order_by(TestConfigVersion.version.desc())
        .limit(1)
        .with_for_update()
    )
    version = TestConfigVersion(
        config_key=body.config_key,
        display_name=body.display_name,
        version=(latest.version if latest else 0) + 1,
        version_label=body.version_label,
        kind="standard",
        content_hash=digest,
        created_by=body.approved_by or "local-operator",
        source_url=body.source_url,
        source_revision=body.source_revision,
        source_sha256=body.source_sha256,
        approved_by=body.approved_by,
    )
    session.add(version)
    await session.flush()
    for dataset, parameters in values.items():
        session.add(
            TestConfigItem(
                config_version_id=version.id,
                dataset_key=dataset,
                parameter_values=parameters,
            )
        )
    await session.flush()
    return result_for(version)


async def run_import(body):
    engine, sessions = create_database(get_settings())
    try:
        async with sessions() as session, session.begin():
            return await import_config(session, body)
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    args = parser.parse_args()
    try:
        body = StandardConfigInput.model_validate_json(args.input.read_bytes())
    except (OSError, ValidationError):
        parser.exit(2, "error: invalid standard configuration JSON\n")
    try:
        result = asyncio.run(run_import(body))
    except (ImportConflict, SQLAlchemyError, OSError):
        parser.exit(1, "error: standard configuration import failed\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
