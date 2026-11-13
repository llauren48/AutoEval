"""Input-only contracts: no model calls and no Docker creation."""

import asyncio
import copy
import os
import subprocess
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from autoeval.api import create_app
from autoeval.config import Settings
from autoeval.import_standard_inputs import read_directory
from autoeval.standard_inputs import (
    ADAPTOR_REVISION,
    Bundle,
    CompileInput,
    compile_config,
    digest,
    discover,
    merge,
)

FIXTURE = Path(__file__).parent / "fixtures/standard_inputs/upstream.json"


def bundle():
    return Bundle.model_validate_json(FIXTURE.read_bytes())


def target():
    return SimpleNamespace(
        id=uuid.uuid4(), remote_model="fixture-model", base_url="https://example.com/v1"
    )


def test_six_pinned_standard_pairs_and_baselines_are_preserved():
    source = bundle()
    entries = discover(source)
    assert len(entries) == 6
    assert {row["mapping"]["dataset"] for row in entries} == {"GPQA", "MMMU-Pro", "DeepSWE"}
    for entry in entries:
        assert entry["startup_missing"]
        for item in entry["files"].values():
            assert source.files[item["path"]] == item["raw"]


@pytest.mark.parametrize("index", range(6))
def test_compile_full_inputs_without_activating_execution(index):
    entry = discover(bundle())[index]
    result = compile_config(
        entry,
        CompileInput(
            model_id=uuid.uuid4(),
            template={"params": {"max_num_workers": 1}},
            tool_root="/benchmark",
            datasets_dir="/data",
            task_path="/data/deep-swe/tasks" if entry["benchmark"] == "DeepSWE" else None,
        ),
        target(),
    )
    assert result["input_ready"]
    assert not result["execution_enabled"]
    assert result["approval_status"] == "not_required"
    assert result["cli_flags"] == []
    assert "api_key" not in result["config"]["params"]
    assert result["runtime_config"]["execution"] == "docker"
    assert result["adaptor_revision"] == "955089ab32ef696c231d628f5ad0d9da28f430c4"


@pytest.mark.parametrize("index", range(6))
def test_missing_startup_requires_platform_template(index):
    entry = discover(bundle())[index]
    result = compile_config(
        entry,
        CompileInput(
            model_id=uuid.uuid4(),
            adjustments={"params": {"max_num_workers": 1}},
            tool_root="/benchmark",
            datasets_dir="/data",
            task_path="/data/deep-swe/tasks" if entry["benchmark"] == "DeepSWE" else None,
        ),
        target(),
    )
    assert not result["input_ready"]
    assert "startup_template_required" in result["blocking_reasons"]
    assert not result["execution_enabled"]


def test_nested_merge_arrays_and_locked_precedence():
    assert merge({"a": {"b": 1, "c": [1]}}, {"a": {"c": [2, 3]}}) == {"a": {"b": 1, "c": [2, 3]}}
    entry = next(row for row in discover(bundle()) if row["benchmark"] == "GPQA_Diamond")
    result = compile_config(
        entry,
        CompileInput(
            adjustments={
                "params": {"generation_kwargs": {"temperature": 0}, "datasets": ["changed"]}
            }
        ),
    )
    assert result["config"]["params"]["datasets"] == ["gpqa_gen_0_shot_cot_chat_prompt"]
    assert result["config"]["params"]["generation_kwargs"]["temperature"] == 1
    assert result["overrides"]
    assert "model_mapping_missing" in result["blocking_reasons"]
    assert "target_missing" in result["blocking_reasons"]


@pytest.mark.parametrize(
    "params",
    [
        {"unknown": 1},
        {"model_overrides": {"max_out_len": 1}},
        {"extra_cli_args": ["--reuse", "old"]},
        {"max_out_len": "wrong"},
        {"datasets": [1]},
        {"model_type": False},
        {"batch_size": 0},
        {"work_dir": "/foreign"},
        {"api_key": "do-not-store"},
    ],
)
def test_invalid_parameters_and_bypasses_are_rejected(params):
    entry = discover(bundle())[0]
    with pytest.raises(ValueError):
        compile_config(entry, CompileInput(adjustments={"params": params}))


def test_environment_cannot_override_locked_task_path():
    entry = next(row for row in discover(bundle()) if row["benchmark"] == "DeepSWE")
    result = compile_config(entry, CompileInput(task_path="/different"))
    assert not result["input_ready"]
    assert "environment_conflicts_with_standard:path" in result["blocking_reasons"]
    assert result["config"]["params"]["path"] == "/data/deep-swe/tasks"


@pytest.mark.parametrize("raw", ['{"x":1,"x":2}', '{"x":NaN}', '{"api_key":"secret"}'])
def test_source_invalid_json_and_credentials_rejected(raw):
    with pytest.raises(ValidationError):
        Bundle(revision="a" * 40, files={"M/DeepSWE/standard_config.json": raw})


def test_offline_bundle_rejects_symlink_and_preserves_bytes(tmp_path):
    source = bundle()
    for path, raw in source.files.items():
        file = tmp_path / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(raw.encode())
    assert read_directory(tmp_path, source.revision).files == source.files


