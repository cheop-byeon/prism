"""Add ietf_session_decision_threads

Revision ID: b909fc6812d7
Revises: dfea80d89431
Create Date: 2026-10-15 20:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

import models as models

# revision identifiers, used by Alembic.
revision = "b909fc6812d7"
down_revision = "dfea80d89431"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ietf_session_decision_threads",
        sa.Column("id", models.types.StringUUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("conversation_id", models.types.StringUUID(), nullable=False),
        sa.Column("draft_hint", sa.String(length=255), server_default=sa.text("''"), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("model_provider", sa.String(length=255), server_default=sa.text("''"), nullable=False),
        sa.Column("model_name", sa.String(length=255), server_default=sa.text("''"), nullable=False),
        sa.Column("data", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("ietf_session_decision_threads_pkey")),
        sa.UniqueConstraint("conversation_id", name="ietf_session_decision_threads_conversation_id_key"),
    )


def downgrade():
    op.drop_table("ietf_session_decision_threads")
