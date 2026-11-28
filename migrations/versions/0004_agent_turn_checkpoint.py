"""persist complete Agent loop checkpoints between tool rounds"""

from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("agent_turns", sa.Column("checkpoint", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("agent_turns", "checkpoint")
