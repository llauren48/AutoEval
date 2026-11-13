import asyncio
import hashlib
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import CheckConstraint, select
from sqlalchemy.exc import IntegrityError

from autoeval.api import create_app
from autoeval.config import Settings
from autoeval.configuration import TestConfigItem as ConfigItem
from autoeval.configuration import TestConfigVersion as ConfigVersion
from autoeval.db import create_database


@pytest.fixture(scope="module")
def standard_module():
    return importlib.import_module("autoeval.import_standard_config")


def valid_payload():
    return {
        "config_key": "approved-standard",
        "display_name": "Approved standard",
        "version_label": "2026.11",
        "items": {
            "GPQA": {
                "output": 100,
                "concurrency": 1,
                "temperature": 0.5,
                "top_p": 1.0,
                "scope": "全量",
            }
        },
        "source_url": "https://example.test/standard.json",
        "source_revision": "revision-1",
        "source_sha256": "a" * 64,
        "approved_by": "release-manager",
    }


def test_standard_input_accepts_only_the_approved_shape(standard_module):
    body = standard_module.StandardConfigInput.model_validate(valid_payload())
    assert body.items["GPQA"].output == 100
    assert body.source_sha256 == "a" * 64


@pytest.mark.parametrize(
    "override",
    [
        {"api_key": "must-not-be-persisted"},
        {"unexpected": True},
        {"profile_bindings": {}},
        {"source_url": "http://example.test/standard.json"},
        {"source_revision": ""},
        {"source_sha256": "A" * 64},
        {"source_sha256": "a" * 63},
        {"approved_by": ""},
        {"items": {"Unknown": valid_payload()["items"]["GPQA"]}},
        {
            "items": {
                "GPQA": {**valid_payload()["items"]["GPQA"], "concurrency": 0},
            }
        },
    ],
)
def test_standard_input_rejects_unapproved_or_invalid_fields(standard_module, override):
    with pytest.raises(ValidationError):
        standard_module.StandardConfigInput.model_validate({**valid_payload(), **override})


def test_standard_input_requires_a_json_object(standard_module):
    with pytest.raises(ValidationError):
        standard_module.StandardConfigInput.model_validate_json("[]")


