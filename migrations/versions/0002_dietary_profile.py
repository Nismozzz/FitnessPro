"""add dietary preferences, habits and restrictions to user profiles"""

from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("user_profiles", sa.Column("dietary_preferences", sa.JSON(), nullable=True))
    op.add_column("user_profiles", sa.Column("dietary_habits", sa.JSON(), nullable=True))
    op.add_column("user_profiles", sa.Column("dietary_restrictions", sa.JSON(), nullable=True))


def downgrade():
    with op.batch_alter_table("user_profiles") as batch:
        batch.drop_column("dietary_restrictions")
        batch.drop_column("dietary_habits")
        batch.drop_column("dietary_preferences")
