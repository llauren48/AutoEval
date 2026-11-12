"""Explicit tool/runtime separation for the pinned adaptor generations."""

import copy

LEGACY_ADAPTOR = "1fb877ace4a087cb805e9a3d5878dcce6349bc35"
CURRENT_ADAPTOR = "955089ab32ef696c231d628f5ad0d9da28f430c4"
STANDARD_REVISION = "4d7e3620bb9a32011155defda33094777a3cd3c6"
RUNTIME_FIELDS = {
    "execution",
    "container_name",
    "docker_image",
    "base_image",
    "mounts",
    "env_isolation",
    "case_images",
    "datasets_dir",
    "benchmark_version",
    "benchmark_repo",
    "benchmark_ref",
    "keep_container",
}


def separate_runtime(config, revision):
    if revision not in {LEGACY_ADAPTOR, CURRENT_ADAPTOR}:
        raise ValueError("Unsupported adaptor revision")
    config = copy.deepcopy(config)
    runtime = {key: value for key, value in config["params"].items() if key in RUNTIME_FIELDS}
    if revision == CURRENT_ADAPTOR:
        config["params"] = {
            key: value for key, value in config["params"].items() if key not in RUNTIME_FIELDS
        }
    return config, runtime, ["--no-template"] if revision == LEGACY_ADAPTOR else []
