"""Add ietf_rfc_families and supporting tables

Revision ID: dfea80d89431
Revises: a1f3c9d02e47
Create Date: 2026-10-15 11:00:00.000000

"""

import sqlalchemy as sa
from alembic import op

import models as models

# revision identifiers, used by Alembic.
revision = "dfea80d89431"
down_revision = "a1f3c9d02e47"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "ietf_rfc_structural_edges",
        sa.Column("id", models.types.StringUUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("target", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("ietf_rfc_structural_edges_pkey")),
        sa.UniqueConstraint("source", "target", "kind", name="ietf_rfc_structural_edges_source_target_kind_key"),
    )
    op.create_index(
        "ietf_rfc_structural_edges_source_idx", "ietf_rfc_structural_edges", ["source"], unique=False
    )
    op.create_index(
        "ietf_rfc_structural_edges_target_idx", "ietf_rfc_structural_edges", ["target"], unique=False
    )

    op.create_table(
        "ietf_rfc_citation_fetch_log",
        sa.Column("id", models.types.StringUUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("rfc_number", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("topic_keyword", sa.String(length=255), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("ietf_rfc_citation_fetch_log_pkey")),
        sa.UniqueConstraint("rfc_number", name="ietf_rfc_citation_fetch_log_rfc_number_key"),
    )

    op.create_table(
        "ietf_rfc_citation_edges",
        sa.Column("id", models.types.StringUUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("target", sa.String(length=16), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("ietf_rfc_citation_edges_pkey")),
        sa.UniqueConstraint("source", "target", name="ietf_rfc_citation_edges_source_target_key"),
    )
    op.create_index("ietf_rfc_citation_edges_source_idx", "ietf_rfc_citation_edges", ["source"], unique=False)
    op.create_index("ietf_rfc_citation_edges_target_idx", "ietf_rfc_citation_edges", ["target"], unique=False)

    op.create_table(
        "ietf_rfc_families",
        sa.Column("id", models.types.StringUUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("label", sa.String(length=255), nullable=False),
        sa.Column("primary_wg", sa.String(length=64), nullable=True),
        sa.Column("member_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("ietf_rfc_families_pkey")),
    )

    op.create_table(
        "ietf_rfc_family_members",
        sa.Column("id", models.types.StringUUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("family_id", models.types.StringUUID(), nullable=False),
        sa.Column("rfc_number", sa.String(length=16), nullable=False),
        sa.Column("title", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("std_level", sa.String(length=32), server_default=sa.text("''"), nullable=False),
        sa.Column("wg", sa.String(length=64), server_default=sa.text("''"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("ietf_rfc_family_members_pkey")),
        sa.UniqueConstraint("rfc_number", name="ietf_rfc_family_members_rfc_number_key"),
    )
    op.create_index(
        "ietf_rfc_family_members_family_id_idx", "ietf_rfc_family_members", ["family_id"], unique=False
    )


def downgrade():
    op.drop_table("ietf_rfc_family_members")
    op.drop_table("ietf_rfc_families")
    op.drop_table("ietf_rfc_citation_edges")
    op.drop_table("ietf_rfc_citation_fetch_log")
    op.drop_table("ietf_rfc_structural_edges")
