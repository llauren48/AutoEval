import asyncio

from alembic import context

import autoeval.analysis  # noqa: F401 -- register migration metadata
import autoeval.audit  # noqa: F401 -- register migration metadata
import autoeval.benchmarks  # noqa: F401 -- register migration metadata
import autoeval.case_history  # noqa: F401 -- register migration metadata
import autoeval.cleanup  # noqa: F401 -- register migration metadata
import autoeval.configuration  # noqa: F401 -- register migration metadata
import autoeval.data_workbench  # noqa: F401 -- register migration metadata
import autoeval.datasets  # noqa: F401 -- register migration metadata
import autoeval.evaluations  # noqa: F401 -- register migration metadata
import autoeval.profiles  # noqa: F401 -- register migration metadata
import autoeval.registry  # noqa: F401 -- register migration metadata
import autoeval.resources  # noqa: F401 -- register migration metadata
import autoeval.runs  # noqa: F401 -- register migration metadata
import autoeval.ssh_resources  # noqa: F401 -- register migration metadata
import autoeval.standard_inputs  # noqa: F401 -- register migration metadata
import autoeval.targets   # noqa: F401 -- register migration metadata
import autoeval.task_monitor  # noqa: F401 -- register migration metadata
import autoeval.unified_outputs  # noqa: F401 -- register migration metadata
import autoeval.unified_artifacts  # noqa: F401 -- register migration metadata
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
