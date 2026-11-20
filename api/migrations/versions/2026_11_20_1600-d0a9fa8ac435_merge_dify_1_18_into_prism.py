"""merge Dify 1.18 migrations into Prism's chain

Dify 1.18's migrations continue from Dify 1.17's head (c3f1a9b2e6d4) to
b9d3f7a5c102, while Prism's chain ends at e6682b5d318d; this
joins the two heads again so `flask db upgrade` has a single target. Dify's
new tables (KnowledgeFS, resource access tokens, workflow archives) do not
touch the tables Prism's chain changes.

Revision ID: d0a9fa8ac435
Revises: e6682b5d318d, b9d3f7a5c102
Create Date: 2026-11-20 16:00:00.000000

"""

# revision identifiers, used by Alembic.
revision = "d0a9fa8ac435"
down_revision = ("e6682b5d318d", "b9d3f7a5c102")
branch_labels = None
depends_on = None


def upgrade():
    pass


def downgrade():
    pass
