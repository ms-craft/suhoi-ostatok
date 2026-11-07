"""Render teacher-facing results as safe Telegram HTML messages."""
import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

STATUS_LABELS = {
    'correct': 'Верно', 'incorrect': 'Ошибка', 'partially_correct': 'Частично верно',
    'needs_review': 'Нужна проверка', 'no_answer': 'Нет ответа', 'unreadable': 'Неразборчиво',
}


def _readable_math(text: str) -> str:
    text = str(text or '')
    text = re.sub(r'\\(?:d?frac)\s*\{([^{}]+)\}\s*\{([^{}]+)\}', r'(\1)/(\2)', text)
    text = re.sub(r'\\sqrt\s*\{([^{}]+)\}', r'√(\1)', text)
    for source, target in {'\\cdot': '·', '\\times': '×', '\\div': '÷', '\\pm': '±',
                           '\\leq': '≤', '\\geq': '≥', '\\neq': '≠', '\\approx': '≈',
                           '\\infty': '∞', '\\(': '', '\\)': '', '\\[': '', '\\]': ''}.items():
        text = text.replace(source, target)
    return text.replace('$$', '').replace('$', '')


def _chunks(text: str, size: int = 400) -> list[str]:
    """Split before HTML escaping so every resulting message stays below Telegram's limit."""
    text = str(text or '')
    return [text[i:i + size] for i in range(0, len(text), size)] or ['—']


def _quoted_errors(explanation: str, answer: str) -> list[str]:
    result = []
    for match in re.finditer(r'«([^«»\n]{1,200})»|“([^“”\n]{1,200})”', explanation or ''):
        fragment = match.group(1) or match.group(2)
        before = explanation[max(0, match.start() - 80):match.start()]
        if re.search(r'(?:вместо|эталон(?:е|а)?|правильно|пропущен(?:о|а|ы)?(?:\s+слово)?)\s*[:—-]?\s*$', before, re.I):
            continue
        if answer.count(fragment) == 1 and fragment not in result:
            result.append(fragment)
    return result


def format_result(submission: dict, tasks: list[dict]) -> list[str]:
    """Return complete, HTML-escaped messages without assuming Telegram renders LaTeX."""
    created = submission.get('created_at')
    try:
        date = datetime.fromisoformat(created.replace('Z', '+00:00')) if isinstance(created, str) else created
        date = date.astimezone(ZoneInfo('Europe/Minsk')).strftime('%d.%m.%Y') if date else ''
    except (ValueError, TypeError, OverflowError):
        date = ''
    title = 'Результат проверки' + (f' от {date}' if date else '')
    messages = [f'<b>{html.escape(title)}</b>\nПредварительный результат, если учитель не подтвердил его.']
    if not tasks:
        messages.append('Задания не распознаны. Обратитесь к учителю.')
    for index, task in enumerate(tasks, 1):
        rec = task.get('recognition') or {}
        original = task.get('original_recognition') or rec
        review = task.get('preliminary') or {}
        teacher = task.get('teacher') or {}
        stale = bool(task.get('review_stale'))
        if stale:
            review = {**review, 'status': 'needs_review',
                      'explanation': 'Ответ или условие исправлено после последней проверки. Нужна повторная проверка.',
                      'uncertainty': None, 'missing_data': []}
        confirmed = bool(teacher.get('confirmed_at'))
        status = (teacher.get('final_status') if confirmed else teacher.get('status')) or review.get('status', 'needs_review')
        if stale and not confirmed:
            status = 'needs_review'
        number = str(rec.get('number') or index)
        label = STATUS_LABELS.get(status, 'Нужна проверка')
        messages.append(f'<b>Задание {html.escape(number)}</b> · {html.escape(label)}\n' +
                        ('Подтверждено учителем' if confirmed else 'Предварительный результат'))
        condition = teacher.get('condition') if 'condition' in teacher else rec.get('condition')
        answer = teacher.get('answer') if 'answer' in teacher else rec.get('answer')
        original_answer = original.get('answer') or '—'
        fields = [('Условие', condition or 'Условие не найдено'), ('Ваш ответ', answer or '—')]
        if original_answer != (answer or '—'):
            fields.append(('Первоначальное распознавание', original_answer))
        if rec.get('answer') and rec.get('answer') != original_answer and rec.get('answer') != answer:
            fields.append(('Последнее распознавание', rec['answer']))
        expected = review.get('expected_answer')
        if expected:
            label = 'Ответ учителя' if review.get('expected_answer_source') == 'teacher_test' else 'Предполагаемый ответ'
            fields.append((label, expected))
        fields.append(('Объяснение', review.get('explanation') or '—'))
        if review.get('uncertainty'):
            fields.append(('Причина неопределённости', review['uncertainty']))
        if rec.get('ambiguity') and rec.get('ambiguity') != review.get('uncertainty'):
            fields.append(('Сомнения при распознавании', rec['ambiguity']))
        if original.get('ambiguity') and original.get('ambiguity') not in (rec.get('ambiguity'), review.get('uncertainty')):
            fields.append(('Сомнения при первоначальном распознавании', original['ambiguity']))
        if rec.get('corrections'):
            fields.append(('Исправления на листе', '\n'.join(rec['corrections'])))
        if review.get('missing_data'):
            fields.append(('Не хватает данных', '\n'.join(review['missing_data'])))
        if teacher.get('explanation'):
            fields.append(('Комментарий учителя', teacher['explanation']))
        for label, value in fields:
            for part, chunk in enumerate(_chunks(_readable_math(value))):
                heading = f'<b>{html.escape(label)}</b>\n' if part == 0 else ''
                messages.append(heading + html.escape(chunk))
        quotes = _quoted_errors(review.get('explanation') or '', answer or '')
        if quotes:
            messages.append('<b>Фрагменты ответа, на которые указала проверка</b>')
            for quote in quotes:
                messages.append(f'<blockquote>{html.escape(_readable_math(quote))}</blockquote>')
    packed = []
    for block in messages:
        if packed and len(packed[-1]) + len(block) + 2 <= 3500:
            packed[-1] += '\n\n' + block
        else:
            packed.append(block)
    return packed
