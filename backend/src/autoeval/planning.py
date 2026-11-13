"""Persistent planning intents. Workers own calculations and remote generation probes."""

import asyncio
import copy
import uuid
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import DateTime, String, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from autoeval.datasets import DatasetManifest, canonical_manifest, read_manifest, write_manifest
from autoeval.models import Base, Record
from autoeval.planning_engine import DATASETS, PLANNER_REVISION, apply_concurrency, selected_samples
from autoeval.standard_inputs import (
    StandardInputCompilation,
    StandardInputVersion,
    digest,
    reject_secrets,
)
from autoeval.targets import TargetBinding


class PlanningArtifact(Record, Base):
    __tablename__ = "planning_artifact"
    kind: Mapped[str] = mapped_column(String(24))
    sha256: Mapped[str] = mapped_column(String(64))
    object_key: Mapped[str] = mapped_column(String(256))
    payload: Mapped[dict] = mapped_column(JSONB)


class PlanningJob(Record, Base):
    __tablename__ = "planning_job"
    kind: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(24), default="pending")
    payload: Mapped[dict] = mapped_column(JSONB)
    result: Mapped[dict | None] = mapped_column(JSONB)
    cancel_requested: Mapped[bool] = mapped_column(default=False)
    # Worker uses a session advisory lock; running jobs may be resumed after worker death.
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC)
    )


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


class ResourceRef(Strict):
    resource_id: uuid.UUID
    resource_kind: Literal["docker_local", "customer_ssh"]


class ProbeInput(ResourceRef):
    target_id: uuid.UUID
    concurrency: list[int] = Field(default_factory=lambda: [1, 2, 4, 8], min_length=1, max_length=7)
    requests_per_level: int = Field(default=2, ge=1, le=64)
    prompt_tokens: int = Field(default=512, ge=1, le=4096)
    max_tokens: int = Field(default=512, ge=1, le=4096)

    @model_validator(mode="after")
    def levels(self):
        if sorted(set(self.concurrency)) != self.concurrency or any(
            not 1 <= n <= 64 for n in self.concurrency
        ):
            raise ValueError("Unique increasing concurrency levels in 1..64 required")
        return self


class TaskInput(Strict):
    task_id: str = Field(pattern=r"^[a-zA-Z0-9_-]{1,64}$")
    compilation_id: uuid.UUID
    manifest_id: uuid.UUID
    dataset: Literal["GPQA", "MMMU-Pro", "DeepSWE"]


class PlanInput(ResourceRef):
    target_id: uuid.UUID
    throughput_profile_id: uuid.UUID | None = None
    tasks: list[TaskInput] = Field(min_length=1, max_length=3)
    dataset_concurrency: int = Field(default=1, ge=1, le=3)
    recommend: bool = True
    runs: int = Field(default=100, ge=1, le=100)
    seed: int = Field(default=20261111, ge=0, le=2147483647)

    @model_validator(mode="after")
    def unique(self):
        if len({t.task_id for t in self.tasks}) != len(self.tasks) or len(
            {t.dataset for t in self.tasks}
        ) != len(self.tasks):
            raise ValueError("Duplicate task or dataset")
        return self


class CurveInput(ResourceRef):
    target_id: uuid.UUID
    curve: dict


class ApplyInput(Strict):
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


router = APIRouter(prefix="/api/autoeval/v1/planning", tags=["task planning"])


def envelope(data, request):
    return {"data": data, "request_id": request.state.request_id}


async def artifact(session, settings, kind, payload):
    raw = canonical_manifest(payload)
    sha = digest(payload)
    key = f"planning/{kind}/{sha}.json"
    await asyncio.to_thread(write_manifest, settings, key, raw)
    row = PlanningArtifact(kind=kind, sha256=sha, object_key=key, payload=payload)
    session.add(row)
    await session.flush()
    return row


async def target_data(session, target_id):
    target = await session.get(TargetBinding, target_id)
    if target is None:
        raise HTTPException(404, "Target not found")
    return {"base_url": target.base_url, "model": target.remote_model}


