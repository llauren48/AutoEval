"""Standalone Linux runtime copied beside job_runtime; never accepts shell commands."""

import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

PLANNER_REVISION = "0ee9abf49a57c5accc62df8900693cdcea88ea9a"
ADAPTOR_REVISION = "955089ab32ef696c231d628f5ad0d9da28f430c4"


def installed_revision(distribution):
    try:
        direct = json.loads(
            importlib.metadata.distribution(distribution).read_text("direct_url.json") or "{}"
        )
        return direct.get("vcs_info", {}).get("commit_id")
    except (importlib.metadata.PackageNotFoundError, ValueError):
        return None


def installed_tool_versions():
    planner = installed_revision("autoeval-task-planner")
    adaptor = installed_revision("autoeval-benchmark-adaptor")
    return {
        "planner": {"expected": PLANNER_REVISION, "actual": planner},
        "adaptor": {"expected": ADAPTOR_REVISION, "actual": adaptor},
    }


def inspect_dataset(task, *, networks, validated):
    config = task["compilation"]["config"]
    params = config["params"]
    runtime = task["compilation"].get("runtime_config", {})
    data_path = (
        runtime.get("datasets_dir")
        if config["subcommand"] == "llm_accuracy"
        else params.get("path")
    )
    reasons = []
    if task["dataset"] not in validated:
        reasons.append("dataset_not_validated")
    if not data_path:
        reasons.append("dataset_path_missing")
    elif not Path(data_path).is_dir():
        reasons.append("dataset_path_unavailable")
    if task["dataset"] == "DeepSWE" and not networks:
        reasons.append("harbor_network_unavailable")
    try:
        tool_revision = verify_tool(task)
    except (KeyError, OSError, ValueError, subprocess.SubprocessError):
        tool_revision = None
        reasons.append("tool_identity_mismatch")
    return {
        "task_id": task["task_id"],
        "dataset": task["dataset"],
        "data_path": data_path,
        "tool_root": params.get("ais_bench_root"),
        "tool_revision": tool_revision,
        "expected_tool_revision": task["tool"]["revision"],
        "blocking_reasons": reasons,
    }


def preflight(tasks=None):
    import resource

    mem = {}
    for line in Path("/proc/meminfo").read_text().splitlines():
        key, value = line.split(":", 1)
        mem[key] = int(value.strip().split()[0])
    networks = None
    if shutil.which("docker"):
        response = subprocess.run(
            ["docker", "network", "ls", "-q"], capture_output=True, timeout=10, check=False
        )
        if response.returncode == 0:
            # Exhaustion cannot be inferred from network count; explicit pool budget required.
            configured = os.environ.get("AE_HARBOR_NETWORK_CAPACITY")
            if configured:
                networks = max(0, int(configured) - len(response.stdout.splitlines()))
    cpu = len(os.sched_getaffinity(0)) if hasattr(os, "sched_getaffinity") else os.cpu_count()
    versions = installed_tool_versions()
    planner_ready = versions["planner"]["actual"] == PLANNER_REVISION
    adaptor_ready = versions["adaptor"]["actual"] == ADAPTOR_REVISION
    # Respect container cgroup limits rather than treating the host as available capacity.
    cpu_max = Path("/sys/fs/cgroup/cpu.max")
    if cpu_max.exists():
        quota, period = cpu_max.read_text().split()
        if quota != "max":
            cpu = min(cpu, int(quota) / int(period))
    available_mem = mem["MemAvailable"] // 1024
    memory_max = Path("/sys/fs/cgroup/memory.max")
    if memory_max.exists() and memory_max.read_text().strip() != "max":
        available_mem = min(
            available_mem,
            max(
                0,
                (
                    int(memory_max.read_text())
                    - int(Path("/sys/fs/cgroup/memory.current").read_text())
                )
                // 1024**2,
            ),
        )
    validated = set(os.environ.get("AE_VALIDATED_DATASETS", "").split(","))
    checks = (
        [inspect_dataset(task, networks=networks, validated=validated) for task in tasks]
        if tasks is not None
        else []
    )
    if tasks is None:
        supported = [
            d
            for d in ["GPQA", "MMMU-Pro", "DeepSWE"]
            if d in validated and (d != "DeepSWE" or networks)
        ]
    else:
        supported = sorted({c["dataset"] for c in checks if not c["blocking_reasons"]})
    blockers = []
    if not planner_ready:
        blockers.append("planner_revision_mismatch")
    if not adaptor_ready:
        blockers.append("adaptor_revision_mismatch")
    selection_ready = (
        bool(supported)
        if tasks is None
        else len(supported) == len({task["dataset"] for task in tasks})
    )
    if not selection_ready:
        blockers.append("no_supported_datasets")
    return {
        "cpu_cores": cpu,
        "mem_mb": available_mem,
        "disk_gb": shutil.disk_usage(Path.cwd()).free / 1024**3,
        "network_available": networks,
        "captured_at": datetime.now(UTC).isoformat(),
        "planner_ready": planner_ready,
        "adaptor_ready": adaptor_ready,
        "tool_versions": versions,
        "dataset_checks": checks,
        "blocking_reasons": blockers,
        "execution_ready": planner_ready and adaptor_ready and selection_ready,
        "supported_datasets": supported,
        "max_probe_concurrency": int(os.environ.get("AE_MAX_PROBE_CONCURRENCY", "8")),
        "node_id": hashlib.sha256(Path("/proc/sys/kernel/random/boot_id").read_bytes()).hexdigest(),
        "process_limit": resource.getrlimit(resource.RLIMIT_NPROC)[0],
    }


