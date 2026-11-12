from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn
class RunWorkflow:
    @workflow.run
    async def run(self, run_id: str) -> None:
        await workflow.execute_activity(
            "execution_not_configured",
            run_id,
            task_queue="demo.io.v1",
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )


@workflow.defn
class UnifiedFixtureWorkflow:
    @workflow.run
    async def run(self, run_id: str) -> dict:
        return await workflow.execute_activity(
            "execute_unified_fixture",
            run_id,
            task_queue="demo.fixture.workflow.v1",
            start_to_close_timeout=timedelta(minutes=3),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )


@workflow.defn
class AnalysisWorkflow:
    @workflow.run
    async def run(self, job_id: str) -> None:
        await workflow.execute_activity(
            "analysis_not_configured",
            job_id,
            task_queue="demo.result.v1",
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )


@workflow.defn
class ReportAnalysisWorkflow:
    @workflow.run
    async def run(self, request: dict) -> dict:
        return await workflow.execute_activity(
            "execute_report_analysis",
            request["analysis_id"],
            task_queue=request["activity_queue"],
            start_to_close_timeout=timedelta(minutes=5),
            heartbeat_timeout=timedelta(seconds=120),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )


@workflow.defn
class DockerDiagnosticWorkflow:
    @workflow.run
    async def run(self, run_id: str) -> dict:
        return await workflow.execute_activity(
            "execute_docker_diagnostic",
            run_id,
            task_queue="demo.io.v1",
            start_to_close_timeout=timedelta(minutes=3),
            heartbeat_timeout=timedelta(seconds=60),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )


@workflow.defn
class GpqaExplorationWorkflow:
    @workflow.run
    async def run(self, request: str | dict) -> dict:
        run_id = request if isinstance(request, str) else request["run_id"]
        queue = "demo.gpqa.v1" if isinstance(request, str) else request["activity_queue"]
        return await workflow.execute_activity(
            "execute_gpqa_exploration",
            args=[run_id, isinstance(request, dict) and request.get("resume", False)],
            task_queue=queue,
            start_to_close_timeout=timedelta(minutes=10),
            heartbeat_timeout=timedelta(seconds=90),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )


@workflow.defn
class MonitoredGpqaWorkflow:
    @workflow.run
    async def run(self, run_id: str) -> dict:
        return await workflow.execute_activity(
            "execute_monitored_gpqa",
            run_id,
            task_queue="demo.monitored.v1",
            start_to_close_timeout=timedelta(minutes=12),
            heartbeat_timeout=timedelta(seconds=90),
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
