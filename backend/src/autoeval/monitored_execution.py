"""Explicit GPQA CLI path with a pinned SSH monitor; never retries generation."""

import asyncio
import hashlib
import json
import logging
import math
import stat
import uuid
from datetime import UTC, datetime
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import delete, select, text
from temporalio import activity

from autoeval.cleanup import ExecutionCleanup
from autoeval.config import get_settings
from autoeval.configuration import TestConfigItem, TestConfigVersion
from autoeval.data_workbench import development
from autoeval.datasets import DatasetManifest, canonical_manifest, read_manifest, write_manifest
from autoeval.db import create_database
from autoeval.evaluations import EvaluationReport
from autoeval.models import EvaluationRun, Outbox, RunEvent, RunSnapshot
from autoeval.monitor_transport import SSHMonitorClient
from autoeval.profiles import ConfigDatasetBinding, EvaluationProfile
from autoeval.runs import ExecutionLease
from autoeval.secrets import FileSecretStore
from autoeval.ssh_executor import SSHConnection, SSHExecutor
from autoeval.targets import TargetBinding
from autoeval.task_monitor import MonitorBinding, save_observation
from autoeval.unified_artifacts import UnifiedArtifact
from autoeval.unified_outputs import archive_output

MONITOR_LOCK = Path(__file__).resolve().parents[2] / "tool-locks" / "aisbench-monitor.json"


def environment(settings):
    if not settings.aisbench_monitor_config:
        raise ValueError("Monitored executor is not configured")
    env = json.loads(settings.aisbench_monitor_config.read_text())
    return validate_environment(env)


def validate_environment(env):
    lock = json.loads(MONITOR_LOCK.read_text())
    constraints = lock["constraints"]
    expected = {
        "aisbench_revision": lock["aisbench_revision"],
        "adaptor_revision": lock["adaptor_revision"],
        "pins": lock["pins"],
        "port": constraints["monitor_port"],
        "runtime": env["root"]
        + "/runtime-"
        + hashlib.sha256(Path(__file__).with_name("monitored_runtime.py").read_bytes()).hexdigest()[
            :12
        ]
        + ".py",
    }
    for key, value in expected.items():
        if env.get(key) != value:
            raise ValueError(f"Monitor deployment does not match the pinned {key}")
    if (
        constraints["monitor_host"] != "127.0.0.1"
        or env.get("monitor_host", "127.0.0.1") != "127.0.0.1"
        or constraints["max_samples"] != 5
        or constraints["max_output_tokens"] != 2048
        or constraints["retry"] != 1
    ):
        raise ValueError("Monitor deployment violates the fixed execution constraints")
    return env


def control_for(settings, env):
    return SSHExecutor(
        SSHConnection(
            **env["ssh"],
            credential=FileSecretStore(settings.secret_store_path).get(env["credential_ref"]),
        )
    )


def verify_environment(settings, env):
    control = control_for(settings, env)
    script = """import hashlib,json,socket,sys
from pathlib import Path
root=Path(sys.argv[1]);pins=json.loads(sys.argv[2])
for name,sha in pins.items():
 if hashlib.sha256((root/name).read_bytes()).hexdigest()!=sha:raise ValueError('Tool checksum changed')
sys.path[:0]=[str(root/'src'),str(root/'benchmark')]
from ais_bench.benchmark.runners.local import LocalRunner,LocalMonitorServer
with socket.socket() as probe:
 probe.setsockopt(socket.SOL_SOCKET,socket.SO_REUSEADDR,1)
 probe.bind(('127.0.0.1',int(sys.argv[3])))
print('ready')
"""
    with control.connect() as client:
        metadata = json.loads(control.command(client, ["docker", "inspect", env["container"]]))[0]
        if metadata["Id"] != env["container_id"] or not metadata["State"]["Running"]:
            raise ValueError("Container identity changed or container is not running")
        bindings = metadata.get("HostConfig", {}).get("PortBindings") or {}
        published = metadata.get("NetworkSettings", {}).get("Ports") or {}
        port_key = f"{env['port']}/tcp"
        if bindings.get(port_key) or published.get(port_key):
            raise ValueError("Monitor port must not be published on the host")
        for root, revision in [
            (env["root"], env["adaptor_revision"]),
            (env["root"] + "/benchmark", env["aisbench_revision"]),
        ]:
            actual = (
                control.command(
                    client,
                    ["docker", "exec", env["container"], "git", "-C", root, "rev-parse", "HEAD"],
                )
                .decode()
                .strip()
            )
            if actual != revision:
                raise ValueError("Pinned tool checkout revision changed")
        control.command(
            client,
            [
                "docker",
                "exec",
                env["container"],
                "python3",
                "-c",
                script,
                env["root"],
                json.dumps(env["pins"]),
                str(env["port"]),
            ],
        )


class MonitoredInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config_version_id: uuid.UUID
    target_id: uuid.UUID
    resource_id: uuid.UUID
    resource_kind: str = Field(pattern="^aisbench_monitor$")
    run_mode: str = Field(pattern="^exploration$")
    max_model_requests: int = Field(ge=1, le=5)


router = APIRouter(
    prefix="/api/autoeval/v1/aisbench-monitored", dependencies=[Depends(development)]
)


@router.get("/resources")
async def resources(request: Request):
    try:
        env = environment(request.app.state.settings)
    except (ValueError, OSError):
        return {"data": [], "next_offset": None}
    return {
        "data": [{"id": env["id"], "binding_key": "远程 AISBench · 实时监控"}],
        "next_offset": None,
    }


@router.post("/resources/{resource_id}/validate")
async def preflight(resource_id: uuid.UUID, request: Request):
    env = environment(request.app.state.settings)
    if str(resource_id) != env["id"]:
        raise HTTPException(404, "Monitor resource not found")
    async with request.app.state.sessions() as session:
        if await session.scalar(
            select(ExecutionLease).where(ExecutionLease.container_id == env["container_id"])
        ):
            raise HTTPException(409, "Monitored container is occupied")
    try:
        await asyncio.to_thread(verify_environment, request.app.state.settings, env)
    except Exception as exc:
        raise HTTPException(
            503, "Pinned monitor environment is unavailable or port is occupied"
        ) from exc
    return {
        "data": {"environment_ready": True, "evaluation_ready": True},
        "request_id": request.state.request_id,
    }


