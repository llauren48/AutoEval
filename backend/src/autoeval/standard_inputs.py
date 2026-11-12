"""Versioned standard inputs and non-executing adaptor configuration compilation."""

import asyncio
import copy
import hashlib
import json
import ssl
import subprocess
import uuid
from pathlib import PurePosixPath

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import ForeignKey, String, UniqueConstraint, func, or_, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from autoeval.adaptor_versions import CURRENT_ADAPTOR, separate_runtime
from autoeval.data_workbench import development
from autoeval.models import Base, Record
from autoeval.registry import Asset, AssetVersion
from autoeval.targets import TargetBinding

STANDARD_REPO = "SJTUyh/AutoEval-Standard"
ADAPTOR_REVISION = "1fb877ace4a087cb805e9a3d5878dcce6349bc35"
BENCHMARKS = {
    "GPQA_Diamond": ("GPQA", "diamond", "llm_accuracy"),
    "MMMU-Pro": ("MMMU-Pro", "options10", "llm_accuracy"),
    "DeepSWE": ("DeepSWE", "v1.1", "harbor_agent"),
}
FILES = {"standard_config.json", "benchmark_config.json", "baseline.json"}
COMMON = {
    "abbr",
    "work_dir",
    "ais_bench_root",
    "python_executable",
    "timeout",
    "debug",
    "reuse",
    "max_num_workers",
    "execution",
    "datasets_dir",
    "container_name",
    "docker_image",
    "env_isolation",
    "monitor_port",
    "purge_exception_cases",
    "extra_cli_args",
    "base_image",
    "mounts",
    "case_images",
    "benchmark_version",
    "benchmark_repo",
    "benchmark_ref",
    "keep_container",
}
ACCURACY = {
    "model_type",
    "datasets",
    "dataset_test_range",
    "model_overrides",
    "num_prompts",
    "max_workers_per_gpu",
    "dump_eval_details",
    "dump_extract_rate",
    "merge_ds",
    "path",
    "model",
    "stream",
    "request_rate",
    "use_timestamp",
    "retry",
    "api_key",
    "host_ip",
    "host_port",
    "url",
    "max_out_len",
    "batch_size",
    "trust_remote_code",
    "generation_kwargs",
}
AGENT = {
    "dataset_abbr",
    "model_overrides",
    "agent_name",
    "agent_import_path",
    "model_names",
    "api_base",
    "api_key",
    "llm_kwargs",
    "model_info",
    "agent_kwargs",
    "agent_env",
    "deps_path",
    "n_concurrent",
    "concurrency_group",
    "skills",
    "mcp_servers",
    "resume_trajectory",
    "load_trajectory",
    "extra_allowed_hosts",
    "include_logs",
    "exclude_logs",
    "override_timeout_sec",
    "override_setup_timeout_sec",
    "max_timeout_sec",
    "path",
    "dataset_name_version",
    "n_attempts",
    "n_concurrent_trials",
    "install_only",
    "metrics",
    "artifacts",
    "extra_instruction_paths",
    "environment_type",
    "environment_force_build",
    "environment_delete",
    "environment_env",
    "environment_kwargs",
    "extra_docker_compose",
    "override_cpus",
    "override_memory_mb",
    "override_storage_mb",
    "override_gpus",
    "timeout_multiplier",
    "agent_timeout_multiplier",
    "verifier_timeout_multiplier",
    "agent_setup_timeout_multiplier",
    "environment_build_timeout_multiplier",
    "max_retries",
    "retry_include_exceptions",
    "retry_exclude_exceptions",
    "task_names",
    "exclude_task_names",
    "n_tasks",
    "disable_verification",
    "verifier_env",
    "verifier_import_path",
    "verifier_kwargs",
    "env_file",
    "registry_url",
    "registry_path",
    "quiet",
    "yes",
}
ENVIRONMENT = {
    "work_dir",
    "execution",
    "container_name",
    "api_key",
    "api_base",
    "url",
    "host_ip",
    "host_port",
    "model",
    "model_names",
    "abbr",
}


