"""Collect already-uploaded output using only a frozen collection contract."""

import asyncio
import hashlib
import uuid

from sqlalchemy import select
from temporalio import activity

from autoeval.adaptor_versions import CURRENT_ADAPTOR
from autoeval.config import get_settings
from autoeval.datasets import canonical_manifest
from autoeval.db import create_database
from autoeval.models import RunSnapshot
from autoeval.unified_artifacts import UnifiedArtifact
from autoeval.unified_outputs import archive_output, read_raw_output


@activity.defn
async def collect_unified_output(run_id: str):
    settings = get_settings()
    engine, sessions = create_database(settings)
    try:
        async with sessions() as session:
            snapshot = await session.scalar(
                select(RunSnapshot).where(RunSnapshot.run_id == uuid.UUID(run_id))
            )
            if (
                snapshot is None
                or hashlib.sha256(canonical_manifest(snapshot.canonical_body)).hexdigest()
                != snapshot.sha256
            ):
                raise ValueError("Frozen snapshot missing or corrupted")
            contract = snapshot.canonical_body.get("unified_collection")
            if not contract or contract.get("adaptor_revision") != CURRENT_ADAPTOR:
                raise ValueError("Pinned unified collection contract is not configured")
            artifact = await session.scalar(
                select(UnifiedArtifact).where(
                    UnifiedArtifact.run_id == uuid.UUID(run_id),
                    UnifiedArtifact.collection_key == contract["collection_key"],
                )
            )
            if artifact is None:
                raise ValueError("Collected artifact is not registered")
            if not artifact.object_key.startswith(f"unified-staging/{run_id}/"):
                raise ValueError("Artifact does not belong to run")
            reference = {"object_key": artifact.object_key, "sha256": artifact.sha256}
        raw = await asyncio.to_thread(read_raw_output, settings, reference)
        return await archive_output(
            sessions,
            settings,
            run_id,
            contract["collection_key"],
            raw,
            contract["expected_cases"],
            contract.get("default_dataset"),
        )
    finally:
        await engine.dispose()
