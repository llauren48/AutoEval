import json
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from autoeval.analysis import (
    ReportAnalysis,
    analysis_is_configured,
    analysis_status_for,
    build_analysis_input,
    enqueue_analysis,
    retry_existing_analysis,
    validate_analysis_output,
)
from autoeval.analysis_activity import build_analysis_result
from autoeval.api import create_app
from autoeval.config import Settings

ATTEMPT_ID = uuid.uuid4()
CATALOG = {
    "run.summary": {"kind": "run_summary", "sha256": "a" * 64},
    f"attempt:{ATTEMPT_ID}": {"kind": "case_attempt", "sha256": "b" * 64},
    "baseline.comparison": {"kind": "baseline_comparison", "sha256": "c" * 64},
}


def test_model_output_accepts_only_server_issued_evidence_aliases():
    output = validate_analysis_output(
        json.dumps(
            {
                "summary": "The failed attempt explains the incomplete run.",
                "evidence_ids": ["run.summary", f"attempt:{ATTEMPT_ID}"],
                "facts": [{"statement": "The run is incomplete", "evidence_ids": ["run.summary"]}],
                "inferences": [
                    {
                        "statement": "A failed attempt may explain it",
                        "evidence_ids": [f"attempt:{ATTEMPT_ID}"],
                    }
                ],
                "recommended_actions": ["Inspect the failed response before rerunning."],
            }
        ),
        CATALOG,
    )
    assert output["evidence_ids"] == ["run.summary", f"attempt:{ATTEMPT_ID}"]


@pytest.mark.parametrize(
    "evidence_ids",
    [
        ["run.summary", "run.summary"],
        ["attempt:00000000-0000-0000-0000-000000000001"],
        ["baseline.comparison", "configuration"],
        [],
    ],
)
def test_model_output_rejects_duplicate_unknown_or_empty_citations(evidence_ids):
    with pytest.raises(ValueError):
        validate_analysis_output(
            {"summary": "Conclusion", "evidence_ids": evidence_ids, "recommended_actions": []},
            CATALOG,
        )


@pytest.mark.parametrize(
    "body",
    [
        {"summary": "ok", "evidence_ids": ["run.summary"]},
        {
            "summary": "ok",
            "evidence_ids": ["run.summary"],
            "recommended_actions": [],
            "release_decision": "approved",
        },
        {"summary": "", "evidence_ids": ["run.summary"], "recommended_actions": []},
        {"summary": "ok", "evidence_ids": "run.summary", "recommended_actions": []},
    ],
)
def test_model_output_has_a_closed_schema(body):
    with pytest.raises(ValueError):
        validate_analysis_output(body, CATALOG)


def test_evidence_input_is_hash_bound_and_contains_no_credentials():
    report_id = uuid.uuid4()
    attempt = {
        "attempt_id": str(ATTEMPT_ID),
        "sha256": "b" * 64,
        "body": {
            "schema_version": "case-evidence.v1",
            "attempt_id": str(ATTEMPT_ID),
            "response": "model answer",
        },
    }
    body = {
        "schema_version": "gpqa-report.v1",
        "run_id": str(uuid.uuid4()),
        "summary": {"accuracy": 50.0},
    }
    snapshot = {"target_id": str(uuid.uuid4()), "api_key": "must-not-be-copied"}
    evidence_input = build_analysis_input(
        report_id=report_id,
        report_sha256="a" * 64,
        snapshot_sha256="d" * 64,
        report_body=body,
        snapshot_body=snapshot,
        attempts=[attempt],
        baseline_comparison={"status": "incomparable"},
        baseline_comparison_sha256="c" * 64,
    )
    assert set(evidence_input["evidence_catalog"]) == {
        "run.summary",
        f"attempt:{ATTEMPT_ID}",
        "baseline.comparison",
    }
    serialized = json.dumps(evidence_input)
    assert "must-not-be-copied" not in serialized
    assert "api_key" not in serialized
    with pytest.raises(ValueError):
        build_analysis_input(
            report_id=report_id,
            report_sha256="a" * 64,
            snapshot_sha256="d" * 64,
            report_body=body,
            snapshot_body=snapshot,
            attempts=[{**attempt, "body": {**attempt["body"], "attempt_id": str(uuid.uuid4())}}],
        )