class StandardInputVersion(Record, Base):
    __tablename__ = "standard_input_version"
    repository: Mapped[str] = mapped_column(String(256))
    revision: Mapped[str] = mapped_column(String(64))
    source_model: Mapped[str] = mapped_column(String(256))
    benchmark: Mapped[str] = mapped_column(String(128))
    content_sha256: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("repository", "revision", "source_model", "benchmark"),)


class StandardInputCompilation(Record, Base):
    __tablename__ = "standard_input_compilation"
    input_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("standard_input_version.id"))
    model_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("asset.id"))
    sha256: Mapped[str] = mapped_column(String(64), unique=True)
    payload: Mapped[dict] = mapped_column(JSONB)


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


def digest(value):
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def safe_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("Non-finite number")

    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def reject_secrets(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if (
                any(
                    word in key.lower()
                    for word in ("api_key", "password", "secret", "authorization", "access_token")
                )
                and item
            ):
                raise ValueError("Plaintext credentials are forbidden; use a target reference")
            reject_secrets(item)
    elif isinstance(value, list):
        for item in value:
            reject_secrets(item)


class Bundle(Strict):
    repository: str = STANDARD_REPO
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    files: dict[str, str] = Field(min_length=1, max_length=300)

    @model_validator(mode="after")
    def validate_files(self):
        if self.repository != STANDARD_REPO:
            raise ValueError("Unsupported standard repository")
        if sum(len(raw.encode()) for raw in self.files.values()) > 2_000_000:
            raise ValueError("Bundle exceeds 2 MB")
        for path, raw in self.files.items():
            parts = PurePosixPath(path).parts
            if (
                len(parts) != 3
                or parts[1] not in BENCHMARKS
                or parts[2] not in FILES
                or "\\" in path
                or any(p in {".", ".."} for p in parts)
                or PurePosixPath(path).as_posix() != path
                or len(parts[0]) > 256
            ):
                raise ValueError("Expected model/benchmark/config.json")
            value = safe_json(raw)
            if not isinstance(value, dict):
                raise ValueError("JSON object required")  # noqa: TRY004 -- Pydantic validation
            reject_secrets(value)
        return self


class Confirm(Strict):
    bundle: Bundle
    expected_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class Sync(Strict):
    ref: str = Field(default="main", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_./-]+$")


class CompileInput(Strict):
    adaptor_revision: str = Field(
        default=CURRENT_ADAPTOR,
        pattern="^(955089ab32ef696c231d628f5ad0d9da28f430c4|1fb877ace4a087cb805e9a3d5878dcce6349bc35)$",
    )
    model_id: uuid.UUID | None = None
    target_id: uuid.UUID | None = None
    template_version: str = Field(default="platform.v1", pattern=r"^[a-zA-Z0-9_.-]{1,128}$")
    template: dict = Field(default_factory=dict)
    adjustments: dict = Field(default_factory=dict)
    execution: str = Field(default="docker", pattern=r"^(docker|local)$")
    datasets_dir: str | None = Field(default=None, max_length=1024)
    task_path: str | None = Field(default=None, max_length=1024)
    tool_root: str | None = Field(default=None, max_length=1024)

    @model_validator(mode="after")
    def no_credentials(self):
        reject_secrets(self.template)
        reject_secrets(self.adjustments)
        for overlay in (self.template, self.adjustments):
            if set(overlay) - {"tool", "subcommand", "params"} or not isinstance(
                overlay.get("params", {}), dict
            ):
                raise ValueError("Template and adjustments require an object-valued params")
            if len(json.dumps(overlay, allow_nan=False).encode()) > 200_000:
                raise ValueError("Template or adjustments exceed 200 KB")
        for value in (self.datasets_dir, self.task_path, self.tool_root):
            if value is not None and (
                not value.startswith("/") or ".." in PurePosixPath(value).parts or "\n" in value
            ):
                raise ValueError("Execution paths must be absolute POSIX paths")
        return self


def validate_config(value):
    if (
        set(value) - {"tool", "subcommand", "params"}
        or value.get("tool") != "ais_bench"
        or value.get("subcommand") not in {"llm_accuracy", "harbor_agent"}
        or not isinstance(value.get("params"), dict)
    ):
        raise ValueError("Expected ais_bench tool/subcommand/params")
    allowed = COMMON | (ACCURACY if value["subcommand"] == "llm_accuracy" else AGENT)
    unknown = set(value["params"]) - allowed
    if unknown:
        raise ValueError("Unknown adaptor parameters: " + ", ".join(sorted(unknown)))
    if value["params"].get("model_overrides"):
        raise ValueError("model_overrides is not allowed: use explicit model fields")
    if value["params"].get("extra_cli_args"):
        raise ValueError("extra_cli_args can bypass standard constraints")
    params = value["params"]
    dictionaries = {
        "generation_kwargs",
        "llm_kwargs",
        "model_info",
        "agent_kwargs",
        "agent_env",
        "environment_env",
        "environment_kwargs",
        "verifier_kwargs",
        "model_overrides",
    }
    lists = {
        "datasets",
        "model_names",
        "skills",
        "mcp_servers",
        "extra_allowed_hosts",
        "include_logs",
        "exclude_logs",
        "artifacts",
        "extra_instruction_paths",
        "extra_docker_compose",
        "retry_include_exceptions",
        "retry_exclude_exceptions",
        "task_names",
        "exclude_task_names",
        "verifier_env",
        "mounts",
        "extra_cli_args",
    }
    integers = {
        "max_out_len",
        "batch_size",
        "retry",
        "host_port",
        "num_prompts",
        "max_workers_per_gpu",
        "max_num_workers",
        "monitor_port",
        "n_attempts",
        "n_concurrent_trials",
        "override_memory_mb",
        "override_storage_mb",
        "override_gpus",
        "max_retries",
        "n_tasks",
    }
    booleans = {
        "stream",
        "use_timestamp",
        "trust_remote_code",
        "debug",
        "purge_exception_cases",
        "keep_container",
        "dump_eval_details",
        "dump_extract_rate",
        "merge_ds",
        "resume_trajectory",
        "install_only",
        "environment_force_build",
        "environment_delete",
        "disable_verification",
        "quiet",
        "yes",
    }
    numbers = {
        "timeout",
        "request_rate",
        "override_timeout_sec",
        "override_setup_timeout_sec",
        "max_timeout_sec",
        "override_cpus",
        "timeout_multiplier",
        "agent_timeout_multiplier",
        "verifier_timeout_multiplier",
        "agent_setup_timeout_multiplier",
        "environment_build_timeout_multiplier",
    }
    integers |= {"n_concurrent"}
    for key, item in params.items():
        if item is None:
            raise ValueError("Null parameters must be omitted: " + key)
        expected = (
            dict
            if key in dictionaries
            else list
            if key in lists
            else int
            if key in integers
            else bool
            if key in booleans
            else (int, float)
            if key in numbers
            else str
        )
        if key in {"metrics"}:
            continue
        if not isinstance(item, expected) or (key in integers | numbers and isinstance(item, bool)):
            raise ValueError("Invalid adaptor parameter type: " + key)
        if key in lists - {"skills", "mcp_servers"} and any(not isinstance(v, str) for v in item):
            raise ValueError("Expected string array: " + key)
        if (
            key
            in {
                "max_out_len",
                "batch_size",
                "num_prompts",
                "max_workers_per_gpu",
                "max_num_workers",
                "n_attempts",
                "n_concurrent",
                "n_concurrent_trials",
            }
            and item < 1
        ):
            raise ValueError("Positive value required: " + key)
    if "datasets" in params and not params["datasets"]:
        raise ValueError("datasets cannot be empty")
    reject_secrets(value)
    return value


def leaves(value, prefix=""):
    result = {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict) and item:
            result.update(leaves(item, path))
        else:
            result[path] = item
    return result


def merge(base, overlay):
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        result[key] = (
            merge(result[key], value)
            if isinstance(value, dict) and isinstance(result.get(key), dict)
            else copy.deepcopy(value)
        )
    return result


def discover(bundle):
    groups = {}
    for path, raw in bundle.files.items():
        model, benchmark, filename = path.split("/")
        groups.setdefault((model, benchmark), {})[filename] = {
            "path": path,
            "raw": raw,
            "sha256": hashlib.sha256(raw.encode()).hexdigest(),
        }
    result = []
    for (model, benchmark), files in sorted(groups.items()):
        if "standard_config.json" not in files:
            raise ValueError(f"Missing standard_config.json: {model}/{benchmark}")
        standard = validate_config(safe_json(files["standard_config.json"]["raw"]))
        if standard["subcommand"] != BENCHMARKS[benchmark][2]:
            raise ValueError("Benchmark/subcommand mismatch")
        if "benchmark_config.json" in files:
            startup = validate_config(safe_json(files["benchmark_config.json"]["raw"]))
            if startup["subcommand"] != standard["subcommand"]:
                raise ValueError("Startup/standard subcommand mismatch")
        result.append(
            {
                "source_model": model,
                "benchmark": benchmark,
                "files": files,
                "mapping": dict(zip(("dataset", "variant", "subcommand"), BENCHMARKS[benchmark])),
                "locked_fields": leaves(standard),
                "startup_missing": "benchmark_config.json" not in files,
            }
        )
    return result


def compile_config(entry, body, target=None):
    standard = safe_json(entry["files"]["standard_config.json"]["raw"])
    base = {"tool": "ais_bench", "subcommand": standard["subcommand"], "params": {}}
    origins = {}
    changes = []

    def apply(overlay, source):
        nonlocal base
        old = leaves(base)
        base = merge(base, overlay)
        for key, value in leaves(overlay).items():
            if key in old and old[key] != value:
                changes.append({"field": key, "before": old[key], "after": value, "source": source})
            origins[key] = source

    if "benchmark_config.json" in entry["files"]:
        apply(
            safe_json(entry["files"]["benchmark_config.json"]["raw"]), "standard-repository-startup"
        )
    for overlay, name in [
        (body.template, "template:" + body.template_version),
        (body.adjustments, "user-adjustment"),
    ]:
        if overlay:
            if set(overlay) - {"tool", "subcommand", "params"}:
                raise ValueError("Only tool/subcommand/params accepted")
            if set(overlay.get("params", {})) & ENVIRONMENT:
                raise ValueError("Environment and target fields must use controlled inputs")
            apply(overlay, name)
    validate_config(base)
    apply(standard, "standard-lock")
    validate_config(base)
    blockers = []
    if entry["startup_missing"] and not body.template.get("params"):
        blockers.append("startup_template_required")
    if body.model_id is None:
        blockers.append("model_mapping_missing")
    if not target:
        blockers.append("target_missing")
    identity_hash = digest({"entry": entry, "body": body.model_dump(mode="json")})[:32]
    environment = {
        "execution": body.execution,
        "work_dir": "/work/autoeval/inputs/" + identity_hash,
        "container_name": "autoeval-input-" + identity_hash,
    }
    if target:
        environment["abbr"] = str(body.model_id)
        if standard["subcommand"] == "llm_accuracy":
            environment.update(model=target.remote_model, url=target.base_url)
        else:
            environment.update(model_names=[target.remote_model], api_base=target.base_url)
    if body.datasets_dir:
        environment["datasets_dir"] = body.datasets_dir
    if body.tool_root:
        environment["ais_bench_root"] = body.tool_root
    else:
        blockers.append("tool_root_missing")
    if not body.datasets_dir and standard["subcommand"] == "llm_accuracy":
        blockers.append("datasets_dir_missing")
    if body.task_path:
        environment["path"] = body.task_path
    for key, value in list(environment.items()):
        if key in standard["params"] and standard["params"][key] != value:
            blockers.append("environment_conflicts_with_standard:" + key)
            del environment[key]
    apply({"params": environment}, "platform-environment")
    params = base["params"]
    required = ["model_type", "datasets"] if standard["subcommand"] == "llm_accuracy" else []
    for key in required:
        if not params.get(key):
            blockers.append("required_parameter_missing:" + key)
    if standard["subcommand"] == "harbor_agent" and not (
        params.get("path") or params.get("dataset_name_version")
    ):
        blockers.append("task_path_missing")
    if standard["subcommand"] == "harbor_agent" and not (
        params.get("agent_name") or params.get("agent_import_path")
    ):
        blockers.append("agent_missing")
    validate_config(base)
    final = leaves(base)
    for key, value in leaves(standard).items():
        if final.get(key) != value:
            blockers.append("standard_lock_conflict:" + key)
    tool_config, runtime_config, cli_flags = separate_runtime(base, body.adaptor_revision)
    result = {
        "config": tool_config,
        "runtime_config": runtime_config,
        "template_version": body.template_version,
        "template_sha256": digest(body.template),
        "field_sources": origins,
        "overrides": changes,
        "locked_fields": leaves(standard),
        "blocking_reasons": sorted(set(blockers)),
        "input_ready": not blockers,
        "execution_enabled": False,
        "approval_status": "not_approved",
        "adaptor_revision": body.adaptor_revision,
        "cli_flags": cli_flags,
        "credential_binding": {"target_id": str(target.id), "injection": "runtime-only"}
        if target
        else None,
    }
    result["sha256"] = digest(result)
    return result


router = APIRouter(
    prefix="/api/autoeval/v1/standard-inputs",
    tags=["standard inputs"],
    dependencies=[Depends(development)],
)


def envelope(data, request):
    return {"data": data, "request_id": request.state.request_id}


@router.get("/dataset-registrations")
async def dataset_registrations(request: Request):
    async with request.app.state.sessions() as session:
        rows = (
            await session.execute(
                select(Asset, AssetVersion)
                .join(AssetVersion, AssetVersion.asset_id == Asset.id)
                .where(Asset.kind == "dataset")
                .order_by(Asset.asset_key, AssetVersion.created_at.desc())
            )
        ).all()
        return envelope(
            [
                {
                    "name": asset.name,
                    "key": asset.asset_key,
                    "version": version.version,
                    "metadata": version.metadata_json,
                }
                for asset, version in rows
            ],
            request,
        )


@router.get("/tags")
async def repository_tags(request: Request):
    """Current repository labels only; immutable input identities remain commit-based."""
    tags = []
    try:
        async with httpx.AsyncClient(
            timeout=8, follow_redirects=False, verify=ssl.create_default_context()
        ) as client:
            for page in range(1, 11):
                response = await client.get(
                    f"https://api.github.com/repos/{STANDARD_REPO}/tags",
                    params={"per_page": 100, "page": page},
                )
                response.raise_for_status()
                items = response.json()
                for item in items:
                    tags.append({"name": item["name"], "revision": item["commit"]["sha"]})
                if len(items) < 100:
                    return envelope({"status": "available", "tags": tags}, request)
        return envelope({"status": "incomplete", "tags": tags}, request)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        try:
            completed = await asyncio.to_thread(
                subprocess.run,
                ["git", "ls-remote", "--tags", f"https://github.com/{STANDARD_REPO}.git"],
                capture_output=True,
                text=True,
                timeout=15,
                check=True,
            )
            resolved = {}
            peeled = {}
            for line in completed.stdout.splitlines():
                sha, ref = line.split("\t", 1)
                name = ref.removeprefix("refs/tags/")
                if name.endswith("^{}"):
                    peeled[name[:-3]] = sha
                else:
                    resolved[name] = sha
            resolved.update(peeled)
            return envelope(
                {
                    "status": "available",
                    "tags": [
                        {"name": name, "revision": sha} for name, sha in sorted(resolved.items())
                    ],
                },
                request,
            )
        except (OSError, subprocess.SubprocessError, ValueError):
            return envelope({"status": "unavailable", "tags": []}, request)


@router.post("/sync-preview")
async def sync_preview(body: Sync, request: Request):
    try:
        async with httpx.AsyncClient(
            timeout=20, follow_redirects=False, verify=ssl.create_default_context()
        ) as client:
            response = await client.get(
                f"https://api.github.com/repos/{STANDARD_REPO}/commits/{body.ref}"
            )
            response.raise_for_status()
            revision = response.json()["sha"]
            response = await client.get(
                f"https://api.github.com/repos/{STANDARD_REPO}/git/trees/{revision}?recursive=1"
            )
            response.raise_for_status()
            tree = response.json()
            if tree.get("truncated"):
                raise ValueError("Truncated repository tree")
            files = {}
            total = 0
            for node in tree["tree"]:
                parts = node["path"].split("/")
                if len(parts) != 3 or parts[1] not in BENCHMARKS or parts[2] not in FILES:
                    continue
                total += node.get("size", 2_000_001)
                if total > 2_000_000 or len(files) >= 300:
                    raise ValueError("Repository input size exceeds limit")
                response = await client.get(
                    f"https://raw.githubusercontent.com/{STANDARD_REPO}/{revision}/{node['path']}"
                )
                response.raise_for_status()
                files[node["path"]] = response.content.decode("utf-8")
            bundle = Bundle(revision=revision, files=files)
        return envelope(await preview_data(bundle, request), request)
    except httpx.HTTPError as exc:
        message = (
            "标准仓 HTTPS 证书校验失败，请检查系统可信 CA 配置"
            if "CERTIFICATE_VERIFY_FAILED" in str(exc)
            else "标准仓网络请求失败，请检查连接、访问限制及分支或 Tag"
        )
        raise HTTPException(502, message) from exc
    except (ValueError, KeyError) as exc:
        raise HTTPException(422, "标准仓内容校验失败，请检查目录、文件大小及 JSON 格式") from exc


async def preview_data(bundle, request):
    try:
        entries = discover(bundle)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    async with request.app.state.sessions() as session:
        for entry in entries:
            latest = await session.scalar(
                select(StandardInputVersion)
                .where(
                    StandardInputVersion.repository == bundle.repository,
                    StandardInputVersion.source_model == entry["source_model"],
                    StandardInputVersion.benchmark == entry["benchmark"],
                )
                .order_by(StandardInputVersion.created_at.desc(), StandardInputVersion.id.desc())
                .limit(1)
            )
            entry["previous_id"] = str(latest.id) if latest else None
            entry["changed_files"] = [
                name
                for name, value in entry["files"].items()
                if not latest
                or latest.payload["files"].get(name, {}).get("sha256") != value["sha256"]
            ]
            entry["removed_files"] = (
                sorted(set(latest.payload["files"]) - set(entry["files"])) if latest else []
            )
            old_fields = latest.payload["locked_fields"] if latest else {}
            new_fields = entry["locked_fields"]
            entry["field_changes"] = [
                {
                    "field": key,
                    "before": old_fields.get(key),
                    "after": new_fields.get(key),
                    "change": "added"
                    if key not in old_fields
                    else "removed"
                    if key not in new_fields
                    else "modified",
                }
                for key in sorted(set(old_fields) | set(new_fields))
                if key not in old_fields
                or key not in new_fields
                or old_fields[key] != new_fields[key]
            ]
    return {
        "bundle": bundle.model_dump(),
        "sha256": digest(bundle.model_dump()),
        "entries": entries,
        "execution_enabled": False,
        "approval_status": "not_approved",
    }


@router.post("/preview")
async def preview(body: Bundle, request: Request):
    return envelope(await preview_data(body, request), request)


@router.post("/import", status_code=201)
async def import_bundle(body: Confirm, request: Request):
    if digest(body.bundle.model_dump()) != body.expected_sha256:
        raise HTTPException(409, "Preview hash differs; preview again")
    try:
        entries = discover(body.bundle)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    result = []
    async with request.app.state.sessions() as session, session.begin():
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": body.bundle.repository}
        )
        for entry in entries:
            identity = {
                "repository": body.bundle.repository,
                "revision": body.bundle.revision,
                "source_model": entry["source_model"],
                "benchmark": entry["benchmark"],
            }
            row = await session.scalar(select(StandardInputVersion).filter_by(**identity))
            sha = digest(entry)
            if row and row.content_sha256 != sha:
                raise HTTPException(409, "Immutable source identity has different content")
            if row is None:
                row = StandardInputVersion(**identity, content_sha256=sha, payload=entry)
                session.add(row)
                await session.flush()
            result.append(row_data(row))
    return envelope(result, request)


