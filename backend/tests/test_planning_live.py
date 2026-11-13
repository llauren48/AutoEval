"""Real PostgreSQL/S3 orchestration with artificial outputs; never model generation."""

import asyncio
import os
import uuid
from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select

from autoeval.api import create_app
from autoeval.config import get_settings
from autoeval.contracts import JobObservation, JobRef
from autoeval.models import EvaluationRun, RunSnapshot
from autoeval.orchestration import CapacityLease, DatasetExecution, execute
from autoeval.planning import PlanningArtifact, PlanningJob
from autoeval.standard_inputs import digest
from autoeval.targets import TargetBinding

pytestmark = pytest.mark.skipif(
    os.getenv("AE_INFRA_TEST") != "1", reason="Explicit PostgreSQL/S3 integration flag required"
)


class ArtificialAdapter:
    """Artificial identity and scores are explicitly recorded as simulation."""

    def __init__(self, children, fail=None):
        self.children = {str(c.id): c for c in children}
        self.submitted = []
        self.active = set()
        self.peak = 0
        self.fail = fail
        self.cleaned = []

    async def submit(self, spec, key):
        self.submitted.append(key)
        self.active.add(key)
        self.peak = max(self.peak, len(self.active))
        return JobRef(external_job_id=key, submission_key=key)

    async def status(self, job):
        child = self.children[job.submission_key]
        return JobObservation(
            job=job, state="failed" if child.task_id == self.fail else "succeeded"
        )

    async def cancel(self, job, key):
        return JobObservation(job=job, state="cancelled")

    async def collect(self, job):
        child = self.children[job.submission_key]
        task = child.payload
        return {
            "state": "succeeded",
            "result": {
                "unified_result": {
                    "schema_version": 1,
                    "tool": "ais_bench",
                    "subcommand": task["compilation"]["config"]["subcommand"],
                    "case_count": 1,
                    "total_score": None,
                    "score_source": "TEST-artificial-simulation",
                    "cases": [
                        {
                            "case_id": "0",
                            "dataset": task["dataset"],
                            "attempt": 1,
                            "status": "success",
                            "score": 1.0,
                            "input_tokens": None,
                            "output_tokens": None,
                            "duration_sec": 0.01,
                        }
                    ],
                }
            },
        }

    async def cleanup(self, job):
        self.active.discard(job.submission_key)
        self.cleaned.append(job.submission_key)
        return {"cleaned": True}


async def seed(app, *, cancelled=False, parallel=2):
    identifier = uuid.uuid4()
    async with app.state.sessions() as session, session.begin():
        target = await session.scalar(select(TargetBinding).limit(1))
        assert target is not None, (
            "Integration environment needs an existing target binding (never called)"
        )
        tasks = []
        for dataset in ("GPQA", "MMMU-Pro", "DeepSWE"):
            tasks.append(
                {
                    "task_id": dataset,
                    "dataset": dataset,
                    "concurrency": 1,
                    "attempts": 1,
                    "samples": [{"sample_id": "TEST-fixture-sample", "tool_case_id": "0"}],
                    "compilation": {
                        "adaptor_revision": "TEST-artificial",
                        "config": {
                            "tool": "ais_bench",
                            "subcommand": "harbor_agent"
                            if dataset == "DeepSWE"
                            else "llm_accuracy",
                            "params": {},
                        },
                    },
                    "tool": {},
                }
            )
        inputs = {
            "target_id": str(target.id),
            "resource_id": str(uuid.uuid4()),
            "resource_kind": "docker_local",
            "dataset_concurrency": parallel,
            "tasks": tasks,
        }
        frozen = {
            "inputs": inputs,
            "estimate": None,
            "execution_origin": "simulation",
            "resource": {
                "node_id": "TEST-node-" + str(identifier),
                "cpu_cores": 32,
                "mem_mb": 262144,
                "disk_gb": 1024,
                "network_available": 16,
            },
        }
        session.add(
            EvaluationRun(
                id=identifier,
                trigger_scope="TEST:planning",
                idempotency_key=str(identifier),
                request_hash=digest(frozen),
                run_mode="exploration",
                created_by="TEST-simulation",
                cancel_requested_at=datetime.now(UTC) if cancelled else None,
            )
        )
        await session.flush()
        session.add(
            RunSnapshot(
                run_id=identifier,
                schema_version="planned-run.v1",
                canonical_body=frozen,
                sha256=digest(frozen),
            )
        )
        children = [
            DatasetExecution(id=uuid.uuid4(), run_id=identifier, task_id=t["task_id"], payload=t)
            for t in tasks
        ]
        session.add_all(children)
    return identifier, children


