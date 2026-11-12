import asyncio

from alembic import context

import autoeval.analysis
import autoeval.audit
import autoeval.benchmarks
import autoeval.case_history
import autoeval.cleanup
import autoeval.configuration
import autoeval.data_workbench
import autoeval.datasets
import autoeval.evaluations
import autoeval.orchestration
import autoeval.planning
import autoeval.profiles
import autoeval.registry
import autoeval.resources
import autoeval.runs
import autoeval.ssh_resources
import autoeval.standard_inputs
import autoeval.targets
import autoeval.task_monitor
import autoeval.unified_artifacts
import autoeval.unified_outputs  # noqa: F401 -- register migration metadata
from autoeval.config import get_settings
from autoeval.db import create_database
from autoeval.models import Base

config = context.config
target_metadata = Base.metadata


def migrate(connection):
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def online():
    engine, _ = create_database(get_settings())
    async with engine.connect() as conn:
        await conn.run_sync(migrate)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(
        url=get_settings().database_url.get_secret_value(),
        target_metadata=target_metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())
