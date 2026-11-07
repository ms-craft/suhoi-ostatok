"""initial schema

Revision ID: 0001
Revises:
Create Date: 2026-11-06 11:28:25.780432
"""
from typing import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = '0001'
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'users',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('email', sa.String(length=254), nullable=False),
        sa.Column('password_hash', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('email'),
    )
    op.create_table(
        'access_tokens',
        sa.Column('digest', sa.LargeBinary(length=32), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('digest'),
    )
    op.create_index(op.f('ix_access_tokens_expires_at'), 'access_tokens', ['expires_at'], unique=False)
    op.create_index(op.f('ix_access_tokens_user_id'), 'access_tokens', ['user_id'], unique=False)
    op.create_table(
        'checkouts',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('plan_code', sa.String(length=32), nullable=False),
        sa.Column('checks_limit', sa.Integer(), nullable=False),
        sa.Column('max_pages', sa.Integer(), nullable=False),
        sa.Column('allow_followups', sa.Boolean(), nullable=False),
        sa.Column('price_minor', sa.Integer(), nullable=True),
        sa.Column('currency', sa.String(length=3), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('last_event_sequence', sa.Integer(), nullable=False),
        sa.Column('external_subscription_id', sa.String(length=128), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_checkouts_user_id'), 'checkouts', ['user_id'], unique=False)
    op.create_table(
        'submissions',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('latest_job_id', sa.String(length=36), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('stage', sa.String(length=32), nullable=False),
        sa.Column('error_code', sa.String(length=64), nullable=True),
        sa.Column('page_count', sa.Integer(), nullable=False),
        sa.Column('first_completed_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('delete_requested_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status in ('queued','processing','completed','failed')", name='ck_submissions_status'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_submissions_cleanup', 'submissions', ['delete_requested_at'], unique=False)
    op.create_index('ix_submissions_owner_created', 'submissions', ['user_id', 'created_at', 'id'], unique=False)
    op.create_table(
        'detected_tasks',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('submission_id', sa.String(length=36), nullable=False),
        sa.Column('position', sa.Integer(), nullable=False),
        sa.Column('page_number', sa.Integer(), nullable=False),
        sa.Column('original_recognition', sa.JSON(), nullable=False),
        sa.Column('original_preliminary', sa.JSON(), nullable=False),
        sa.Column('recognition', sa.JSON(), nullable=False),
        sa.Column('preliminary', sa.JSON(), nullable=False),
        sa.Column('teacher', sa.JSON(), nullable=True),
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('demonstration', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['submission_id'], ['submissions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_detected_tasks_submission_order', 'detected_tasks', ['submission_id', 'position'], unique=False)
    op.create_table(
        'payment_events',
        sa.Column('provider', sa.String(length=32), nullable=False),
        sa.Column('id', sa.String(length=128), nullable=False),
        sa.Column('checkout_id', sa.String(length=36), nullable=False),
        sa.Column('event_type', sa.String(length=24), nullable=False),
        sa.Column('sequence', sa.Integer(), nullable=False),
        sa.Column('applied', sa.Boolean(), nullable=False),
        sa.Column('received_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['checkout_id'], ['checkouts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('provider', 'id'),
    )
    op.create_index(op.f('ix_payment_events_checkout_id'), 'payment_events', ['checkout_id'], unique=False)
    op.create_table(
        'submission_files',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('submission_id', sa.String(length=36), nullable=False),
        sa.Column('number', sa.Integer(), nullable=False),
        sa.Column('object_key', sa.String(length=220), nullable=False),
        sa.Column('mime_type', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['submission_id'], ['submissions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('object_key'),
        sa.UniqueConstraint('submission_id', 'number', name='uq_submission_file_number'),
    )
    op.create_table(
        'subscriptions',
        sa.Column('user_id', sa.String(length=36), nullable=False),
        sa.Column('checkout_id', sa.String(length=36), nullable=True),
        sa.Column('pending_checkout_id', sa.String(length=36), nullable=True),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('cancel_at_period_end', sa.Boolean(), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('inactive','active','past_due')", name='ck_subscription_status'),
        sa.ForeignKeyConstraint(['checkout_id'], ['checkouts.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['pending_checkout_id'], ['checkouts.id'], ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('user_id'),
    )
    op.create_index(op.f('ix_subscriptions_checkout_id'), 'subscriptions', ['checkout_id'], unique=False)
    op.create_index(op.f('ix_subscriptions_pending_checkout_id'), 'subscriptions', ['pending_checkout_id'], unique=False)
    op.create_table(
        'usage_periods',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('checkout_id', sa.String(length=36), nullable=False),
        sa.Column('starts_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('ends_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('checks_limit', sa.Integer(), nullable=False),
        sa.Column('max_pages', sa.Integer(), nullable=False),
        sa.Column('allow_followups', sa.Boolean(), nullable=False),
        sa.CheckConstraint('checks_limit > 0 AND max_pages > 0', name='ck_usage_period_limits'),
        sa.CheckConstraint('starts_at < ends_at', name='ck_usage_period_range'),
        sa.ForeignKeyConstraint(['checkout_id'], ['checkouts.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('checkout_id', 'starts_at', name='uq_usage_period_checkout_start'),
    )
    op.create_table(
        'submission_pages',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('submission_id', sa.String(length=36), nullable=False),
        sa.Column('source_file_id', sa.String(length=36), nullable=False),
        sa.Column('number', sa.Integer(), nullable=False),
        sa.Column('source_page', sa.Integer(), nullable=False),
        sa.Column('object_key', sa.String(length=220), nullable=False),
        sa.CheckConstraint('number >= 1 and source_page >= 1', name='ck_submission_page_numbers'),
        sa.ForeignKeyConstraint(['source_file_id'], ['submission_files.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['submission_id'], ['submissions.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('object_key'),
        sa.UniqueConstraint('submission_id', 'number', name='uq_submission_page_number'),
    )
    op.create_index(op.f('ix_submission_pages_source_file_id'), 'submission_pages', ['source_file_id'], unique=False)
    op.create_table(
        'teacher_edits',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('task_id', sa.String(length=36), nullable=False),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('snapshot', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['task_id'], ['detected_tasks.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_teacher_edits_task_id'), 'teacher_edits', ['task_id'], unique=False)
    op.create_table(
        'usage_records',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('period_id', sa.String(length=36), nullable=False),
        sa.Column('status', sa.String(length=8), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('settled_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("status IN ('reserved','used','released')", name='ck_usage_record_status'),
        sa.ForeignKeyConstraint(['period_id'], ['usage_periods.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index('ix_usage_records_period_status', 'usage_records', ['period_id', 'status'], unique=False)
    op.create_table(
        'processing_jobs',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('submission_id', sa.String(length=36), nullable=False),
        sa.Column('task_id', sa.String(length=36), nullable=True),
        sa.Column('kind', sa.String(length=16), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('stage', sa.String(length=32), nullable=False),
        sa.Column('attempts', sa.Integer(), nullable=False),
        sa.Column('lease_token', sa.String(length=36), nullable=True),
        sa.Column('lease_until', sa.DateTime(timezone=True), nullable=True),
        sa.Column('next_attempt_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('reservation_id', sa.String(length=36), nullable=True),
        sa.Column('origin_request_id', sa.String(length=36), nullable=True),
        sa.Column('clarification', sa.Text(), nullable=True),
        sa.Column('error_code', sa.String(length=64), nullable=True),
        sa.Column('provider', sa.String(length=32), nullable=True),
        sa.Column('demonstration', sa.Boolean(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("kind in ('initial','clarification','recheck')", name='ck_processing_jobs_kind'),
        sa.CheckConstraint("status in ('queued','processing','completed','failed')", name='ck_processing_jobs_status'),
        sa.CheckConstraint('attempts >= 0', name='ck_processing_jobs_attempts'),
        sa.ForeignKeyConstraint(['reservation_id'], ['usage_records.id'], ),
        sa.ForeignKeyConstraint(['submission_id'], ['submissions.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['task_id'], ['detected_tasks.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('reservation_id'),
    )
    op.create_index('ix_processing_jobs_due', 'processing_jobs', ['status', 'next_attempt_at', 'lease_until'], unique=False)
    op.create_index('ix_processing_jobs_submission_created', 'processing_jobs', ['submission_id', 'created_at'], unique=False)
    op.create_index(op.f('ix_processing_jobs_task_id'), 'processing_jobs', ['task_id'], unique=False)
    op.create_table(
        'task_analyses',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('task_id', sa.String(length=36), nullable=False),
        sa.Column('job_id', sa.String(length=36), nullable=False),
        sa.Column('recognition', sa.JSON(), nullable=False),
        sa.Column('review_input', sa.JSON(), nullable=False),
        sa.Column('preliminary', sa.JSON(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['job_id'], ['processing_jobs.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['task_id'], ['detected_tasks.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('task_id', 'job_id', name='uq_task_analysis_job'),
    )
    op.create_index(op.f('ix_task_analyses_job_id'), 'task_analyses', ['job_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_task_analyses_job_id'), table_name='task_analyses')
    op.drop_table('task_analyses')
    op.drop_index(op.f('ix_processing_jobs_task_id'), table_name='processing_jobs')
    op.drop_index('ix_processing_jobs_submission_created', table_name='processing_jobs')
    op.drop_index('ix_processing_jobs_due', table_name='processing_jobs')
    op.drop_table('processing_jobs')
    op.drop_index('ix_usage_records_period_status', table_name='usage_records')
    op.drop_table('usage_records')
    op.drop_index(op.f('ix_teacher_edits_task_id'), table_name='teacher_edits')
    op.drop_table('teacher_edits')
    op.drop_index(op.f('ix_submission_pages_source_file_id'), table_name='submission_pages')
    op.drop_table('submission_pages')
    op.drop_table('usage_periods')
    op.drop_index(op.f('ix_subscriptions_pending_checkout_id'), table_name='subscriptions')
    op.drop_index(op.f('ix_subscriptions_checkout_id'), table_name='subscriptions')
    op.drop_table('subscriptions')
    op.drop_table('submission_files')
    op.drop_index(op.f('ix_payment_events_checkout_id'), table_name='payment_events')
    op.drop_table('payment_events')
    op.drop_index('ix_detected_tasks_submission_order', table_name='detected_tasks')
    op.drop_table('detected_tasks')
    op.drop_index('ix_submissions_owner_created', table_name='submissions')
    op.drop_index('ix_submissions_cleanup', table_name='submissions')
    op.drop_table('submissions')
    op.drop_index(op.f('ix_checkouts_user_id'), table_name='checkouts')
    op.drop_table('checkouts')
    op.drop_index(op.f('ix_access_tokens_user_id'), table_name='access_tokens')
    op.drop_index(op.f('ix_access_tokens_expires_at'), table_name='access_tokens')
    op.drop_table('access_tokens')
    op.drop_table('users')