async def resolve_inputs(session, settings, body):
    from autoeval.profiles import EvaluationProfile
    from autoeval.registry import Asset, AssetVersion

    target = await target_data(session, body.target_id)
    tasks = []
    for spec in body.tasks:
        compilation = await session.get(StandardInputCompilation, spec.compilation_id)
        manifest = await session.get(DatasetManifest, spec.manifest_id)
        if not compilation or not manifest:
            raise HTTPException(404, "Compilation or manifest not found")
        version = await session.get(AssetVersion, manifest.dataset_version_id)
        asset_row = await session.get(Asset, version.asset_id)
        if asset_row.asset_key not in {
            spec.dataset,
            "GPQA_Diamond" if spec.dataset == "GPQA" else spec.dataset,
        }:
            raise HTTPException(422, "Manifest dataset identity mismatch")
        profile = await session.scalar(
            select(EvaluationProfile)
            .where(
                EvaluationProfile.dataset_name == spec.dataset,
                (EvaluationProfile.full_manifest_id == manifest.id)
                | (EvaluationProfile.lite_manifest_id == manifest.id),
            )
            .order_by(EvaluationProfile.created_at.desc())
            .limit(1)
        )
        if not profile:
            raise HTTPException(422, "Versioned tool/scorer profile required for selected manifest")
        compiled = copy.deepcopy(compilation.payload)
        if (
            not compiled.get("input_ready")
            or compiled.get("adaptor_revision") != "955089ab32ef696c231d628f5ad0d9da28f430c4"
        ):
            raise HTTPException(422, "Compilation is blocked or uses an unsupported adaptor")
        if (compiled.get("credential_binding") or {}).get("target_id") != str(body.target_id):
            raise HTTPException(422, "Compilation belongs to another target")
        source_input = await session.get(StandardInputVersion, compilation.input_id)
        if source_input.benchmark != ("GPQA_Diamond" if spec.dataset == "GPQA" else spec.dataset):
            raise HTTPException(422, "Compilation dataset identity mismatch")
        config = compiled["config"]
        params = config["params"]
        if config["subcommand"] == "llm_accuracy" and len(params.get("datasets", [])) != 1:
            raise HTTPException(422, "Each compilation must select exactly one dataset")
        if (spec.dataset == "DeepSWE") != (config["subcommand"] == "harbor_agent"):
            raise HTTPException(422, "Dataset tool family mismatch")
        full = await session.get(DatasetManifest, profile.full_manifest_id)
        source = await asyncio.to_thread(read_manifest, settings, full)
        samples = [
            dict(sample, tool_case_id=str(i) if spec.dataset != "DeepSWE" else sample["sample_id"])
            for i, sample in enumerate(source["samples"])
        ]
        if manifest.scope == "lite":
            lite = await asyncio.to_thread(read_manifest, settings, manifest)
            wanted = {s["sample_id"]: s["sha256"] for s in lite["samples"]}
            if any(
                wanted.get(s["sample_id"]) != s["sha256"]
                for s in samples
                if s["sample_id"] in wanted
            ):
                raise HTTPException(422, "Lite manifest differs from full manifest")
            # Accuracy Lite selections must map to a contiguous tool range.
            positions = [i for i, s in enumerate(samples) if s["sample_id"] in wanted]
            if len(positions) != len(wanted):
                raise HTTPException(422, "Lite samples missing from full manifest")
            if config["subcommand"] == "llm_accuracy":
                if positions != list(range(positions[0], positions[-1] + 1)):
                    raise HTTPException(
                        422,
                        "Non-contiguous accuracy Lite selection is not supported by this adaptor",
                    )
                params["dataset_test_range"] = f"[{positions[0]}:{positions[-1] + 1}]"
            else:
                params["task_names"] = list(wanted)
        chosen = selected_samples(config, samples)
        field = "batch_size" if config["subcommand"] == "llm_accuracy" else "n_concurrent_trials"
        attempts = int(params.get("n_attempts", 1)) if spec.dataset == "DeepSWE" else 1
        concurrency = int(params.get(field, 1))
        if not 1 <= concurrency <= 256 or not 1 <= attempts <= 100:
            raise HTTPException(422, "Invalid concurrency or attempts")
        tasks.append(
            {
                **spec.model_dump(mode="json"),
                "compilation": compiled,
                "compilation_sha256": compilation.sha256,
                "manifest_sha256": manifest.sha256,
                "profile_sha256": profile.sha256,
                "tool": profile.contract,
                "samples": chosen,
                "attempts": attempts,
                "concurrency": concurrency,
            }
        )
    return {
        **body.model_dump(mode="json"),
        "target": target,
        "tasks": tasks,
        "planner_revision": PLANNER_REVISION,
    }


