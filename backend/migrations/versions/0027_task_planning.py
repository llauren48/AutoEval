"""Persistent planning jobs, artifacts, dataset executions and capacity ownership."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0027_task_planning"
down_revision = "0026_run_monitor"
branch_labels = depends_on = None


def base_columns():
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    ]


def upgrade():
    op.create_table(
        "planning_artifact",
        *base_columns(),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("object_key", sa.String(256), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
    )
    op.create_table(
        "planning_job",
        *base_columns(),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("result", JSONB()),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "dataset_execution",
        *base_columns(),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("evaluation_run.id"), nullable=False),
        sa.Column("task_id", sa.String(64), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.Column("summary", JSONB()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("run_id", "task_id"),
    )
    op.create_table(
        "planning_capacity_lease",
        *base_columns(),
        sa.Column("owner_id", sa.Uuid(), unique=True, nullable=False),
        sa.Column("node_id", sa.String(128), nullable=False),
        sa.Column("demand", JSONB(), nullable=False),
        sa.Column("released", sa.Boolean(), nullable=False),
    )
    op.create_index("ix_planning_capacity_lease_node_id", "planning_capacity_lease", ["node_id"])


def downgrade():
    for name in (
        "planning_capacity_lease",
        "dataset_execution",
        "planning_job",
        "planning_artifact",
    ):
        op.drop_table(name)
