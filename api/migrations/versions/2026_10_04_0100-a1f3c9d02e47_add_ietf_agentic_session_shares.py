"""Add ietf_agentic_session_shares

Revision ID: a1f3c9d02e47
Revises: 8bcc6c522271
Create Date: 2026-10-04 01:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

import models as models

# revision identifiers, used by Alembic.
revision = "a1f3c9d02e47"
down_revision = "8bcc6c522271"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ietf_agentic_session_shares",
        sa.Column("id", models.types.StringUUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("conversation_id", models.types.StringUUID(), nullable=False),
        sa.Column("shared", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.PrimaryKeyConstraint("id", name=op.f("ietf_agentic_session_shares_pkey")),
        sa.UniqueConstraint("conversation_id", name="ietf_agentic_session_shares_conversation_id_key"),
    )
    # No backfill, unlike ietf_local_sources' legacy-registry backfill --
    # there is no prior "everything was public" state for sessions to
    # preserve. Every existing session simply has no row here, which is
    # exactly "private" under this table's own convention -- explicit, per
    # the user's own instruction that existing sessions stay private.


def downgrade():
    op.drop_table("ietf_agentic_session_shares")