@router.get("/options")
async def options(request: Request, offset: int = 0, limit: int = 100, include_test: bool = False):
    from autoeval.profiles import EvaluationProfile

    if offset < 0 or not 1 <= limit <= 200:
        raise HTTPException(422, "Invalid pagination")
    async with request.app.state.sessions() as session:
        compilations = list(
            await session.scalars(
                select(StandardInputCompilation)
                .order_by(StandardInputCompilation.created_at.desc(), StandardInputCompilation.id)
                .offset(offset)
                .limit(limit + 1)
            )
        )
        profiles = list(
            await session.scalars(
                select(EvaluationProfile)
                .where(EvaluationProfile.dataset_name.in_(DATASETS))
                .where(
                    True
                    if include_test
                    else ~EvaluationProfile.profile_key.op("~")(
                        r"^(fixture-[a-f0-9]{32}|browser-[a-f0-9]{12}|pipeline-[a-f0-9]{32})$"
                    )
                )
                .order_by(EvaluationProfile.created_at.desc(), EvaluationProfile.id)
                .offset(offset)
                .limit(limit + 1)
            )
        )
        curves = list(
            await session.scalars(
                select(PlanningArtifact)
                .where(PlanningArtifact.kind == "throughput")
                .order_by(PlanningArtifact.created_at.desc(), PlanningArtifact.id)
                .offset(offset)
                .limit(limit + 1)
            )
        )
        has_more = any(len(rows) > limit for rows in (compilations, profiles, curves))
        compilations, profiles, curves = compilations[:limit], profiles[:limit], curves[:limit]
        source_rows = list(
            await session.scalars(
                select(StandardInputVersion).where(
                    StandardInputVersion.id.in_({c.input_id for c in compilations})
                )
            )
        )
        sources = {r.id: r.benchmark for r in source_rows}
        labels = {r.id: r.source_model + " / " + r.benchmark for r in source_rows}
        manifest_ids = {m for p in profiles for m in (p.full_manifest_id, p.lite_manifest_id) if m}
        manifests = list(
            await session.scalars(
                select(DatasetManifest).where(DatasetManifest.id.in_(manifest_ids))
            )
        )
        return {
            **envelope(
                {
                    "compilations": [
                        {
                            "id": str(c.id),
                            "dataset": "GPQA"
                            if sources.get(c.input_id) == "GPQA_Diamond"
                            else sources.get(c.input_id),
                            "label": labels.get(c.input_id, "")
                            + " / "
                            + c.payload.get("template_version", ""),
                            "sha256": c.sha256,
                            "adaptor_revision": c.payload.get("adaptor_revision"),
                            "runtime_config": {
                                k: v
                                for k, v in c.payload.get("runtime_config", {}).items()
                                if k not in {"work_dir", "container_name"}
                            },
                            "target_id": (c.payload.get("credential_binding") or {}).get(
                                "target_id"
                            ),
                            "config": c.payload["config"],
                            "input_ready": c.payload.get("input_ready"),
                        }
                        for c in compilations
                    ],
                    "manifests": list(
                        {
                            str(m.id): {
                                "id": str(m.id),
                                "scope": m.scope,
                                "sample_count": m.sample_count,
                                "sha256": m.sha256,
                                "dataset": p.dataset_name,
                            }
                            for p in profiles
                            for m in manifests
                            if m.id in {p.full_manifest_id, p.lite_manifest_id}
                        }.values()
                    ),
                    "curves": [
                        {
                            "id": str(c.id),
                            **{
                                k: c.payload.get(k)
                                for k in (
                                    "target_id",
                                    "resource_id",
                                    "resource_kind",
                                    "measured_at",
                                )
                            },
                        }
                        for c in curves
                    ],
                },
                request,
            ),
            "next_offset": offset + limit if has_more else None,
        }


