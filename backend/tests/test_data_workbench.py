"""Curation validation and opt-in real PostgreSQL transaction checks."""

import os
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import select

from autoeval.api import create_app
from autoeval.audit import AuditEvent
from autoeval.config import Settings
from autoeval.data_workbench import EntryInput, development, validate_report


def payload():
    token = uuid.uuid4().hex
    return {
        "source_key": "test-" + token,
        "source_type": "official",
        "model_name": "Test model",
        "dataset": "TEST-" + token,
        "metric": "accuracy",
        "value": 75,
        "unit": "%",
        "direction": "higher_is_better",
        "scope": "full",
        "conditions": {"version": "v1"},
        "source_name": "Test fixture, not official data",
        "source_url": "https://example.com/data",
        "source_version": "v1",
        "source_sha256": "a" * 64,
        "report_id": None,
        "notes": "test",
    }


@pytest.mark.parametrize(
    "patch",
    [
        {"value": 101},
        {"value": float("nan")},
        {"source_url": "http://example.com"},
        {"conditions": {}},
        {"source_type": "report"},
        {"source_sha256": "not-a-hash"},
    ],
)
def test_reject_invalid_provenance_and_scores(patch):
    with pytest.raises(ValidationError):
        EntryInput.model_validate({**payload(), **patch})


def test_workbench_server_side_environment_guard():
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(settings=SimpleNamespace(environment="production"))
        )
    )
    with pytest.raises(HTTPException) as exc:
        development(request)
    assert exc.value.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("evidence_status,expected", [("missing", 409), ("verified", 422)])
async def test_report_evidence_and_score_cannot_be_overridden(
    monkeypatch, evidence_status, expected
):
    entry = EntryInput.model_validate(
        {
            **payload(),
            "source_type": "report",
            "report_id": str(uuid.uuid4()),
            "value": 99,
        }
    )
    report = SimpleNamespace(
        validity="complete",
        execution_origin="real",
        run_id=uuid.uuid4(),
        summary={"accuracy": 75},
        sha256="a" * 64,
        id=entry.report_id,
    )
    run = SimpleNamespace(trigger_scope="local-operator:evaluation")
    snapshot = SimpleNamespace(
        canonical_body={
            "remote_model": entry.model_name,
            "manifest_sha256": "b" * 64,
            "tool": {"revision": "fixed"},
            "parameters": {"scope": "Lite"},
        }
    )
    session = SimpleNamespace(
        get=AsyncMock(side_effect=[report, run]), scalar=AsyncMock(return_value=snapshot)
    )
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(settings=None)))
    monkeypatch.setattr(
        "autoeval.data_workbench.report_evidence_status", AsyncMock(return_value=evidence_status)
    )
    with pytest.raises(HTTPException) as exc:
        await validate_report(request, session, entry)
    assert exc.value.status_code == expected


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("AE_INFRA_TEST") != "1", reason="Requires real PostgreSQL")
async def test_review_edit_history_and_duplicate_source_transactions():
    settings = Settings(environment="test")
    app = create_app(settings)
    body = payload()
    identifiers = []
    async with app.router.lifespan_context(app):
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                base = "/api/autoeval/v1/data-workbench"
                response = await client.post(base + "/entries", json=body)
                assert response.status_code == 201, response.text
                row = response.json()["data"]
                identifiers.append(uuid.UUID(row["id"]))
                repeat = await client.post(base + "/entries", json=body)
                assert repeat.json()["data"]["id"] == row["id"]
                conflict = await client.post(base + "/entries", json={**body, "value": 76})
                assert conflict.status_code == 409
                route = base + "/entries/" + row["id"]
                decision = {"expected_revision": 1, "action": "approve", "reason": "test approval"}
                approved = await client.post(route + "/review", json=decision)
                assert approved.json()["data"]["state"] == "approved"
                assert (await client.post(route + "/review", json=decision)).status_code == 409
                edit = {
                    "expected_revision": 2,
                    "entry": {**body, "value": 80},
                    "reason": "correction",
                }
                assert (await client.post(route + "/edit", json=edit)).status_code == 409
                groups = (await client.get(base + "/published")).json()["data"]
                group = next(g for g in groups if g["comparison"]["dataset"] == body["dataset"])
                assert group["entries"][0]["entry"]["value"] == 75
                assert group["entries"][0]["rank"] == 1
                withdrawn = await client.post(
                    route + "/review",
                    json={
                        "expected_revision": 2,
                        "action": "withdraw",
                        "reason": "test withdrawal",
                    },
                )
                assert withdrawn.json()["data"]["state"] == "withdrawn"
                edit["expected_revision"] = 3
                edited = await client.post(route + "/edit", json=edit)
                assert edited.json()["data"]["state"] == "draft"
                assert edited.json()["data"]["revision"] == 4
                groups = (await client.get(base + "/published")).json()["data"]
                assert not any(g["comparison"]["dataset"] == body["dataset"] for g in groups)
            async with app.state.sessions() as session:
                history = (
                    await session.scalars(
                        select(AuditEvent)
                        .where(AuditEvent.resource_id == row["id"])
                        .order_by(AuditEvent.created_at)
                    )
                ).all()
                assert len(history) == 4
                assert history[0].details["entry"]["value"] == 75
                assert history[-1].details["entry"]["value"] == 80
        finally:
            # Audit is append-only. Retain clearly marked TEST drafts and their history;
            # they are not published. Never disable immutability triggers for cleanup.
            pass


def test_best_score_preserves_models_zero_and_newest_ties():
    from autoeval.data_workbench import best_per_model

    rows = [
        {"id": "new-low", "model_id": "m1", "entry": {"model_name": "same-name", "value": 0}},
        {"id": "new-best", "model_id": "m1", "entry": {"model_name": "same-name", "value": 80}},
        {"id": "old-best", "model_id": "m1", "entry": {"model_name": "same-name", "value": 80}},
        {"id": "other-model", "model_id": "m2", "entry": {"model_name": "same-name", "value": 60}},
    ]
    assert [r["id"] for r in best_per_model(rows, "higher_is_better")] == [
        "new-best",
        "other-model",
    ]
    assert [r["id"] for r in best_per_model(rows, "lower_is_better")] == ["new-low", "other-model"]
    assert len(rows) == 4
