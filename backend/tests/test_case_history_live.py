import os
import uuid

import httpx
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from autoeval.api import create_app
from autoeval.case_history import AttemptEvidence, AttemptInput, begin_attempt, finish_attempt
from autoeval.datasets import storage
from autoeval.models import EvaluationRun

pytestmark = pytest.mark.skipif(not os.environ.get("AE_INFRA_TEST"), reason="Live SQL/S3 required")


async def test_fail_retry_success_preserves_both_evidence_and_rejects_overwrite():
    app = create_app()
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as api,
    ):
        async with app.state.sessions() as session, session.begin():
            run = EvaluationRun(
                trigger_scope="case-history-fixture",
                idempotency_key=uuid.uuid4().hex,
                request_hash="a" * 64,
                run_mode="exploration",
                created_by="integration-test",
            )
            session.add(run)
            await session.flush()
            run_id = run.id
        body = AttemptInput(
            run_id=run_id,
            dataset_key="fixture",
            sample_id="case-1",
            attempt_no=1,
            operation_key="attempt-1",
        )
        first = await begin_attempt(app.state.sessions, body)
        assert await begin_attempt(app.state.sessions, body) == first
        prefix = "/api/autoeval/v1"
        assert (await api.get(f"{prefix}/case-executions/{first}/evidence")).status_code == 409
        failure = AttemptEvidence(
            state="failed", prompt="Fixture prompt", error_code="UPSTREAM_TIMEOUT"
        )
        await finish_attempt(app.state.sessions, app.state.settings, first, failure)
        await finish_attempt(app.state.sessions, app.state.settings, first, failure)
        with pytest.raises(DBAPIError, match="immutable"):
            async with app.state.sessions() as session, session.begin():
                await session.execute(
                    text("UPDATE case_execution_record SET state = 'succeeded' WHERE id = :id"),
                    {"id": first},
                )
        with pytest.raises(DBAPIError, match="cannot be deleted"):
            async with app.state.sessions() as session, session.begin():
                await session.execute(
                    text("DELETE FROM case_execution_record WHERE id = :id"), {"id": first}
                )
        with pytest.raises(ValueError, match="immutable"):
            await finish_attempt(
                app.state.sessions,
                app.state.settings,
                first,
                AttemptEvidence(state="succeeded", prompt="Fixture prompt", response="A"),
            )
        second = await begin_attempt(
            app.state.sessions,
            body.model_copy(update={"attempt_no": 2, "operation_key": "attempt-2"}),
        )
        await finish_attempt(
            app.state.sessions,
            app.state.settings,
            second,
            AttemptEvidence(
                state="succeeded", prompt="Fixture prompt", response="", finish_reason="length"
            ),
        )
        page = (
            await api.get(f"{prefix}/runs/{run_id}/case-executions?sample_id=case-1")
        ).json()
        history = page["data"]
        assert page["total"] == 2
        assert [r["state"] for r in history] == ["failed", "succeeded"]
        assert [r["attempt_no"] for r in history] == [1, 2]
        failed_page = (
            await api.get(f"{prefix}/runs/{run_id}/case-executions?state=failed&sample_id=case-1")
        ).json()
        assert failed_page["total"] == 1
        succeeded_only = (
            await api.get(
                f"{prefix}/runs/{run_id}/case-executions?state=succeeded&sample_id=case-1"
            )
        ).json()["data"]
        assert [r["state"] for r in succeeded_only] == ["succeeded"]
        assert (
            await api.get(f"{prefix}/runs/{run_id}/case-executions?state=unknown")
        ).status_code == 422
        old = (await api.get(f"{prefix}/case-executions/{first}/evidence")).json()["data"]
        assert old["error_code"] == "UPSTREAM_TIMEOUT"
        new = (await api.get(f"{prefix}/case-executions/{second}/evidence")).json()["data"]
        assert new["response"] == "" and new["finish_reason"] == "length"
        # S3 corruption cannot masquerade as valid historical evidence.
        from autoeval.case_history import CaseExecutionRecord

        async with app.state.sessions() as session:
            row = await session.scalar(
                select(CaseExecutionRecord).where(CaseExecutionRecord.id == second)
            )
        client = storage(app.state.settings)
        try:
            original = client.get_object(Bucket=app.state.settings.s3_bucket, Key=row.object_key)[
                "Body"
            ]
            raw = original.read()
            original.close()
            client.put_object(
                Bucket=app.state.settings.s3_bucket, Key=row.object_key, Body=b"corrupt"
            )
            assert (await api.get(f"{prefix}/case-executions/{second}/evidence")).status_code == 503
            client.put_object(Bucket=app.state.settings.s3_bucket, Key=row.object_key, Body=raw)
        finally:
            client.close()
