"""merge Dify 1.20 migrations into Prism's chain

Dify 1.20's migrations continue from Dify 1.19's head (53c10126d551) to
f8b6e2c4a901, while Prism's chain ends at 9f0081ddd61b; this joins the two
heads again so `flask db upgrade` has a single target. Dify's new tables
(evaluations, file scanning, personal access tokens) do not touch the tables
Prism's chain changes.

Revision ID: 9c3a8a561fcc
Revises: 9f0081ddd61b, f8b6e2c4a901
Create Date: 2026-11-21 10:00:00.000000

"""

# revision identifiers, used by Alembic.
revision = "9c3a8a561fcc"
down_revision = ("9f0081ddd61b", "f8b6e2c4a901")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
