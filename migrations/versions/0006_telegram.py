"""Add class-scoped Telegram registration and result delivery."""
import sqlalchemy as sa
from alembic import op

revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('teacher_workspaces',
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), primary_key=True),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('data', sa.JSON(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False))
    op.add_column('submissions', sa.Column('class_reference', sa.String(100), nullable=True))
    op.create_index('ix_submissions_class_reference', 'submissions', ['class_reference'])
    op.create_table('telegram_classes',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.String(36), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('external_id', sa.String(100), nullable=False),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('code_digest', sa.String(64), nullable=False, unique=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('user_id', 'external_id', name='uq_telegram_class_owner_external'))
    op.create_index('ix_telegram_classes_user_id', 'telegram_classes', ['user_id'])
    op.create_table('telegram_bindings',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('class_id', sa.String(36), sa.ForeignKey('telegram_classes.id', ondelete='CASCADE'), nullable=False),
        sa.Column('name', sa.String(120), nullable=False),
        sa.Column('name_key', sa.String(240), nullable=False),
        sa.Column('chat_id', sa.BigInteger, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('class_id', 'name_key', name='uq_telegram_class_name'),
        sa.UniqueConstraint('class_id', 'chat_id', name='uq_telegram_class_chat'))
    op.create_index('ix_telegram_bindings_class_id', 'telegram_bindings', ['class_id'])
    op.create_table('telegram_deliveries',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('submission_id', sa.String(36), sa.ForeignKey('submissions.id', ondelete='CASCADE'), nullable=False),
        sa.Column('binding_id', sa.String(36), sa.ForeignKey('telegram_bindings.id', ondelete='CASCADE'), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('digest', sa.String(64), nullable=True),
        sa.Column('inflight_digest', sa.String(64), nullable=True),
        sa.Column('sent_parts', sa.Integer(), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('submission_id', 'binding_id', name='uq_telegram_delivery'),
        sa.CheckConstraint("status IN ('pending','sent','failed')", name='ck_telegram_delivery_status'))
    op.create_index('ix_telegram_delivery_status', 'telegram_deliveries', ['status'])


def downgrade():
    raise RuntimeError('Telegram registrations and delivery history require a backup before downgrade.')
