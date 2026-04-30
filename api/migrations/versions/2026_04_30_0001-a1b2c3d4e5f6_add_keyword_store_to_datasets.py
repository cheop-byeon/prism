"""add keyword_store to datasets

Revision ID: a1b2c3d4e5f6
Revises: 6b5f9f8b1a2c
Create Date: 2026-04-30 00:01:00.000000

"""
import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "a1b2c3d4e5f6"
down_revision = "6b5f9f8b1a2c"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("datasets", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "keyword_store",
                sa.String(length=50),
                nullable=False,
                server_default=sa.text("'bm25'"),
            )
        )


def downgrade():
    with op.batch_alter_table("datasets", schema=None) as batch_op:
        batch_op.drop_column("keyword_store")
