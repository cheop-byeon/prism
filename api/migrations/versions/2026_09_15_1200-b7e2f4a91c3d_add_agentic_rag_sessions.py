"""Add agentic_rag_sessions and agentic_rag_messages

Revision ID: b7e2f4a91c3d
Revises: a1b2c3d4e5f6
Create Date: 2026-09-15 12:00:00.000000

"""

from alembic import op
import models as models
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = "b7e2f4a91c3d"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade():
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


def downgrade():
    op.drop_table("agentic_rag_messages")
    op.drop_table("agentic_rag_sessions")