def row_data(row):
    return {
        "id": str(row.id),
        "repository": row.repository,
        "revision": row.revision,
        "source_model": row.source_model,
        "benchmark": row.benchmark,
        "sha256": row.content_sha256,
        "created_at": row.created_at.isoformat(),
        **row.payload,
        "execution_enabled": False,
        "approval_status": "not_approved",
    }


@router.get("")
async def versions(
    request: Request,
    offset: int = 0,
    limit: int = 20,
    model_id: uuid.UUID | None = None,
    include_test: bool = False,
):
    if offset < 0 or not 1 <= limit <= 100:
        raise HTTPException(422, "Invalid pagination")
    async with request.app.state.sessions() as session:
        query = select(StandardInputVersion)
        if not include_test:
            query = query.where(~StandardInputVersion.source_model.startswith("TEST-"))
        if model_id:
            query = query.where(
                StandardInputVersion.id.in_(
                    select(StandardInputCompilation.input_id).where(
                        StandardInputCompilation.model_id == model_id
                    )
                )
            )
        rows = (
            await session.scalars(
                query.order_by(
                    StandardInputVersion.created_at.desc(), StandardInputVersion.id.desc()
                )
                .offset(offset)
                .limit(limit + 1)
            )
        ).all()
        latest = {}
        if rows:
            compiled = (
                await session.scalars(
                    select(StandardInputCompilation)
                    .where(StandardInputCompilation.input_id.in_([row.id for row in rows[:limit]]))
                    .order_by(
                        StandardInputCompilation.created_at.desc(),
                        StandardInputCompilation.id.desc(),
                    )
                )
            ).all()
            for item in compiled:
                latest.setdefault(
                    item.input_id,
                    {
                        "input_ready": item.payload["input_ready"],
                        "template_version": item.payload["template_version"],
                        "blocking_reasons": item.payload.get("blocking_reasons", []),
                    },
                )
        return {
            **envelope(
                [
                    {**row_data(row), "latest_compilation": latest.get(row.id)}
                    for row in rows[:limit]
                ],
                request,
            ),
            "next_offset": offset + limit if len(rows) > limit else None,
        }


