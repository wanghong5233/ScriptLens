"""Add workspace_config to scriptlens.scripts.

Revision ID: 04_add_workspace_cfg
Revises: 03_drop_script_chunks
Create Date: 2026-05-20
"""

from typing import Sequence, Union

from alembic import op


revision: str = "04_add_workspace_cfg"
down_revision: Union[str, None] = "03_drop_script_chunks"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        ALTER TABLE scriptlens.scripts
        ADD COLUMN IF NOT EXISTS workspace_config JSONB NOT NULL DEFAULT '{}'::jsonb
        """
    )


def downgrade() -> None:
    op.execute(
        """
        ALTER TABLE scriptlens.scripts
        DROP COLUMN IF EXISTS workspace_config
        """
    )
