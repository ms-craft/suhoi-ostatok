"""Add school subjects for input suggestions."""
import sqlalchemy as sa
from alembic import op

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None

SUBJECTS = {
    'stem': ('Математика', 'Алгебра', 'Геометрия', 'Физика', 'Химия', 'Биология', 'Информатика', 'Астрономия', 'Естествознание', 'Черчение', 'Технология'),
    'humanities': ('Русский язык', 'Белорусский язык', 'Литература', 'Русская литература', 'Белорусская литература', 'История', 'История Беларуси', 'Всемирная история', 'Обществознание', 'Английский язык', 'Немецкий язык', 'Французский язык', 'Испанский язык', 'География', 'Искусство', 'Музыка', 'Мировая художественная культура', 'Право', 'Экономика', 'Основы безопасности жизнедеятельности', 'Физическая культура'),
}


def upgrade() -> None:
    table = op.create_table('subjects', sa.Column('name', sa.String(100), primary_key=True),
                            sa.Column('category', sa.String(16), nullable=False))
    op.bulk_insert(table, [{'name': name, 'category': category} for category, names in SUBJECTS.items() for name in names])


def downgrade() -> None:
    op.drop_table('subjects')
