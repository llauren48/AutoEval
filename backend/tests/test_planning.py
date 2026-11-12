"""Planner contracts and scheduling tests; all model data here is artificial."""

import copy
import json
import sys
from datetime import UTC, datetime

import pytest
from autoeval_planner.library.db import Library
from pydantic import ValidationError

from autoeval.orchestration import demand_for, fits
from autoeval.planning import PlanInput, ProbeInput, selection_identity
from autoeval.planning_engine import apply_concurrency, calculate, import_history, selected_samples
from autoeval.planning_runtime import validate_spec


def inputs():
    return {
        "target": {"base_url": "https://fixture.invalid", "model": "TEST-no-model"},
        "dataset_concurrency": 1,
        "runs": 4,
        "seed": 42,
        "tasks": [
            {
                "task_id": "gpqa",
                "dataset": "GPQA",
                "attempts": 1,
                "concurrency": 2,
                "samples": [{"sample_id": "0"}, {"sample_id": "1"}],
                "compilation": {
                    "config": {
                        "tool": "ais_bench",
                        "subcommand": "llm_accuracy",
                        "params": {"datasets": ["fixture_gpqa"], "batch_size": 2},
                    },
                    "locked_fields": {},
                },
            }
        ],
    }


def test_preflight_selection_tracks_compilation_manifest_and_profile():
    data = inputs()
    data["tasks"][0].update(
        compilation_id="compilation", manifest_id="manifest", profile_sha256="profile"
    )
    snapshot = copy.deepcopy(data)
    snapshot["tasks"].reverse()
    assert selection_identity(snapshot) == selection_identity(data)
    for field in ("compilation_id", "manifest_id", "profile_sha256"):
        changed = copy.deepcopy(data)
        changed["tasks"][0][field] = "changed"
        assert selection_identity(changed) != selection_identity(data)


def test_sample_selection_honors_offset_limit_and_harbor_attempt_selection():
    samples = [{"sample_id": str(i)} for i in range(8)]
    config = inputs()["tasks"][0]["compilation"]["config"]
    config["params"].update(dataset_test_range="[3:7]", num_prompts=2)
    assert [s["sample_id"] for s in selected_samples(config, samples)] == ["3", "4"]
    harbor = {
        "subcommand": "harbor_agent",
        "params": {
            "task_names": ["2", "4", "6"],
            "exclude_task_names": ["4"],
            "n_tasks": 1,
            "n_attempts": 3,
        },
    }
    assert selected_samples(harbor, samples) == [{"sample_id": "2"}]
    harbor["params"]["task_names"] = ["missing"]
    with pytest.raises(ValueError, match="Unknown"):
        selected_samples(harbor, samples)


@pytest.mark.skipif(sys.platform != "linux", reason="SSH job runtime is Linux-only")
def test_job_runtime_credential_file_is_private(tmp_path):
    from autoeval.job_runtime import write_credential

    path = tmp_path / "credential.json"
    write_credential(path, "TEST-only")
    assert json.loads(path.read_text()) == {"value": "TEST-only"}
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        write_credential(path, "replacement")


def test_standard_locks_are_revalidated_when_applying():
    compilation = inputs()["tasks"][0]["compilation"]
    compilation["locked_fields"] = {"params.batch_size": 2}
    with pytest.raises(ValueError, match="lock conflicts"):
        apply_concurrency(compilation, 8)
    changed = apply_concurrency(compilation, 2)
    assert changed["config"]["params"]["max_num_workers"] == 1
    assert "max_num_workers" not in compilation["config"]["params"]


def test_unknown_tokens_are_not_zero_filled_and_import_is_idempotent(tmp_path):
    lib = Library(tmp_path / "history.sqlite3")
    base = {
        "run_id": "TEST-history",
        "dataset": "GPQA",
        "sample_id": "0",
        "attempt": 1,
        "state": "succeeded",
        "finished_at": datetime.now(UTC).isoformat(),
        "metadata": {"input_tokens": 10, "output_tokens": 50, "duration_sec": 2},
    }
    try:
        import_history(
            lib,
            [
                base,
                {**base, "sample_id": "1", "metadata": {"input_tokens": None, "output_tokens": 3}},
            ],
        )
        import_history(lib, [base])
        assert len(lib.load_cases("GPQA")) == 1
        assert lib.load_cases("GPQA")[0]["output_tokens"] == 50
    finally:
        lib.close()


