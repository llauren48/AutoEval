import asyncio
import hashlib
import math
import subprocess
import uuid
from types import SimpleNamespace

import paramiko
from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import delete, select
from temporalio import activity

from autoeval.case_history import (
    AttemptEvidence,
    AttemptInput,
    CaseExecutionRecord,
    begin_attempt,
    finish_attempt,
)
from autoeval.config import get_settings
from autoeval.datasets import canonical_manifest, read_manifest, write_manifest
from autoeval.db import create_database
from autoeval.domain import RunState, check_transition
from autoeval.evaluations import EvaluationReport
from autoeval.execution_transport import ssh_transport
from autoeval.models import EvaluationRun, RunEvent, RunSnapshot
from autoeval.native_smoke import invoke
from autoeval.resources import inspect_executor
from autoeval.run_activities import record_unresolved, transition
from autoeval.runs import ExecutionLease
from autoeval.secrets import FileSecretStore
from autoeval.targets import TargetBinding


async def _verified_report(settings, report, identifier, snapshot):
    body = await asyncio.to_thread(read_manifest, settings, report)
    if (
        body.get("schema_version") != "gpqa-report.v1"
        or body.get("run_id") != str(identifier)
        or body.get("snapshot_sha256") != snapshot.sha256
        or body.get("execution_origin") != report.execution_origin
        or body.get("validity") != report.validity
        or body.get("summary") != report.summary
    ):
        raise ValueError("Report evidence is inconsistent")
    return body


def model_payload(frozen, sample, credential=""):
    return {
        "operation": "infer_gpqa_smoke",
        "sample": sample,
        "base_url": frozen["base_url"].rstrip("/"),
        "model": frozen["remote_model"],
        "max_tokens": frozen["parameters"]["output"],
        "api_key": credential,
    }


async def heartbeat_request(transport, payload, attempt_id, run_id):
    task = asyncio.create_task(transport.request(payload, str(attempt_id)))
    try:
        while not task.done():
            await asyncio.wait({task}, timeout=10)
            activity.heartbeat(run_id)
        return await task
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