@router.post("/runs", status_code=202)
async def submit(
    body: MonitoredInput,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=128),
):
    settings = request.app.state.settings
    env = environment(settings)
    if str(body.resource_id) != env["id"]:
        raise HTTPException(422, "Monitor resource mismatch")
    digest = hashlib.sha256(canonical_manifest(body.model_dump(mode="json"))).hexdigest()
    async with request.app.state.sessions() as session, session.begin():
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": "monitored:" + idempotency_key},
        )
        old = await session.scalar(
            select(EvaluationRun).where(
                EvaluationRun.trigger_scope == "local-operator:evaluation",
                EvaluationRun.idempotency_key == idempotency_key,
            )
        )
        if old:
            if old.request_hash != digest:
                raise HTTPException(409, "Idempotency conflict")
            return {"data": {"run_id": str(old.id), "state": old.state}}
        config = await session.get(TestConfigVersion, body.config_version_id)
        items = (
            await session.scalars(
                select(TestConfigItem).where(
                    TestConfigItem.config_version_id == body.config_version_id
                )
            )
        ).all()
        if not config or len(items) != 1 or items[0].dataset_key != "GPQA":
            raise HTTPException(422, "GPQA-only configuration required")
        params = items[0].parameter_values
        if (
            params.get("scope") != "Lite"
            or params.get("concurrency") != 1
            or not 1 <= params.get("output", 0) <= 2048
            or params.get("temperature") != 0
            or params.get("top_p") != 1
        ):
            raise HTTPException(
                422,
                "Monitored pilot requires Lite, concurrency=1, output<=2048, temperature=0, top_p=1",
            )
        binding = await session.scalar(
            select(ConfigDatasetBinding).where(ConfigDatasetBinding.config_item_id == items[0].id)
        )
        manifest = await session.get(DatasetManifest, binding.manifest_id) if binding else None
        if (
            not manifest
            or manifest.scope != "lite"
            or not 1 <= manifest.sample_count <= body.max_model_requests
        ):
            raise HTTPException(422, "Frozen Lite manifest exceeds budget")
        data = await asyncio.to_thread(read_manifest, settings, manifest)
        samples = data["samples"]
        if [s["sample_id"] for s in samples] != [f"gpqa_diamond:{i}" for i in range(len(samples))]:
            raise HTTPException(
                422, "CLI pilot supports the approved deterministic first-N GPQA manifest"
            )
        target = await session.get(TargetBinding, body.target_id)
        if (
            not target
            or target.remote_model != "deepseek-flash"
            or target.base_url != "https://api.deepseek.com"
        ):
            raise HTTPException(422, "Pilot target is not the approved DeepSeek service")
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": "monitor-resource:" + env["container_id"]},
        )
        if await session.scalar(
            select(ExecutionLease).where(ExecutionLease.container_id == env["container_id"])
        ):
            raise HTTPException(409, "Monitored container is occupied")
        await asyncio.to_thread(verify_environment, settings, env)
        run = EvaluationRun(
            trigger_scope="local-operator:evaluation",
            idempotency_key=idempotency_key,
            request_hash=digest,
            run_mode="exploration",
            created_by="local-operator",
        )
        session.add(run)
        await session.flush()
        abbr = "ae-" + run.id.hex[:16]
        job = env["root"] + "/jobs/" + str(run.id)
        identity = {
            "container": env["container"],
            "container_id": env["container_id"],
            "port": env["port"],
            "work_dir": job + "/output",
            "task_name": abbr + "/GPQA_diamond",
        }
        profile = await session.get(EvaluationProfile, binding.profile_id)
        frozen = {
            "execution_origin": "real",
            "execution_path": "aisbench_cli_monitor",
            "remote_model": target.remote_model,
            "base_url": target.base_url,
            "target_id": str(target.id),
            "config_version_id": str(config.id),
            "config_sha256": config.content_hash,
            "parameters": params,
            "manifest_id": str(manifest.id),
            "manifest_sha256": manifest.sha256,
            "samples": samples,
            "profile_id": str(binding.profile_id),
            "profile_sha256": profile.sha256,
            "profile_execution_override": "pinned_cli_monitor",
            "model_version_id": str(target.model_version_id),
            "resource_kind": "aisbench_monitor",
            "resource_id": env["id"],
            "max_model_requests": body.max_model_requests,
            "max_output_tokens": params["output"],
            "tool": {
                "revision": env["aisbench_revision"],
                "source_url": "https://github.com/SJTUyh/benchmark",
                "patches": env["pins"],
            },
            "monitor_environment": env,
            "monitor_identity": identity,
            "model_abbr": abbr,
        }
        session.add(ExecutionLease(container_id=env["container_id"], run_id=run.id))
        session.add(
            RunSnapshot(
                run_id=run.id,
                schema_version="gpqa-cli.v1",
                canonical_body=frozen,
                sha256=hashlib.sha256(canonical_manifest(frozen)).hexdigest(),
            )
        )
        session.add(MonitorBinding(run_id=run.id, attempt=1, identity=identity))
        session.add(
            Outbox(
                run_id=run.id,
                kind="START_RUN",
                dedup_key="start:" + str(run.id),
                payload={
                    "workflow_kind": "monitored_gpqa_v1",
                    "workflow_queue": "demo.monitored.v1",
                },
            )
        )
        session.add(
            RunEvent(
                run_id=run.id,
                seq=1,
                type="accepted",
                body={"execution_path": "aisbench_cli_monitor"},
            )
        )
        return {
            "data": {"run_id": str(run.id), "state": "accepted"},
            "request_id": request.state.request_id,
        }