@router.get("/{input_id}")
async def detail(input_id: uuid.UUID, request: Request):
    async with request.app.state.sessions() as session:
        row = await session.get(StandardInputVersion, input_id)
        if row is None:
            raise HTTPException(404, "Standard input not found")
        history = (
            await session.scalars(
                select(StandardInputCompilation)
                .where(StandardInputCompilation.input_id == input_id)
                .order_by(StandardInputCompilation.created_at.desc())
                .limit(20)
            )
        ).all()
        return envelope(
            {**row_data(row), "compilations": [item.payload for item in history]}, request
        )


def autocomplete_template(entry):
    """Build a deterministic input-only template from pinned repository evidence."""
    source = entry["files"].get("benchmark_config.json")
    template = (
        safe_json(source["raw"])
        if source
        else safe_json(entry["files"]["standard_config.json"]["raw"])
    )
    template = copy.deepcopy(template)
    template["params"] = {
        key: value for key, value in template["params"].items() if key not in ENVIRONMENT
    }
    validate_config(template)
    return template


@router.post("/{input_id}/autocomplete")
async def autocomplete_input(input_id: uuid.UUID, body: CompileInput, request: Request):
    """Preview only: never persist, probe a host, approve, or start execution."""
    async with request.app.state.sessions() as session:
        row = await session.get(StandardInputVersion, input_id)
        if row is None:
            raise HTTPException(404, "Standard input not found")
        model = await session.get(Asset, body.model_id) if body.model_id else None
        if body.model_id and (not model or model.kind != "model"):
            raise HTTPException(422, "Select a registered model")
        if not body.model_id:
            candidates = (
                await session.scalars(
                    select(Asset).where(
                        Asset.kind == "model",
                        or_(
                            func.lower(Asset.asset_key) == row.payload["source_model"].lower(),
                            func.lower(Asset.name) == row.payload["source_model"].lower(),
                        ),
                    )
                )
            ).all()
            if len(candidates) == 1:
                model = candidates[0]
        model_id = model.id if model else None
        target = await session.get(TargetBinding, body.target_id) if body.target_id else None
        if target:
            version = await session.get(AssetVersion, target.model_version_id)
            if not version or version.asset_id != model_id:
                raise HTTPException(422, "Target must belong to selected model")
        elif model_id:
            candidates = (
                await session.scalars(
                    select(TargetBinding)
                    .join(AssetVersion, TargetBinding.model_version_id == AssetVersion.id)
                    .where(AssetVersion.asset_id == model_id)
                )
            ).all()
            if len(candidates) == 1:
                target = candidates[0]
        try:
            template = autocomplete_template(row.payload)
            version = "auto.v1." + digest(template)[:16]
            candidate = body.model_copy(
                update={
                    "model_id": model_id,
                    "target_id": target.id if target else None,
                    "template": template,
                    "template_version": version,
                }
            )
            preview = compile_config(row.payload, candidate, target)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return envelope(
            {
                "candidate": candidate.model_dump(mode="json"),
                "preview": preview,
                "template_source": "benchmark_config.json"
                if not row.payload["startup_missing"]
                else "platform-standard-template.v1:" + row.revision,
                "requires_confirmation": True,
                "notes": [
                    "模型名忽略大小写匹配；只有唯一模型与唯一服务可自动选择。",
                    "未绑定的资源路径与Agent参数不推测，请填写并确认。",
                    "预览不保存、不批准、不执行；确认后保存新的不可变编译快照。",
                ],
            },
            request,
        )


