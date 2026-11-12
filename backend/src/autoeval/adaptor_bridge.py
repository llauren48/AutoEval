"""Pure, pinned invocation planning; SSH transport owns submission and cancellation.

No public API enables this bridge until the resource/tool versions are validated.
"""

import copy
import re
import uuid
from pathlib import PurePosixPath

from autoeval.adaptor_versions import CURRENT_ADAPTOR, RUNTIME_FIELDS


def invocation(compilation, submission_key, owned_root, aisbench_revision, result_dir=None):
    key = str(uuid.UUID(submission_key))
    root = PurePosixPath(owned_root)
    if (
        not root.is_absolute()
        or ".." in root.parts
        or str(root) != owned_root
        or root == root.parent
    ):
        raise ValueError("Canonical owned root required")
    if compilation.get("adaptor_revision") != CURRENT_ADAPTOR or not re.fullmatch(
        r"[0-9a-f]{40}", aisbench_revision
    ):
        raise ValueError("Pinned adaptor and AISBench revisions required")
    if not compilation.get("input_ready"):
        raise ValueError("Compilation is blocked")
    config = copy.deepcopy(compilation["config"])
    if set(config["params"]) & RUNTIME_FIELDS or config["params"].get("extra_cli_args"):
        raise ValueError("Runtime/CLI overrides cannot be passed to adaptor")
    job_root = root / "jobs" / key
    workdir = str(job_root / "output")
    locked = compilation.get("locked_fields", {})
    if "params.work_dir" in locked and locked["params.work_dir"] != workdir:
        raise ValueError("Locked working directory conflicts with isolated job")
    config["params"]["work_dir"] = workdir
    config_path = str(job_root / "input.json")
    command = ["autoeval-adaptor", "--config", config_path]
    if result_dir is not None:
        path = PurePosixPath(result_dir)
        if (
            ".." in path.parts
            or not path.is_relative_to(job_root / "output")
            or str(path) != result_dir
        ):
            raise ValueError("Collection requires an explicit owned result directory")
        command += [
            "--collect-results",
            "--result-dir",
            result_dir,
            "--output",
            str(job_root / "unified_result.json"),
        ]
    return {
        "submission_key": key,
        "job_root": str(job_root),
        "config": config,
        "config_path": config_path,
        "command": command,
        "adaptor_revision": CURRENT_ADAPTOR,
        "aisbench_revision": aisbench_revision,
        "credential_injection": "protected-runtime-file",
        "execution_enabled": False,
    }
