from threading import BoundedSemaphore, Lock
from typing import Annotated

from fastapi import APIRouter, Depends, File, Request, UploadFile

from app.accounts import current_user
from app.ai import AIError, PageInput
from app.core import ApiError
from app.files import JPEG, PDF, prepare_uploads

router = APIRouter(prefix='/question-import', tags=['Работы'])
_slots = BoundedSemaphore(2)
_lock = Lock()
_active_users: set[str] = set()


@router.post('')
def import_questions(request: Request, user_id: Annotated[str, Depends(current_user)],
                     file: Annotated[UploadFile, File()]):
    with _lock:
        if user_id in _active_users:
            raise ApiError(409, 'import_in_progress', 'Распознавание уже выполняется. Дождитесь результата.')
        if not _slots.acquire(blocking=False):
            raise ApiError(503, 'import_busy', 'Распознавание занято. Попробуйте позже.')
        _active_users.add(user_id)
    try:
        prepared, = prepare_uploads([file], request.app.state.settings, max_pages=1)
        if prepared.mime_type == PDF:
            raise ApiError(415, 'image_required', 'Выберите фотографию задания в формате JPG, PNG или HEIC.')
        provider = request.app.state.ai_provider
        if not hasattr(provider, 'extract_questions'):
            raise ApiError(503, 'import_unavailable', 'Распознавание заданий временно недоступно.')
        try:
            questions = provider.extract_questions(PageInput(1, prepared.pages[0], JPEG))
        except AIError as error:
            if error.code == 'ai_unavailable':
                raise ApiError(503, error.code, 'Не удалось получить ответ модели. Повторная попытка отправит новый платный запрос.') from error
            raise ApiError(502, error.code, 'Не удалось распознать задания. Проверьте фотографию и попробуйте снова.') from error
        return {'questions': [{'question': question} for question in questions],
                'demonstration': provider.demonstration}
    finally:
        with _lock:
            _active_users.discard(user_id)
            _slots.release()
