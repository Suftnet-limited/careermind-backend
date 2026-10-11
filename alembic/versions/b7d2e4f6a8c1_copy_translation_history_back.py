"""copy translation_history rows back into translations

Translations made while the API wrote to translation_history (Oct 2026)
are copied into translations so the app's history shows them. The
translation_history and phrasebook_entries tables are left in place.

Revision ID: b7d2e4f6a8c1
Revises: a3f8c1d4e9b2
Create Date: 2026-10-11 12:00:00.000000

"""
from alembic import op

revision = 'b7d2e4f6a8c1'
down_revision = 'a3f8c1d4e9b2'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        INSERT INTO translations (id, user_id, type, source_lang, target_lang,
                                  source_text, translated_text, tone, created_at)
        SELECT h.id, h.user_id, h.mode, h.source_lang, h.target_lang,
               h.source_text, h.translated_text, NULL, h.created_at
        FROM translation_history h
        ON CONFLICT (id) DO NOTHING
    """)


def downgrade() -> None:
    op.execute("DELETE FROM translations WHERE id IN (SELECT id FROM translation_history)")
