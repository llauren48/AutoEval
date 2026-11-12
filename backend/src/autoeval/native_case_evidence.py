"""Read-time enrichment from immutable native predictions; never rewrite case evidence."""

import hashlib
import json
from pathlib import PurePosixPath


def prediction_records(content):
    # AISBench may contain unescaped line breaks within prediction strings.
    # A bounded streaming decoder preserves those bytes and handles multiline records.
    if len(content.encode("utf-8")) > 12 * 1024 * 1024:
        raise ValueError("Prediction file exceeds limit")
    decoder = json.JSONDecoder(strict=False)
    offset = 0
    rows = []
    while offset < len(content):
        while offset < len(content) and content[offset].isspace():
            offset += 1
        if offset == len(content):
            break
        row, offset = decoder.raw_decode(content, offset)
        if not isinstance(row, dict):
            raise TypeError("Prediction record must be an object")
        rows.append(row)
        if len(rows) > 10000:
            raise ValueError("Too many prediction records")
    return rows


def native_content(bundle, *, run_id, model_abbr, dataset, sample_id, attempt):
    if bundle.get("run_id") != str(run_id):
        raise ValueError("Native archive belongs to another run")
    matches = []
    for entry in bundle.get("files", []):
        path = PurePosixPath(entry["path"])
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("Unsafe native evidence path")
        if not str(path).endswith(f"/predictions/{model_abbr}/{dataset}.jsonl"):
            continue
        content = entry["content"]
        if hashlib.sha256(content.encode("utf-8")).hexdigest() != entry["sha256"]:
            raise ValueError("Native prediction checksum mismatch")
        for row in prediction_records(content):
            if row.get("data_abbr") != dataset or str(row.get("id")) != sample_id:
                continue
            # No positional matching or assigning one response to a later retry.
            if row.get("attempt", 1) != attempt:
                continue
            messages = row.get("origin_prompt")
            if not isinstance(messages, list) or not isinstance(row.get("prediction"), str):
                continue
            if not all(
                isinstance(message, dict) and isinstance(message.get("prompt"), str)
                for message in messages
            ):
                raise ValueError("Invalid native prompt structure")
            matches.append(
                {
                    "prompt": "\n\n".join(message["prompt"] for message in messages),
                    "response": row["prediction"],
                    "messages": messages,
                    "native_evidence": {
                        "path": str(path),
                        "sha256": entry["sha256"],
                        "record_id": str(row["id"]),
                        "record_uuid": row.get("uuid"),
                        "source": "aisbench_native_prediction",
                    },
                }
            )
    if len(matches) > 1:
        raise ValueError("Ambiguous native case evidence")
    return matches[0] if matches else None


async def enrich_case(session, settings, row, original):
    import asyncio

    from sqlalchemy import select

    from autoeval.datasets import read_manifest
    from autoeval.models import RunSnapshot
    from autoeval.unified_artifacts import UnifiedArtifact

    if original.get("schema_version") != "unified-case.v1" or "prompt" in original:
        return original
    artifact = await session.scalar(
        select(UnifiedArtifact).where(
            UnifiedArtifact.run_id == row.run_id, UnifiedArtifact.collection_key == "cli-native-v1"
        )
    )
    if artifact is None:
        return original
    snapshot = await session.scalar(select(RunSnapshot).where(RunSnapshot.run_id == row.run_id))
    if not snapshot or snapshot.canonical_body.get("execution_path") != "aisbench_cli_monitor":
        return original
    bundle = await asyncio.to_thread(read_manifest, settings, artifact)
    content = native_content(
        bundle,
        run_id=row.run_id,
        model_abbr=snapshot.canonical_body["model_abbr"],
        dataset=row.dataset_key,
        sample_id=row.sample_id,
        attempt=row.attempt_no,
    )
    if content is None:
        return original
    return {
        **original,
        **content,
        "evidence_sources": {
            "case_sha256": row.sha256,
            "native_archive_sha256": artifact.sha256,
            "enrichment": "read_time_from_verified_native_archive",
        },
    }
