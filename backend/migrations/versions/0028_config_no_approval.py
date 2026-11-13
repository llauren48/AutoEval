"""Configuration approval is no longer required; retain historical approver metadata."""

from alembic import op

revision = "0028_config_no_approval"
down_revision = "0027_task_planning"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_constraint("ck_standard_config_metadata", "test_config_version", type_="check")
    op.create_check_constraint(
        "ck_standard_config_metadata",
        "test_config_version",
        "(kind = 'standard' AND source_url IS NOT NULL AND source_revision IS NOT NULL "
        "AND source_sha256 IS NOT NULL) OR (kind = 'custom' AND source_url IS NULL "
        "AND source_revision IS NULL AND source_sha256 IS NULL AND approved_by IS NULL)",
    )


def downgrade():
    raise RuntimeError("Manual review required: standard configurations may lack legacy approvers")
