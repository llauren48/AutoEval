"""Allow explicit reanalysis and preserve previous attempt metadata."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0030_analysis_reanalysis"
down_revision = "0029_analysis_manual_start"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "report_analysis",
        sa.Column("retry_history", postgresql.JSONB(), nullable=False, server_default="[]"),
    )
    op.drop_constraint("ck_report_analysis_retry_count", "report_analysis", type_="check")
    op.create_check_constraint(
        "ck_report_analysis_retry_count", "report_analysis", "retry_count >= 0"
    )


def downgrade():
    raise RuntimeError("Manual review required: reanalysis history must be preserved")
