import asyncio
import copy
import hashlib
import json
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import func, select

from autoeval.adaptor_bridge import invocation
from autoeval.adaptor_versions import CURRENT_ADAPTOR, LEGACY_ADAPTOR, separate_runtime
from autoeval.case_history import CaseExecutionRecord
from autoeval.config import Settings
from autoeval.db import create_database
from autoeval.models import EvaluationRun
from autoeval.standard_inputs import Bundle, CompileInput, compile_config, discover
from autoeval.unified_outputs import archive_output, parse_output, read_raw_output

FIXTURES = Path(__file__).parent / "fixtures"


def small():
    body = json.loads((FIXTURES / "unified_outputs/gpqa.json").read_bytes())
    body["cases"] = body["cases"][:2]
    body["case_count"] = 2
    return body


def parse(body, expected=None):
    return parse_output(json.dumps(body).encode(), expected or {"GPQA_diamond": ["0", "1"]})


@pytest.mark.parametrize("name", ["gpqa", "deepswe"])
def test_fixed_upstream_samples(name):
    raw = (FIXTURES / f"unified_outputs/{name}.json").read_bytes()
    body = json.loads(raw)
    dataset = "DeepSWE"
    expected = {}
    for case in body["cases"]:
        key = case.get("dataset", dataset)
        expected.setdefault(key, [])
        if case["case_id"] not in expected[key]:
            expected[key].append(case["case_id"])
    cases, summary = parse_output(raw, expected, dataset)
    assert len(cases) == body["case_count"]
    assert summary["token_coverage"]["total_attempts"] == len(cases)
    assert summary["metric_conversion"] == "not_verified"


def test_nine_standard_pairs_and_runtime_split():
    original = Bundle.model_validate_json((FIXTURES / "standard_inputs/upstream.json").read_bytes())
    minimax = Bundle.model_validate_json((FIXTURES / "standard_inputs/minimax.json").read_bytes())
    assert len(discover(original)) + len(discover(minimax)) == 9
    for entry in discover(minimax):
        result = compile_config(entry, CompileInput())
        assert result["adaptor_revision"] == CURRENT_ADAPTOR
        assert result["cli_flags"] == []
        assert "execution" not in result["config"]["params"]
        assert "container_name" in result["runtime_config"]
    cfg = {"params": {"execution": "docker", "work_dir": "/a"}}
    legacy, _, flags = separate_runtime(cfg, LEGACY_ADAPTOR)
    assert legacy == cfg and flags == ["--no-template"]


def test_zero_score_success_missing_token_and_time_diagnostics():
    body = small()
    body["cases"][0].update(
        score=0, input_tokens=None, output_tokens=None, finished_at="2020-01-01T00:00:00Z"
    )
    cases, summary = parse(body)
    assert cases[0]["status"] == "success"
    assert "reversed_tool_time" in cases[0]["diagnostics"]
    assert summary["validity"] == "complete"  # timing diagnostics do not rescore
    assert summary["token_coverage"]["known_attempts"] == 1


def test_missing_failed_and_retry_history():
    body = small()
    failed = copy.deepcopy(body["cases"][0])
    failed.update(status="failed", score=None)
    body["cases"][0]["attempt"] = 2
    body["cases"].append(failed)
    body["case_count"] = 3
    cases, summary = parse(body, {"GPQA_diamond": ["0", "1", "missing"]})
    assert len(cases) == 3
    assert summary["missing_cases"] == 1 and summary["failed_cases"] == 0
    assert summary["validity"] == "partial"


@pytest.mark.parametrize("change", ["schema", "duplicate", "count", "tokens", "foreign"])
def test_invalid_contract(change):
    body = small()
    if change == "schema":
        body["schema_version"] = 2
    if change == "duplicate":
        body["cases"][1] = body["cases"][0]
    if change == "count":
        body["case_count"] = 1
    if change == "tokens":
        body["cases"][0]["input_tokens"] = True
    if change == "foreign":
        body["cases"][0]["case_id"] = "foreign"
    with pytest.raises(ValueError):
        parse(body)


def test_explicit_owned_collection_and_unique_execution_directory():
    config = {
        "adaptor_revision": CURRENT_ADAPTOR,
        "input_ready": True,
        "config": {"params": {"work_dir": "/preview"}},
    }
    key = str(uuid.uuid4())
    plan = invocation(config, key, "/owned", "a" * 40)
    assert plan["config"]["params"]["work_dir"] == f"/owned/jobs/{key}/output"
    collected = invocation(
        config, key, "/owned", "a" * 40, f"/owned/jobs/{key}/output/20260101_000000"
    )
    assert "--collect-results" in collected["command"]
    with pytest.raises(ValueError):
        invocation(config, key, "/owned", "a" * 40, "/foreign")


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("AE_INFRA_TEST") != "1", reason="Requires real PostgreSQL and S3")
async def test_archive_idempotency_conflict_and_original_bytes():
    settings = Settings(environment="test")
    engine, sessions = create_database(settings)
    run_id = uuid.uuid4()
    try:
        async with sessions() as session, session.begin():
            session.add(
                EvaluationRun(
                    id=run_id,
                    trigger_scope="TEST-unified",
                    idempotency_key=str(run_id),
                    request_hash="a" * 64,
                    run_mode="exploration",
                    created_by="TEST-unified",
                    state="completed",
                )
            )
        raw = json.dumps(small(), indent=2).encode()
        expected = {"GPQA_diamond": ["0", "1"]}
        first, duplicate = await asyncio.gather(
            archive_output(sessions, settings, run_id, "test", raw, expected),
            archive_output(sessions, settings, run_id, "test", raw, expected),
        )
        assert first == duplicate
        async with sessions() as session:
            assert (
                await session.scalar(
                    select(func.count())
                    .select_from(CaseExecutionRecord)
                    .where(CaseExecutionRecord.run_id == run_id)
                )
                == 2
            )
        artifact = {
            "object_key": f"unified-results/{run_id}/{hashlib.sha256(raw).hexdigest()}.json",
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
        assert await asyncio.to_thread(read_raw_output, settings, artifact) == raw
        bad = small()
        bad["cases"][0]["score"] = 0
        with pytest.raises(ValueError):
            await archive_output(
                sessions, settings, run_id, "test", json.dumps(bad).encode(), expected
            )
        with pytest.raises(ValueError):
            await archive_output(
                sessions, settings, run_id, "different", json.dumps(bad).encode(), expected
            )
    finally:
        await engine.dispose()