def validate_spec(spec):
    if spec.get("kind") not in {"planner_probe", "adaptor"}:
        raise ValueError("Unsupported controlled job")
    if spec["kind"] == "planner_probe":
        if set(spec) - {"kind", "options", "credential"}:
            raise ValueError("Unknown probe fields")
        options = spec["options"]
        if set(options) != {
            "base_url",
            "model",
            "concurrency_levels",
            "requests_per_level",
            "prompt_tokens",
            "max_tokens",
        }:
            raise ValueError("Unsupported probe options")
        if any(type(n) is not int or not 1 <= n <= 64 for n in options["concurrency_levels"]):
            raise ValueError("Invalid concurrency")
        if (
            not 1 <= options["requests_per_level"] <= 64
            or not 1 <= options["max_tokens"] <= 4096
            or not 1 <= options["prompt_tokens"] <= 4096
        ):
            raise ValueError("Invalid probe budget")
    else:
        if set(spec) != {"kind", "compilation", "tool", "credential"}:
            raise ValueError("Unknown adaptor fields")
        if spec["compilation"]["adaptor_revision"] != ADAPTOR_REVISION:
            raise ValueError("Unpinned adaptor")


def verify_tool(spec):
    config = spec["compilation"]["config"]
    root = Path(config["params"]["ais_bench_root"])
    if not root.is_absolute():
        raise ValueError("Absolute tool root required")
    revision = (
        subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            check=True,
            timeout=10,
        )
        .stdout.decode()
        .strip()
    )
    if revision != spec["tool"]["revision"]:
        raise ValueError("Tool revision mismatch")
    scorer = (root / spec["tool"]["scorer_reference"]).resolve()
    if (
        not scorer.is_relative_to(root.resolve())
        or hashlib.sha256(scorer.read_bytes()).hexdigest() != spec["tool"]["scorer_sha256"]
    ):
        raise ValueError("Scorer identity mismatch")
    return revision


def controlled_failure_reason(spec, exc):
    if spec.get("kind") != "planner_probe":
        return "controlled_job_failed"
    from autoeval_planner.errors import ProbeError

    if isinstance(exc, TimeoutError):
        return "probe_timeout"
    if isinstance(exc, OSError) and not isinstance(exc, ProbeError):
        return "probe_connection_failed"
    if not isinstance(exc, ProbeError):
        return "remote_probe_failed"
    message = str(exc).lower()
    if any(value in message for value in ("timed out", "timeout", "http 408", "http 504")):
        return "probe_timeout"
    if "http 401" in message or "http 403" in message:
        return "probe_authentication_failed"
    if "http 429" in message:
        return "probe_rate_limited"
    if any(
        value in message
        for value in ("http 400", "http 404", "http 405", "http 415", "http 422", "json")
    ):
        return "probe_protocol_incompatible"
    if "请求失败" in message:
        return "probe_connection_failed"
    return "remote_probe_failed"


def execute(folder, spec):
    validate_spec(spec)
    secret_path = folder / "credential.json"
    key = json.loads(secret_path.read_text())["value"]
    try:
        if spec["kind"] == "planner_probe":
            if installed_revision("autoeval-task-planner") != PLANNER_REVISION:
                raise ValueError("Planner revision mismatch")
            import urllib.request

            from autoeval_planner.probe import run_probe

            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self, req, fp, code, msg, headers, newurl):
                    return None

            urllib.request.install_opener(urllib.request.build_opener(NoRedirect()))

            # Retries disabled; budget includes a full non-stream fallback per level.
            profile = run_probe(api_key=key, **spec["options"], retries=0, timeout=120, stream=True)
            return {"curve": profile.to_dict()}
        if installed_revision("autoeval-benchmark-adaptor") != ADAPTOR_REVISION:
            raise ValueError("Adaptor revision mismatch")
        verify_tool(spec)
        config = json.loads(json.dumps(spec["compilation"]["config"]))
        config["params"]["api_key"] = key
        output = folder / "output"
        output.mkdir(exist_ok=True)
        config["params"]["work_dir"] = str(output)
        config_path = folder / "private-config.json"
        config_path.write_text(json.dumps(config))
        config_path.chmod(0o600)
        command = ["autoeval-adaptor", "--config", str(config_path)]
        runtime = spec["compilation"].get("runtime_config", {})
        if set(runtime) - {"execution", "datasets_dir", "container_name"}:
            raise ValueError("Unsupported nested runtime settings")
        # The selected Docker container / SSH host is already the execution environment.
        # No nested container launch is inferred from compiler UI defaults.
        if runtime.get("datasets_dir") and not Path(runtime["datasets_dir"]).is_dir():
            raise ValueError("Dataset directory missing in execution environment")
        completed = subprocess.run(
            command, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        candidates = list(output.rglob("unified_result.json"))
        if len(candidates) != 1:
            raise ValueError("Exactly one unified_result.json required")
        raw = candidates[0].read_bytes()
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError("Unified result too large")
        result = json.loads(raw)
        if key and key in json.dumps(result):
            raise ValueError("Credential present in result")
        return {"unified_result": result, "tool_exit_code": completed.returncode}
    finally:
        secret_path.unlink(missing_ok=True)
        (folder / "private-config.json").unlink(missing_ok=True)


if __name__ == "__main__":
    if sys.argv[1:] == ["preflight"]:
        raw = sys.stdin.read()
        print(json.dumps(preflight(json.loads(raw) if raw.strip() else None)))
