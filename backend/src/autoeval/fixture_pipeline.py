"""Development-only platform collection pipeline; cannot execute model requests."""

import asyncio
import hashlib
import json
import stat
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from temporalio import activity

from autoeval.cleanup import ExecutionCleanup
from autoeval.config import get_settings
from autoeval.configuration import TestConfigVersion
from autoeval.data_workbench import development
from autoeval.datasets import canonical_manifest, read_manifest, write_manifest
from autoeval.db import create_database
from autoeval.evaluations import EvaluationReport
from autoeval.models import EvaluationRun, Outbox, RunEvent, RunSnapshot
from autoeval.secrets import FileSecretStore
from autoeval.ssh_executor import SSHConnection, SSHExecutor
from autoeval.unified_artifacts import UnifiedArtifact
from autoeval.unified_outputs import archive_output


def environment(settings):
    if settings.environment not in {"development", "test"} or not settings.unified_fixture_config:
        raise ValueError("Fixture pipeline environment is not configured")
    body = json.loads(Path(settings.unified_fixture_config).read_text(encoding="utf-8"))
    if body.get("origin") != "simulation" or body.get("expected_cases") != {
        "validation-dataset": ["0", "1"]
    }:
        raise ValueError("Only the approved synthetic fixture is supported")
    return body


class Submission(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config_version_id: uuid.UUID


router = APIRouter(
    prefix="/api/autoeval/v1/diagnostics/unified-fixture", dependencies=[Depends(development)]
)


@router.post("", status_code=202)
async def submit(
    body: Submission, request: Request, idempotency_key: str = Header(min_length=1, max_length=128)
):
    try:
        env = environment(request.app.state.settings)
    except (ValueError, OSError) as exc:
        raise HTTPException(503, "Fixture environment not configured") from exc
    digest = hashlib.sha256(canonical_manifest(body.model_dump(mode="json"))).hexdigest()
    async with request.app.state.sessions() as session, session.begin():
        from sqlalchemy import text

        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": "fixture:" + idempotency_key},
        )
        old = await session.scalar(
            select(EvaluationRun).where(
                EvaluationRun.trigger_scope == "local-operator:unified-fixture",
                EvaluationRun.idempotency_key == idempotency_key,
            )
        )
        if old:
            if old.request_hash != digest:
                raise HTTPException(409, "Idempotency conflict")
            return {
                "data": {"run_id": str(old.id), "state": old.state},
                "request_id": request.state.request_id,
            }
        config = await session.get(TestConfigVersion, body.config_version_id)
        if not config or config.kind != "standard":
            raise HTTPException(422, "Select an existing standard configuration")
        run = EvaluationRun(
            trigger_scope="local-operator:unified-fixture",
            idempotency_key=idempotency_key,
            request_hash=digest,
            run_mode="exploration",
            created_by="local-operator",
        )
        session.add(run)
        await session.flush()
        frozen = {
            "fixture_environment": env,
            "execution_origin": "simulation",
            "remote_model": "TEST-unified-fixture",
            "config_version_id": str(config.id),
            "config_sha256": config.content_hash,
            "manifest_id": "fixture-only",
            "manifest_sha256": hashlib.sha256(
                canonical_manifest(env["expected_cases"])
            ).hexdigest(),
            "parameters": {
                "scope": "Lite",
                "output": 64,
                "concurrency": 1,
                "temperature": 0,
                "top_p": 1,
            },
            "tool": {"revision": env["aisbench_revision"]},
            "dataset_key": "GPQA",
        }
        session.add(
            RunSnapshot(
                run_id=run.id,
                schema_version="fixture.v1",
                canonical_body=frozen,
                sha256=hashlib.sha256(canonical_manifest(frozen)).hexdigest(),
            )
        )
        session.add(
            Outbox(
                run_id=run.id,
                kind="START_RUN",
                dedup_key="start:" + str(run.id),
                payload={
                    "workflow_kind": "unified_fixture_v1",
                    "workflow_queue": "demo.fixture.workflow.v1",
                },
            )
        )
        return {
            "data": {"run_id": str(run.id), "state": "accepted", "execution_origin": "simulation"},
            "request_id": request.state.request_id,
        }


def fetch_remote(settings, frozen, run_id):
    env = frozen["fixture_environment"]
    connection = SSHConnection(
        **env["ssh"],
        credential=FileSecretStore(settings.secret_store_path).get(env["credential_ref"]),
    )
    control = SSHExecutor(connection)
    staging = f"{control.root}/jobs/{uuid.UUID(run_id)}"
    with control.connect() as client:
        control.prepare(client)
        # Fixed source and unique UUID destination; no caller-supplied command.
        control.command(client, ["mkdir", "-m", "700", "-p", staging])
        with client.open_sftp() as sftp:
            if not stat.S_ISDIR(sftp.lstat(staging).st_mode):
                raise ValueError("Staging path is not an owned directory")
        control.command(
            client,
            [
                "docker",
                "cp",
                env["container"] + ":" + env["source_file"],
                staging + "/unified_result.json",
            ],
        )
        with client.open_sftp() as sftp:
            path = staging + "/unified_result.json"
            if not stat.S_ISREG(sftp.lstat(path).st_mode):
                raise ValueError("Collected result is not a regular file")
            with sftp.open(path, "rb") as stream:
                raw = stream.read(16 * 1024 * 1024 + 1)
            if (
                len(raw) > 16 * 1024 * 1024
                or hashlib.sha256(raw).hexdigest() != env["source_sha256"]
            ):
                raise ValueError("Fixture source checksum changed")
            sftp.remove(path)
            sftp.rmdir(staging)
        return raw


