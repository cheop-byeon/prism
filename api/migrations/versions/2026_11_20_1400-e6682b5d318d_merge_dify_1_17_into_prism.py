"""merge Dify 1.17 migrations into Prism's chain

Dify 1.17's migrations continue from 6f5a9c2d8e1b, which Prism's chain had
already merged in at a71f4775a10d; this joins the two heads again so
`flask db upgrade` has a single target. Neither side's tables depend on the
other's.

Revision ID: e6682b5d318d
Revises: a71f4775a10d, c3f1a9b2e6d4
Create Date: 2026-11-20 14:00:00.000000

"""

# revision identifiers, used by Alembic.
revision = "e6682b5d318d"
down_revision = ("a71f4775a10d", "c3f1a9b2e6d4")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