@router.post("/throughput-profiles", status_code=201)
async def import_curve(body: CurveInput, request: Request):
    from autoeval_planner.errors import PlannerError
    from autoeval_planner.throughput import ThroughputProfile

    # Request headers/body are not needed for simulation and can contain credentials.
    curve = {
        k: v
        for k, v in body.curve.items()
        if k
        in {
            "schema_version",
            "source",
            "base_url",
            "model",
            "measured_at",
            "levels",
            "derived",
            "decode_tps",
            "max_concurrency",
        }
    }
    try:
        reject_secrets(curve)
        digest(curve)  # Reject non-finite nested numbers before storing or simulating.
        profile = ThroughputProfile.from_dict(curve)
        profile.source = "manual"
    except (PlannerError, ValueError, KeyError) as exc:
        raise HTTPException(422, "Invalid throughput profile") from exc
    async with request.app.state.sessions() as session, session.begin():
        await target_data(session, body.target_id)
        row = await artifact(
            session,
            request.app.state.settings,
            "throughput",
            {
                **body.model_dump(mode="json"),
                "curve": profile.to_dict(),
                "measured_at": datetime.now(UTC).isoformat(),
                "source": "manual",
            },
        )
        return envelope({"id": str(row.id)}, request)


@router.post("/resources/preflight", status_code=202)
async def preflight(body: PlanInput, request: Request):
    async with request.app.state.sessions() as session:
        inputs = await resolve_inputs(session, request.app.state.settings, body)
    return await enqueue("preflight", inputs, request)


async def enqueue(kind, payload, request):
    async with request.app.state.sessions() as session, session.begin():
        key = request.headers.get("Idempotency-Key")
        if key:
            if len(key) > 256:
                raise HTTPException(422, "Idempotency key too long")
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtext(:key))"), {"key": "planning-job:" + key}
            )
            old = await session.scalar(
                select(PlanningJob).where(PlanningJob.payload["intent_key"].astext == key).limit(1)
            )
            if old:
                if (
                    old.kind != kind
                    or {k: v for k, v in old.payload.items() if k != "intent_key"} != payload
                ):
                    raise HTTPException(409, "Idempotency content conflict")
                return envelope(
                    {"id": str(old.id), "state": old.state, "result": old.result}, request
                )
            payload = {**payload, "intent_key": key}
        await artifact(session, request.app.state.settings, "job_input", payload)
        job = PlanningJob(kind=kind, payload=payload)
        session.add(job)
        await session.flush()
        return envelope({"id": str(job.id), "state": "pending"}, request)


@router.post("/probes", status_code=202)
async def probe(body: ProbeInput, request: Request):
    async with request.app.state.sessions() as session:
        await target_data(session, body.target_id)
    count = 2 * sum(max(n, body.requests_per_level) for n in body.concurrency)
    return await enqueue(
        "probe",
        {
            **body.model_dump(mode="json"),
            "request_budget": count,
            "max_output_token_budget": count * body.max_tokens,
        },
        request,
    )


@router.get("/probes/{job_id}")
async def job_status(job_id: uuid.UUID, request: Request):
    async with request.app.state.sessions() as session:
        row = await session.get(PlanningJob, job_id)
        if row is None:
            raise HTTPException(404, "Planning job not found")
        return envelope(
            {
                "id": str(row.id),
                "kind": row.kind,
                "state": row.state,
                "result": row.result,
                "cancel_requested": row.cancel_requested,
                "created_at": row.created_at.isoformat(),
                "updated_at": row.updated_at.isoformat(),
            },
            request,
        )