@pytest.mark.parametrize("parallel,fail", [(1, None), (2, None), (2, "MMMU-Pro")])
async def test_real_storage_three_dataset_execution_and_partial_failure(
    monkeypatch, parallel, fail
):
    app = create_app(get_settings())
    async with app.router.lifespan_context(app):
        run_id, children = await seed(app, parallel=parallel)
        fake = ArtificialAdapter(children, fail)

        async def transport(*args):
            return fake

        monkeypatch.setattr("autoeval.orchestration.transport", transport)
        monkeypatch.setattr(
            "autoeval.orchestration.FileSecretStore.get", lambda *args: "TEST-no-secret"
        )
        await asyncio.wait_for(execute(app.state.sessions, app.state.settings, run_id), 25)
        assert len(fake.submitted) == 3
        assert fake.peak == parallel
        async with app.state.sessions() as session:
            run = await session.get(EvaluationRun, run_id)
            assert run.state == ("failed" if fail else "completed")
            assert not list(
                await session.scalars(
                    select(CapacityLease).where(
                        CapacityLease.owner_id.in_([c.id for c in children]),
                        CapacityLease.released.is_(False),
                    )
                )
            )
            report = await session.scalar(
                select(PlanningArtifact).where(
                    PlanningArtifact.kind == "run_report",
                    PlanningArtifact.payload["run_id"].astext == str(run_id),
                )
            )
            assert report.payload["execution_origin"] == "simulation"
            assert report.payload["actual_duration_sec"] is not None
            assert all(c["actual_duration_sec"] is not None for c in report.payload["datasets"])
        # Retry after completion must not run a tool or emit a second report.
        await execute(app.state.sessions, app.state.settings, run_id)
        assert len(fake.submitted) == 3


async def test_cancel_before_submission_never_calls_model(monkeypatch):
    app = create_app(get_settings())
    async with app.router.lifespan_context(app):
        run_id, children = await seed(app, cancelled=True)
        fake = ArtificialAdapter(children)

        async def transport(*args):
            return fake

        monkeypatch.setattr("autoeval.orchestration.transport", transport)
        monkeypatch.setattr(
            "autoeval.orchestration.FileSecretStore.get", lambda *args: "TEST-no-secret"
        )
        await execute(app.state.sessions, app.state.settings, run_id)
        assert not fake.submitted
        async with app.state.sessions() as session:
            assert (await session.get(EvaluationRun, run_id)).state == "cancelled"


async def test_planning_routes_and_invalid_probe_validation():
    app = create_app(get_settings())
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
    ):
        options = await client.get("/api/autoeval/v1/planning/options")
        assert options.status_code == 200, options.text
        assert set(options.json()["data"]) == {"compilations", "manifests", "curves"}
        response = await client.post(
            "/api/autoeval/v1/planning/probes",
            json={
                "target_id": str(uuid.uuid4()),
                "resource_id": str(uuid.uuid4()),
                "resource_kind": "docker_local",
                "concurrency": [65],
            },
        )
        assert response.status_code == 422


