from app.telegram_notifier import format_result


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