@router.post("/probes/{job_id}/cancel", status_code=202)
async def cancel_job(job_id: uuid.UUID, request: Request):
    async with request.app.state.sessions() as session, session.begin():
        row = await session.get(PlanningJob, job_id, with_for_update=True)
        if row is None:
            raise HTTPException(404, "Planning job not found")
        if row.state in {"pending", "running", "unresolved"}:
            row.cancel_requested = True
        return envelope({"accepted": row.cancel_requested}, request)


@router.post("/plans", status_code=202)
async def plan(body: PlanInput, request: Request):
    async with request.app.state.sessions() as session:
        inputs = await resolve_inputs(session, request.app.state.settings, body)
        if not body.throughput_profile_id:
            raise HTTPException(422, "Select or probe a throughput curve before estimating")
        curve = await session.get(PlanningArtifact, body.throughput_profile_id)
        if (
            not curve
            or curve.kind != "throughput"
            or any(
                curve.payload.get(k) != inputs[k]
                for k in ("target_id", "resource_id", "resource_kind")
            )
        ):
            raise HTTPException(
                422, "Throughput curve does not match target and execution environment"
            )
    return await enqueue("plan", inputs, request)


@router.get("/plans/{job_id}")
async def plan_status(job_id: uuid.UUID, request: Request):
    return await job_status(job_id, request)


def selection_identity(inputs):
    return sorted(
        (
            {
                **{
                    key: task[key]
                    for key in ("task_id", "dataset", "compilation_id", "manifest_id")
                },
                "profile_sha256": task.get("profile_sha256"),
            }
            for task in inputs.get("tasks", [])
        ),
        key=lambda task: task["task_id"],
    )


async def current_preflight(session, inputs, *, require_selection=True):
    rows = (
        await session.scalars(
            select(PlanningArtifact)
            .where(PlanningArtifact.kind == "resource")
            .order_by(PlanningArtifact.created_at.desc())
            .limit(100)
        )
    ).all()
    return next(
        (
            r
            for r in rows
            if all(r.payload.get(k) == inputs[k] for k in ("resource_id", "resource_kind"))
            and (
                not require_selection
                or (
                    r.payload.get("target_id") == inputs.get("target_id")
                    and selection_identity(r.payload) == selection_identity(inputs)
                )
            )
            and r.created_at > datetime.now(UTC) - timedelta(minutes=5)
        ),
        None,
    )


@router.post("/plans/{job_id}/apply", status_code=201)
async def apply_plan(job_id: uuid.UUID, body: ApplyInput, request: Request):
    async with request.app.state.sessions() as session, session.begin():
        job = await session.get(PlanningJob, job_id)
        if not job or job.kind != "plan" or job.state != "completed":
            raise HTTPException(409, "Plan is not ready")
        if digest(job.payload) != body.input_sha256:
            raise HTTPException(409, "Plan inputs changed")
        fresh = await current_preflight(session, job.payload)
        if not fresh or not fresh.payload.get("execution_ready"):
            raise HTTPException(409, "Fresh successful resource preflight required")
        if job.result.get("resource_sha256") != fresh.sha256:
            raise HTTPException(409, "Resource snapshot changed; replan")
        report = job.result["report"]
        inputs = copy.deepcopy(job.payload)
        try:
            from autoeval.orchestration import demand_for, fits

            for item in inputs["tasks"]:
                item["concurrency"] = report["plan"]["case_concurrency"][item["task_id"]]
                item["compilation"] = apply_concurrency(item["compilation"], item["concurrency"])
                if item["dataset"] not in fresh.payload["supported_datasets"] or not fits(
                    demand_for(item), {}, fresh.payload
                ):
                    raise ValueError("Dataset or concurrency exceeds validated resource capability")
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        inputs["dataset_concurrency"] = report["plan"]["dataset_concurrency"]
        inputs["tasks"].sort(key=lambda t: report["plan"]["dataset_order"].index(t["task_id"]))
        row = await artifact(
            session,
            request.app.state.settings,
            "execution",
            {
                "inputs": inputs,
                "report": report,
                "resource_sha256": fresh.sha256,
                "resource": fresh.payload,
                "source_plan_id": str(job.id),
            },
        )
        return envelope(
            {"execution_plan_id": str(row.id), "sha256": row.sha256, "report": report}, request
        )