@activity.defn
async def execute_gpqa_exploration(run_id: str, resume: bool = False):
    identifier = uuid.UUID(run_id)
    settings = get_settings()
    engine, sessions = create_database(settings)
    try:
        async with sessions() as session, session.begin():
            snapshot = await session.scalar(
                select(RunSnapshot).where(RunSnapshot.run_id == identifier)
            )
            frozen = snapshot.canonical_body
            existing_records = (
                await session.scalars(
                    select(CaseExecutionRecord)
                    .where(CaseExecutionRecord.run_id == identifier)
                    .order_by(CaseExecutionRecord.created_at, CaseExecutionRecord.id)
                )
            ).all()
            row = await session.scalar(
                select(EvaluationRun).where(EvaluationRun.id == identifier).with_for_update()
            )
            existing_report = await session.scalar(
                select(EvaluationReport).where(EvaluationReport.run_id == identifier)
            )
            if row.state in {"completed", "failed", "cancelled"}:
                if existing_report is not None:
                    await _verified_report(settings, existing_report, identifier, snapshot)
                return {"state": row.state}
            if existing_report is not None:
                await _verified_report(settings, existing_report, identifier, snapshot)
                return {"state": "unresolved", "ownership_retained": True}
            if frozen.get("execution_origin", "unverified") != settings.evaluation_origin:
                raise ValueError("Worker execution origin differs from frozen request")
            # Redelivery must never replay a potentially billable request. Only
            # evidence-based recovery may continue after an attempt was registered.
            if not resume and (existing_records or row.state != "accepted"):
                return {"state": "unresolved"}
            if (
                any(record.state == "started" for record in existing_records)
                and frozen.get("resource_kind") != "customer_ssh"
            ):
                return {"state": "unresolved", "ownership_retained": True}
            initial_state = row.state
            cancelled = row.cancel_requested_at is not None or initial_state == "cancelling"
            target = await session.get(TargetBinding, uuid.UUID(frozen["target_id"]))
            credential_ref = target.credential_ref
            if initial_state == "accepted":
                row.state = "queued"
                row.projection_version += 1
                session.add(
                    RunEvent(run_id=identifier, seq=row.projection_version, type="queued", body={})
                )
        if initial_state in {"accepted", "queued"}:
            await transition(sessions, identifier, "preparing")
        transport = (
            await ssh_transport(sessions, settings, frozen)
            if frozen.get("resource_kind") == "customer_ssh"
            else None
        )
        if transport is None:
            identity = await asyncio.to_thread(
                inspect_executor, settings.docker_executable, frozen["container_id"]
            )
            if not identity["running"] or identity["image_id"] != frozen["image_id"]:
                raise ValueError("Executor identity changed")

        async def native_call(payload):
            if transport is not None:
                return await asyncio.to_thread(transport.invoke_local, payload)
            return await asyncio.to_thread(invoke, settings, frozen["container_id"], payload)

        bundle = await asyncio.to_thread(
            read_manifest, settings, SimpleNamespace(**frozen["dataset_content"])
        )
        members = {s["sample_id"]: s for s in bundle["samples"]}
        attempts, predictions, references = [], [], []
        attempted_samples = set()
        expected_samples = {sample["sample_id"]: sample for sample in frozen["samples"]}
        for record in existing_records:
            expected = expected_samples.get(record.sample_id)
            if (
                record.dataset_key != "GPQA"
                or expected is None
                or record.sample_id in attempted_samples
            ):
                raise ValueError("Unexpected or ambiguous archived attempt")
            sample = members[record.sample_id]
            if (
                sample["sha256"] != expected["sha256"]
                or hashlib.sha256(canonical_manifest(sample["native_sample"])).hexdigest()
                != expected["sha256"]
            ):
                raise ValueError("Native sample identity changed")
            if record.state == "started":
                recovered = await transport.collect_request(
                    str(record.id), model_payload(frozen, sample["native_sample"])
                )
                if recovered["state"] != "succeeded":
                    raise RuntimeError("SSH native outcome remains unconfirmed")
                result = recovered["result"]
                prompt = await native_call(
                    {"operation": "prompt_gpqa_0shot", "sample": sample["native_sample"]}
                )
                if (
                    result.get("tool_revision") != frozen["tool"]["revision"]
                    or result.get("prompt") != prompt["prompt"]
                ):
                    raise ValueError("Recovered native evidence identity differs from snapshot")
                await finish_attempt(
                    sessions,
                    settings,
                    record.id,
                    AttemptEvidence(
                        state=result["state"],
                        prompt=result["prompt"],
                        response=result["response"],
                        finish_reason=result["finish_reason"],
                        error_code=result["error_code"],
                        usage=result.get("usage"),
                    ),
                )
                async with sessions() as session:
                    record = await session.get(CaseExecutionRecord, record.id)
            evidence = await asyncio.to_thread(read_manifest, settings, record)
            if (
                evidence.get("schema_version") != "case-evidence.v1"
                or evidence.get("attempt_id") != str(record.id)
                or evidence.get("state") != record.state
                or (record.state == "succeeded" and evidence.get("response") is None)
            ):
                raise ValueError("Archived attempt evidence is inconsistent")
            attempts.append(
                {
                    "sample_id": record.sample_id,
                    "attempt_id": str(record.id),
                    "state": record.state,
                    "usage": evidence.get("usage"),
                }
            )
            attempted_samples.add(record.sample_id)
            if record.state == "succeeded":
                predictions.append(evidence["response"])
                references.append(sample["native_sample"]["answer"])
        credential = FileSecretStore(settings.secret_store_path).get(credential_ref)
        if initial_state in {"accepted", "queued", "preparing", "running"}:
            await transition(sessions, identifier, "running")
        for expected in frozen["samples"]:
            if expected["sample_id"] in attempted_samples:
                continue
            activity.heartbeat(run_id)
            async with sessions() as session:
                cancelled = (
                    cancelled
                    or (await session.get(EvaluationRun, identifier)).cancel_requested_at
                    is not None
                )
            if cancelled:
                break
            sample = members[expected["sample_id"]]
            if (
                sample["sha256"] != expected["sha256"]
                or hashlib.sha256(canonical_manifest(sample["native_sample"])).hexdigest()
                != expected["sha256"]
            ):
                raise ValueError("Native sample identity changed")
            prompt = await native_call(
                {"operation": "prompt_gpqa_0shot", "sample": sample["native_sample"]}
            )
            if prompt["tool_revision"] != frozen["tool"]["revision"]:
                raise ValueError("Native tool revision changed")
            attempt_id = await begin_attempt(
                sessions,
                AttemptInput(
                    run_id=identifier,
                    dataset_key="GPQA",
                    sample_id=expected["sample_id"],
                    attempt_no=1,
                    operation_key="sample:" + expected["sample_id"],
                ),
            )
            payload = model_payload(frozen, sample["native_sample"], credential)
            result = (
                await heartbeat_request(transport, payload, attempt_id, run_id)
                if transport is not None
                else await asyncio.to_thread(invoke, settings, frozen["container_id"], payload)
            )
            if transport is not None and (
                result.get("tool_revision") != frozen["tool"]["revision"]
                or result.get("prompt") != prompt["prompt"]
            ):
                raise ValueError("Native response differs from approved prompt/tool")
            await finish_attempt(
                sessions,
                settings,
                attempt_id,
                AttemptEvidence(
                    state=result["state"],
                    prompt=prompt["prompt"],
                    response=result["response"],
                    finish_reason=result["finish_reason"],
                    error_code=result["error_code"],
                    usage=result.get("usage"),
                ),
            )
            attempts.append(
                {
                    "sample_id": expected["sample_id"],
                    "attempt_id": str(attempt_id),
                    "state": result["state"],
                    "usage": result["usage"],
                }
            )
            if result["state"] == "succeeded":
                predictions.append(result["response"])
                references.append(sample["native_sample"]["answer"])
        async with sessions() as session:
            cancelled = (
                cancelled
                or (await session.get(EvaluationRun, identifier)).cancel_requested_at is not None
            )
        await transition(sessions, identifier, "cancelling" if cancelled else "aggregating")
        scoring = None
        scoring_status = "not_applicable"
        if predictions:
            try:
                scoring = await native_call(
                    {
                        "operation": "score_gpqa_0shot",
                        "predictions": predictions,
                        "references": references,
                    },
                )
                accuracy = scoring["native_result"]["accuracy"]
                if (
                    type(accuracy) not in (int, float)
                    or not math.isfinite(accuracy)
                    or not 0 <= accuracy <= 100
                ):
                    raise ValueError("Invalid native metric")
                scoring_status = "succeeded"
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                EOFError,
                paramiko.SSHException,
                subprocess.SubprocessError,
            ):
                scoring, scoring_status = None, "failed"
        validity = (
            "complete"
            if len(predictions) == len(frozen["samples"]) and not cancelled
            else "partial"
            if predictions
            else "failed_summary"
        )
        if scoring_status == "failed":
            validity = "invalid"
        summary = {
            "expected_samples": len(frozen["samples"]),
            "executed_attempts": len(attempts),
            "successful_samples": len(predictions),
            "failed_samples": sum(a["state"] == "failed" for a in attempts),
            "missing_samples": len(frozen["samples"]) - len(attempts),
            "cancelled": cancelled,
            "gate_status": "not_applicable",
            "metric_scope": "selected_manifest"
            if validity == "complete"
            else "successful_samples_only",
            "subset_scope": "lite",
            "dataset_samples": frozen["dataset_content"]["sample_count"],
            "accuracy": scoring["native_result"]["accuracy"] if scoring else None,
            "scoring_status": scoring_status,
            "formal_score_eligible": False,
        }
        raw = canonical_manifest(
            {
                "schema_version": "gpqa-report.v1",
                "execution_origin": frozen.get("execution_origin", "unverified"),
                "run_id": run_id,
                "snapshot_sha256": snapshot.sha256,
                "validity": validity,
                "summary": summary,
                "attempts": attempts,
                "native_scoring": scoring,
            }
        )
        digest = hashlib.sha256(raw).hexdigest()
        key = f"reports/{run_id}/{digest}.json"
        await asyncio.to_thread(write_manifest, settings, key, raw)
        async with sessions() as session, session.begin():
            row = await session.scalar(
                select(EvaluationRun).where(EvaluationRun.id == identifier).with_for_update()
            )
            final_state = "cancelled" if cancelled else "completed" if predictions else "failed"
            check_transition(RunState(row.state), RunState(final_state))
            row.state = final_state
            row.projection_version += 1
            session.add(
                RunEvent(
                    run_id=identifier,
                    seq=row.projection_version,
                    type=final_state,
                    body={"validity": validity},
                )
            )
            existing_report = await session.scalar(
                select(EvaluationReport)
                .where(EvaluationReport.run_id == identifier)
                .with_for_update()
            )
            if existing_report is not None:
                report = existing_report
                body = await _verified_report(settings, existing_report, identifier, snapshot)
                if (
                    existing_report.object_key != key
                    or existing_report.sha256 != digest
                    or canonical_manifest(body) != raw
                ):
                    raise ValueError("Existing report conflicts with recovered execution")
            else:
                report = EvaluationReport(
                    execution_origin=frozen.get("execution_origin", "unverified"),
                    run_id=identifier,
                    validity=validity,
                    snapshot_sha256=snapshot.sha256,
                    summary=summary,
                    object_key=key,
                    sha256=digest,
                )
                session.add(report)
                await session.flush()
            # Analysis is explicitly started by the operator from the report page.
            if frozen.get("resource_kind") == "customer_ssh":
                from autoeval.cleanup import schedule_cleanup

                await schedule_cleanup(session, identifier)
            else:
                await session.execute(
                    delete(ExecutionLease).where(ExecutionLease.run_id == identifier)
                )
        return {"state": final_state, "validity": validity}
    except (
        OSError,
        ValueError,
        RuntimeError,
        KeyError,
        BotoCoreError,
        ClientError,
        subprocess.SubprocessError,
        paramiko.SSHException,
        EOFError,
    ):
        await record_unresolved(sessions, identifier)
        return {"state": "unresolved", "ownership_retained": True}
    finally:
        await engine.dispose()