def test_evidence_input_accepts_unified_report_and_case_schema():
    attempt_id = uuid.uuid4()
    evidence_input = build_analysis_input(
        report_id=uuid.uuid4(),
        report_sha256="a" * 64,
        snapshot_sha256="d" * 64,
        report_body={"schema_version": "gpqa-report.v1", "summary": {}},
        snapshot_body={"config_version_id": "config"},
        attempts=[
            {
                "attempt_id": str(attempt_id),
                "sha256": "b" * 64,
                "body": {
                    "schema_version": "unified-case.v1",
                    "sample_id": "sample-1",
                    "dataset_key": "gpqa",
                    "attempt_no": 1,
                    "status": "succeeded",
                    "raw": {"case_id": "sample-1", "status": "succeeded"},
                },
            }
        ],
        baseline_comparison={"status": "missing"},
        baseline_comparison_sha256="c" * 64,
    )
    assert set(evidence_input["evidence_catalog"]) == {
        "run.summary",
        f"attempt:{attempt_id}",
        "baseline.comparison",
    }
    assert evidence_input["evidence"][0]["content"]["report"]["schema_version"] == "gpqa-report.v1"
    assert evidence_input["evidence"][1]["content"]["raw"]["case_id"] == "sample-1"


class FakeSession:
    def __init__(self, report, target, resource, existing=None, evaluated_target=None):
        self.report = report
        self.target = target
        self.resource = resource
        self.existing = existing
        self.evaluated_target = evaluated_target
        self.added = []
        self.scalar_calls = 0

    async def scalar(self, _query):
        self.scalar_calls += 1
        return self.report if self.scalar_calls == 1 else self.existing

    async def get(self, model, identifier):
        candidates = {
            "TargetBinding": [self.target, self.evaluated_target],
            "ResourceBinding": [self.resource],
        }[model.__name__]
        return next((row for row in candidates if row is not None and row.id == identifier), None)

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        pass


def configured_settings():
    return SimpleNamespace(
        analysis_target_id=uuid.uuid4(),
        analysis_resource_id=uuid.uuid4(),
        analysis_activity_queue="demo.result.v1",
        analysis_workflow_queue="demo.workflow.v1",
    )


def isolated_analysis_setup():
    settings = configured_settings()
    evaluated_target = SimpleNamespace(
        id=uuid.uuid4(),
        model_version_id=uuid.uuid4(),
        base_url="https://api.deepseek.com",
        remote_model="evaluated-model",
        credential_ref="f" * 32,
    )
    evaluated_resource_id = uuid.uuid4()
    report = SimpleNamespace(
        id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        sha256="a" * 64,
        snapshot_sha256="d" * 64,
        execution_origin="real",
    )
    snapshot = SimpleNamespace(
        sha256="d" * 64,
        canonical_body={
            "target_id": str(evaluated_target.id),
            "model_version_id": str(evaluated_target.model_version_id),
            "remote_model": evaluated_target.remote_model,
            "resource_id": str(evaluated_resource_id),
            "resource_kind": "docker_local",
            "container_id": "evaluated-container",
        },
    )
    target = SimpleNamespace(
        id=settings.analysis_target_id,
        model_version_id=uuid.uuid4(),
        base_url="https://api.deepseek.com",
        remote_model="analysis-model",
        credential_ref="0" * 32,
    )
    resource = SimpleNamespace(
        id=settings.analysis_resource_id,
        container_id="analysis-container",
        image_id="analysis-image",
    )
    session = FakeSession(report, target, resource, evaluated_target=evaluated_target)
    return SimpleNamespace(
        settings=settings,
        report=report,
        snapshot=snapshot,
        target=target,
        resource=resource,
        evaluated_target=evaluated_target,
        evaluated_resource_id=evaluated_resource_id,
        session=session,
    )


