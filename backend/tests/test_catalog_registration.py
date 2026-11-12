"""Real database checks for catalog identity registration without execution binding."""

import asyncio
import os
import uuid

import httpx
import pytest
from sqlalchemy import delete, select

from autoeval.api import create_app
from autoeval.config import Settings
from autoeval.data_workbench import LeaderboardRecord
from autoeval.registry import Asset, AssetVersion


@pytest.mark.asyncio
@pytest.mark.skipif(os.getenv("AE_INFRA_TEST") != "1", reason="Requires real PostgreSQL")
async def test_reviewed_catalog_registration_is_idempotent_and_identity_only():
    app = create_app(Settings(environment="test"))
    key = "TEST-" + uuid.uuid4().hex
    canonical = "Fixture/" + key
    async with app.router.lifespan_context(app):
        async with app.state.sessions() as session, session.begin():
            record = LeaderboardRecord(
                source_key=key,
                state="approved",
                revision=1,
                payload={
                    "source_type": "official",
                    "model_name": key,
                    "conditions": {"modelscope_model_id": canonical},
                },
            )
            session.add(record)
            await session.flush()
            record_id = record.id
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                path = "/api/autoeval/v1/models/from-catalog"
                missing = await client.post(path, json={"catalog_id": "Fixture/unknown"})
                assert missing.status_code == 422
                a, b = await asyncio.gather(
                    *[
                        client.post(path, json={"catalog_id": canonical.swapcase()})
                        for _ in range(2)
                    ]
                )
                assert a.status_code == b.status_code == 200, (a.text, b.text)
                assert a.json()["data"] == b.json()["data"]
                assert a.json()["data"]["model_id"] == canonical
                assert a.json()["data"]["connection_state"] == "not_configured"
                assert not a.json()["data"]["execution_enabled"]
                identifier = uuid.UUID(a.json()["data"]["id"])
                async with app.state.sessions() as session:
                    version = await session.scalar(
                        select(AssetVersion).where(AssetVersion.asset_id == identifier)
                    )
                    assert version.metadata_json["identity_only"]
                    assert str(record_id) in version.metadata_json["source_record_ids"]
        finally:
            async with app.state.sessions() as session, session.begin():
                asset = await session.scalar(select(Asset).where(Asset.asset_key == canonical))
                if asset:
                    await session.execute(
                        delete(AssetVersion).where(AssetVersion.asset_id == asset.id)
                    )
                    await session.delete(asset)
                await session.delete(await session.get(LeaderboardRecord, record_id))
