"""merge Dify 1.14 migrations into Prism's chain

Prism's migrations and Dify 1.14's both branch from 6b5f9f8b1a2c, the last
Dify 1.13 revision; this joins the two heads so `flask db upgrade` has a
single target. Neither side's tables depend on the other's.

Revision ID: 4519fbf37595
Revises: b909fc6812d7, a4f2d8c9b731
Create Date: 2026-11-18 12:00:00.000000

"""

# revision identifiers, used by Alembic.
revision = "4519fbf37595"
down_revision = ("b909fc6812d7", "a4f2d8c9b731")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