def content_hash(payload):
    canonical = json.dumps(
        payload["items"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def stored_standard(payload, **changes):
    values = {
        "id": uuid.uuid4(),
        "config_key": payload["config_key"],
        "display_name": payload["display_name"],
        "version": 2,
        "version_label": payload["version_label"],
        "kind": "standard",
        "content_hash": content_hash(payload),
        "created_by": payload["approved_by"],
        "source_url": payload["source_url"],
        "source_revision": payload["source_revision"],
        "source_sha256": payload["source_sha256"],
        "approved_by": payload["approved_by"],
    }
    return ConfigVersion(**(values | changes))


class ImportSession:
    def __init__(self, existing=None, latest=None):
        self.existing = existing
        self.latest = latest
        self.queries = 0
        self.added = []

    async def scalar(self, statement):
        self.queries += 1
        return self.existing if self.queries == 1 else self.latest

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        for row in self.added:
            if row.id is None:
                row.id = uuid.uuid4()


async def test_import_creates_the_next_standard_version_with_source_metadata(standard_module):
    payload = valid_payload()
    body = standard_module.StandardConfigInput.model_validate(payload)
    latest = stored_standard(payload, version_label="old", version=3)
    session = ImportSession(latest=latest)

    result = await standard_module.import_config(session, body)

    version = session.added[0]
    assert result == {
        "id": str(version.id),
        "config_key": payload["config_key"],
        "version": 4,
        "content_hash": content_hash(payload),
    }
    assert version.kind == "standard"
    assert version.source_url == payload["source_url"]
    assert version.source_revision == payload["source_revision"]
    assert version.source_sha256 == payload["source_sha256"]
    assert version.approved_by == payload["approved_by"]
    assert len(session.added) == 2
    item = session.added[1]
    assert isinstance(item, ConfigItem)
    assert item.config_version_id == version.id
    assert item.dataset_key == "GPQA"
    assert item.parameter_values == payload["items"]["GPQA"]


async def test_identical_reimport_returns_the_existing_id_without_writes(standard_module):
    payload = valid_payload()
    body = standard_module.StandardConfigInput.model_validate(payload)
    existing = stored_standard(payload)
    session = ImportSession(existing=existing)

    result = await standard_module.import_config(session, body)

    assert result["id"] == str(existing.id)
    assert result["version"] == 2
    assert session.added == []
    assert session.queries == 1


@pytest.mark.parametrize(
    "override",
    [
        {"display_name": "Changed"},
        {"source_url": "https://example.test/other.json"},
        {"source_revision": "revision-2"},
        {"source_sha256": "b" * 64},
        {"approved_by": "other-approver"},
        {
            "items": {
                "GPQA": {**valid_payload()["items"]["GPQA"], "output": 101},
            }
        },
    ],
)
async def test_conflicting_reimport_fails_without_overwriting(standard_module, override):
    original = valid_payload()
    body = standard_module.StandardConfigInput.model_validate({**original, **override})
    existing = stored_standard(original)
    session = ImportSession(existing=existing)

    with pytest.raises(standard_module.ImportConflict):
        await standard_module.import_config(session, body)
    assert session.added == []
    assert session.queries == 1


def test_cli_imports_approved_json_and_prints_the_id(
    standard_module, monkeypatch, tmp_path, capsys
):
    path = tmp_path / "standard.json"
    path.write_text(json.dumps(valid_payload()), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["import_standard_config", "--input", str(path)])

    def completed(coroutine):
        coroutine.close()
        return {"id": "00000000-0000-0000-0000-000000000001", "version": 1}

    monkeypatch.setattr(standard_module.asyncio, "run", completed)
    standard_module.main()
    assert json.loads(capsys.readouterr().out)["id"] == ("00000000-0000-0000-0000-000000000001")


def test_cli_rejects_extra_secrets_before_connecting(
    standard_module, monkeypatch, tmp_path, capsys
):
    path = tmp_path / "standard.json"
    path.write_text(
        json.dumps({**valid_payload(), "api_key": "must-not-be-echoed"}), encoding="utf-8"
    )
    monkeypatch.setattr(sys, "argv", ["import_standard_config", "--input", str(path)])

    def unexpected(coroutine):
        raise AssertionError("database must not be contacted")

    monkeypatch.setattr(standard_module.asyncio, "run", unexpected)
    with pytest.raises(SystemExit) as exc:
        standard_module.main()
    assert exc.value.code == 2
    output = capsys.readouterr()
    assert "must-not-be-echoed" not in output.out + output.err


def test_cli_conflict_exits_nonzero(standard_module, monkeypatch, tmp_path):
    path = tmp_path / "standard.json"
    path.write_text(json.dumps(valid_payload()), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["import_standard_config", "--input", str(path)])

    def conflict(coroutine):
        coroutine.close()
        raise standard_module.ImportConflict("conflict")

    monkeypatch.setattr(standard_module.asyncio, "run", conflict)
    with pytest.raises(SystemExit) as exc:
        standard_module.main()
    assert exc.value.code == 1


def test_standard_metadata_has_postgresql_sql_constraints():
    checks = [
        str(constraint.sqltext)
        for constraint in ConfigVersion.__table__.constraints
        if isinstance(constraint, CheckConstraint)
    ]
    metadata = next((check for check in checks if "source_url IS NOT NULL" in check), "")
    assert "approved_by IS NOT NULL" not in metadata
    for column in ("source_url", "source_revision", "source_sha256"):
        assert f"{column} IS NOT NULL" in metadata
        assert f"{column} IS NULL" in metadata
    assert any("source_url LIKE 'https://%'" in check for check in checks)
    assert any("source_sha256 ~ '^[0-9a-f]{64}$'" in check for check in checks)


def test_migration_0014_extends_report_origin_revision():
    path = Path(__file__).parents[1] / "migrations" / "versions" / "0014_standard_config.py"
    assert path.is_file()
    spec = importlib.util.spec_from_file_location("migration_0014", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    assert migration.revision == "0014_standard_config"
    assert migration.down_revision == "0013_report_origin"


@pytest.mark.skipif(
    os.environ.get("AE_STANDARD_CONFIG_TEST") != "1",
    reason="Migrated PostgreSQL and AE_STANDARD_CONFIG_TEST=1 required",
)
def test_postgres_standard_import_constraints_idempotency_and_read_only(tmp_path):
    settings = Settings(
        environment="test",
        **(
            {"database_url": os.environ["AE_TEST_DATABASE_URL"]}
            if os.environ.get("AE_TEST_DATABASE_URL")
            else {}
        ),
    )
    payload = {
        **valid_payload(),
        "config_key": "standard-" + uuid.uuid4().hex,
        "version_label": "v1",
    }
    path = tmp_path / "standard.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    env = {
        **os.environ,
        "AE_ENVIRONMENT": "test",
        "AE_DATABASE_URL": settings.database_url.get_secret_value(),
    }
    command = [sys.executable, "-m", "autoeval.import_standard_config", "--input", str(path)]
    options = {
        "capture_output": True,
        "text": True,
        "env": env,
        "cwd": Path(__file__).parents[1],
        "timeout": 30,
    }

    first = subprocess.run(command, check=False, **options)
    assert first.returncode == 0, first.stderr
    imported = json.loads(first.stdout)
    repeated = subprocess.run(command, check=False, **options)
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout)["id"] == imported["id"]

    conflicting = {
        **payload,
        "items": {"GPQA": {**payload["items"]["GPQA"], "output": 101}},
    }
    path.write_text(json.dumps(conflicting), encoding="utf-8")
    assert subprocess.run(command, check=False, **options).returncode != 0

    async def verify_database():
        engine, sessions = create_database(settings)
        try:
            async with sessions() as session:
                rows = (
                    await session.scalars(
                        select(ConfigVersion).where(
                            ConfigVersion.config_key == payload["config_key"]
                        )
                    )
                ).all()
                assert len(rows) == 1
                stored = rows[0]
                assert str(stored.id) == imported["id"]
                assert stored.content_hash == content_hash(payload)
                assert stored.source_sha256 == payload["source_sha256"]
                assert stored.approved_by == payload["approved_by"]
            invalid_rows = [
                ConfigVersion(
                    config_key="invalid-standard-" + uuid.uuid4().hex,
                    display_name="Invalid",
                    version=1,
                    version_label="v1",
                    kind="standard",
                    content_hash="c" * 64,
                    created_by="test",
                ),
                ConfigVersion(
                    config_key="invalid-custom-" + uuid.uuid4().hex,
                    display_name="Invalid",
                    version=1,
                    version_label="v1",
                    kind="custom",
                    content_hash="c" * 64,
                    created_by="test",
                    source_url=payload["source_url"],
                    source_revision=payload["source_revision"],
                    source_sha256=payload["source_sha256"],
                    approved_by=payload["approved_by"],
                ),
            ]
            for row in invalid_rows:
                async with sessions() as session:
                    with pytest.raises(IntegrityError):
                        async with session.begin():
                            session.add(row)
                            await session.flush()
        finally:
            await engine.dispose()

    asyncio.run(verify_database())
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/api/autoeval/v1/test-configs",
            json={
                "config_key": payload["config_key"],
                "display_name": "Must not replace standard",
                "version_label": "custom-attempt",
                "expected_version": 1,
                "items": payload["items"],
            },
        )
        assert response.status_code == 403
