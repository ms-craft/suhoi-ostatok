"""Allow teachers to label work after uploading it."""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("submissions", sa.Column("student_reference", sa.String(120), nullable=True))


def downgrade() -> None:
    op.drop_column("submissions", "student_reference")
