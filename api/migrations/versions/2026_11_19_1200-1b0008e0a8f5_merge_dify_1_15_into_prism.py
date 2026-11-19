"""merge Dify 1.15 migrations into Prism's chain

Dify 1.15's migrations continue from a4f2d8c9b731, which Prism's chain had
already merged in at 4519fbf37595; this joins the two heads again so
`flask db upgrade` has a single target. Neither side's tables depend on the
other's.

Revision ID: 1b0008e0a8f5
Revises: 4519fbf37595, d9e8f7a6b5c4
Create Date: 2026-11-19 12:00:00.000000

"""

# revision identifiers, used by Alembic.
revision = "1b0008e0a8f5"
down_revision = ("4519fbf37595", "d9e8f7a6b5c4")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
