from app.telegram_notifier import format_result, rich_to_plain


def test_message_distinguishes_corrected_student_and_teacher_answers():
    messages = format_result({}, [{
        'recognition': {'number': '1', 'condition': 'Напишите слово', 'answer': 'комар'},
        'preliminary': {'status': 'incorrect', 'expected_answer': 'комар',
                        'expected_answer_source': 'teacher_test',
                        'explanation': 'Написано «камар» вместо «комар».'},
        'teacher': {'answer': 'камар', 'explanation': 'Проверьте букву'},
    }])
    text = '\n'.join(messages)
    assert '<b>Ваш ответ</b>\nкамар' in text
    assert '<b>Ответ учителя</b>\nкомар' in text
    assert '<blockquote>камар</blockquote>' in text
    assert '<b>Комментарий учителя</b>' in text
    assert 'Предварительный результат' in text


def test_long_content_is_not_truncated_and_telegram_html_is_escaped():
    answer = 'а<&> ' * 3000
    messages = format_result({}, [{'recognition': {'answer': answer}, 'preliminary': {'status': 'needs_review'}}])
    assert all(len(message) <= 3500 for message in messages)
    assert sum(message.count('а&lt;&amp;&gt; ') for message in messages) == 3000


def test_missing_or_repeated_quotes_do_not_point_to_wrong_location():
    messages = format_result({}, [{'recognition': {'answer': 'там там'},
                                    'preliminary': {'explanation': '«там» вместо «тут».'}}])
    assert '<blockquote>' not in '\n'.join(messages)


def test_corrected_task_number_is_used_in_student_result():
    text = '\n'.join(format_result({}, [{'recognition': {'number': '1', 'answer': '2'},
                                        'preliminary': {'status': 'correct', 'explanation': 'Верно.'},
                                        'teacher': {'number': '2', 'confirmed_at': '2026-01-01'}}]))
    assert '<b>Задание 2</b>' in text


def test_original_recognition_and_its_ambiguity_are_not_lost_after_correction():
    messages = format_result({}, [{
        'original_recognition': {'answer': 'x₁ = -11\nx₂ = 32', 'ambiguity': 'Возможно, цифры записаны раздельно.'},
        'recognition': {'answer': 'x₁ = -1\nx₂ = 3'},
        'preliminary': {'status': 'incorrect', 'explanation': 'Проверьте корни.'},
        'teacher': {'answer': 'x₁ = -1\nx₂ = 3'},
    }])
    text = '\n'.join(messages)
    assert '<b>Первоначальное распознавание</b>\nx₁ = -11\nx₂ = 32' in text
    assert '<b>Ваш ответ</b>\nx₁ = -1\nx₂ = 3' in text
    assert 'Сомнения при первоначальном распознавании' in text


def test_stale_review_is_not_presented_as_current_after_an_edit():
    messages = format_result({}, [{
        'recognition': {'answer': 'x = 2'},
        'preliminary': {'status': 'correct', 'explanation': 'Ответ верен.', 'expected_answer': '2'},
        'teacher': {'answer': 'x = 21'}, 'review_stale': True,
    }])
    text = '\n'.join(messages)
    assert 'Нужна повторная проверка' in text
    assert 'Ответ верен.' not in text
    assert '<b>Ваш ответ</b>\nx = 21' in text
    assert '<b>Первоначальное распознавание</b>\nx = 2' in text


def test_rich_result_preserves_delimited_latex_and_escapes_user_text():
    task = {'recognition': {'answer': r'Решение: $x^2 + \frac{1}{2}$ и \(\sqrt{x}\) <tag>'},
            'preliminary': {'status': 'incorrect', 'expected_answer': r'$$\frac{3}{4}$$',
                            'expected_answer_source': 'teacher_test',
                            'explanation': r'Получается \frac{x_{1}}{2}. Также x_1 \cdot x_2 = 3.'}}
    text = '\n'.join(format_result({}, [task], rich=True))
    assert r'<tg-math>x^2 + \frac{1}{2}</tg-math>' in text
    assert r'<tg-math>\sqrt{x}</tg-math>' in text
    assert r'<tg-math>\frac{3}{4}</tg-math>' in text
    assert r'<tg-math>\frac{x_{1}}{2}</tg-math>' in text
    assert '<tg-math>x_1</tg-math> · <tg-math>x_2</tg-math>' in text
    assert '&lt;tag&gt;' in text
    assert '<tag>' not in text


def test_rich_formulas_are_not_split_at_message_boundary_and_fallback_is_safe():
    answer = ('а' * 2950) + r' $\frac{1}{2}$ ' + ('б' * 3000)
    messages = format_result({}, [{'recognition': {'answer': answer}, 'preliminary': {}}], rich=True)
    assert all(len(message) <= 3500 for message in messages)
    assert sum(message.count('<tg-math>') for message in messages) == 1
    assert all(message.count('<tg-math>') == message.count('</tg-math>') for message in messages)
    fallback = '\n'.join(rich_to_plain(message) for message in messages)
    assert '<tg-math>' not in fallback
    assert '(1)/(2)' in fallback
    assert fallback.count('а') >= 2950
    assert fallback.count('б') >= 3000
