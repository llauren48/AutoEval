import argparse
import asyncio

from temporalio.client import Client
from temporalio.worker import Worker

from autoeval.activities import analysis_not_configured, execution_not_configured
from autoeval.analysis_activity import execute_report_analysis
from autoeval.config import get_settings
from autoeval.evaluation_activity import execute_gpqa_exploration
from autoeval.fixture_pipeline import execute_unified_fixture
from autoeval.monitored_execution import execute_monitored_gpqa
from autoeval.orchestration import execute_planned_evaluation
from autoeval.run_activities import execute_docker_diagnostic
from autoeval.unified_activity import collect_unified_output
from autoeval.workflows import (
    AnalysisWorkflow,
    DockerDiagnosticWorkflow,
    GpqaExplorationWorkflow,
    MonitoredGpqaWorkflow,
    PlannedEvaluationWorkflow,
    ReportAnalysisWorkflow,
    RunWorkflow,
    UnifiedFixtureWorkflow,
)


async def serve(role: str):
    settings = get_settings()
    client = await Client.connect(settings.temporal_address, namespace=settings.temporal_namespace)
    options = {
        "monitored": {
            "task_queue": "demo.monitored.v1",
            "workflows": [MonitoredGpqaWorkflow],
            "activities": [execute_monitored_gpqa],
        },
        "fixture": {
            "task_queue": "demo.fixture.workflow.v1",
            "workflows": [UnifiedFixtureWorkflow],
            "activities": [execute_unified_fixture],
        },
        "workflow": {
            "task_queue": settings.gpqa_workflow_queue,
            "workflows": [
                PlannedEvaluationWorkflow,
                RunWorkflow,
                AnalysisWorkflow,
                MonitoredGpqaWorkflow,
                DockerDiagnosticWorkflow,
                GpqaExplorationWorkflow,
                ReportAnalysisWorkflow,
            ],
        },
        "analysis-workflow": {
            "task_queue": settings.analysis_workflow_queue,
            "workflows": [ReportAnalysisWorkflow],
        },
        "io": {
            "task_queue": "demo.io.v1",
            "activities": [execution_not_configured, execute_docker_diagnostic],
        },
        "result": {
            "task_queue": settings.analysis_activity_queue,
            "activities": [
                analysis_not_configured,
                execute_report_analysis,
                collect_unified_output,
            ],
        },
        "planning-execution": {
            "task_queue": "demo.planning-execution.v1",
            "activities": [execute_planned_evaluation],
        },
        "gpqa": {
            "task_queue": settings.gpqa_activity_queue,
            "activities": [execute_gpqa_exploration],
        },
    }
    async with Worker(client, **options[role]):
        await asyncio.Event().wait()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "role",
        choices=[
            "workflow",
            "analysis-workflow",
            "io",
            "result",
            "gpqa",
            "fixture",
            "monitored",
            "planning-execution",
        ],
    )
    asyncio.run(serve(parser.parse_args().role))


if __name__ == "__main__":
    main()
