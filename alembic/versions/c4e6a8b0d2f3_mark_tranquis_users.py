"""mark existing Tranquis users with app_source = 'tranquis'

The app didn't send app_source at sign-up, so every user defaulted to
'careermind'. Users with translations or saved phrases and no interviews
signed up through Tranquis.

Revision ID: c4e6a8b0d2f3
Revises: b7d2e4f6a8c1
Create Date: 2026-10-11 18:30:00.000000

"""
from alembic import op

revision = 'c4e6a8b0d2f3'
down_revision = 'b7d2e4f6a8c1'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        UPDATE users u SET app_source = 'tranquis'
        WHERE (EXISTS (SELECT 1 FROM translations t WHERE t.user_id = u.id)
               OR EXISTS (SELECT 1 FROM phrasebook p WHERE p.user_id = u.id))
          AND NOT EXISTS (SELECT 1 FROM interview_sessions i WHERE i.user_id = u.id)
    """)


def downgrade() -> None:
    pass