class Gateway:
    def __init__(self, settings, frozen, run_id):
        self.settings, self.frozen, self.run_id = settings, frozen, run_id
        self.env = frozen["monitor_environment"]
        self.control = control_for(settings, self.env)

    def operation(self, action, payload=None):
        with self.control.connect() as client:
            raw = self.control.command(
                client,
                [
                    "docker",
                    "exec",
                    "-i",
                    self.env["container"],
                    "python3",
                    self.env["runtime"],
                    action,
                    self.env["root"],
                    self.run_id,
                ],
                json.dumps(payload).encode() if payload else None,
            )
            return json.loads(raw)

    def read(self, relative):
        # Fixed filenames only, never an arbitrary path from HTTP parameters.
        if relative not in {
            "monitor-final.json",
            "raw-artifacts.json",
            "unified_result.json",
            "output/adaptor_result.json",
        }:
            raise ValueError("Unknown artifact")
        with self.control.connect() as client:
            job_root = f"{self.control.root}/jobs/{self.run_id}"
            self.control.prepare(client)
            self.control.command(client, ["mkdir", "-p", "-m", "700", job_root])
            filename = relative.replace("/", "-")
            source = f"{self.env['container']}:{self.env['root']}/jobs/{self.run_id}/{relative}"
            path = job_root + "/" + filename
            self.control.command(client, ["docker", "cp", source, path])
            with client.open_sftp() as sftp:
                if not stat.S_ISREG(sftp.lstat(path).st_mode):
                    raise ValueError("Artifact is not regular")
                with sftp.open(path, "rb") as stream:
                    raw = stream.read(16 * 1024 * 1024 + 1)
                if len(raw) > 16 * 1024 * 1024:
                    raise ValueError("Artifact too large")
                sftp.remove(path)
                sftp.rmdir(job_root)
            return raw


async def transition(sessions, run_id, state, event):
    async with sessions() as session, session.begin():
        run = await session.scalar(
            select(EvaluationRun).where(EvaluationRun.id == run_id).with_for_update()
        )
        run.state = state
        run.projection_version += 1
        session.add(
            RunEvent(
                run_id=run_id,
                seq=run.projection_version,
                type=event,
                body={"execution_path": "aisbench_cli_monitor"},
            )
        )


async def submit_once(sessions, run_id, gateway, payload):
    """Commit submit intent before SSH. Uncertain submissions may only be queried."""
    async with sessions() as session, session.begin():
        run = await session.scalar(
            select(EvaluationRun).where(EvaluationRun.id == run_id).with_for_update()
        )
        old = await session.scalar(
            select(RunEvent)
            .where(RunEvent.run_id == run_id, RunEvent.type == "monitor_submit_intent")
            .limit(1)
        )
        first = old is None
        if first and run.cancel_requested_at:
            return {"state": "cancelled", "submitted": False}
        if first:
            run.projection_version += 1
            session.add(
                RunEvent(
                    run_id=run_id,
                    seq=run.projection_version,
                    type="monitor_submit_intent",
                    body={"operation_key": str(run_id), "automatic_resubmission": False},
                )
            )
    if first:
        return await asyncio.to_thread(gateway.operation, "submit", payload)
    result = await asyncio.to_thread(gateway.operation, "status")
    if result["state"] == "unknown":
        raise ValueError("Prior submission is uncertain; generation resubmission prohibited")
    return result


async def archive_terminal_monitor(
    sessions, settings, binding, gateway, identity, *, final_allowed=True
):
    """Archive terminal evidence without changing scoring or cancellation outcomes."""
    try:
        if not final_allowed:
            raise ValueError("Only a confirmed terminal job may archive a final snapshot")
        final = json.loads(await asyncio.to_thread(gateway.read, "monitor-final.json"))
        matching = [t for t in final["tasks"] if t["task_name"] == identity["task_name"]]
        if len(matching) != 1:
            raise ValueError("Final monitor task identity mismatch")
        await save_observation(
            sessions,
            settings,
            binding.id,
            matching[0],
            "local_monitor",
            datetime.now(UTC).isoformat(),
            "final",
        )
        return "final"
    except Exception:  # noqa: BLE001 -- final evidence may be absent after abnormal termination
        logging.getLogger(__name__).warning("Final monitor unavailable run=%s", binding.run_id)
    try:
        async with sessions() as session:
            last = await session.get(MonitorBinding, binding.id)
            projection = last.latest
        if projection:
            task = {
                "task_name": identity["task_name"],
                "status": projection["tool_status"],
                "process_id": projection["process_id"],
                "progress_detail": {
                    "finish_count": projection["finish_count"],
                    "total_count": projection["total_count"],
                },
            }
            await save_observation(
                sessions,
                settings,
                binding.id,
                task,
                "local_monitor",
                projection["observed_at"],
                "last_observed",
            )
            return "last_observed"
    except Exception:  # noqa: BLE001 -- monitoring must not alter the original result
        logging.getLogger(__name__).warning("Monitor fallback unavailable run=%s", binding.run_id)
    return None


