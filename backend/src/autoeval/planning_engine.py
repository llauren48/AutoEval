"""Pinned Planner integration; pure calculations never contact a model endpoint."""

import copy
import re
from dataclasses import replace
from pathlib import Path

from autoeval_planner.config import parse_taskset
from autoeval_planner.library.db import CaseRecord, Library, ServerSnapshot
from autoeval_planner.library.profile import profile_from_rows
from autoeval_planner.optimize import OptimizationResult, _monte_carlo, optimize
from autoeval_planner.report import build_report
from autoeval_planner.resources import build_provider
from autoeval_planner.simulate import Plan, percentile, simulate
from autoeval_planner.taskset import build_task_models, plan_tasks
from autoeval_planner.throughput import ThroughputProfile

from autoeval.standard_inputs import digest, leaves, validate_config

PLANNER_REVISION = "0ee9abf49a57c5accc62df8900693cdcea88ea9a"
DATASETS = {"GPQA", "MMMU-Pro", "DeepSWE"}


def selected_samples(config, samples):
    """Use the same ordered selection for simulation and actual tool inputs."""
    params = config["params"]
    ids = [s["sample_id"] for s in samples]
    if len(ids) != len(set(ids)) or not ids:
        raise ValueError("Nonempty unique sample manifest required")
    if config["subcommand"] == "llm_accuracy":
        start, end = 0, len(ids)
        if params.get("dataset_test_range"):
            match = re.fullmatch(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]", params["dataset_test_range"])
            if not match:
                raise ValueError("Only contiguous dataset_test_range is supported")
            start, end = map(int, match.groups())
            if not 0 <= start < end <= len(ids):
                raise ValueError("Sample range exceeds manifest")
        if params.get("num_prompts"):
            end = min(end, start + int(params["num_prompts"]))
        selected = samples[start:end]
    else:
        included = set(params.get("task_names") or ids)
        excluded = set(params.get("exclude_task_names") or [])
        if included - set(ids):
            raise ValueError("Unknown Harbor task names")
        selected = [s for s in samples if s["sample_id"] in included - excluded]
        if params.get("n_tasks"):
            selected = selected[: int(params["n_tasks"])]
    if not selected:
        raise ValueError("Selection contains no samples")
    return selected


def apply_concurrency(compilation, concurrency):
    result = copy.deepcopy(compilation)
    config = result["config"]
    field = "batch_size" if config["subcommand"] == "llm_accuracy" else "n_concurrent_trials"
    config["params"][field] = concurrency
    # Each isolated child owns exactly one dataset; the platform owns outer concurrency.
    config["params"]["max_num_workers"] = 1
    conflicts = [
        key
        for key, value in result.get("locked_fields", {}).items()
        if leaves(config).get(key) != value
    ]
    if conflicts:
        raise ValueError("Standard lock conflicts: " + ", ".join(conflicts))
    validate_config(config)
    return result


def import_history(library, observations):
    """Only known token observations enter Planner; unknown tokens stay unknown upstream."""
    for observation in observations:
        meta = observation["metadata"]
        if meta.get("input_tokens") is None or meta.get("output_tokens") is None:
            continue
        dataset = observation["dataset"]
        task_type = "ais_bench/" + ("harbor_agent" if dataset == "DeepSWE" else "llm_accuracy")
        run_id = observation["run_id"]
        library.upsert_run(
            run_id=run_id,
            source_type="autoeval",
            source_uri="run:" + run_id,
            model_name=None,
            task_type=task_type,
            observed_concurrency=None,
            imported_at=observation["finished_at"],
            notes="Archived real case evidence",
        )
        library.upsert_dataset(
            dataset_key=dataset,
            task_type=task_type,
            display_name=dataset,
            created_at=observation["finished_at"],
        )
        library.add_cases(
            run_id,
            dataset,
            [
                CaseRecord(
                    case_id=observation["sample_id"],
                    attempt=observation["attempt"],
                    input_tokens=meta["input_tokens"],
                    output_tokens=meta["output_tokens"],
                    duration_sec=meta.get("duration_sec"),
                    overhead_sec=meta.get("overhead_sec") or 0,
                    status="success" if observation["state"] == "succeeded" else "failed",
                )
            ],
        )