@pytest.mark.skipif(
    not os.getenv("AE_ADAPTOR_SOURCE"), reason="Requires pinned upstream adaptor checkout"
)
def test_pinned_adaptor_prepare_all_six_without_execution(tmp_path, monkeypatch):
    root = Path(os.environ["AE_ADAPTOR_SOURCE"])
    assert (
        subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
        == ADAPTOR_REVISION
    )
    sys.path.insert(0, str(root / "src"))
    try:
        from autoeval_adaptor.registry import get_adapter

        benchmark = tmp_path / "benchmark"
        marker = benchmark / "ais_bench/benchmark/cli/main.py"
        marker.parent.mkdir(parents=True)
        marker.write_text("# fixture only", encoding="utf-8")
        for path, content in {
            "configs/models/vllm_api/vllm_api_general_chat.py": "models = []",
            "configs/datasets/gpqa_gen_0_shot_cot_chat_prompt.py": "gpqa_datasets = []",
            "configs/datasets/mmmu_pro_options10_gen.py": "mmmu_datasets = []",
        }.items():
            file = benchmark / path
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(content, encoding="utf-8")
        monkeypatch.setattr(
            subprocess, "Popen", lambda *a, **k: pytest.fail("Must not launch a process")
        )
        for entry in discover(bundle()):
            params = compile_config(
                entry,
                CompileInput(
                    model_id=uuid.uuid4(),
                    template={"params": {"max_num_workers": 1}},
                    tool_root=str(benchmark).replace("\\", "/")
                    if os.name != "nt"
                    else "/benchmark",
                    datasets_dir="/data",
                ),
                target(),
            )["config"]["params"]
            params["ais_bench_root"] = str(benchmark)
            prepared = get_adapter("ais_bench", entry["mapping"]["subcommand"]).prepare(params)
            assert prepared.config_content
            assert prepared.command
            assert not prepared.config_path.exists()
    finally:
        sys.path.remove(str(root / "src"))


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("AE_INFRA_TEST") != "1", reason="Requires real PostgreSQL")
async def test_postgres_concurrent_import_conflict_and_immutable_history():
    app = create_app(Settings(environment="test"))
    source = bundle().model_dump()
    source["revision"] = uuid.uuid4().hex + uuid.uuid4().hex[:8]
    source["files"] = {"TEST-" + path: value for path, value in source["files"].items()}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            base = "/api/autoeval/v1/standard-inputs"
            preview = await client.post(base + "/preview", json=source)
            assert preview.status_code == 200, preview.text
            body = {"bundle": source, "expected_sha256": digest(source)}
            a, b = await asyncio.gather(
                client.post(base + "/import", json=body), client.post(base + "/import", json=body)
            )
            assert a.status_code == b.status_code == 201, (a.text, b.text)
            assert [r["id"] for r in a.json()["data"]] == [r["id"] for r in b.json()["data"]]
            identifier = a.json()["data"][0]["id"]
            changed = copy.deepcopy(source)
            first = next(iter(changed["files"]))
            changed["files"][first] += "\n"
            conflict = await client.post(
                base + "/import", json={"bundle": changed, "expected_sha256": digest(changed)}
            )
            assert conflict.status_code == 409
            suggestion = await client.post(base + f"/{identifier}/autocomplete", json={})
            assert suggestion.status_code == 200, suggestion.text
            assert suggestion.json()["data"]["requires_confirmation"]
            before = (await client.get(base + f"/{identifier}")).json()["data"]
            assert before["compilations"] == []
            from autoeval.registry import Asset

            source_name = before["source_model"]
            async with app.state.sessions() as session, session.begin():
                matched_model = Asset(
                    asset_key=source_name.swapcase(), kind="model", name="Case fixture"
                )
                session.add(matched_model)
                await session.flush()
                matched_id = matched_model.id
            try:
                matched = await client.post(base + f"/{identifier}/autocomplete", json={})
                assert matched.status_code == 200, matched.text
                assert matched.json()["data"]["candidate"]["model_id"] == str(matched_id)
                async with app.state.sessions() as session, session.begin():
                    ambiguous_model = Asset(
                        asset_key=source_name, kind="model", name="Ambiguous fixture"
                    )
                    session.add(ambiguous_model)
                    await session.flush()
                    ambiguous_id = ambiguous_model.id
                try:
                    ambiguous = await client.post(base + f"/{identifier}/autocomplete", json={})
                    assert ambiguous.json()["data"]["candidate"]["model_id"] is None
                finally:
                    async with app.state.sessions() as session, session.begin():
                        await session.delete(await session.get(Asset, ambiguous_id))
            finally:
                async with app.state.sessions() as session, session.begin():
                    await session.delete(await session.get(Asset, matched_id))

            assert not suggestion.json()["data"]["preview"]["execution_enabled"]
            compiled = await client.post(base + f"/{identifier}/compile", json={})
            assert compiled.status_code == 200, compiled.text
            assert not compiled.json()["data"]["execution_enabled"]
            assert (await client.post(base + f"/{identifier}/compile", json={})).json()["data"][
                "sha256"
            ] == compiled.json()["data"]["sha256"]
            altered = await client.post(
                base + f"/{identifier}/compile", json={"template": {"params": {"debug": True}}}
            )
            assert altered.status_code == 409
            detail = (await client.get(base + f"/{identifier}")).json()["data"]
            assert len(detail["compilations"]) == 1
        async with app.state.sessions() as session:
            with pytest.raises(DBAPIError):
                await session.execute(
                    text("UPDATE standard_input_version SET source_model = 'changed' WHERE id=:id"),
                    {"id": uuid.UUID(identifier)},
                )
            await session.rollback()
