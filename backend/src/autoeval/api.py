import asyncio
from contextlib import asynccontextmanager
from uuid import uuid4

import boto3
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.exceptions import HTTPException as StarletteHTTPException
from temporalio.client import Client

from autoeval.analysis import analysis_is_configured
from autoeval.analysis import router as analysis_router
from autoeval.audit import router as audit_router
from autoeval.case_history import router as case_history_router
from autoeval.config import Settings, get_settings
from autoeval.configuration import router as configuration_router
from autoeval.data_workbench import router as data_workbench_router
from autoeval.datasets import router as datasets_router
from autoeval.db import create_database
from autoeval.evaluations import router as evaluations_router
from autoeval.fixture_pipeline import router as fixture_pipeline_router
from autoeval.leaderboards import router as leaderboard_router
from autoeval.monitored_execution import router as monitored_router
from autoeval.planning import router as planning_router
from autoeval.profiles import router as profiles_router
from autoeval.registry import router as registry_router
from autoeval.reports import router as reports_router
from autoeval.resources import router as resources_router
from autoeval.runs import router as runs_router
from autoeval.ssh_resources import router as ssh_resources_router
from autoeval.standard_inputs import router as standard_inputs_router
from autoeval.targets import router as targets_router
from autoeval.task_monitor import router as task_monitor_router
from autoeval.unified_outputs import router as unified_outputs_router


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app):
        app.state.settings = settings
        app.state.engine, app.state.sessions = create_database(settings)
        yield
        await app.state.engine.dispose()

    app = FastAPI(title="AutoEval Backend", version="0.1.0", lifespan=lifespan)
    app.include_router(data_workbench_router)
    app.include_router(standard_inputs_router)
    app.include_router(unified_outputs_router)
    app.include_router(fixture_pipeline_router)
    app.include_router(task_monitor_router)
    app.include_router(monitored_router)
    app.include_router(planning_router)
    app.include_router(configuration_router)
    app.include_router(case_history_router)
    app.include_router(datasets_router)
    app.include_router(profiles_router)
    app.include_router(registry_router)
    app.include_router(targets_router)
    app.include_router(ssh_resources_router)
    app.include_router(resources_router)
    app.include_router(runs_router)
    app.include_router(evaluations_router)
    app.include_router(leaderboard_router)
    app.include_router(analysis_router)
    app.include_router(audit_router)
    app.include_router(reports_router)

    @app.middleware("http")
    async def correlation(request: Request, call_next):
        request.state.request_id = str(uuid4())
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request, exc):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {"code": "HTTP_ERROR", "message": str(exc.detail)},
                "request_id": request.state.request_id,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(
            status_code=422,
            content={
                "error": {"code": "VALIDATION_ERROR", "message": "Invalid request"},
                "request_id": request.state.request_id,
            },
        )

    @app.exception_handler(Exception)
    async def unexpected_error(request, exc):
        return JSONResponse(
            status_code=500,
            content={
                "error": {"code": "INTERNAL_ERROR", "message": "Internal server error"},
                "request_id": getattr(request.state, "request_id", "unknown"),
            },
        )

    @app.get("/health/live")
    async def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready(request: Request):
        async def database():
            async with request.app.state.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))

        async def temporal():
            client = await Client.connect(
                settings.temporal_address, namespace=settings.temporal_namespace
            )
            await client.service_client.check_health()

        def storage():
            from botocore.config import Config

            client = boto3.client(
                "s3",
                endpoint_url=settings.s3_endpoint,
                aws_access_key_id=settings.s3_access_key.get_secret_value(),
                aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
                config=Config(connect_timeout=2, read_timeout=2, retries={"max_attempts": 0}),
            )
            try:
                client.head_bucket(Bucket=settings.s3_bucket)
            finally:
                client.close()

        async def check(coro):
            try:
                await asyncio.wait_for(coro, settings.dependency_timeout_seconds)
                return "ready"
            except Exception:  # noqa: BLE001 -- readiness isolates third-party dependency failures
                return "unavailable"

        results = await asyncio.gather(
            check(database()), check(temporal()), check(asyncio.to_thread(storage))
        )
        checks = dict(zip(("database", "temporal", "storage"), results))
        return JSONResponse(
            status_code=200 if all(x == "ready" for x in results) else 503,
            content={"checks": checks},
        )

    @app.get("/api/autoeval/v1/system/capabilities")
    async def capabilities(request: Request):
        return {
            "data": {
                "workspace_mode": "single",
                "subject": "local-operator",
                "foundation": True,
                "evaluation_enabled": False,
                "gpqa_exploration_enabled": True,
                "planning_enabled": True,
                "planning_datasets": ["GPQA", "MMMU-Pro", "DeepSWE"],
                "planned_execution_requires_resource_preflight": True,
                "analysis_enabled": analysis_is_configured(settings),
                "aurora_enabled": False,
                "customer_ssh_enabled": True,
                "customer_ssh_scope": "gpqa_lite_exploration",
            },
            "request_id": request.state.request_id,
        }

    return app


app = create_app()