def calculate(inputs, curve, snapshot, history, directory, *, recommend=True):
    """Build a private SQLite snapshot per job; database advisory lock owns cache writes."""
    directory = Path(directory)
    library = Library(directory / "history.sqlite3")
    try:
        import_history(library, history)
        task_specs = []
        for item in inputs["tasks"]:
            config = copy.deepcopy(item["compilation"]["config"])
            params = config["params"]
            alias = (
                params["datasets"][0]
                if config["subcommand"] == "llm_accuracy"
                else params.get("dataset_abbr", item["dataset"])
            )
            library.add_alias(alias, "ais_bench/" + config["subcommand"], item["dataset"])
            if recommend:
                # User setting is an upper bound; standard locks remain enforced on apply.
                params.pop(
                    "batch_size"
                    if config["subcommand"] == "llm_accuracy"
                    else "n_concurrent_trials",
                    None,
                )
            task_specs.append(
                {
                    "task_id": item["task_id"],
                    "config_object": config,
                    "case_count": len(item["samples"]),
                }
            )
        resource_spec = {"mode": "infinite"}
        if snapshot:
            library.upsert_server(
                ServerSnapshot(
                    name="selected",
                    cpu_cores=snapshot["cpu_cores"],
                    mem_mb=snapshot["mem_mb"],
                    disk_gb=snapshot["disk_gb"],
                    network_available=snapshot.get("network_available"),
                    network_pool=None,
                    docker_networks=None,
                    captured_at=snapshot["captured_at"],
                    source="autoeval-preflight",
                )
            )
            resource_spec = {"mode": "server", "server": "selected", "headroom": 0.2}
        taskset = parse_taskset(
            {
                "target": inputs["target"],
                "throughput": {"mode": "inline", "inline": curve},
                "resources": resource_spec,
                "tasks": task_specs,
                "simulation": {
                    "runs": inputs.get("runs", 100),
                    "seed": inputs.get("seed", 20261111),
                    "throughput_cv": 0.1,
                },
            }
        )
        models, warnings = build_task_models(
            plan_tasks(taskset, library), library, seed=taskset.simulation.seed, strict=False
        )
        # Match history by selected sample identity instead of silently taking the first N rows.
        for model, item in zip(models, inputs["tasks"], strict=True):
            rows = library.load_cases(item["dataset"])
            wanted = {s["sample_id"] for s in item["samples"]}
            matching = [row for row in rows if str(row["case_id"]) in wanted]
            matched_ids = {str(row["case_id"]) for row in matching}
            if matching and matched_ids == wanted:
                source_runs = sorted(
                    {
                        str(observation["run_id"])
                        for observation in history
                        if observation["dataset"] == item["dataset"]
                        and observation["sample_id"] in wanted
                        and observation["metadata"].get("input_tokens") is not None
                        and observation["metadata"].get("output_tokens") is not None
                    }
                )
                profile = profile_from_rows(
                    item["dataset"], model.task_type, matching, source_runs
                )
                count = len(item["samples"]) * item["attempts"]
                model.cases = profile.cases * item["attempts"]
                model.pool = list(model.cases)
                assert len(model.cases) == count
            elif rows:
                warnings.append(
                    item["task_id"]
                    + ": selected sample history incomplete; using sampled distribution"
                )
        provider = build_provider(taskset.resources, library, models)
        throughput = ThroughputProfile.from_dict(curve)
        result = optimize(
            models,
            throughput,
            provider,
            runs=taskset.simulation.runs,
            seed=taskset.simulation.seed,
            throughput_cv=0.1,
        )
        if not recommend:
            order = [m.task_id for m in models]
            plan = Plan(
                order,
                inputs["dataset_concurrency"],
                {
                    m.task_id: item["concurrency"]
                    for m, item in zip(models, inputs["tasks"], strict=True)
                },
            )
            agent_ids = {m.task_id for m in models if m.task_type.endswith("harbor_agent")}
            simulations = _monte_carlo(
                models,
                plan,
                throughput,
                provider,
                runs=taskset.simulation.runs,
                seed=taskset.simulation.seed,
                throughput_cv=0.1,
                ignore_input_prefill=agent_ids,
            )
            values = [s[0].makespan_sec for s in simulations]
            p50 = percentile(values, 0.5)
            representative, sampled, scaled = min(
                simulations, key=lambda s: abs(s[0].makespan_sec - p50)
            )
            standalone = {
                m.task_id: simulate(
                    {m.task_id: sampled[m.task_id]},
                    Plan([m.task_id], 1, {m.task_id: plan.case_concurrency[m.task_id]}),
                    scaled,
                    provider,
                    ignore_input_prefill=agent_ids,
                ).makespan_sec
                for m in models
            }
            candidate = replace(
                result.best,
                plan=plan,
                p10_sec=percentile(values, 0.1),
                p50_sec=p50,
                p90_sec=percentile(values, 0.9),
                timeline=representative.timeline_dict(),
                standalone_durations=standalone,
                resource_peaks=representative.resource_peaks,
                utilization=representative.utilization,
            )
            result = OptimizationResult(candidate, [], 1, 1, taskset.simulation.runs)
        report = build_report(
            taskset=taskset,
            throughput=throughput,
            tasks=models,
            result=result,
            warnings=warnings,
            provider=provider,
        )
        if any(
            o["dataset"] == "DeepSWE" and o["metadata"].get("overhead_sec") is None for o in history
        ):
            report["warnings"].append(
                "DeepSWE historical non-LLM overhead is unknown; duration may be underestimated"
            )
            report["confidence"] = "low"
        report["planner_revision"] = PLANNER_REVISION
        report["input_sha256"] = digest(inputs)
        report["representative_duration_sec"] = max(
            t["end_sec"] for t in report["plan"]["timeline"]
        )
        report["conditional"] = snapshot is None
        return report
    finally:
        library.close()
