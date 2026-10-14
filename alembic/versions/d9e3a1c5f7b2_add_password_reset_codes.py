"""add password_reset_codes for the Tranquis emailed reset code

Revision ID: d9e3a1c5f7b2
Revises: b7d2e4f1a8c3
Create Date: 2026-10-14 15:00:00.000000

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'd9e3a1c5f7b2'
down_revision = 'b7d2e4f1a8c3'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        'password_reset_codes',
        sa.Column('id', postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column('user_id', postgresql.UUID(as_uuid=True),
                  sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('code_hash', sa.String(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('used', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index('ix_password_reset_codes_user_id', 'password_reset_codes', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_password_reset_codes_user_id', table_name='password_reset_codes')
    op.drop_table('password_reset_codes')
