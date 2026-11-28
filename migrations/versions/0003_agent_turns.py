"""persist chat requests, execution state and committed tool results"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "agent_turns",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("session_id", sa.String(32), sa.ForeignKey("agent_sessions.id", ondelete="CASCADE"), nullable=False),
        sa.Column("request_id", sa.String(32), nullable=False),
        sa.Column("request", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("lease_owner", sa.String(32)),
        sa.Column("writes", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_agent_turns_session_id", "agent_turns", ["session_id"])


def downgrade():
    op.drop_table("agent_turns")