async def test_enqueue_is_idempotent_and_snapshot_contains_no_credential_ref():
    setup = isolated_analysis_setup()
    settings = setup.settings
    report = setup.report
    snapshot = setup.snapshot
    target = setup.target
    resource = setup.resource
    session = setup.session
    job, created = await enqueue_analysis(
        session,
        settings,
        report,
        snapshot,
        trigger="report_created",
        baseline_comparison={"status": "missing"},
    )
    assert created is True
    assert isinstance(job, ReportAnalysis)
    assert "credential_ref" not in job.target_snapshot
    assert "api_key" not in job.target_snapshot
    assert job.baseline_comparison == {"status": "missing"}
    assert len(job.baseline_comparison_sha256) == 64
    assert len(session.added) == 2
    assert session.added[1].kind == "ANALYZE_REPORT"

    duplicate = FakeSession(
        report,
        target,
        resource,
        existing=job,
        evaluated_target=setup.evaluated_target,
    )
    same, created = await enqueue_analysis(
        duplicate, settings, report, snapshot, trigger="manual_retry"
    )
    assert same is job and created is False
    assert duplicate.added == []


async def test_manual_start_preserves_the_single_failure_retry():
    setup = isolated_analysis_setup()
    job, created = await enqueue_analysis(
        setup.session,
        setup.settings,
        setup.report,
        setup.snapshot,
        trigger="manual_start",
    )
    assert created is True
    assert job.trigger == "manual_start"
    assert job.retry_count == 0

    job.state = "failed"
    retry_session = RetrySession(job)
    retried, retried_created = await retry_existing_analysis(retry_session, setup.settings, job)
    assert retried is job
    assert retried_created is True
    assert job.state == "queued"
    assert job.retry_count == 1


async def test_enqueue_rejects_non_real_report():
    setup = isolated_analysis_setup()
    setup.report.execution_origin = "simulation"

    with pytest.raises(ValueError, match="real execution"):
        await enqueue_analysis(
            setup.session,
            setup.settings,
            setup.report,
            setup.snapshot,
            trigger="report_created",
        )
    assert setup.session.added == []


@pytest.mark.parametrize(
    "violation",
    [
        "same_target_binding",
        "same_model_version",
        "same_remote_model",
        "same_credential",
        "same_resource_binding",
        "same_container",
    ],
)
async def test_enqueue_requires_strictly_independent_analysis_bindings(violation):
    setup = isolated_analysis_setup()
    if violation == "same_target_binding":
        setup.settings.analysis_target_id = setup.evaluated_target.id
    elif violation == "same_model_version":
        setup.target.model_version_id = setup.evaluated_target.model_version_id
    elif violation == "same_remote_model":
        setup.target.remote_model = setup.evaluated_target.remote_model
    elif violation == "same_credential":
        setup.target.credential_ref = setup.evaluated_target.credential_ref
    elif violation == "same_resource_binding":
        setup.resource.id = setup.evaluated_resource_id
        setup.settings.analysis_resource_id = setup.evaluated_resource_id
    elif violation == "same_container":
        setup.resource.container_id = "evaluated-container"

    with pytest.raises(ValueError, match="Independent analysis"):
        await enqueue_analysis(
            setup.session,
            setup.settings,
            setup.report,
            setup.snapshot,
            trigger="report_created",
        )
    assert setup.session.added == []


def test_analysis_capability_requires_two_separate_explicit_bindings():
    settings = configured_settings()
    assert analysis_is_configured(settings)
    settings.analysis_resource_id = None
    assert not analysis_is_configured(settings)


class RetrySession:
    def __init__(self, job):
        self.job = job
        self.added = []

    async def scalar(self, _query):
        return self.job

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        pass


