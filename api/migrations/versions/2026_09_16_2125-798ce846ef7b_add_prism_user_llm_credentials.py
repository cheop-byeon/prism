"""add prism_user_llm_credentials

Revision ID: 798ce846ef7b
Revises: c4a8e2f19d7b
Create Date: 2026-09-16 21:25:00.000000

"""
import sqlalchemy as sa
from alembic import op

import models.types


def _is_pg(conn):
    return conn.dialect.name == "postgresql"


# revision identifiers, used by Alembic.
revision = '798ce846ef7b'
down_revision = 'c4a8e2f19d7b'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()

    if _is_pg(conn):
        op.create_table('prism_user_llm_credentials',
        sa.Column('id', models.types.StringUUID(), nullable=False),
        sa.Column('account_id', models.types.StringUUID(), nullable=False),
        sa.Column('provider', sa.String(length=255), nullable=False),
        sa.Column('encrypted_config', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('CURRENT_TIMESTAMP'), nullable=False),
        sa.PrimaryKeyConstraint('id', name='prism_user_llm_credential_pkey'),
        sa.UniqueConstraint('account_id', 'provider', name='prism_user_llm_credential_account_provider_key'),
        )
    else:
        op.create_table('prism_user_llm_credentials',
        sa.Column('id', models.types.StringUUID(), nullable=False),
        sa.Column('account_id', models.types.StringUUID(), nullable=False),
        sa.Column('provider', sa.String(length=255), nullable=False),
        sa.Column('encrypted_config', models.types.LongText(), nullable=False),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.func.current_timestamp(), nullable=False),
        sa.PrimaryKeyConstraint('id', name='prism_user_llm_credential_pkey'),
        sa.UniqueConstraint('account_id', 'provider', name='prism_user_llm_credential_account_provider_key'),
        )

    with op.batch_alter_table('prism_user_llm_credentials', schema=None) as batch_op:
        batch_op.create_index('prism_user_llm_credential_account_idx', ['account_id'], unique=False)


def downgrade():
    op.drop_table('prism_user_llm_credentials')