async def archive_native_artifacts(sessions, settings, run_id, gateway):
    await asyncio.to_thread(gateway.operation, "bundle")
    artifacts = await asyncio.to_thread(gateway.read, "raw-artifacts.json")
    artifact_sha = hashlib.sha256(artifacts).hexdigest()
    artifact_key = f"native-artifacts/{run_id}/{artifact_sha}.json"
    await asyncio.to_thread(write_manifest, settings, artifact_key, artifacts)
    from types import SimpleNamespace

    await asyncio.to_thread(
        read_manifest, settings, SimpleNamespace(object_key=artifact_key, sha256=artifact_sha)
    )
    async with sessions() as session, session.begin():
        existing = await session.scalar(
            select(UnifiedArtifact).where(
                UnifiedArtifact.run_id == run_id,
                UnifiedArtifact.collection_key == "cli-native-v1",
            )
        )
        if existing:
            if existing.sha256 != artifact_sha or existing.object_key != artifact_key:
                raise ValueError("Native artifact history differs from the immutable archive")
        else:
            session.add(
                UnifiedArtifact(
                    run_id=run_id,
                    collection_key="cli-native-v1",
                    object_key=artifact_key,
                    sha256=artifact_sha,
                    manifest={
                        "files": [
                            {k: v for k, v in f.items() if k != "content"}
                            for f in json.loads(artifacts)["files"]
                        ]
                    },
                )
            )
    return artifact_sha


async def finish_cleanup(sessions, run_id, gateway, artifact_sha):
    cleanup = await asyncio.to_thread(gateway.operation, "cleanup")
    proofs = {"artifact_sha256": artifact_sha, "remote": cleanup["state"]}
    async with sessions() as session, session.begin():
        existing = await session.scalar(
            select(ExecutionCleanup).where(ExecutionCleanup.run_id == run_id)
        )
        if existing:
            if existing.state != "cleaned" or existing.verified_proofs != proofs:
                raise ValueError("Cleanup history differs from the verified remote proof")
        else:
            session.add(
                ExecutionCleanup(
                    run_id=run_id,
                    state="cleaned",
                    attempt_count=1,
                    verified_proofs=proofs,
                )
            )
        await session.execute(delete(ExecutionLease).where(ExecutionLease.run_id == run_id))