async def test_failed_analysis_retries_same_job_at_most_once():
    settings = configured_settings()
    job = ReportAnalysis(
        id=uuid.uuid4(),
        report_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        state="failed",
        trigger="report_created",
        retry_count=0,
        report_sha256="a" * 64,
        snapshot_sha256="d" * 64,
        target_snapshot={},
        evidence_catalog={},
        error_code="INVALID_MODEL_OUTPUT",
    )
    session = RetrySession(job)
    retried, created = await retry_existing_analysis(session, settings, job)
    assert retried is job and created is True
    assert job.state == "queued" and job.retry_count == 1
    assert job.error_code is None
    assert len(session.added) == 1
    assert session.added[0].dedup_key == f"analysis:{job.id}:manual-retry"
    assert session.added[0].payload["retry_count"] == 1

    job.state = "failed"
    duplicate = RetrySession(job)
    same, created = await retry_existing_analysis(duplicate, settings, job)
    assert same is job and created is False
    assert duplicate.added == []


async def test_unresolved_analysis_is_never_retried_blindly():
    settings = configured_settings()
    job = ReportAnalysis(
        id=uuid.uuid4(),
        report_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        state="unresolved",
        trigger="report_created",
        retry_count=0,
        report_sha256="a" * 64,
        snapshot_sha256="d" * 64,
        target_snapshot={},
        evidence_catalog={},
    )
    session = RetrySession(job)
    same, created = await retry_existing_analysis(session, settings, job)
    assert same is job and created is False
    assert job.state == "unresolved" and job.retry_count == 0
    assert session.added == []


def test_nested_facts_and_inferences_use_the_same_evidence_allowlist():
    with pytest.raises(ValueError, match="citation"):
        validate_analysis_output(
            {
                "summary": "Summary",
                "evidence_ids": ["run.summary"],
                "facts": [{"statement": "Unsupported fact", "evidence_ids": ["foreign"]}],
                "inferences": [],
                "recommended_actions": [],
            },
            CATALOG,
        )


def test_uncited_inference_must_explicitly_state_insufficient_evidence():
    body = {
        "summary": "Summary",
        "evidence_ids": ["run.summary"],
        "facts": [],
        "inferences": [{"statement": "The model is weak at science", "evidence_ids": []}],
        "recommended_actions": [],
    }
    with pytest.raises(ValueError, match="insufficient evidence"):
        validate_analysis_output(body, CATALOG)

    body["inferences"][0]["statement"] = "Insufficient evidence to attribute the failure"
    validated = validate_analysis_output(body, CATALOG)
    assert validated["inferences"][0]["evidence_ids"] == []


def analysis_target_snapshot():
    return {
        "target_id": str(uuid.uuid4()),
        "model_version_id": str(uuid.uuid4()),
        "base_url": "https://api.deepseek.com",
        "remote_model": "deepseek-flash",
        "resource_id": str(uuid.uuid4()),
        "container_id": "container",
        "image_id": "image",
        "credential_ref": "secret-reference",
    }


