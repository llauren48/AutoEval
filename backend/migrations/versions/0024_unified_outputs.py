"""Case tool observations and immutable unified output collections."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0024_unified_outputs"
down_revision = "0023_standard_inputs"
branch_labels = depends_on = None


def upgrade():
    op.add_column("case_execution_record", sa.Column("tool_metadata", JSONB(), nullable=True))
    op.create_table(
        "unified_result_collection",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "run_id",
            sa.Uuid(),
            sa.ForeignKey("evaluation_run.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("collection_key", sa.String(128), nullable=False),
        sa.Column("object_key", sa.String(256), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("payload", JSONB(), nullable=False),
        sa.UniqueConstraint("run_id", "collection_key"),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'"),
    )
    op.execute(
        "CREATE TRIGGER unified_collection_immutable BEFORE UPDATE OR DELETE ON unified_result_collection FOR EACH ROW EXECUTE FUNCTION standard_input_immutable()"
    )


def downgrade():
    op.drop_table("unified_result_collection")
    op.drop_column("case_execution_record", "tool_metadata")
