"""Track sheet allowances and non-expiring top-ups."""
import sqlalchemy as sa
from alembic import op

revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('submissions', sa.Column('accounted_pages', sa.Integer(), nullable=False, server_default='0'))
    # Existing completed work has already been charged under the previous scheme.
    op.execute('UPDATE submissions SET accounted_pages = page_count WHERE first_completed_at IS NOT NULL')
    with op.batch_alter_table('usage_records') as batch:
        batch.alter_column('period_id', existing_type=sa.String(36), nullable=True)
        batch.add_column(sa.Column('user_id', sa.String(36), nullable=True))
        batch.add_column(sa.Column('free_pages', sa.Integer(), nullable=False, server_default='0'))
        batch.add_column(sa.Column('monthly_pages', sa.Integer(), nullable=False, server_default='0'))
        batch.add_column(sa.Column('extra_pages', sa.Integer(), nullable=False, server_default='0'))
        batch.create_foreign_key('fk_usage_records_user_id', 'users', ['user_id'], ['id'], ondelete='CASCADE')
        batch.create_index('ix_usage_records_user_id', ['user_id'])
    op.create_table('sheet_topups',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('checkout_id', sa.String(36), sa.ForeignKey('checkouts.id', ondelete='CASCADE'), nullable=False, unique=True),
        sa.Column('pages', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index('ix_sheet_topups_user_id', 'sheet_topups', ['user_id'])


def downgrade() -> None:
    raise RuntimeError('Sheet billing contains usage and top-up data; restore a backup instead of downgrading.')