def test_invalid_model_citation_is_archived_as_failed_output():
    native = {
        "state": "succeeded",
        "response": json.dumps(
            {
                "summary": "Unsupported conclusion",
                "evidence_ids": [f"attempt:{uuid.uuid4()}"],
                "facts": [],
                "inferences": [],
                "recommended_actions": ["Approve immediately"],
            }
        ),
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    result = build_analysis_result(
        native,
        {"evidence_catalog": CATALOG},
        analysis_id=uuid.uuid4(),
        report_id=uuid.uuid4(),
        input_sha256="e" * 64,
        target_snapshot=analysis_target_snapshot(),
    )
    assert result["state"] == "failed"
    assert result["error_code"] == "INVALID_MODEL_OUTPUT"
    assert result["analysis"] is None
    assert result["raw_response"] == native["response"]
    assert "secret-reference" not in json.dumps(result)


def test_valid_model_output_is_immutable_analysis_not_a_gate_decision():
    native = {
        "state": "succeeded",
        "response": json.dumps(
            {
                "summary": "Summary",
                "evidence_ids": ["run.summary"],
                "facts": [],
                "inferences": [],
                "recommended_actions": [],
            }
        ),
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    result = build_analysis_result(
        native,
        {"evidence_catalog": CATALOG},
        analysis_id=uuid.uuid4(),
        report_id=uuid.uuid4(),
        input_sha256="e" * 64,
        target_snapshot=analysis_target_snapshot(),
    )
    assert result["state"] == "succeeded"
    assert result["analysis"]["evidence_ids"] == ["run.summary"]
    assert result["raw_response"] == native["response"]
    assert set(result) == {
        "schema_version",
        "analysis_id",
        "report_id",
        "input_sha256",
        "state",
        "analysis",
        "raw_response",
        "error_code",
        "finish_reason",
        "usage",
        "model",
    }


async def test_activity_freezes_input_before_running(monkeypatch):
    import hashlib
    from pathlib import Path

    from autoeval import analysis_activity
    from autoeval.datasets import canonical_manifest

    snapshot_body = {"config_version_id": "config", "parameters": {}, "tool": {}}
    snapshot_digest = hashlib.sha256(canonical_manifest(snapshot_body)).hexdigest()
    target_id = uuid.uuid4()
    resource_id = uuid.uuid4()
    model_version_id = uuid.uuid4()
    job = SimpleNamespace(
        id=uuid.uuid4(),
        report_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        state="queued",
        report_sha256="a" * 64,
        snapshot_sha256=snapshot_digest,
        baseline_comparison=None,
        baseline_comparison_sha256=None,
        target_snapshot={
            "target_id": str(target_id),
            "model_version_id": str(model_version_id),
            "base_url": "https://api.deepseek.com",
            "remote_model": "analysis-model",
            "resource_id": str(resource_id),
            "container_id": "analysis-container",
            "image_id": "analysis-image",
        },
        input_object_key=None,
        input_sha256=None,
        evidence_catalog={},
        object_key=None,
        sha256=None,
        error_code=None,
        usage=None,
        started_at=None,
        finished_at=None,
    )
    report = SimpleNamespace(
        id=job.report_id, sha256=job.report_sha256, snapshot_sha256=snapshot_digest
    )
    snapshot = SimpleNamespace(sha256=snapshot_digest, canonical_body=snapshot_body)
    target = SimpleNamespace(
        id=target_id,
        model_version_id=model_version_id,
        base_url="https://api.deepseek.com",
        remote_model="analysis-model",
        credential_ref="0" * 32,
    )
    resource = SimpleNamespace(
        id=resource_id, container_id="analysis-container", image_id="analysis-image"
    )

    class ActivitySession:
        def __init__(self, initial):
            self.initial = initial
            self.scalar_calls = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        def begin(self):
            return self

        async def scalar(self, _query):
            self.scalar_calls += 1
            return snapshot if self.initial and self.scalar_calls == 2 else job

        async def scalars(self, _query):
            return SimpleNamespace(all=list)

        async def get(self, model, _identifier):
            return {
                "EvaluationReport": report,
                "TargetBinding": target,
                "ResourceBinding": resource,
            }[model.__name__]

    session_count = 0

    def sessions():
        nonlocal session_count
        session = ActivitySession(initial=session_count == 0)
        session_count += 1
        return session

    class Engine:
        async def dispose(self):
            pass

    settings = SimpleNamespace(
        allowed_model_hosts=["api.deepseek.com"],
        docker_executable="docker",
        secret_store_path=Path("unused"),
        analysis_max_output_tokens=64,
    )
    events = []

    def write_manifest(_settings, key, _raw):
        if key.startswith("analysis-inputs/"):
            assert job.state == "queued"
            assert job.input_sha256 is None
        events.append((key.split("/")[0], job.state))

    def invoke(_settings, _container_id, _payload):
        assert job.state == "running"
        assert job.input_sha256 is not None
        assert "run.summary" in job.evidence_catalog
        events.append(("model", job.state))
        return {
            "state": "succeeded",
            "response": json.dumps(
                {
                    "summary": "Summary",
                    "evidence_ids": ["run.summary"],
                    "facts": [],
                    "inferences": [],
                    "recommended_actions": [],
                }
            ),
            "usage": {},
        }

    monkeypatch.setattr(analysis_activity, "get_settings", lambda: settings)
    monkeypatch.setattr(
        analysis_activity, "create_database", lambda _settings: (Engine(), sessions)
    )
    monkeypatch.setattr(
        analysis_activity,
        "read_manifest",
        lambda *_args: {"schema_version": "gpqa-report.v1", "summary": {}},
    )
    monkeypatch.setattr(analysis_activity, "write_manifest", write_manifest)
    monkeypatch.setattr(
        analysis_activity,
        "inspect_executor",
        lambda *_args: {"running": True, "image_id": "analysis-image"},
    )
    monkeypatch.setattr(analysis_activity.FileSecretStore, "get", lambda *_args: "secret")
    monkeypatch.setattr(analysis_activity.activity, "heartbeat", lambda *_args: None)
    monkeypatch.setattr(analysis_activity, "invoke", invoke)

    response = await analysis_activity.execute_report_analysis(str(job.id))

    assert response["state"] == "succeeded"
    assert events == [
        ("analysis-inputs", "queued"),
        ("model", "running"),
        ("analysis-results", "running"),
    ]


def test_analysis_api_and_capabilities_reflect_explicit_configuration():
    unconfigured = Settings(environment="test", analysis_target_id=None, analysis_resource_id=None)
    with TestClient(create_app(unconfigured)) as client:
        response = client.post(f"/api/autoeval/v1/reports/{uuid.uuid4()}/analysis:retry")
        assert response.status_code == 503
        start_response = client.post(f"/api/autoeval/v1/reports/{uuid.uuid4()}/analysis:start")
        assert start_response.status_code == 503
        capabilities = client.get("/api/autoeval/v1/system/capabilities").json()["data"]
        assert capabilities["analysis_enabled"] is False

    configured = Settings(
        environment="test",
        analysis_target_id=uuid.uuid4(),
        analysis_resource_id=uuid.uuid4(),
    )
    with TestClient(create_app(configured)) as client:
        capabilities = client.get("/api/autoeval/v1/system/capabilities").json()["data"]
        assert capabilities["analysis_enabled"] is True


def test_outbox_metadata_accepts_analysis_intents():
    from autoeval.models import Outbox

    assert any(
        "ANALYZE_REPORT" in str(constraint.sqltext)
        for constraint in Outbox.__table_args__
        if hasattr(constraint, "sqltext")
    )


def test_report_analysis_status_distinguishes_disabled_unqueued_and_actual_state():
    settings = SimpleNamespace(analysis_target_id=None, analysis_resource_id=None)
    assert analysis_status_for(None, settings) == "not_enabled"
    settings.analysis_target_id = uuid.uuid4()
    settings.analysis_resource_id = uuid.uuid4()
    assert analysis_status_for(None, settings) == "not_queued"
    assert analysis_status_for(SimpleNamespace(state="running"), settings) == "running"
    assert (
        analysis_status_for(
            SimpleNamespace(state="queued"), settings, execution_origin="simulation"
        )
        == "not_enabled"
    )


def test_analysis_prompt_requests_chinese_without_changing_evidence():
    from autoeval.analysis import analysis_prompt

    evidence = {"evidence_catalog": {"case.0": {"dataset": "GPQA_diamond", "score": 0}}}
    prompt = analysis_prompt(evidence)
    assert "简体中文" in prompt
    assert "summary、facts 和 inferences" in prompt
    assert "recommended_actions 中的每一条建议" in prompt
    assert "证据不足" in prompt
    assert json.loads(prompt.split("\n", 1)[1]) == evidence
