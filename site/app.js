/* Сухой остаток — экраны «Скан», «Листы», «Проверка» и «Итог». */
(function () {
  const { icon, api } = window.SK;
  const $ = selector => document.querySelector(selector);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, char => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
  })[char]);
  function mathText(value) {
    const text = String(value ?? '');
    const tokens = /(\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|\\\([\s\S]+?\\\)|\$(?!\s)[^$\n]+?\$|\\(?:d?frac)\s*\{[^{}]*\}\s*\{[^{}]*\}|\\sqrt\s*\{[^{}]*\})/g;
    const plain = part => esc(part)
      .replace(/\\(?:cdot|times|div|pm|mp|leq|geq|neq|approx|infty)\b/g, token => ({
        '\\cdot': '·', '\\times': '×', '\\div': '÷', '\\pm': '±', '\\mp': '∓',
        '\\leq': '≤', '\\geq': '≥', '\\neq': '≠', '\\approx': '≈', '\\infty': '∞',
      })[token])
      .replace(/\\?\bsqrt\s*(?:\{([^{}]+)\}|\(([^()]+)\)|\s+(\d+(?:[.,]\d+)?))/gi,
        (_, braced, parenthesized, number) => `√<span class="radicand">${braced || parenthesized || number}</span>`)
      .replace(/\^\{([\d+−-]+)\}|\^([\d+−-]+)/g, (_, braced, simple) => `<sup>${braced || simple}</sup>`);
    let result = '', last = 0, match;
    while ((match = tokens.exec(text))) {
      result += plain(text.slice(last, match.index));
      const token = match[0];
      const delimited = token.startsWith('$') || token.startsWith('\\(') || token.startsWith('\\[');
      const display = token.startsWith('$$') || token.startsWith('\\[');
      const tex = delimited ? token.slice(display || token.startsWith('\\(') ? 2 : 1, -(display || token.startsWith('\\(') ? 2 : 1)) : token;
      try { result += window.katex.renderToString(tex, { throwOnError: true, trust: false, displayMode: display }); }
      catch { result += plain(token); }
      last = tokens.lastIndex;
    }
    return result + plain(text.slice(last));
  }

  const FILE_TYPES = 'image/jpeg,image/png,application/pdf,.jpg,.jpeg,.png,.pdf';
  const PAGE_SIZE = 20;
  const STATUS = { queued: 'В очереди', processing: 'Проверяется', completed: 'Готово', failed: 'Ошибка проверки' };
  const STAGE = {
    queued: 'Ожидаем запуска', preparing_pages: 'Готовим страницы', recognition: 'Распознаём почерк',
    preparing_review: 'Применяем исправления', preliminary_review: 'Проверяем ответы',
    validation: 'Проверяем результат', saving_results: 'Сохраняем результат', completed: 'Готово',
  };
  const REVIEWS = {
    correct: ['Верно', 'ok'], incorrect: ['Ошибка', 'terra'],
    partially_correct: ['Частично верно', 'warn'], needs_review: ['Нужна проверка', 'warn'],
    no_answer: ['Нет ответа', 'linen'], unreadable: ['Неразборчиво', 'linen'],
  };
  const dateFormat = new Intl.DateTimeFormat('ru-RU', { dateStyle: 'short', timeStyle: 'short' });

  const state = {
    screen: 'auth', authMode: 'login', authEmail: '', authError: '', inviteRequired: false,
    files: [], uploadError: '', pending: false,
    subscription: null, subscriptionError: '',
    works: [], worksOffset: 0, worksLoaded: false, worksLoading: false, worksError: '', hasMore: false,
    work: null, results: null, filter: 'all', checkError: '',
    deleteOpen: false, modal: null,
  };
  let screenController;
  let pollTimer;
  let disposeBilling;
  let billingBack = 'scan';
  let subscriptionEpoch = 0;
  let focusBeforeModal;
  let workspace = { classes: [], tests: [], links: {} };
  let activeClass = null;
  let activeTest = '';
  let editingTest = '';
  let addingWorkspaceItem = '';
  let workspaceError = '';
  const workspaceKey = () => `stopkadr.workspace.${api.getSession()?.user_id || ''}`;
  function saveWorkspace() { localStorage.setItem(workspaceKey(), JSON.stringify(workspace)); }
  function loadWorkspace() {
    try {
      const value = JSON.parse(localStorage.getItem(workspaceKey()) || '{}');
      workspace = { classes: Array.isArray(value.classes) ? value.classes : [], tests: Array.isArray(value.tests) ? value.tests : [], links: value.links || {} };
    } catch { workspace = { classes: [], tests: [], links: {} }; }
    activeClass = null;
    activeTest = '';
  }
  const newId = () => `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;

  function workspaceForm(kind) {
    if (addingWorkspaceItem !== kind) return '';
    return `<form id="workspace-form" class="card card-pad stack" style="margin-top:10px">
      <label class="form-field">${kind === 'class' ? 'Название класса' : 'Название работы'}
        <input name="name" maxlength="100" required autocomplete="off" placeholder="${kind === 'class' ? 'Например, 5 А' : 'Например, Дроби'}">
      </label>
      ${workspaceError ? `<p class="form-error" role="alert">${esc(workspaceError)}</p>` : ''}
      <button type="submit" class="btn-primary btn-block">Сохранить</button>
      ${btn('cancel-workspace', 'Отмена', 'btn-ghost btn-block')}
    </form>`;
  }
  function addWorkspaceItem(form) {
    const name = form.elements.name.value.trim();
    if (!name) { form.elements.name.focus(); return; }
    const list = addingWorkspaceItem === 'class' ? workspace.classes : workspace.tests;
    list.push({ id: newId(), name });
    try { saveWorkspace(); }
    catch {
      list.pop();
      workspaceError = 'Не удалось сохранить данные в браузере. Проверьте настройки хранения.';
      render();
      return;
    }
    addingWorkspaceItem = '';
    workspaceError = '';
    render();
  }

  function viewHome() {
    return `<div style="padding:6px 2px 16px"><h1 class="page-title">Кабинет учителя</h1>
      <p class="page-sub">Классы и работы в одном месте.</p></div>
      <div class="between"><h2>Классы</h2>${btn('add-class', 'Добавить класс', 'btn-ghost btn-sm')}</div>
      ${workspaceForm('class')}
      ${workspace.classes.length ? workspace.classes.map(item => `<button type="button" class="card link-row" data-act="open-class" data-id="${esc(item.id)}" style="margin-top:10px"><span class="ico">${icon('list', 19)}</span><span class="who"><b>${esc(item.name)}</b><span>${Object.values(workspace.links).filter(link => link.classId === item.id).length} работ</span></span><span class="chev">${icon('chevRight', 18)}</span></button>`).join('') : '<p class="page-sub">Добавьте класс, чтобы собрать работы учеников.</p>'}
      <div class="between" style="margin-top:26px"><h2>Работы</h2>${btn('add-test', 'Добавить работу', 'btn-ghost btn-sm')}</div>
      ${workspaceForm('test')}
      ${workspace.tests.length ? workspace.tests.map(item => `<button type="button" class="card link-row" data-act="edit-test" data-id="${esc(item.id)}" style="margin-top:8px"><span class="who"><b>${esc(item.name)}</b><span>Заданий: ${Array.isArray(item.questions) ? item.questions.length : 0}</span></span><span class="chev">${icon('chevRight', 18)}</span></button>`).join('') : '<p class="page-sub">Добавьте работу с заданиями и выберите её при загрузке листов.</p>'}
      <p class="page-sub" style="margin-top:20px">Классы и задания сохраняются в этом браузере.</p>`;
  }
  function viewTest() {
    const test = workspace.tests.find(item => item.id === editingTest);
    if (!test) return `<p>Работа не найдена.</p>${btn('home', 'На главную', 'btn-ghost')}`;
    const questions = Array.isArray(test.questions) ? test.questions : [];
    return `${back('home', test.name, 'Задания и правильные ответы')}
      ${questions.map((q, i) => `<div class="card card-pad" style="margin-bottom:10px"><b>Задание ${i + 1}</b><p style="white-space:pre-wrap">${mathText(q.question)}</p><p class="page-sub">Правильный ответ: ${mathText(q.answer)}</p><button type="button" class="btn-quiet" data-act="remove-question" data-index="${i}">Удалить</button></div>`).join('')}
      <form id="question-form" class="card card-pad stack">
        <h2>Новое задание</h2>
        <label class="form-field">Условие<textarea name="question" maxlength="8000" required rows="3"></textarea></label>
        <label class="form-field">Правильный ответ<textarea name="answer" maxlength="3000" required rows="2"></textarea></label>
        ${workspaceError ? `<p class="form-error" role="alert">${esc(workspaceError)}</p>` : ''}
        <button type="submit" class="btn-primary btn-block">Добавить задание</button>
      </form>`;
  }
  function saveQuestion(form) {
    const test = workspace.tests.find(item => item.id === editingTest);
    const question = form.elements.question.value.trim();
    const answer = form.elements.answer.value.trim();
    if (!test || !question || !answer) return;
    const previous = test.questions;
    test.questions = [...(Array.isArray(previous) ? previous : []), { question, answer }];
    try { saveWorkspace(); } catch {
      test.questions = previous;
      workspaceError = 'Не удалось сохранить задание в браузере.';
      toast(workspaceError);
      return;
    }
    workspaceError = '';
    render();
  }
  const selectedPreviews = new Map();
  const workPreviews = new Map();

  function clearPreviews(cache) {
    for (const url of cache.values()) URL.revokeObjectURL(url);
    cache.clear();
  }

  function selectedPreview(file) {
    if (!file.type.startsWith('image/')) return '';
    if (!selectedPreviews.has(file)) selectedPreviews.set(file, URL.createObjectURL(file));
    return selectedPreviews.get(file);
  }

  async function loadWorkPreviews() {
    const session = api.getSession();
    for (const image of document.querySelectorAll('[data-work-preview]')) {
      const id = image.dataset.workPreview;
      if (!workPreviews.has(id)) {
        try {
          const blob = await api.request(`/v1/submissions/${encodeURIComponent(id)}/pages/1`, { response: 'blob' });
          if (session !== api.getSession()) return;
          if (!workPreviews.has(id)) workPreviews.set(id, URL.createObjectURL(blob));
        } catch { continue; }
      }
      if (image.isConnected) image.src = workPreviews.get(id);
    }
  }

  const workPath = work => `/v1/submissions/${encodeURIComponent(work.id)}`;
  const taskPath = task => `${workPath(state.work)}/tasks/${encodeURIComponent(task.id)}`;
  const stageName = stage => STAGE[stage] || 'Обрабатываем работу';
  const displayDate = iso => Number.isFinite(Date.parse(iso)) ? dateFormat.format(new Date(iso)) : '';
  const workName = work => {
    const created = displayDate(work.created_at);
    return work.student_reference || (created ? `Работа от ${created}` : 'Работа');
  };
  const activeWork = work => work && ['queued', 'processing'].includes(work.status);
  const reviewStatus = task => task.confirmed ? task.teacher?.final_status || task.preliminary.status : task.preliminary.status;
  const reviewLabel = status => REVIEWS[status] || ['Нужна проверка', 'warn'];
  const chosenTask = () => state.results?.tasks.find(task => task.id === state.modal?.taskId);
  const taskName = task => task.recognition.number ? `Задание № ${task.recognition.number}` : `Задание · страница ${task.source_page}`;
  const followupLabel = () => state.subscription?.allow_followups === false ? 'Уточнения и перепроверка доступны по действующему тарифу.' : '';
  const errorText = error => error?.message || 'Не удалось выполнить действие. Попробуйте ещё раз.';
  const alert = (message, id = '') => `<p ${id ? `id="${id}"` : ''} class="form-error" role="alert"${message ? '' : ' hidden'}>${esc(message)}</p>`;
  const empty = text => `<p class="empty">${esc(text)}</p>`;
  const btn = (act, label, cls = 'btn-ghost', glyph = '', extra = '') =>
    `<button type="button" class="btn ${cls}" data-act="${act}" ${extra}>${glyph ? icon(glyph, 19) : ''} ${esc(label)}</button>`;
  const back = (screen, title, subtitle = '') => `
    <div class="screen-head">
      <button type="button" class="iconbtn" data-act="screen" data-screen="${screen}" aria-label="Назад">${icon('chevLeft', 20)}</button>
      <div><h1 class="${screen === 'sheets' ? 'page-title' : 'section-title'}">${esc(title)}</h1>${subtitle ? `<div class="muted small">${esc(subtitle)}</div>` : ''}</div>
    </div>`;

  function toast(message, failure = false) {
    const item = document.createElement('div');
    item.className = `toast${failure ? ' terra' : ''}`;
    item.setAttribute('role', failure ? 'alert' : 'status');
    item.innerHTML = icon(failure ? 'alert' : 'checkCircle', 18) + `<span>${esc(message)}</span>`;
    $('#toasts').append(item);
    setTimeout(() => item.remove(), 3000);
  }

  function showError(id, message) {
    const element = document.getElementById(id);
    if (element) {
      element.textContent = message;
      element.hidden = !message;
    }
  }

  function subscriptionBlock() {
    const sub = state.subscription;
    if (!sub) return '';
    if (!['active', 'past_due'].includes(sub.status) ||
        Date.parse(sub.period_start) > Date.now() || !(Date.parse(sub.period_end) > Date.now())) {
      return 'Для проверки нужна действующая подписка.';
    }
    if (sub.checks_remaining <= 0) return 'Лимит проверок на текущий период исчерпан.';
    return '';
  }

  function renderActionbar() {
    const bar = $('#actionbar');
    bar.hidden = !['scan', 'result'].includes(state.screen);
    if (bar.hidden) {
      bar.replaceChildren();
    } else if (state.screen === 'scan') {
      const block = subscriptionBlock();
      bar.innerHTML = `
        ${btn('start-check', state.pending ? 'Загружаем работу…' : 'Проверить работу', 'btn-primary btn-block', 'check',
          !state.files.length || state.pending || block ? 'disabled' : '')}
        <div class="hint">${state.files.length ? `Выбрано файлов: ${state.files.length}` : 'Добавьте страницы одной работы'}</div>`;
    } else {
      bar.innerHTML = btn('again', 'Проверить новую работу', 'btn-primary btn-block', 'camera', state.pending ? 'disabled' : '');
    }
  }

  function viewAuth() {
    const signup = state.authMode === 'register';
    return `
      <div style="padding:6px 2px 16px">
        <h1 class="page-title">${signup ? 'Регистрация учителя' : 'Вход для учителя'}</h1>
        <p class="page-sub">Войдите, чтобы загрузить и проверить работу ученика.</p>
      </div>
      <form id="auth-form" class="card card-pad stack">
        <label class="form-field">Электронная почта
          <input name="email" type="email" autocomplete="username" maxlength="254" required value="${esc(state.authEmail)}">
        </label>
        <label class="form-field">Пароль
          <input name="password" type="password" autocomplete="${signup ? 'new-password' : 'current-password'}" minlength="12" maxlength="256" required>
        </label>
        ${signup ? `<label id="invite-field" class="form-field" ${state.inviteRequired ? '' : 'hidden'}>Код приглашения
          <input name="invite_code" type="password" autocomplete="off" maxlength="256" ${state.inviteRequired ? 'required' : 'disabled'}>
        </label>` : ''}
        ${signup ? '<p class="muted small">Используйте не менее 12 символов.</p>' : ''}
        ${alert(state.authError, 'authError')}
        <button class="btn btn-primary btn-block" type="submit">${signup ? 'Зарегистрироваться' : 'Войти'}</button>
        ${btn('auth-mode', signup ? 'Уже есть аккаунт' : 'Создать аккаунт', 'btn-quiet btn-block')}
      </form>`;
  }

  function viewScan() {
    const block = subscriptionBlock();
    const sub = state.subscription;
    const countText = state.worksLoaded ? `${state.hasMore ? 'Показано работ' : 'Загружено работ'}: ${state.works.length}` :
      state.worksError ? 'Откройте список, чтобы обновить' : 'Ваши загруженные работы';
    const chosen = state.files.map((file, index) => `
      <li class="file-row between">
        <span class="selected-file">${selectedPreview(file) ? `<img src="${esc(selectedPreview(file))}" alt="">` : icon('doc', 24)} <span>${esc(file.name)}</span></span>
        <button type="button" class="iconbtn" data-act="remove-file" data-index="${index}"
          aria-label="Убрать файл ${esc(file.name)}" ${state.pending ? 'disabled' : ''}>${icon('x', 17)}</button>
      </li>`).join('');
    return `
      <div style="padding:6px 2px 16px">
        <button type="button" class="btn-quiet" data-act="home">← Все классы</button>
        <h1 class="page-title">${esc(workspace.classes.find(item => item.id === activeClass)?.name || 'Работы')}</h1>
        <p class="page-sub">Сфотографируйте листы одной работы — соберём ответы и найдём ошибки.</p>
      </div>
      <label class="form-field" style="margin-bottom:14px">Работа
        <select id="work-test"><option value="">Без выбранной работы</option>${workspace.tests.map(item => `<option value="${esc(item.id)}" ${activeTest === item.id ? 'selected' : ''}>${esc(item.name)}</option>`).join('')}</select>
      </label>
      <div class="capture">
        <div class="cam">${icon('camera', 28)}</div>
        <div class="cap-title">Сфотографируйте лист</div>
        <div class="cap-hint">Целиком, при ровном свете · JPG, PNG или PDF</div>
        <div class="cap-hint">Имя на снимке будет передано Gemini для распознавания. Проверьте подпись после обработки.</div>
        <div class="cap-actions">
          ${btn('capture', 'Сканировать лист', 'btn-primary', 'camera', state.pending ? 'disabled' : '')}
          ${btn('gallery', 'Выбрать из галереи', 'btn-ghost', 'image', state.pending ? 'disabled' : '')}
        </div>
      </div>
      ${chosen ? `<ul class="card card-pad file-list" aria-label="Выбранные файлы" style="margin-top:12px">${chosen}</ul>` : ''}
      ${alert(state.uploadError)}
      <button type="button" class="card link-row" data-act="screen" data-screen="sheets" style="margin-top:14px">
        <span class="ico">${icon('list', 19)}</span>
        <span class="who"><b>Листы класса</b><span>${countText}</span></span>
        <span class="chev">${icon('chevRight', 18)}</span>
      </button>
      ${block || state.subscriptionError ? `<div class="callout blue" style="margin-top:12px">
        <p>${esc(block || state.subscriptionError)}</p>
        ${btn('billing', 'Открыть подписку', 'btn-quiet btn-block')}
      </div>` : sub ? `<p class="page-sub" style="margin:12px 2px">Осталось проверок: ${esc(sub.checks_remaining)} · до ${esc(sub.max_pages)} стр. в работе</p>` : ''}`;
  }

  function viewSheets() {
    const cards = state.works.filter(work => workspace.links[work.id]?.classId === activeClass).map(work => `
      <button type="button" class="sheet-card" data-act="open-sheet" data-id="${esc(work.id)}">
        <div class="sheet sheet-placeholder"><img data-work-preview="${esc(work.id)}" alt="Страница работы" loading="lazy"></div>
        <div class="meta">
          <div class="nm">${esc(workName(work))}</div><div class="st">${esc(workspace.tests.find(item => item.id === workspace.links[work.id]?.testId)?.name || 'Без выбранной работы')}</div>
          <div class="st ${work.status === 'failed' ? 'err' : work.status === 'completed' ? 'ok' : ''}">
            ${icon(work.status === 'failed' ? 'alert' : work.status === 'completed' ? 'check' : 'clock', 13)}
            ${esc(STATUS[work.status] || '')} · ${esc(work.page_count)} стр.
          </div>
        </div>
      </button>`).join('');
    return `${back('scan', 'Листы класса', state.worksLoaded ? `${state.hasMore ? 'Показано' : 'Загружено'} работ: ${state.works.length}` : '')}
      ${cards ? `<div class="sheet-grid">${cards}</div>` : state.worksLoaded ? empty('Здесь появятся ваши загруженные работы.') : ''}
      ${state.worksLoading ? '<p class="empty" role="status">Загружаем список работ…</p>' : ''}
      ${alert(state.worksError)}
      ${state.worksError ? btn('reload-works', 'Обновить список', 'btn-ghost btn-block') :
        state.hasMore ? btn('more-works', 'Показать ещё', 'btn-ghost btn-block', '', state.worksLoading ? 'disabled' : '') : ''}`;
  }

  function deleteControls() {
    if (state.deleteOpen) return `
      <div class="callout terra stack-sm" style="margin-top:12px">
        <p>Удалить работу, страницы и результаты?</p>
        ${btn('delete-confirm', state.pending ? 'Удаляем…' : 'Удалить работу', 'btn-primary btn-block', 'trash', state.pending ? 'disabled' : '')}
        ${btn('delete-cancel', 'Оставить работу', 'btn-ghost btn-block', '', state.pending ? 'disabled' : '')}
      </div>`;
    return btn('delete', 'Удалить работу', 'btn-quiet btn-block', 'trash');
  }

  function viewCheck() {
    const work = state.work;
    if (!work) return back('sheets', 'Проверка работы');
    const failed = work.status === 'failed';
    const busy = activeWork(work) && !state.checkError;
    return `
      ${back('sheets', 'Проверка работы', `${workName(work)} · ${work.page_count} стр.`)}
      <div class="card card-pad" aria-live="polite">
        <div class="ringwrap">
          <div class="stage-spinner${busy ? ' spin' : ''}" aria-hidden="true">
            ${icon(failed || state.checkError ? 'alert' : work.status === 'completed' ? 'checkCircle' : 'clock', 48)}
          </div>
        </div>
        <div style="text-align:center;margin-top:8px">
          <div class="section-title">${esc(STATUS[work.status] || 'Проверка работы')}</div>
          <p class="muted small">${work.status === 'completed' ? 'Загружаем результаты' : 'Текущий этап'}</p>
        </div>
        <hr class="hr">
        <div class="stage">
          <span class="stage-badge ${failed ? '' : 'active'}">${icon(failed ? 'alert' : 'scan', 17)}</span>
          <div class="stage-body"><div class="stage-title">${esc(stageName(work.stage))}</div></div>
        </div>
        ${alert(state.checkError || (failed ? work.error_message || 'Обработка завершилась ошибкой.' : ''))}
      </div>
      <div class="stack-sm" style="margin-top:12px">
        ${state.checkError ? btn('refresh-work', 'Обновить статус', 'btn-primary btn-block') : ''}
        ${failed ? btn('retry', state.pending ? 'Отправляем…' : 'Запустить снова', 'btn-primary btn-block', '', state.pending ? 'disabled' : '') : ''}
        ${failed && state.results ? btn('old-results', 'Открыть предыдущий результат', 'btn-ghost btn-block') : ''}
        ${work.page_count > 0 ? btn('preview', 'Открыть исходную страницу', 'btn-ghost btn-block', 'image') : ''}
        ${btn('screen', 'К списку работ', 'btn-quiet btn-block', '', 'data-screen="sheets"')}
        ${failed ? deleteControls() : ''}
      </div>`;
  }

  function viewResult() {
    const work = state.work;
    const result = state.results;
    const tasks = result.tasks;
    const confirmed = tasks.filter(task => task.confirmed).length;
    const needsAttention = tasks.filter(task => reviewStatus(task) !== 'correct').length;
    const shown = tasks.filter(task => state.filter === 'all' ||
      (state.filter === 'clean' ? reviewStatus(task) === 'correct' : reviewStatus(task) !== 'correct'));
    const rows = shown.map(task => {
      const [label, color] = reviewLabel(reviewStatus(task));
      return `<button type="button" class="sheet-item" data-act="open-task" data-id="${esc(task.id)}">
        <span class="ava">${icon(task.confirmed ? 'checkCircle' : 'clipboard', 17)}</span>
        <span class="who"><b>${esc(taskName(task))}</b><span>Стр. ${esc(task.source_page)} · ${task.confirmed ? 'Подтверждено' : 'Предварительно'}</span></span>
        <span class="chip ${color} task-status">${esc(label)}</span>
        <span class="chev">${icon('chevRight', 18)}</span>
      </button>`;
    }).join('');
    return `
      ${back('sheets', work.status === 'failed' ? 'Предыдущий результат' : 'Проверка готова', workName(work))}
      <div class="stat-grid">
        <div class="stat"><div class="n">${esc(work.page_count)}</div><div class="l">страниц</div></div>
        <div class="stat"><div class="n">${tasks.length}</div><div class="l">заданий</div></div>
        <div class="stat"><div class="n terra">${needsAttention}</div><div class="l">требуют внимания</div></div>
        <div class="stat"><div class="n blue">${confirmed}</div><div class="l">подтверждено</div></div>
      </div>
      ${result.demonstration ? `<div class="callout blue" style="margin-top:12px">
        <div class="eyebrow">Демонстрационный результат</div><p>Часть данных создана тестовым ИИ и не отражает содержимое вашей работы.</p>
      </div>` : ''}
      <div class="between" style="margin:20px 2px 10px">
        <span class="section-title">Задания</span><span class="muted small">${shown.length} из ${tasks.length}</span>
      </div>
      ${tasks.length ? `<div class="chiprow" style="margin-bottom:6px">
        ${[['all', 'Все'], ['issues', 'Требуют внимания'], ['clean', 'Верные']].map(([key, label]) =>
          `<button type="button" class="fchip${state.filter === key ? ' on' : ''}" data-act="filter" data-filter="${key}"
            aria-pressed="${state.filter === key}">${label}</button>`).join('')}
      </div>` : ''}
      <div class="card" style="padding:4px 14px">
        <div class="sheet-list">${rows || empty(tasks.length ? 'По этому фильтру нет заданий.' : 'Задания не найдены. Проверьте исходную страницу или добавьте уточнение.')}</div>
      </div>
      <div class="stack-sm" style="margin-top:12px">
        ${btn('preview', 'Открыть исходную страницу', 'btn-ghost btn-block', 'image')}
        ${btn('clarify', 'Добавить уточнение или страницу', 'btn-ghost btn-block', 'plus')}
        ${deleteControls()}
      </div>`;
  }

  function previewContent() {
    const modal = state.modal;
    if (modal.previewError) return `${alert(modal.previewError)}
      ${btn('refresh-preview', 'Открыть страницу снова', 'btn-ghost btn-sm')}`;
    if (!modal.objectUrl) return '<p class="empty" role="status">Загружаем страницу…</p>';
    return `<img class="page-image" alt="Страница ${modal.page} работы" src="${esc(modal.objectUrl)}">`;
  }

  function previewShell() {
    const { page, objectUrl } = state.modal;
    const total = state.work.page_count;
    return `
      <div class="viewer" style="margin-bottom:14px">
        <div class="sheet" id="pagePreview">${previewContent()}</div>
        <div class="between" style="margin-top:10px">
          <span class="muted small">Страница ${page} из ${total}</span>
          <div class="chiprow">
            ${total > 1 ? `<button type="button" class="iconbtn" data-act="preview-page" data-step="-1" aria-label="Предыдущая страница"
              ${page <= 1 ? 'disabled' : ''}>${icon('chevLeft', 18)}</button>
            <button type="button" class="iconbtn" data-act="preview-page" data-step="1" aria-label="Следующая страница"
              ${page >= total ? 'disabled' : ''}>${icon('chevRight', 18)}</button>` : ''}
            <button type="button" class="iconbtn" id="rotatePage" data-act="rotate" aria-label="Повернуть страницу"
              ${!objectUrl ? 'disabled' : ''}>${icon('rotate', 18)}</button>
          </div>
        </div>
      </div>`;
  }

  function textDetail(label, content) {
    return `<div class="task-detail">
      <div class="eyebrow">${esc(label)}</div><p>${mathText(content || '—')}</p>
    </div>`;
  }

  function detailContent(task) {
    const review = task.preliminary;
    const rec = task.recognition;
    const teacher = task.teacher || {};
    const editedAnswer = Object.hasOwn(teacher, 'answer');
    const answerColor = editedAnswer ? 'original' : review.status === 'incorrect' ? 'err' : 'ref';
    const [label, color] = reviewLabel(reviewStatus(task));
    const tabs = state.results.tasks.map(item => {
      const on = item.id === task.id;
      const status = reviewStatus(item);
      return `<button type="button" class="task-chip${on ? ' on' : ''}${on && status === 'incorrect' ? ' err-chip' : ''}"
        data-act="task" data-id="${esc(item.id)}" aria-pressed="${on}">${esc(taskName(item))}
        <span class="st ${status === 'correct' ? 'ok' : status === 'incorrect' ? 'err' : ''}">
          ${icon(status === 'correct' ? 'check' : status === 'incorrect' ? 'x' : 'alert', 15)}
        </span></button>`;
    }).join('');
    return `
      <div class="task-chips" aria-label="Задания" style="margin-bottom:16px">${tabs}</div>
      <div class="between" style="margin-bottom:12px">
        <span class="section-title">${esc(taskName(task))}</span><span class="chip ${color} task-status">${esc(label)}</span>
      </div>
      <div class="stack">
        <div class="chiprow">
          <span class="chip ${task.confirmed ? 'ok' : 'linen'}">${task.confirmed ? 'Подтверждено учителем' : 'Предварительный результат'}</span>
          ${task.demonstration ? '<span class="chip blue">Демонстрационные данные</span>' : ''}
        </div>
        ${textDetail('Распознанное условие', rec.condition || 'Условие не найдено')}
        <div class="compare">
          <div class="cell ${answerColor}"${review.expected_answer ? '' : ' style="grid-column:1/-1"'}>
            <div class="l">${editedAnswer ? 'Исходное распознавание ответа' : 'Ответ ученика'}</div><div class="v">${mathText(rec.answer || '—')}</div>
          </div>
          ${review.expected_answer ? `<div class="cell ref">
            <div class="l">${review.expected_answer_source === 'teacher_test' ? 'Ответ учителя в работе' : 'Предполагаемый верный ответ'}</div><div class="v">${mathText(review.expected_answer)}</div>
          </div>` : ''}
        </div>
        ${textDetail('Объяснение', review.explanation)}
        ${review.uncertainty ? textDetail('Причина неопределённости', review.uncertainty) : ''}
        ${review.missing_data?.length ? textDetail('Не хватает данных', review.missing_data.join(' · ')) : ''}
        ${Object.hasOwn(teacher, 'condition') ? textDetail('Условие от учителя', teacher.condition) : ''}
        ${editedAnswer ? textDetail('Ответ после исправления', teacher.answer) : ''}
        ${teacher.status ? textDetail('Оценка учителя', reviewLabel(teacher.status)[0]) : ''}
        ${teacher.explanation ? textDetail('Комментарий учителя', teacher.explanation) : ''}
        ${teacher.confirmed_at ? textDetail('Дата подтверждения', displayDate(teacher.confirmed_at)) : ''}
        <div class="stack-sm">
          ${btn('edit', 'Исправить результат', 'btn-ghost btn-block', 'pen')}
          ${btn('recheck', 'Перепроверить задание', 'btn-ghost btn-block', 'rotate')}
          ${followupLabel() ? `<p class="muted small">${followupLabel()}</p>` : ''}
        </div>
      </div>`;
  }

  function editContent(task) {
    const teacher = task.teacher || {};
    const condition = Object.hasOwn(teacher, 'condition') ? teacher.condition : task.recognition.condition;
    const answer = Object.hasOwn(teacher, 'answer') ? teacher.answer : task.recognition.answer;
    return `
      <form id="correction-form" class="stack">
        <label class="form-field">Условие<textarea name="condition" rows="3" maxlength="8000">${esc(condition)}</textarea></label>
        <label class="form-field">Ответ ученика<textarea name="answer" rows="2" maxlength="16000">${esc(answer)}</textarea></label>
        <label class="form-field">Оценка учителя<select name="status">
          <option value="">Оставить предварительную оценку</option>
          ${Object.entries(REVIEWS).map(([value, [label]]) =>
            `<option value="${value}"${teacher.status === value ? ' selected' : ''}>${label}</option>`).join('')}
        </select></label>
        <label class="form-field">Комментарий учителя<textarea name="explanation" rows="3" maxlength="4000">${esc(teacher.explanation)}</textarea></label>
        <p class="muted small">Изменения сохраняются отдельно. После исправления подтвердите результат заново.</p>
      </form>`;
  }

  function clarifyContent() {
    return `
      <form id="clarification-form" class="stack">
        <p class="page-sub">Добавьте условие, комментарий или недостающие страницы этой работы.</p>
        <label class="form-field">Текст уточнения<textarea name="text" rows="4" maxlength="4000"></textarea></label>
        <label class="form-field">Дополнительные файлы<input name="files" type="file" accept="${FILE_TYPES}" multiple></label>
        ${followupLabel() ? `<p class="muted small">${followupLabel()}</p>` : ''}
      </form>`;
  }

  function rotatePreview() {
    const image = $('#pagePreview img');
    if (!image || !state.modal) return;
    const angle = state.modal.angle;
    const quarter = angle % 180 !== 0;
    const size = Math.max(image.clientWidth, image.clientHeight);
    const scale = quarter && size ? Math.min(image.clientWidth, image.clientHeight) / size : 1;
    image.style.transform = `rotate(${angle}deg) scale(${scale})`;
  }

  function bindPreviewImage() {
    const image = $('#pagePreview img');
    if (!image) return;
    if (image.complete) rotatePreview();
    else image.addEventListener('load', rotatePreview, { once: true });
  }

  function renderModal({ top = false } = {}) {
    const modal = state.modal;
    if (!modal) return;
    const task = chosenTask();
    const body = $('#modalBody');
    const topBefore = body.scrollTop;
    let detail = '';
    let footer = btn('close-modal', 'Закрыть', 'btn-ghost btn-block');
    if (modal.mode === 'detail' && task) {
      detail = detailContent(task);
      footer = task.confirmed ? footer : btn('confirm', 'Подтвердить результат', 'btn-primary btn-block', 'check');
    } else if (modal.mode === 'edit' && task) {
      detail = editContent(task);
      footer = `<div class="stack-sm">
        <button type="submit" form="correction-form" class="btn btn-primary btn-block">Сохранить</button>
        ${btn('cancel-edit', 'К заданию', 'btn-quiet btn-block')}
      </div>`;
    } else if (modal.mode === 'clarify') {
      detail = clarifyContent();
      footer = `<button type="submit" form="clarification-form" class="btn btn-primary btn-block">Отправить уточнение</button>`;
    }
    $('#modalTitle').textContent = modal.mode === 'edit' ? 'Исправление результата' :
      modal.mode === 'clarify' ? 'Уточнение к работе' : workName(state.work);
    body.innerHTML = `${modal.mode === 'clarify' ? '' : previewShell()}${detail}${alert(modal.error, 'modalError')}`;
    $('#modalFoot').innerHTML = footer;
    body.scrollTop = top ? 0 : topBefore;
    bindPreviewImage();
  }

  function releasePreview(modal) {
    if (!modal) return;
    modal.previewController?.abort();
    modal.previewController = null;
    if (modal.objectUrl) URL.revokeObjectURL(modal.objectUrl);
    modal.objectUrl = null;
  }

  function updatePreview() {
    const panel = $('#pagePreview');
    if (!panel || !state.modal) return;
    panel.innerHTML = previewContent();
    $('#rotatePage').disabled = !state.modal.objectUrl;
    bindPreviewImage();
  }

  async function loadPreview() {
    const modal = state.modal;
    if (!modal || modal.mode === 'clarify') return;
    releasePreview(modal);
    modal.previewError = '';
    updatePreview();
    const controller = new AbortController();
    modal.previewController = controller;
    try {
      const task = chosenTask();
      const url = task?.source_page === modal.page ? task.source_url : `${workPath(state.work)}/pages/${modal.page}`;
      const blob = await api.request(url, { response: 'blob', signal: controller.signal });
      if (controller.signal.aborted || state.modal !== modal) return;
      modal.objectUrl = URL.createObjectURL(blob);
    } catch (error) {
      if (controller.signal.aborted || state.modal !== modal) return;
      modal.previewError = errorText(error);
    }
    updatePreview();
  }

  function openModal(mode, taskId = null) {
    if (!state.work) return;
    closeModal(false);
    focusBeforeModal = document.activeElement;
    const task = state.results?.tasks.find(item => item.id === taskId);
    state.modal = { mode, taskId, page: task?.source_page || 1, angle: 0, busy: false, error: '' };
    renderModal({ top: true });
    $('.app').inert = true;
    $('#modal').inert = false;
    $('#modal').removeAttribute('aria-hidden');
    $('#scrim').classList.add('open');
    $('#modal').classList.add('open');
    $('#modalClose').focus();
    loadPreview();
  }

  function closeModal(restoreFocus = true) {
    if (!state.modal) return;
    releasePreview(state.modal);
    $('#scrim').classList.remove('open');
    $('#modal').classList.remove('open');
    $('#modal').inert = true;
    $('#modal').setAttribute('aria-hidden', 'true');
    $('#modal').setAttribute('aria-busy', 'false');
    $('#modalClose').disabled = false;
    $('#modalBody').replaceChildren();
    $('#modalFoot').replaceChildren();
    $('.app').inert = false;
    state.modal = null;
    if (restoreFocus) {
      const source = focusBeforeModal;
      const replacement = source?.dataset.act && [...$('#view').querySelectorAll('[data-act]')].find(
        button => button.dataset.act === source.dataset.act && button.dataset.id === source.dataset.id,
      );
      (source?.isConnected ? source : replacement || $('#view button'))?.focus();
    }
    focusBeforeModal = null;
  }

  function selectTask(id) {
    const task = state.results?.tasks.find(item => item.id === id);
    if (!task) return;
    const modal = state.modal;
    if (modal.page !== task.source_page) releasePreview(modal);
    Object.assign(modal, { taskId: id, page: task.source_page, mode: 'detail', error: '', angle: 0 });
    renderModal({ top: true });
    $('#modalBody [aria-pressed=true]')?.focus();
    if (!modal.objectUrl) loadPreview();
  }

  const VIEWS = { auth: viewAuth, home: viewHome, test: viewTest, scan: viewScan, sheets: viewSheets, check: viewCheck, result: viewResult };

  function focusScreenHeading() {
    const title = $('#view h1');
    if (!title) return;
    title.tabIndex = -1;
    title.focus({ preventScroll: true });
  }

  function render() {
    const session = api.getSession();
    for (const action of ['billing', 'logout']) {
      const button = document.querySelector(`.appbar [data-act="${action}"]`);
      if (button) button.hidden = !session;
    }
    const view = $('#view');
    const restoreHeading = document.activeElement === view.querySelector('h1');
    view.dataset.screen = state.screen;
    if (state.screen !== 'billing') view.innerHTML = VIEWS[state.screen]();
    if (state.screen === 'sheets') void loadWorkPreviews();
    if (restoreHeading) focusScreenHeading();
    renderActionbar();
  }

  function stopScreen() {
    clearTimeout(pollTimer);
    screenController?.abort();
    disposeBilling?.();
    disposeBilling = null;
  }

  function setScreen(screen, { reopen = null } = {}) {
    stopScreen();
    closeModal(false);
    screenController = new AbortController();
    const signal = screenController.signal;
    state.screen = api.getSession() ? screen : 'auth';
    state.checkError = '';
    state.deleteOpen = false;
    state.worksLoading = false;
    render();
    $('#main').scrollTop = 0;
    if (state.screen !== 'billing') focusScreenHeading();

    if (state.screen === 'auth') {
      refreshAuthOptions(signal);
    } else if (state.screen === 'scan') {
      refreshSubscription();
      loadWorks(signal);
    } else if (state.screen === 'sheets') {
      loadWorks(signal);
    } else if (state.screen === 'check') {
      pollWork(signal, reopen);
    } else if (state.screen === 'billing') {
      disposeBilling = window.SK.billing.mount($('#view'), {
        onBack: () => setScreen(billingBack),
        onChange: subscription => {
          subscriptionEpoch++;
          state.subscription = subscription;
          state.subscriptionError = '';
        },
      });
    } else if (state.screen === 'result' && reopen && state.results.tasks.some(task => task.id === reopen)) {
      openModal('detail', reopen);
    }
  }

  async function refreshAuthOptions(signal) {
    try {
      const options = await api.request('/v1/auth/options', { auth: false, signal });
      if (signal.aborted) return;
      state.inviteRequired = options.invite_required === true;
      const field = $('#invite-field');
      if (field) {
        const input = field.querySelector('input');
        field.hidden = input.disabled = !state.inviteRequired;
        input.required = state.inviteRequired;
      }
    } catch {
      // Login remains available during an options request failure.
    }
  }

  async function refreshSubscription() {
    if (!api.getSession()) return;
    const epoch = ++subscriptionEpoch;
    try {
      const subscription = await api.request('/v1/subscription');
      if (epoch !== subscriptionEpoch) return;
      state.subscription = subscription;
      state.subscriptionError = '';
    } catch (error) {
      if (epoch !== subscriptionEpoch) return;
      state.subscriptionError = errorText(error);
    }
    if (state.screen === 'scan') render();
  }

  async function loadWorks(signal, append = false) {
    if (state.worksLoading) return;
    state.worksLoading = true;
    state.worksError = '';
    if (state.screen === 'sheets') render();
    const offset = append ? state.worksOffset : 0;
    try {
      const result = await api.request(`/v1/submissions?limit=${PAGE_SIZE}&offset=${offset}`, { signal });
      if (signal.aborted) return;
      const ids = new Set(append ? state.works.map(work => work.id) : []);
      state.works = [...(append ? state.works : []), ...result.items.filter(work => !ids.has(work.id))];
      state.worksOffset = offset + result.items.length;
      state.worksLoaded = true;
      state.hasMore = result.items.length === PAGE_SIZE;
    } catch (error) {
      if (!signal.aborted) state.worksError = errorText(error);
    } finally {
      if (!signal.aborted) {
        state.worksLoading = false;
        if (['scan', 'sheets'].includes(state.screen)) render();
      }
    }
  }

  async function pollWork(signal, reopen = null) {
    try {
      const before = state.work;
      const work = await api.request(workPath(before), { signal });
      if (signal.aborted) return;
      state.work = work;
      if (before.status !== work.status || before.stage !== work.stage || before.error_message !== work.error_message) render();
      if (activeWork(work)) {
        pollTimer = setTimeout(() => pollWork(signal, reopen), 1000);
      } else if (work.status === 'completed') {
        const result = await api.request(`${workPath(work)}/results`, { signal });
        if (signal.aborted) return;
        state.results = result;
        state.filter = 'all';
        refreshSubscription();
        setScreen('result', { reopen });
      } else if (work.status === 'failed') {
        refreshSubscription();
        if (!state.results) {
          try {
            const previous = await api.request(`${workPath(work)}/results`, { signal });
            if (signal.aborted) return;
            state.results = previous;
            render();
          } catch (error) {
            // A first attempt may fail before any result exists.
            if (!signal.aborted && error.status !== 409) throw error;
          }
        }
      }
    } catch (error) {
      if (signal.aborted) return;
      state.checkError = errorText(error);
      render();
    }
  }

  function checkNewWork(work, reopen = null) {
    if (state.work?.id !== work.id) state.results = null;
    state.work = work;
    setScreen('check', { reopen });
  }

  const fileKind = file => {
    if (file.type === 'application/pdf') return 'pdf';
    if (['image/jpeg', 'image/png'].includes(file.type)) return 'photo';
    if (file.type && file.type !== 'application/octet-stream') return '';
    if (/\.pdf$/i.test(file.name)) return 'pdf';
    if (/\.(?:jpe?g|png)$/i.test(file.name)) return 'photo';
    return '';
  };

  function invalidFiles(files, existingPages = 0) {
    if (files.some(file => !file.size || !fileKind(file))) return 'Выберите непустые файлы JPG, PNG или PDF.';
    if (files.some(file => fileKind(file) === 'pdf') && files.length > 1) {
      return 'Отправьте один PDF или несколько фотографий одной работы.';
    }
    const limit = state.subscription?.max_pages;
    if (limit && existingPages + files.length > limit) return `В тариф входит до ${limit} страниц на работу.`;
    return '';
  }

  function selectFiles(input) {
    if (state.screen !== 'scan' || state.pending) return;
    const files = [...state.files, ...input.files];
    input.value = '';
    const error = invalidFiles(files);
    state.uploadError = error;
    if (!error) state.files = files;
    render();
  }

  function fileBody(files, text = '') {
    const form = new FormData();
    if (text) form.append('text', text);
    for (const file of files) form.append('files', file);
    return form;
  }

  async function submitUpload() {
    if (!state.files.length || state.pending || subscriptionBlock()) return;
    state.uploadError = invalidFiles(state.files);
    if (state.uploadError) { render(); return; }
    const session = api.getSession();
    state.pending = true;
    render();
    try {
      const body = fileBody(state.files);
      const test = workspace.tests.find(item => item.id === activeTest);
      if (test) {
        if (!Array.isArray(test.questions) || !test.questions.length) {
          throw new Error('Добавьте задания и ответы в выбранную работу.');
        }
        const questions = JSON.stringify(test.questions);
        if (questions.length > 30000) throw new Error('Работа слишком большая для одной проверки.');
        body.append('test_questions', questions);
      }
      const work = await api.request('/v1/submissions', { method: 'POST', body });
      if (session !== api.getSession()) return;
      workspace.links[work.id] = { classId: activeClass, testId: activeTest };
      saveWorkspace();
      state.files = [];
      clearPreviews(selectedPreviews);
      checkNewWork(work);
      refreshSubscription();
    } catch (error) {
      if (session !== api.getSession()) return;
      state.uploadError = errorText(error);
      refreshSubscription();
    } finally {
      if (session === api.getSession()) {
        state.pending = false;
        render();
      }
    }
  }

  function modalBusy(modal, busy) {
    modal.busy = busy;
    $('#modal').setAttribute('aria-busy', String(busy));
    $('#modalClose').disabled = busy;
    const footer = $('#modalFoot');
    for (const button of footer.querySelectorAll('button')) {
      if (button.dataset.act !== 'close-modal') button.disabled = busy;
    }
    for (const button of $('#modalBody').querySelectorAll('[data-act=edit], [data-act=recheck]')) {
      button.disabled = busy;
    }
  }

  async function modalMutation(path, options, apply) {
    const modal = state.modal;
    if (!modal || modal.busy) return;
    modal.error = '';
    showError('modalError', '');
    modalBusy(modal, true);
    const session = api.getSession();
    state.pending = true;
    try {
      const result = await api.request(path, options);
      if (session !== api.getSession() || state.modal !== modal) return;
      apply(result);
    } catch (error) {
      if (session !== api.getSession() || state.modal !== modal) return;
      modal.error = errorText(error);
      showError('modalError', modal.error);
      const errorElement = $('#modalError');
      errorElement?.scrollIntoView({ block: 'nearest' });
    } finally {
      if (session === api.getSession()) {
        state.pending = false;
        renderActionbar();
      }
      if (state.modal === modal) {
        modalBusy(modal, false);
        if (document.activeElement === document.body) $('#modalClose').focus();
      }
    }
  }

  function updateTask(task) {
    const index = state.results.tasks.findIndex(item => item.id === task.id);
    if (index >= 0) state.results.tasks[index] = task;
    state.modal.mode = 'detail';
    render();
    renderModal();
  }

  function saveCorrection(form) {
    const task = chosenTask();
    if (!task) return;
    const fields = new FormData(form);
    const teacher = task.teacher || {};
    const defaults = {
      condition: Object.hasOwn(teacher, 'condition') ? teacher.condition : task.recognition.condition,
      answer: Object.hasOwn(teacher, 'answer') ? teacher.answer : task.recognition.answer,
      explanation: teacher.explanation,
      status: teacher.status,
    };
    const changes = {};
    for (const [key, before] of Object.entries(defaults)) {
      const after = fields.get(key).trim() || null;
      if (after !== (before || null)) changes[key] = after;
    }
    if (!Object.keys(changes).length) {
      showError('modalError', 'Укажите хотя бы одно исправление.');
      return;
    }
    modalMutation(taskPath(task), { method: 'PATCH', body: changes }, result => {
      updateTask(result);
      toast('Исправление сохранено.');
    });
  }

  function submitClarification(form) {
    const fields = new FormData(form);
    const text = fields.get('text').trim();
    const files = [...form.elements.files.files];
    const problem = !text && !files.length ? 'Добавьте уточнение или файл.' : invalidFiles(files, state.work.page_count);
    if (problem) { showError('modalError', problem); return; }
    const path = `${workPath(state.work)}/clarifications`;
    modalMutation(path, { method: 'POST', body: fileBody(files, text) }, checkNewWork);
  }

  async function submitAuth(form) {
    const button = form.querySelector('[type=submit]');
    if (button.disabled) return;
    button.disabled = true;
    const fields = new FormData(form);
    state.authEmail = fields.get('email').trim();
    state.authError = '';
    showError('authError', '');
    try {
      const credentials = { email: state.authEmail, password: fields.get('password') };
      if (fields.has('invite_code')) credentials.invite_code = fields.get('invite_code');
      const session = await api.request(`/v1/auth/${state.authMode}`, {
        method: 'POST', auth: false, body: credentials,
      });
      api.setSession(session);
    } catch (error) {
      state.authError = errorText(error);
      showError('authError', state.authError);
    } finally {
      button.disabled = false;
    }
  }

  async function retryWork() {
    if (state.pending) return;
    const session = api.getSession();
    state.pending = true;
    state.checkError = '';
    render();
    try {
      const work = await api.request(`${workPath(state.work)}/retry`, { method: 'POST' });
      if (session === api.getSession()) checkNewWork(work);
    } catch (error) {
      if (session === api.getSession()) state.checkError = errorText(error);
    } finally {
      if (session === api.getSession()) { state.pending = false; render(); }
    }
  }

  async function removeWork() {
    const target = state.work;
    if (state.pending || !target) return;
    const session = api.getSession();
    state.pending = true;
    render();
    let removed = false;
    try {
      await api.request(workPath(target), { method: 'DELETE' });
      if (session !== api.getSession()) return;
      removed = true;
      toast('Работа удалена.');
    } catch (error) {
      if (session !== api.getSession()) return;
      removed = error.code === 'deletion_pending' || error.status === 404;
      toast(errorText(error), true);
    } finally {
      if (session === api.getSession()) {
        state.pending = false;
        if (removed) {
          state.works = state.works.filter(work => work.id !== target.id);
          refreshSubscription();
          if (state.work?.id === target.id) {
            state.work = state.results = null;
            setScreen('sheets');
          }
        } else {
          render();
        }
      }
    }
  }

  const actions = {
    home: () => setScreen('home'),
    'add-class': () => { addingWorkspaceItem = 'class'; workspaceError = ''; render(); $('#workspace-form input')?.focus(); },
    'add-test': () => { addingWorkspaceItem = 'test'; workspaceError = ''; render(); $('#workspace-form input')?.focus(); },
    'cancel-workspace': () => { addingWorkspaceItem = ''; workspaceError = ''; render(); },
    'open-class': element => { activeClass = element.dataset.id; activeTest = ''; setScreen('scan'); },
    'edit-test': element => { editingTest = element.dataset.id; workspaceError = ''; setScreen('test'); },
    'remove-question': element => {
      const test = workspace.tests.find(item => item.id === editingTest);
      const index = Number(element.dataset.index);
      if (!test || !Array.isArray(test.questions) || !Number.isInteger(index) || index < 0 || index >= test.questions.length) return;
      const previous = test.questions;
      test.questions = previous.filter((_, i) => i !== index);
      try { saveWorkspace(); } catch { test.questions = previous; toast('Не удалось сохранить изменения.'); return; }
      render();
    },
    screen: element => setScreen(element.dataset.screen),
    billing: () => {
      if (state.screen === 'billing') return;
      billingBack = state.screen;
      setScreen('billing');
    },
    logout: () => api.clearSession(),
    help: () => toast('Загрузите фотографии или один PDF одной работы. Проверьте вывод и подтвердите результат.'),
    'auth-mode': () => {
      const form = $('#auth-form');
      if (form.querySelector('[type=submit]').disabled) return;
      state.authEmail = form.elements.email.value.trim();
      state.authError = '';
      state.authMode = state.authMode === 'login' ? 'register' : 'login';
      render();
      $('#auth-form input').focus();
    },
    capture: () => $('#capture-input').click(),
    gallery: () => $('#gallery-input').click(),
    'remove-file': element => {
      const [file] = state.files.splice(Number(element.dataset.index), 1);
      if (selectedPreviews.has(file)) URL.revokeObjectURL(selectedPreviews.get(file));
      selectedPreviews.delete(file);
      state.uploadError = '';
      render();
    },
    'start-check': submitUpload,
    again: () => {
      state.work = state.results = null;
      setScreen('scan');
    },
    'reload-works': () => loadWorks(screenController.signal),
    'more-works': () => loadWorks(screenController.signal, true),
    'open-sheet': element => {
      const work = state.works.find(item => item.id === element.dataset.id);
      if (work) checkNewWork(work);
    },
    'refresh-work': () => {
      state.checkError = '';
      render();
      pollWork(screenController.signal);
    },
    retry: retryWork,
    'old-results': () => setScreen('result'),
    filter: element => { state.filter = element.dataset.filter; render(); },
    'open-task': element => openModal('detail', element.dataset.id),
    preview: () => openModal('preview'),
    clarify: () => openModal('clarify'),
    'close-modal': () => closeModal(),
    task: element => selectTask(element.dataset.id),
    'preview-page': element => {
      releasePreview(state.modal);
      state.modal.page += Number(element.dataset.step);
      state.modal.angle = 0;
      state.modal.previewError = '';
      renderModal();
      loadPreview();
    },
    'refresh-preview': loadPreview,
    rotate: () => {
      state.modal.angle = (state.modal.angle + 90) % 360;
      rotatePreview();
    },
    edit: () => {
      Object.assign(state.modal, { mode: 'edit', error: '' });
      renderModal();
      $('#correction-form textarea').focus();
    },
    'cancel-edit': () => {
      Object.assign(state.modal, { mode: 'detail', error: '' });
      renderModal();
    },
    recheck: () => {
      const task = chosenTask();
      modalMutation(`${taskPath(task)}/recheck`, { method: 'POST' }, work => checkNewWork(work, task.id));
    },
    confirm: () => {
      const task = chosenTask();
      modalMutation(`${taskPath(task)}/confirm`, { method: 'POST' }, result => {
        updateTask(result);
        toast('Результат подтверждён.');
      });
    },
    delete: () => {
      state.deleteOpen = true;
      render();
      $('[data-act=delete-confirm]').focus();
    },
    'delete-cancel': () => {
      state.deleteOpen = false;
      render();
      $('[data-act=delete]').focus();
    },
    'delete-confirm': removeWork,
  };

  document.addEventListener('change', event => {
    if (event.target.id === 'work-test') activeTest = event.target.value;
  });

  document.addEventListener('click', event => {
    const element = event.target.closest('[data-act]');
    const action = element?.dataset.act;
    if (!Object.hasOwn(actions, action)) return;
    event.preventDefault();
    if (state.pending && action !== 'logout') return;
    actions[action](element);
  });

  const formActions = { 'auth-form': submitAuth, 'correction-form': saveCorrection, 'clarification-form': submitClarification, 'workspace-form': addWorkspaceItem, 'question-form': saveQuestion };
  document.addEventListener('submit', event => {
    const form = event.target;
    if (!Object.hasOwn(formActions, form.id)) return;
    event.preventDefault();
    if (!state.pending) formActions[form.id](form);
  });

  document.addEventListener('keydown', event => {
    if (!state.modal) return;
    if (event.key === 'Escape' && !state.modal.busy) {
      closeModal();
      return;
    }
    if (event.key !== 'Tab') return;
    const focusables = Array.from($('#modal').querySelectorAll('button, input, textarea, select, a[href]'))
      .filter(item => !item.disabled && !item.hidden);
    const first = focusables[0];
    const last = focusables.at(-1);
    if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
    else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
  });

  $('#helpBtn').innerHTML = icon('help', 20);
  $('#modalClose').innerHTML = icon('x', 20);
  $('#modal').inert = true;
  $('#modal').setAttribute('aria-hidden', 'true');
  $('#modal').removeAttribute('aria-label');
  $('#modal').setAttribute('aria-labelledby', 'modalTitle');

  for (const camera of [true, false]) {
    const input = document.createElement('input');
    input.id = camera ? 'capture-input' : 'gallery-input';
    input.type = 'file';
    input.accept = camera ? 'image/jpeg,image/png' : FILE_TYPES;
    if (camera) input.setAttribute('capture', 'environment');
    else input.multiple = true;
    input.hidden = true;
    input.addEventListener('change', () => selectFiles(input));
    $('.app').append(input);
  }

  api.onSessionChange((session, reason) => {
    subscriptionEpoch++;
    clearPreviews(selectedPreviews);
    clearPreviews(workPreviews);
    Object.assign(state, {
      authMode: 'login', authEmail: '',
      authError: reason === 'expired' ? 'Срок сессии истёк. Войдите снова.' : '',
      files: [], uploadError: '', pending: false,
      subscription: null, subscriptionError: '',
      works: [], worksOffset: 0, worksLoaded: false, worksLoading: false, worksError: '', hasMore: false,
      work: null, results: null,
    });
    $('#toasts').replaceChildren();
    if (session) loadWorkspace();
    setScreen(session ? 'home' : 'auth');
  });

  window.addEventListener('pagehide', () => {
    stopScreen();
    closeModal(false);
  });
  window.addEventListener('pageshow', event => {
    if (event.persisted) setScreen(state.screen);
  });

  if (api.getSession()) loadWorkspace();
  setScreen('home');
})();