async def test_capacity_is_shared_across_distinct_runs():
    from autoeval.orchestration import acquire, release

    app = create_app(get_settings())
    resource = {
        "node_id": "TEST-capacity-" + uuid.uuid4().hex,
        "cpu_cores": 10,
        "mem_mb": 32768,
        "disk_gb": 100,
        "network_available": 4,
    }
    demand = {"cpu": 6, "mem": 4096, "disk": 5, "networks": 1}
    first, second = uuid.uuid4(), uuid.uuid4()
    async with app.router.lifespan_context(app):
        async with app.state.sessions() as session, session.begin():
            assert await acquire(session, first, resource, demand)
        async with app.state.sessions() as session, session.begin():
            assert not await acquire(session, second, resource, demand)
            await release(session, first)
        async with app.state.sessions() as session, session.begin():
            assert await acquire(session, second, resource, demand)
            await release(session, second)


async def test_worker_does_not_repeat_completed_job_or_connect_after_pending_cancel(monkeypatch):
    from autoeval.planning import PlanningJob
    from autoeval.planning_worker import execute_job

    app = create_app(get_settings())
    async with app.router.lifespan_context(app):
        completed, cancelled = uuid.uuid4(), uuid.uuid4()
        async with app.state.sessions() as session, session.begin():
            session.add_all(
                [
                    PlanningJob(
                        id=completed,
                        kind="probe",
                        state="completed",
                        payload={},
                        result={"TEST": "artificial"},
                    ),
                    PlanningJob(
                        id=cancelled,
                        kind="probe",
                        state="pending",
                        payload={},
                        cancel_requested=True,
                    ),
                ]
            )

        async def forbidden(*args):
            pytest.fail("No transport or model contact is allowed")

        monkeypatch.setattr("autoeval.planning_worker.transport", forbidden)
        await execute_job(app.state.sessions, app.state.settings, completed)
        await execute_job(app.state.sessions, app.state.settings, cancelled)
        async with app.state.sessions() as session:
            assert (await session.get(PlanningJob, completed)).state == "completed"
            assert (await session.get(PlanningJob, cancelled)).state == "cancelled"


async def test_running_probe_cancel_is_confirmed_without_resubmission(monkeypatch):
    from autoeval.planning_worker import execute_job

    app = create_app(get_settings())
    async with app.router.lifespan_context(app):
        identifier = uuid.uuid4()
        resource_id = uuid.uuid4()
        async with app.state.sessions() as session, session.begin():
            target = await session.scalar(select(TargetBinding).limit(1))
            assert target is not None
            session.add(
                PlanningArtifact(
                    kind="resource",
                    sha256="0" * 64,
                    object_key="TEST/resource-preflight",
                    payload={
                        "resource_id": str(resource_id),
                        "resource_kind": "docker_local",
                        "planner_ready": True,
                        "max_probe_concurrency": 8,
                    },
                )
            )
            session.add(
                PlanningJob(
                    id=identifier,
                    kind="probe",
                    state="running",
                    payload={
                        "target_id": str(target.id),
                        "resource_id": str(resource_id),
                        "resource_kind": "docker_local",
                        "concurrency": [1],
                        "requests_per_level": 2,
                        "prompt_tokens": 8,
                        "max_tokens": 16,
                    },
                    cancel_requested=True,
                )
            )

        class CancelAdapter:
            def __init__(self):
                self.cleaned = []

            async def submit(self, *args):
                pytest.fail("A running probe must not be submitted again")

            async def status(self, job):
                pytest.fail("Cancellation should be requested instead of another status poll")

            async def cancel(self, job, key):
                return JobObservation(job=job, state="cancelled")

            async def cleanup(self, job):
                self.cleaned.append(job.submission_key)
                return {"cleaned": True}

        adapter = CancelAdapter()

        async def transport(*args):
            return adapter

        monkeypatch.setattr("autoeval.planning_worker.transport", transport)
        monkeypatch.setattr("autoeval.planning_worker.validate_url", lambda *args: None)
        monkeypatch.setattr(
            "autoeval.planning_worker.FileSecretStore.get", lambda *args: "TEST-no-secret"
        )
        await execute_job(app.state.sessions, app.state.settings, identifier)
        async with app.state.sessions() as session:
            row = await session.get(PlanningJob, identifier)
            assert row.state == "cancelled"
            assert row.result["reason"] == "probe_cancelled"
        assert adapter.cleaned == [str(identifier)]


