# ruff: noqa: E501,I001
"""moderation decision template audit fields"""

from __future__ import annotations

from typing import TYPE_CHECKING

from alembic import op
import sqlalchemy as sa

if TYPE_CHECKING:
    from collections.abc import Sequence

# revision identifiers, used by Alembic.
revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


ACTION_CONSTRAINT = "ck_moderation_decisions_moderationaction"
# Revision 0010 used SQLAlchemy's unnamed non-native Enum check with
# name="moderationaction"; the metadata naming convention prefixes that to
# this persisted name. Wrap it with op.f() in operations below to prevent
# Alembic from applying the convention a second time.


def upgrade() -> None:
    """Apply this revision."""

    op.drop_constraint(op.f(ACTION_CONSTRAINT), "moderation_decisions", type_="check")
    op.create_check_constraint(
        op.f(ACTION_CONSTRAINT),
        "moderation_decisions",
        "action IN ('hide', 'hide_and_mark_nsfw', 'mark_nsfw', 'mark_sfw', 'no_action', 'template_override', 'override_flags', 'publish')",
    )
    op.add_column("moderation_decisions", sa.Column("previous_template_id", sa.Uuid(), nullable=True))
    op.add_column("moderation_decisions", sa.Column("new_template_id", sa.Uuid(), nullable=True))
    op.create_foreign_key(
        op.f("fk_moderation_decisions_previous_template_id_meme_templates"),
        "moderation_decisions",
        "meme_templates",
        ["previous_template_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        op.f("fk_moderation_decisions_new_template_id_meme_templates"),
        "moderation_decisions",
        "meme_templates",
        ["new_template_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    """Revert this revision."""

    op.drop_constraint(op.f("fk_moderation_decisions_new_template_id_meme_templates"), "moderation_decisions", type_="foreignkey")
    op.drop_constraint(op.f("fk_moderation_decisions_previous_template_id_meme_templates"), "moderation_decisions", type_="foreignkey")
    op.drop_column("moderation_decisions", "new_template_id")
    op.drop_column("moderation_decisions", "previous_template_id")
    op.drop_constraint(op.f(ACTION_CONSTRAINT), "moderation_decisions", type_="check")
    op.create_check_constraint(
        op.f(ACTION_CONSTRAINT),
        "moderation_decisions",
        "action IN ('hide', 'hide_and_mark_nsfw', 'mark_nsfw', 'mark_sfw', 'no_action', 'override_flags', 'publish')",
    )