@router.post("/{input_id}/compile")
async def compile_input(input_id: uuid.UUID, body: CompileInput, request: Request):
    async with request.app.state.sessions() as session, session.begin():
        row = await session.get(StandardInputVersion, input_id)
        if row is None:
            raise HTTPException(404, "Standard input not found")
        model = await session.get(Asset, body.model_id) if body.model_id else None
        if body.model_id and (not model or model.kind != "model"):
            raise HTTPException(422, "Select a registered model")
        target = await session.get(TargetBinding, body.target_id) if body.target_id else None
        target_version = (
            await session.get(AssetVersion, target.model_version_id) if target else None
        )
        if body.target_id and (not target_version or target_version.asset_id != body.model_id):
            raise HTTPException(422, "Target must belong to selected model")
        try:
            result = compile_config(row.payload, body, target)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        result.update(
            input_id=str(row.id),
            model_id=str(body.model_id) if body.model_id else None,
            source_revision=row.revision,
        )
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": str(row.id) + body.template_version},
        )
        prior = await session.scalar(
            select(StandardInputCompilation)
            .where(
                StandardInputCompilation.input_id == row.id,
                StandardInputCompilation.payload["template_version"].astext
                == body.template_version,
            )
            .limit(1)
        )
        if prior and prior.payload["template_sha256"] != result["template_sha256"]:
            raise HTTPException(409, "Template version is immutable; choose a new version")
        result.pop("sha256", None)
        result["sha256"] = digest(result)
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": result["sha256"]}
        )
        if not await session.scalar(
            select(StandardInputCompilation.id).where(
                StandardInputCompilation.sha256 == result["sha256"]
            )
        ):
            session.add(
                StandardInputCompilation(
                    input_id=row.id, model_id=body.model_id, sha256=result["sha256"], payload=result
                )
            )
        return envelope(result, request)
