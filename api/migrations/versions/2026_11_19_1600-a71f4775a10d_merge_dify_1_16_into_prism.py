"""merge Dify 1.16 migrations into Prism's chain

Dify 1.16's migrations continue from d9e8f7a6b5c4, which Prism's chain had
already merged in at 1b0008e0a8f5; this joins the two heads again so
`flask db upgrade` has a single target. Neither side's tables depend on the
other's.

Revision ID: a71f4775a10d
Revises: 1b0008e0a8f5, 6f5a9c2d8e1b
Create Date: 2026-11-19 16:00:00.000000

"""

# revision identifiers, used by Alembic.
revision = "a71f4775a10d"
down_revision = ("1b0008e0a8f5", "6f5a9c2d8e1b")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
