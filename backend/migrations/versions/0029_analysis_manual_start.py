"""Allow an explicit first manual start without consuming the failure retry."""

from alembic import op

revision = "0029_analysis_manual_start"
down_revision = "0028_config_no_approval"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_report_analysis_trigger", "report_analysis", type_="check")
    op.create_check_constraint(
        "ck_report_analysis_trigger",
        "report_analysis",
        "trigger IN ('report_created','manual_start','manual_retry')",
    )


def downgrade():
    raise RuntimeError("Manual review required: manual_start analysis rows may exist")
