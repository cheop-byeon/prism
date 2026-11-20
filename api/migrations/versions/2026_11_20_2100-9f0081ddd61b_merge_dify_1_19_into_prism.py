"""merge Dify 1.19 migrations into Prism's chain

Dify 1.19's migrations continue from Dify 1.18's head (b9d3f7a5c102) to
53c10126d551, while Prism's chain ends at d0a9fa8ac435; this joins the two
heads again so `flask db upgrade` has a single target. Dify's new tables do
not touch the tables Prism's chain changes.

Revision ID: 9f0081ddd61b
Revises: d0a9fa8ac435, 53c10126d551
Create Date: 2026-11-20 21:00:00.000000

"""

# revision identifiers, used by Alembic.
revision = "9f0081ddd61b"
down_revision = ("d0a9fa8ac435", "53c10126d551")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
