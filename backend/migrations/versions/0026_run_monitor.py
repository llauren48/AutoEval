"""Monitor identity and immutable evidence indexes."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0026_run_monitor"
down_revision = "0025_unified_artifacts"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "run_monitor_binding",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("evaluation_run.id"), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("identity", JSONB(), nullable=False),
        sa.Column("latest", JSONB()),
        sa.UniqueConstraint("run_id", "attempt"),
        sa.CheckConstraint("attempt > 0"),
    )
    op.create_table(
        "run_monitor_snapshot",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("binding_id", sa.Uuid(), sa.ForeignKey("run_monitor_binding.id"), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("object_key", sa.String(256), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.UniqueConstraint("binding_id", "sha256"),
    )
    op.execute(
        "CREATE TRIGGER run_monitor_snapshot_immutable BEFORE UPDATE OR DELETE ON run_monitor_snapshot FOR EACH ROW EXECUTE FUNCTION standard_input_immutable()"
    )


def downgrade():
    op.drop_table("run_monitor_snapshot")
    op.drop_table("run_monitor_binding")
