"""Store the teacher's name without changing existing accounts."""
import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("full_name", sa.String(160), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "full_name")
