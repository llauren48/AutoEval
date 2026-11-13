"""Report-object health is independent of immutable historical execution validity."""

import asyncio

from botocore.exceptions import BotoCoreError, ClientError

from autoeval.datasets import read_manifest


async def report_evidence_status(settings, report, run, snapshot):
    try:
        body = await asyncio.to_thread(read_manifest, settings, report)
        if (
            not isinstance(body, dict)
            or body.get("schema_version") not in {"gpqa-report.v1", "planned-report.v1"}
            or body.get("run_id") != str(run.id)
            or body.get("snapshot_sha256") != snapshot.sha256
            or report.snapshot_sha256 != snapshot.sha256
            or body.get("execution_origin") != report.execution_origin
            or body.get("summary") != report.summary
            or body.get("validity") != report.validity
        ):
            return "invalid"
        return "verified"
    except ClientError as exc:
        return (
            "missing"
            if exc.response["Error"]["Code"] in {"NoSuchKey", "NoSuchBucket", "404"}
            else "unavailable"
        )
    except BotoCoreError:
        return "unavailable"
    except (ValueError, TypeError, OSError):
        return "invalid"
