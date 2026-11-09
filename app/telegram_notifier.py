"""Render teacher-facing results as safe Telegram HTML messages."""
import html
import re
from datetime import datetime
from zoneinfo import ZoneInfo

STATUS_LABELS = {
    'correct': 'Верно', 'incorrect': 'Ошибка', 'partially_correct': 'Частично верно',
    'needs_review': 'Нужна проверка', 'no_answer': 'Нет ответа', 'unreadable': 'Неразборчиво',
}

_GROUP = r'\{(?:[^{}]|\{[^{}]*\})*\}'
_MATH = re.compile(
    r'(\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)|'
    r'(?<!\$)\$(?!\s)[^$\n]+?(?<!\s)\$(?!\$)|'
    r'\\(?:d?frac)\s*' + _GROUP + r'\s*' + _GROUP + r'|\\sqrt(?:\[[^\[\]]+\])?\s*' + _GROUP +
    r'|(?<![A-Za-z0-9_\\])(?:[A-Za-z]|[0-9]+)(?:_\{[A-Za-z0-9+\-]+\}|_[A-Za-z0-9]|\^\{[A-Za-z0-9+\-]+\}|\^[A-Za-z0-9])+'
    r'|\\(?:alpha|beta|gamma|delta|Delta|theta|lambda|mu|pi|sigma|phi|omega|Omega|infty)\b)'
)
_OPERATORS = {'\\cdot': '·', '\\times': '×', '\\div': '÷', '\\pm': '±',
              '\\leq': '≤', '\\geq': '≥', '\\neq': '≠', '\\approx': '≈'}


def _replace_operators(value: str) -> str:
    return re.sub(r'\\(?:cdot|times|div|pm|leq|geq|neq|approx)\b',
                  lambda match: _OPERATORS[match.group()], value)


def _readable_math(text: str) -> str:
    text = str(text or '')
    text = re.sub(r'\\(?:d?frac)\s*\{([^{}]+)\}\s*\{([^{}]+)\}', r'(\1)/(\2)', text)
    text = re.sub(r'\\sqrt\s*\{([^{}]+)\}', r'√(\1)', text)
    text = _replace_operators(text)
    for source, target in {'\\infty': '∞', '\\(': '', '\\)': '', '\\[': '', '\\]': ''}.items():
        text = text.replace(source, target)
    return text.replace('$$', '').replace('$', '')


def _chunks(text: str, size: int = 400) -> list[str]:
    """Split before HTML escaping so every resulting message stays below Telegram's limit."""
    text = str(text or '')
    return [text[i:i + size] for i in range(0, len(text), size)] or ['—']


def _rich_chunks(value: str) -> list[str]:
    """Escape text and keep each delimited LaTeX formula in one Telegram tag."""
    value = str(value or '—')
    parts = []
    last = 0
    for match in _MATH.finditer(value):
        if match.start() > last:
            parts.extend(html.escape(_replace_operators(chunk)).replace('\n', '<br>') for chunk in _chunks(value[last:match.start()]))
        token = match.group()
        formula = (token[2:-2] if token.startswith(('$$', r'\(', r'\[')) else
                   token[1:-1] if token.startswith('$') else token)
        escaped = html.escape(formula)
        if len(escaped) <= 2500:
            parts.append('<tg-math>' + escaped + '</tg-math>')
        else:
            # Keep unusually long expressions intact as readable source across messages.
            parts.extend(html.escape(chunk).replace('\n', '<br>') for chunk in _chunks(token))
        last = match.end()
    if last < len(value):
        parts.extend(html.escape(_replace_operators(chunk)).replace('\n', '<br>') for chunk in _chunks(value[last:]))
    packed = []
    for part in parts:
        if packed and len(packed[-1]) + len(part) <= 3000:
            packed[-1] += part
        else:
            packed.append(part)
    return packed or ['—']


def rich_to_plain(value: str) -> str:
    """Produce normal Telegram HTML if a server doesn't support rich messages."""
    value = re.sub(r'<tg-math>(.*?)</tg-math>',
                   lambda match: html.escape(_readable_math(html.unescape(match.group(1)))), value, flags=re.S)
    return value.replace('<p>', '').replace('</p>', '\n\n').replace('<br>', '\n').strip()


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


def format_result(submission: dict, tasks: list[dict], *, rich: bool = False) -> list[str]:
    """Return escaped normal or rich HTML, split at safe boundaries."""
    created = submission.get('created_at')
    try:
        date = datetime.fromisoformat(created.replace('Z', '+00:00')) if isinstance(created, str) else created
        date = date.astimezone(ZoneInfo('Europe/Minsk')).strftime('%d.%m.%Y') if date else ''
    except (ValueError, TypeError, OverflowError):
        date = ''
    title = submission.get('title') or 'Работа'
    meta = ' · '.join(filter(None, [submission.get('class_name'), date]))
    messages = [f'<b>{html.escape(str(title))}</b>' + (f'\n{html.escape(meta)}' if meta else '') +
                '\n\nРазбор работы · подтверждён учителем']
    if rich:
        messages[0] = '<p>' + messages[0].replace('\n', '<br>') + '</p>'
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
        number = str((teacher.get('number') if 'number' in teacher else rec.get('number')) or index)
        label = STATUS_LABELS.get(status, 'Нужна проверка')
        task_heading = (f'━━━━━━━━━━━━━━\n<b>Задание {html.escape(number)}</b> · {html.escape(label)}\n' +
                        ('Подтверждено учителем' if confirmed else 'Предварительный результат'))
        messages.append('<p>' + task_heading.replace('\n', '<br>') + '</p>' if rich else task_heading)
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
            chunks = _rich_chunks(value) if rich else [html.escape(chunk) for chunk in _chunks(_readable_math(value))]
            for part, chunk in enumerate(chunks):
                heading = f'<b>{html.escape(label)}</b>' + ('<br>' if rich else '\n') if part == 0 else ''
                messages.append('<p>' + heading + chunk + '</p>' if rich else heading + chunk)
        quotes = _quoted_errors(review.get('explanation') or '', answer or '')
        if quotes:
            heading = '<b>Фрагменты ответа, на которые указала проверка</b>'
            messages.append('<p>' + heading + '</p>' if rich else heading)
            for quote in quotes:
                content = ''.join(_rich_chunks(quote)) if rich else html.escape(_readable_math(quote))
                messages.append(f'<blockquote>{content}</blockquote>')
    packed = []
    for block in messages:
        if packed and len(packed[-1]) + len(block) + 2 <= 3500:
            packed[-1] += '\n\n' + block
        else:
            packed.append(block)
    return packed