@router.post("/runs", status_code=202)
async def submit_run(
    body: PlanInput,
    request: Request,
    idempotency_key: str = Header(min_length=1, max_length=256),
    execution_plan_id: uuid.UUID | None = None,
):
    from autoeval.orchestration import create_run

    async with request.app.state.sessions() as session, session.begin():
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": "orchestration:" + idempotency_key},
        )
        return envelope(
            await create_run(
                session, request.app.state.settings, body, execution_plan_id, idempotency_key
            ),
            request,
        )


@router.get("/runs")
async def list_runs(request: Request, offset: int = 0, limit: int = 20):
    from autoeval.models import EvaluationRun

    if offset < 0 or not 1 <= limit <= 100:
        raise HTTPException(422, "Invalid pagination")
    async with request.app.state.sessions() as session:
        rows = list(
            await session.scalars(
                select(EvaluationRun)
                .where(
                    EvaluationRun.trigger_scope == "local-operator:orchestration",
                )
                .order_by(EvaluationRun.created_at.desc())
                .offset(offset)
                .limit(limit + 1)
            )
        )
        return {
            **envelope(
                [
                    {"id": str(r.id), "state": r.state, "created_at": r.created_at.isoformat()}
                    for r in rows[:limit]
                ],
                request,
            ),
            "next_offset": offset + limit if len(rows) > limit else None,
        }


@router.get("/runs/{run_id}")
async def run_detail(run_id: uuid.UUID, request: Request):
    from autoeval.models import EvaluationRun, RunSnapshot
    from autoeval.orchestration import DatasetExecution, child_view

    async with request.app.state.sessions() as session:
        run = await session.get(EvaluationRun, run_id)
        if not run or run.trigger_scope != "local-operator:orchestration":
            raise HTTPException(404, "Planned run not found")
        snapshot = await session.scalar(select(RunSnapshot).where(RunSnapshot.run_id == run_id))
        children = list(
            await session.scalars(select(DatasetExecution).where(DatasetExecution.run_id == run_id))
        )
        reports = list(
            await session.scalars(
                select(PlanningArtifact)
                .where(
                    PlanningArtifact.kind == "run_report",
                    PlanningArtifact.payload["run_id"].astext == str(run_id),
                )
                .limit(1)
            )
        )
        return envelope(
            {
                "id": str(run.id),
                "state": run.state,
                "estimate": snapshot.canonical_body.get("estimate"),
                "datasets": [child_view(c) for c in children],
                "report": reports[0].payload if reports else None,
                "execution_origin": snapshot.canonical_body.get("execution_origin"),
            },
            request,
        )


@router.post("/runs/{run_id}/cancel", status_code=202)
async def cancel_run(run_id: uuid.UUID, request: Request):
    from autoeval.models import EvaluationRun

    async with request.app.state.sessions() as session, session.begin():
        run = await session.get(EvaluationRun, run_id, with_for_update=True)
        if not run or run.trigger_scope != "local-operator:orchestration":
            raise HTTPException(404, "Planned run not found")
        accepted = run.state not in {"completed", "failed", "cancelled"}
        if accepted:
            run.cancel_requested_at = datetime.now(UTC)
        return envelope({"accepted": accepted}, request)


@router.get("/runs/{run_id}/report")
async def run_report(run_id: uuid.UUID, request: Request):
    async with request.app.state.sessions() as session:
        report = await session.scalar(
            select(PlanningArtifact)
            .where(
                PlanningArtifact.kind == "run_report",
                PlanningArtifact.payload["run_id"].astext == str(run_id),
            )
            .limit(1)
        )
        if report is None:
            raise HTTPException(404, "Report not available")
        try:
            evidence = await asyncio.to_thread(read_manifest, request.app.state.settings, report)
        except ValueError as exc:
            raise HTTPException(503, "Report evidence integrity check failed") from exc
        return envelope(evidence, request)
