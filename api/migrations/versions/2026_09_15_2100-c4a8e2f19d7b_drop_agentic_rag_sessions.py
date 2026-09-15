"""Drop agentic_rag_sessions and agentic_rag_messages

Superseded by storing Prism's conversations directly on Dify's real
Conversation/Message tables (see services/agentic_rag_session_service.py)
instead of a bespoke pair -- inherits cost/token columns and real
auth-scoped ownership rather than reinventing them.

Revision ID: c4a8e2f19d7b
Revises: b7e2f4a91c3d
Create Date: 2026-09-15 21:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

import models as models

# revision identifiers, used by Alembic.
revision = "c4a8e2f19d7b"
down_revision = "b7e2f4a91c3d"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_table("agentic_rag_messages")
    op.drop_table("agentic_rag_sessions")


def downgrade():
    op.create_table(
        "agentic_rag_sessions",
        sa.Column("id", models.types.StringUUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False, server_default=sa.text("''")),
        sa.PrimaryKeyConstraint("id", name=op.f("agentic_rag_sessions_pkey")),
    )
    with op.batch_alter_table("agentic_rag_sessions", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("agentic_rag_sessions_updated_at_idx"), ["updated_at"], unique=False)

    op.create_table(
        "agentic_rag_messages",
        sa.Column("id", models.types.StringUUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("session_id", models.types.StringUUID(), nullable=False),
        sa.Column("tier", sa.String(length=20), nullable=False),
        sa.Column("query", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("trace", models.types.AdjustedJSON(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("agentic_rag_messages_pkey")),
    )
    with op.batch_alter_table("agentic_rag_messages", schema=None) as batch_op:
        batch_op.create_index(batch_op.f("agentic_rag_messages_session_id_idx"), ["session_id"], unique=False)