@activity.defn
async def execute_monitored_gpqa(run_id: str):
    settings = get_settings()
    engine, sessions = create_database(settings)
    identifier = uuid.UUID(run_id)
    gateway = binding = identity = None
    try:
        async with sessions() as session:
            snapshot = await session.scalar(
                select(RunSnapshot).where(RunSnapshot.run_id == identifier)
            )
            binding = await session.scalar(
                select(MonitorBinding).where(MonitorBinding.run_id == identifier)
            )
            existing_report = await session.scalar(
                select(EvaluationReport).where(EvaluationReport.run_id == identifier)
            )
            existing_cleanup = await session.scalar(
                select(ExecutionCleanup).where(ExecutionCleanup.run_id == identifier)
            )
            run = await session.get(EvaluationRun, identifier)
            if run.state == "completed" and existing_report:
                await asyncio.to_thread(read_manifest, settings, existing_report)
                return {
                    "report_id": str(existing_report.id),
                    "accuracy": existing_report.summary.get("accuracy"),
                }
            frozen = snapshot.canonical_body
            if (
                hashlib.sha256(canonical_manifest(frozen)).hexdigest() != snapshot.sha256
                or frozen.get("execution_path") != "aisbench_cli_monitor"
            ):
                raise ValueError("Invalid CLI snapshot")
            if existing_cleanup and existing_cleanup.state == "cleaned":
                if existing_report:
                    await asyncio.to_thread(read_manifest, settings, existing_report)
                    if run.state != "completed":
                        await transition(sessions, identifier, "completed", "completed")
                    return {
                        "report_id": str(existing_report.id),
                        "accuracy": existing_report.summary.get("accuracy"),
                    }
                if run.cancel_requested_at or run.state in {"cancelling", "cancelled"}:
                    if run.state != "cancelled":
                        await transition(sessions, identifier, "cancelled", "cancelled")
                    return {"state": "cancelled"}
            if run.state == "cancelled":
                artifact = await session.scalar(
                    select(UnifiedArtifact).where(
                        UnifiedArtifact.run_id == identifier,
                        UnifiedArtifact.collection_key == "cli-native-v1",
                    )
                )
                if artifact:
                    await asyncio.to_thread(read_manifest, settings, artifact)
                return {"state": "cancelled"}
            target = await session.get(TargetBinding, uuid.UUID(frozen["target_id"]))
        gateway = Gateway(settings, frozen, run_id)
        identity = frozen["monitor_identity"]
        budget = {
            "max_model_requests": frozen.get("max_model_requests"),
            "max_output_tokens": frozen.get("max_output_tokens", 64),
        }
        sample_count = len(frozen["samples"])
        parameters = frozen["parameters"]
        if (
            type(budget["max_model_requests"]) is not int
            or not 1 <= budget["max_model_requests"] <= 5
            or not 1 <= sample_count <= budget["max_model_requests"]
            or parameters.get("concurrency") != 1
            or type(parameters.get("output")) is not int
            or not 1 <= parameters["output"] <= budget["max_output_tokens"]
        ):
            raise ValueError("Frozen CLI configuration exceeds the model budget")
        config = {
            "tool": "ais_bench",
            "subcommand": "llm_accuracy",
            "params": {
                "abbr": frozen["model_abbr"],
                "model_type": "vllm_api_general_chat",
                "model": frozen["remote_model"],
                "url": frozen["base_url"],
                "max_out_len": parameters["output"],
                "retry": 1,
                "batch_size": 1,
                "datasets": ["gpqa_gen_0_shot_cot_chat_prompt"],
                "dataset_test_range": f"[0:{sample_count}]",
                "num_prompts": sample_count,
                "max_num_workers": 1,
                "dump_eval_details": True,
                "monitor_port": identity["port"],
                "work_dir": identity["work_dir"],
                "ais_bench_root": gateway.env["root"] + "/benchmark",
                "generation_kwargs": {
                    "temperature": 0,
                    "top_p": 1,
                    "thinking": {"type": "disabled"},
                },
            },
        }
        key = FileSecretStore(settings.secret_store_path).get(target.credential_ref)
        await transition(sessions, identifier, "preparing", "preparing")
        result = await submit_once(
            sessions,
            identifier,
            gateway,
            {"config": config, "pins": gateway.env["pins"], "api_key": key, "budget": budget},
        )
        key = ""
        if result["state"] == "running":
            await transition(sessions, identifier, "running", "running")
        probe = (
            SSHMonitorClient(gateway.control.connection, identity)
            if result["state"] == "running"
            else None
        )
        deadline = asyncio.get_running_loop().time() + 480
        while result["state"] == "running" and asyncio.get_running_loop().time() < deadline:
            activity.heartbeat("monitoring")
            async with sessions() as session:
                run = await session.get(EvaluationRun, identifier)
                if run.cancel_requested_at:
                    if run.state != "cancelling":
                        await transition(sessions, identifier, "cancelling", "cancelling")
                    await asyncio.to_thread(gateway.operation, "cancel")
            try:
                task = await asyncio.to_thread(probe.poll)
                await save_observation(
                    sessions,
                    settings,
                    binding.id,
                    task,
                    "local_monitor",
                    datetime.now(UTC).isoformat(),
                )
            except Exception:  # noqa: BLE001 -- collection must not restart generation
                logging.getLogger(__name__).warning("Monitor unavailable run=%s", run_id)
            await asyncio.sleep(2)
            result = await asyncio.to_thread(gateway.operation, "status")
        if result["state"] == "cancelled":
            await archive_terminal_monitor(sessions, settings, binding, gateway, identity)
            artifact_sha = None
            if result.get("submitted", True):
                artifact_sha = await archive_native_artifacts(
                    sessions, settings, identifier, gateway
                )
            await finish_cleanup(sessions, identifier, gateway, artifact_sha)
            await transition(sessions, identifier, "cancelled", "cancelled")
            return {"state": "cancelled"}
        if result["state"] not in {"succeeded", "failed"}:
            raise ValueError("Remote outcome unknown; submission remains occupied")
        await archive_terminal_monitor(sessions, settings, binding, gateway, identity)
        await transition(sessions, identifier, "aggregating", "aggregating")
        await asyncio.to_thread(gateway.operation, "collect")
        raw = await asyncio.to_thread(gateway.read, "unified_result.json")
        expected = {"GPQA_diamond": [str(i) for i in range(len(frozen["samples"]))]}
        summary = await archive_output(sessions, settings, identifier, "cli-v1", raw, expected)
        native = json.loads(raw)
        complete = (
            summary["validity"] == "complete"
            and summary["attempt_count"] <= budget["max_model_requests"]
            and native.get("score_source") == "ais_bench_results_json"
            and len(native["cases"]) == sample_count
        )
        scores = [row.get("score") for row in native["cases"]]
        complete = complete and all(type(v) in {int, float} and v in {0, 1} for v in scores)
        accuracy = 100 * sum(scores) / len(scores) if complete else None
        if complete and not math.isclose(native["total_score"] * 100, accuracy, abs_tol=0.01):
            raise ValueError("Upstream score denominator mismatch")
        report_summary = {
            "expected_samples": len(frozen["samples"]),
            "executed_attempts": summary["attempt_count"],
            "successful_samples": summary["observed_cases"] - summary["failed_cases"],
            "failed_samples": summary["failed_cases"],
            "missing_samples": summary["missing_cases"],
            "accuracy": accuracy,
            "subset_scope": "lite",
            "dataset_samples": 198,
            "metric_scope": "selected_manifest",
            "scoring_status": "succeeded" if complete else "failed",
            "budget_exceeded": summary["attempt_count"] > budget["max_model_requests"],
        }
        validity = (
            "failed"
            if summary["attempt_count"] > budget["max_model_requests"]
            else "complete"
            if complete
            else summary["validity"]
            if summary["validity"] != "complete"
            else "partial"
        )
        body = {
            "schema_version": "gpqa-report.v1",
            "run_id": run_id,
            "snapshot_sha256": snapshot.sha256,
            "execution_origin": "real",
            "summary": report_summary,
            "validity": validity,
        }
        report_raw = canonical_manifest(body)
        sha = hashlib.sha256(report_raw).hexdigest()
        object_key = f"reports/{run_id}/{sha}.json"
        await asyncio.to_thread(write_manifest, settings, object_key, report_raw)
        artifact_sha = await archive_native_artifacts(sessions, settings, identifier, gateway)
        async with sessions() as session, session.begin():
            report = await session.scalar(
                select(EvaluationReport).where(EvaluationReport.run_id == identifier)
            )
            if report:
                if (
                    report.sha256 != sha
                    or report.object_key != object_key
                    or report.snapshot_sha256 != snapshot.sha256
                ):
                    raise ValueError("Report history differs from the deterministic result")
            else:
                report = EvaluationReport(
                    run_id=identifier,
                    execution_origin="real",
                    validity=validity,
                    snapshot_sha256=snapshot.sha256,
                    summary=report_summary,
                    object_key=object_key,
                    sha256=sha,
                )
                session.add(report)
                await session.flush()
            await asyncio.to_thread(read_manifest, settings, report)
            return_id = str(report.id)
        await finish_cleanup(sessions, identifier, gateway, artifact_sha)
        await transition(sessions, identifier, "completed", "completed")
        return {"report_id": return_id, "accuracy": accuracy}
    except Exception:
        if gateway is not None and binding is not None:
            await archive_terminal_monitor(
                sessions, settings, binding, gateway, identity, final_allowed=False
            )
        async with sessions() as session:
            run = await session.get(EvaluationRun, identifier)
            cancelled = bool(run and run.cancel_requested_at)
        await transition(
            sessions,
            identifier,
            "cancelling" if cancelled else "failed",
            "execution_unresolved" if cancelled else "monitor_execution_requires_reconciliation",
        )
        raise
    finally:
        await engine.dispose()
