"""Indexes on foreign keys that reference transactions (fast retention deletes)

Revision ID: 0002
Revises: 0001
"""
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_test_events_transaction_id", "test_events", ["transaction_id"])
    op.create_index("ix_followup_large_transaction_id", "followup_events", ["large_transaction_id"])


def downgrade() -> None:
    op.drop_index("ix_followup_large_transaction_id", table_name="followup_events")
    op.drop_index("ix_test_events_transaction_id", table_name="test_events")
