"""Initial run outbox dispatcher. Report/analysis intents are not enabled yet."""

import asyncio
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from autoeval.config import get_settings
from autoeval.db import create_database
from autoeval.models import Outbox
from autoeval.workflows import (
    DockerDiagnosticWorkflow,
    GpqaExplorationWorkflow,
    MonitoredGpqaWorkflow,
    PlannedEvaluationWorkflow,
    ReportAnalysisWorkflow,
    RunWorkflow,
    UnifiedFixtureWorkflow,
)

log = logging.getLogger(__name__)


async def deliver_once(sessions, client, *, run_id=None) -> bool:
    # Keep a bounded single-row transaction during dispatch: rollback releases
    # the row after a crash, while a stable Workflow ID handles re-delivery.
    async with sessions() as session, session.begin():
        row = await session.scalar(
            select(Outbox)
            .where(Outbox.state == "pending", Outbox.next_retry_at <= datetime.now(UTC))
            .where(Outbox.run_id == run_id if run_id is not None else True)
            .order_by(Outbox.created_at, Outbox.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if row is None:
            return False
        if row.kind not in {"START_RUN", "ANALYZE_REPORT"}:
            row.next_retry_at = datetime.now(UTC) + timedelta(minutes=1)
            log.error("Unsupported outbox intent id=%s kind=%s", row.id, row.kind)
            return True
        try:
            workflow_kind = row.payload.get("workflow_kind")
            workflow_entry = {
                "monitored_gpqa_v1": MonitoredGpqaWorkflow.run,
                "planned_evaluation": PlannedEvaluationWorkflow.run,
                "docker_diagnostic": DockerDiagnosticWorkflow.run,
                "gpqa_exploration": GpqaExplorationWorkflow.run,
                "report_analysis": ReportAnalysisWorkflow.run,
                "unified_fixture_v1": UnifiedFixtureWorkflow.run,
            }.get(workflow_kind, RunWorkflow.run)
            if row.kind == "ANALYZE_REPORT":
                analysis_id = row.payload["analysis_id"]
                request = {
                    "analysis_id": analysis_id,
                    "activity_queue": row.payload.get("activity_queue", "demo.result.v1"),
                }
                workflow_id = f"demo/analysis/{analysis_id}/{row.payload.get('retry_count', 0)}"
                default_queue = "demo.workflow.v1"
            else:
                request = (
                    {
                        "run_id": str(row.run_id),
                        "activity_queue": row.payload.get("activity_queue", "demo.gpqa.v1"),
                    }
                    if workflow_kind == "gpqa_exploration"
                    else str(row.run_id)
                )
                workflow_id = f"demo/run/{row.run_id}"
                default_queue = "demo.workflow.v1"
            await asyncio.wait_for(
                client.start_workflow(
                    workflow_entry,
                    request,
                    id=workflow_id,
                    task_queue=row.payload.get("workflow_queue", default_queue),
                    id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                ),
                timeout=5,
            )
        except WorkflowAlreadyStartedError:
            pass
        except Exception:  # noqa: BLE001 -- delivery uncertainty is retried with the same Workflow ID
            row.attempt_count += 1
            row.next_retry_at = datetime.now(UTC) + timedelta(seconds=30)
            log.warning("Outbox delivery deferred id=%s", row.id)
            return True
        row.state = "delivered"
        row.attempt_count += 1
        return True


async def serve():
    settings = get_settings()
    engine, sessions = create_database(settings)
    try:
        client = await Client.connect(
            settings.temporal_address, namespace=settings.temporal_namespace
        )
        while True:
            if not await deliver_once(sessions, client):
                await asyncio.sleep(settings.poll_seconds)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve())