def test_history_matching_selected_samples_records_source_runs(tmp_path):
    history = [
        {
            "run_id": "TEST-real-run",
            "dataset": "GPQA",
            "sample_id": "0",
            "attempt": 1,
            "state": "succeeded",
            "finished_at": datetime.now(UTC).isoformat(),
            "metadata": {"input_tokens": 10, "output_tokens": 50, "overhead_sec": 1.0},
        }
    ]
    data = inputs()
    data["tasks"][0]["samples"] = [{"sample_id": "0"}]
    report = calculate(
        data, {"decode_tps": 100, "max_concurrency": 4}, None, history, tmp_path, recommend=False
    )
    task = report["tasks"][0]
    assert task["data_quality"] == "history"
    assert task["source_runs"] == ["TEST-real-run"]


def test_conditional_estimate_has_both_duration_views(tmp_path):
    report = calculate(inputs(), {"decode_tps": 100, "max_concurrency": 4}, None, [], tmp_path)
    assert report["conditional"]
    assert report["confidence"] == "low"
    task = report["tasks"][0]
    assert task["standalone_duration_sec"] > 0
    assert task["scheduled_duration_sec"] > 0
    assert task["scheduled_end_sec"] >= task["scheduled_start_sec"]
    interval = report["estimate"]["range"]
    assert interval["p10_sec"] <= interval["p50_sec"] <= interval["p90_sec"]


def test_fixed_parameters_are_estimated_without_silent_optimization(tmp_path):
    data = inputs()
    data["tasks"][0]["concurrency"] = 1
    report = calculate(
        data, {"decode_tps": 100, "max_concurrency": 4}, None, [], tmp_path, recommend=False
    )
    assert report["plan"]["case_concurrency"] == {"gpqa": 1}
    assert report["plan"]["dataset_concurrency"] == 1


def test_three_dataset_parallel_plan_and_attempt_counts(tmp_path):
    data = inputs()
    second = copy.deepcopy(data["tasks"][0])
    second.update(task_id="mmmu", dataset="MMMU-Pro")
    second["compilation"]["config"]["params"]["datasets"] = ["fixture_mmmu"]
    harbor = copy.deepcopy(second)
    harbor.update(task_id="deepswe", dataset="DeepSWE", attempts=2)
    harbor["compilation"]["config"] = {
        "tool": "ais_bench",
        "subcommand": "harbor_agent",
        "params": {
            "dataset_abbr": "fixture_swe",
            "path": "/TEST/tasks",
            "n_attempts": 2,
            "n_concurrent_trials": 2,
        },
    }
    data["tasks"] += [second, harbor]
    resource = {
        "cpu_cores": 16,
        "mem_mb": 131072,
        "disk_gb": 200,
        "network_available": 8,
        "captured_at": datetime.now(UTC).isoformat(),
    }
    report = calculate(data, {"decode_tps": 100, "max_concurrency": 4}, resource, [], tmp_path)
    assert len(report["tasks"]) == 3
    assert not report["conditional"]
    assert next(t for t in report["tasks"] if t["task_id"] == "deepswe")["case_count"] == 4
    assert report["plan"]["dataset_concurrency"] <= 3


def test_capacity_counts_existing_leases_and_reserves_headroom():
    resource = {"cpu_cores": 10, "mem_mb": 32768, "disk_gb": 100, "network_available": 4}
    demand = {"cpu": 4, "mem": 8192, "disk": 20, "networks": 2}
    assert fits(demand, demand, resource)
    assert not fits(demand, {**demand, "cpu": 4.1}, resource)
    item = inputs()["tasks"][0]
    item["dataset"] = "DeepSWE"
    assert demand_for(item)["cpu"] == 4.5
    assert not fits(demand_for(item), {"networks": 3}, resource)


def test_probe_input_limits_and_no_arbitrary_remote_commands():
    import uuid

    common = {
        "resource_id": uuid.uuid4(),
        "target_id": uuid.uuid4(),
        "resource_kind": "docker_local",
    }
    assert ProbeInput(**common).concurrency == [1, 2, 4, 8]
    for levels in ([8, 1], [1, 1], [0], [65]):
        with pytest.raises(ValidationError):
            ProbeInput(**common, concurrency=levels)
    with pytest.raises(ValueError):
        validate_spec({"kind": "shell", "command": "TEST"})
    with pytest.raises(ValidationError):
        PlanInput(**common, tasks=[])


async def test_planned_workflow_is_valid_in_temporal_sandbox():
    from temporalio import workflow
    from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

    from autoeval.workflows import PlannedEvaluationWorkflow

    SandboxedWorkflowRunner().prepare_workflow(
        workflow._Definition.must_from_class(PlannedEvaluationWorkflow)
    )