async def phase(sessions, identifier, state, event):
    async with sessions() as session, session.begin():
        run = await session.scalar(
            select(EvaluationRun).where(EvaluationRun.id == identifier).with_for_update()
        )
        if run.cancel_requested_at:
            run.state = "cancelled"
            return False
        run.state = state
        run.projection_version += 1
        session.add(
            RunEvent(
                run_id=identifier,
                seq=run.projection_version,
                type=event,
                body={"execution_origin": "simulation"},
            )
        )
        return True


@activity.defn
async def execute_unified_fixture(run_id: str):
    settings = get_settings()
    engine, sessions = create_database(settings)
    identifier = uuid.UUID(run_id)
    try:
        async with sessions() as session:
            snapshot = await session.scalar(
                select(RunSnapshot).where(RunSnapshot.run_id == identifier)
            )
            if (
                not snapshot
                or hashlib.sha256(canonical_manifest(snapshot.canonical_body)).hexdigest()
                != snapshot.sha256
            ):
                raise ValueError("Frozen fixture input is invalid")
            frozen = snapshot.canonical_body
            if frozen.get("execution_origin") != "simulation":
                raise ValueError("Fixture workflow cannot execute real evaluations")
        if not await phase(sessions, identifier, "preparing", "fixture_preparing"):
            return {"state": "cancelled"}
        if not await phase(sessions, identifier, "running", "fixture_collecting"):
            return {"state": "cancelled"}
        raw = await asyncio.to_thread(fetch_remote, settings, frozen, run_id)
        if not await phase(sessions, identifier, "aggregating", "fixture_remote_collected"):
            return {"state": "cancelled"}
        key = f"unified-staging/{run_id}/{hashlib.sha256(raw).hexdigest()}.json"
        await asyncio.to_thread(write_manifest, settings, key, raw)
        artifact_sha = hashlib.sha256(raw).hexdigest()
        async with sessions() as session, session.begin():
            await session.scalar(
                select(EvaluationRun).where(EvaluationRun.id == identifier).with_for_update()
            )
            old = await session.scalar(
                select(UnifiedArtifact).where(UnifiedArtifact.run_id == identifier)
            )
            if not old:
                session.add(
                    UnifiedArtifact(
                        run_id=identifier,
                        collection_key="fixture-v1",
                        object_key=key,
                        sha256=artifact_sha,
                        manifest={
                            "files": [{"key": key, "sha256": artifact_sha, "size": len(raw)}],
                            "remote_staging_cleaned": True,
                        },
                    )
                )
            elif old.sha256 != artifact_sha:
                raise ValueError("Collected artifact conflicts")
            cleaned = await session.scalar(
                select(ExecutionCleanup).where(ExecutionCleanup.run_id == identifier)
            )
            if cleaned is None:
                session.add(
                    ExecutionCleanup(
                        run_id=identifier,
                        state="cleaned",
                        attempt_count=1,
                        verified_proofs={"remote_staging_removed": True, "source_preserved": True},
                    )
                )
        result = await archive_output(
            sessions,
            settings,
            identifier,
            "fixture-v1",
            raw,
            frozen["fixture_environment"]["expected_cases"],
        )
        summary = {
            "expected_samples": result["expected_cases"],
            "executed_attempts": result["attempt_count"],
            "successful_samples": result["observed_cases"] - result["failed_cases"],
            "failed_samples": result["failed_cases"],
            "missing_samples": result["missing_cases"],
            "accuracy": None,
            "subset_scope": "lite",
            "dataset_samples": 2,
            "metric_scope": "fixture_only",
            "scoring_status": "not_applicable",
            "fixture_only": True,
        }
        report_body = {
            "schema_version": "gpqa-report.v1",
            "run_id": run_id,
            "snapshot_sha256": snapshot.sha256,
            "execution_origin": "simulation",
            "summary": summary,
            "validity": result["validity"],
        }
        report_raw = canonical_manifest(report_body)
        report_sha = hashlib.sha256(report_raw).hexdigest()
        report_key = f"reports/{run_id}/{report_sha}.json"
        await asyncio.to_thread(write_manifest, settings, report_key, report_raw)
        async with sessions() as session, session.begin():
            run = await session.scalar(
                select(EvaluationRun).where(EvaluationRun.id == identifier).with_for_update()
            )
            if run.cancel_requested_at:
                run.state = "cancelled"
                return {"state": "cancelled"}
            old = await session.scalar(
                select(EvaluationReport).where(EvaluationReport.run_id == identifier)
            )
            if not old:
                old = EvaluationReport(
                    run_id=identifier,
                    validity=result["validity"],
                    snapshot_sha256=snapshot.sha256,
                    summary=summary,
                    object_key=report_key,
                    sha256=report_sha,
                    execution_origin="simulation",
                )
                session.add(old)
                await session.flush()
            elif old.sha256 != report_sha:
                raise ValueError("Report conflicts")
            await asyncio.to_thread(read_manifest, settings, old)
            run.state = "completed"
            run.projection_version += 1
            session.add(
                RunEvent(
                    run_id=identifier,
                    seq=run.projection_version,
                    type="fixture_completed",
                    body={
                        "report_id": str(old.id),
                        "remote_staging_cleaned": True,
                        "execution_origin": "simulation",
                    },
                )
            )
            return {
                "report_id": str(old.id),
                "report_sha256": report_sha,
                "artifact_sha256": artifact_sha,
                "execution_origin": "simulation",
            }
    except Exception:
        await phase(sessions, identifier, "failed", "fixture_stage_failed")
        raise
    finally:
        await engine.dispose()
