"""add tranquis pro subscription fields

Revision ID: b7d2e4f1a8c3
Revises: a3f8c1d4e9b2
Create Date: 2026-10-12 10:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

revision = 'b7d2e4f1a8c3'
down_revision = 'c4e6a8b0d2f3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'users',
        sa.Column('is_pro', sa.Boolean(), nullable=False, server_default='false'),
    )
    op.add_column(
        'users',
        sa.Column('pro_expires_at', sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        'users',
        sa.Column('pro_source', sa.String(), nullable=True),
    )
    op.add_column(
        'users',
        sa.Column('stripe_customer_id', sa.String(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column('users', 'stripe_customer_id')
    op.drop_column('users', 'pro_source')
    op.drop_column('users', 'pro_expires_at')
    op.drop_column('users', 'is_pro')