async def test_planned_run_is_visible_in_task_management_detail():
    settings = get_settings()
    app = create_app(settings)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
    ):
        run_id = uuid.uuid4()
        frozen = {
            "schema_version": "planned-run.v1",
            "inputs": {
                "target_id": str(uuid.uuid4()),
                "resource_id": str(uuid.uuid4()),
                "resource_kind": "customer_ssh",
                "dataset_concurrency": 1,
                "tasks": [
                    {
                        "task_id": "GPQA",
                        "dataset": "GPQA",
                        "samples": [{"sample_id": "TEST-1"}, {"sample_id": "TEST-2"}],
                        "compilation": {"config": {"subcommand": "llm_accuracy", "params": {}}},
                        "tool": {},
                    }
                ],
            },
            "estimate": None,
            "execution_mode": "direct",
            "execution_origin": settings.evaluation_origin,
            "resource": {"cpu_cores": 2, "mem_mb": 1024, "disk_gb": 10},
            "resource_id": str(uuid.uuid4()),
            "resource_kind": "customer_ssh",
            "remote_model": "TEST-no-model",
            "config_version_id": str(uuid.uuid4()),
            "manifest_id": str(uuid.uuid4()),
            "parameters": {"scope": "lite", "output": 2048},
        }
        async with app.state.sessions() as session, session.begin():
            session.add(
                EvaluationRun(
                    id=run_id,
                    trigger_scope="local-operator:orchestration",
                    idempotency_key=str(run_id),
                    request_hash=digest(frozen),
                    run_mode="exploration",
                    created_by="TEST",
                )
            )
            await session.flush()
            session.add(
                RunSnapshot(
                    run_id=run_id,
                    schema_version="planned-run.v1",
                    canonical_body=frozen,
                    sha256=digest(frozen),
                )
            )

        listing = await client.get("/api/autoeval/v1/runs?limit=200")
        assert listing.status_code == 200, listing.text
        assert str(run_id) in {row["id"] for row in listing.json()["data"]}
        detail = await client.get(f"/api/autoeval/v1/runs/{run_id}")
        assert detail.status_code == 200, detail.text
        data = detail.json()["data"]
        assert data["snapshot"]["sample_count"] == 2
        assert data["snapshot"]["resource_kind"] == "customer_ssh"
        # Planned runs release capacity inline; no separate cleanup job is scheduled.
        assert data["cleanup"]["state"] == "not_required"


async def test_preflight_job_does_not_create_evaluation_run(monkeypatch):
    from autoeval.planning_worker import execute_job

    app = create_app(get_settings())
    async with app.router.lifespan_context(app):
        identifier = uuid.uuid4()
        payload = {
            "target_id": str(uuid.uuid4()),
            "resource_id": str(uuid.uuid4()),
            "resource_kind": "customer_ssh",
            "tasks": [
                {
                    "task_id": "GPQA",
                    "dataset": "GPQA",
                    "compilation": {
                        "config": {"tool": "ais_bench", "subcommand": "llm_accuracy", "params": {}}
                    },
                    "tool": {},
                }
            ],
        }
        async with app.state.sessions() as session, session.begin():
            session.add(PlanningJob(id=identifier, kind="preflight", state="pending", payload=payload))
        async with app.state.sessions() as session:
            before = len((await session.scalars(select(EvaluationRun))).all())

        class PreflightAdapter:
            async def preflight(self, checks):
                return {
                    "execution_ready": True,
                    "blocking_reasons": [],
                    "supported_datasets": ["GPQA"],
                    "dataset_checks": [],
                }

        async def transport(*args):
            return PreflightAdapter()

        monkeypatch.setattr("autoeval.planning_worker.transport", transport)
        await execute_job(app.state.sessions, app.state.settings, identifier)

        async with app.state.sessions() as session:
            assert (await session.get(PlanningJob, identifier)).state == "completed"
            after = len((await session.scalars(select(EvaluationRun))).all())
        assert after == before, "Preflight must never create an evaluation run"
