"""Add ietf_local_sources

Revision ID: 8bcc6c522271
Revises: c4a8e2f19d7b
Create Date: 2026-09-28 23:19:00.000000

"""

import json
from pathlib import Path

import sqlalchemy as sa
from alembic import op

import models as models

# revision identifiers, used by Alembic.
revision = "8bcc6c522271"
down_revision = "c4a8e2f19d7b"
branch_labels = None
depends_on = None

# Same path IetfImportService._LOCAL_SOURCES_PATH has always used -- read
# directly here (not imported) since a migration must stay correct even
# after that service module's own path constant someday changes.
_LEGACY_LOCAL_SOURCES_PATH = Path("/app/api/storage/ietf-downloads/local_sources.json")


def upgrade():
    op.create_table(
        "ietf_local_sources",
        sa.Column("id", models.types.StringUUID(), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        sa.Column("draft_name", sa.String(length=255), nullable=False),
        sa.Column("owner_account_id", models.types.StringUUID(), nullable=False),
        sa.Column("shared", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.PrimaryKeyConstraint("id", name=op.f("ietf_local_sources_pkey")),
        sa.UniqueConstraint("draft_name", name="ietf_local_sources_draft_name_key"),
    )

    # Backfill every draft the old flat-JSON registry already tracked, owned
    # by whichever account this deployment's _is_data_admin has always meant
    # (the first tenant ever created here) and marked shared=True -- these
    # were pulled and visible to everyone under the old all-or-nothing model,
    # and should stay visible to everyone now that sharing is opt-in; only
    # pulls made *after* this migration start private by default.
    if not _LEGACY_LOCAL_SOURCES_PATH.is_file():
        return
    try:
        draft_names = json.loads(_LEGACY_LOCAL_SOURCES_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return
    if not isinstance(draft_names, list) or not draft_names:
        return

    conn = op.get_bind()
    owner_id = conn.execute(
        sa.text(
            """
            select taj.account_id
            from tenants t
            join tenant_account_joins taj on taj.tenant_id = t.id and taj.role = 'owner'
            order by t.created_at asc
            limit 1
            """
        )
    ).scalar()
    if not owner_id:
        return

    ietf_local_sources = sa.table(
        "ietf_local_sources",
        sa.column("id", models.types.StringUUID()),
        sa.column("draft_name", sa.String()),
        sa.column("owner_account_id", models.types.StringUUID()),
        sa.column("shared", sa.Boolean()),
    )
    from uuid import uuid4

    conn.execute(
        ietf_local_sources.insert(),
        [
            {"id": str(uuid4()), "draft_name": name, "owner_account_id": owner_id, "shared": True}
            for name in dict.fromkeys(str(n) for n in draft_names)
        ],
    )


def downgrade():
    op.drop_table("ietf_local_sources")
