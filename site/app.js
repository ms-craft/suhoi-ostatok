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

  const FILE_TYPES = 'image/jpeg,image/png,image/heic,image/heif,application/pdf,.jpg,.jpeg,.png,.heic,.heif,.pdf';
  const LEGACY_SUBJECTS = [
    ['math', 'Математика'], ['russian', 'Русский язык'], ['belarusian', 'Белорусский язык'],
    ['literature', 'Литература'], ['history', 'История'], ['social_studies', 'Обществознание'],
    ['english', 'Английский язык'], ['other_humanities', 'Другой гуманитарный предмет'],
  ];
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
    profile: null, profileError: '',
    deleteOpen: false, modal: null,
    sheetDeleteId: null,
  };
  let screenController;
  let pollTimer;
  let disposeBilling;
  let billingBack = 'scan';
  let profileBack = 'home';
  let subscriptionEpoch = 0;
  let focusBeforeModal;
  let workspace = { classes: [], tests: [], links: {} };
  let workspaceSync = { ready: false, revision: 0, dirty: false, running: false, error: '', conflict: null };
  let activeClass = null;
  let activeTest = '';
  let editingTest = '';
  let addingWorkspaceItem = '';
  let workspaceError = '';
  let telegramClassCode = null;
  let telegramClassInfo = null;
  let telegramStudentToRemove = null;
  let telegramDelivery = null;
  let editingStudentName = false;
  const questionImports = new Map();
  let subjectCatalog = [];
  let workspaceDelete = null;
  const workspaceKey = () => `stopkadr.workspace.${api.getSession()?.user_id || ''}`;
  const workspaceMetaKey = () => `${workspaceKey()}.sync`;
  function saveWorkspaceMeta() {
    localStorage.setItem(workspaceMetaKey(), JSON.stringify({ revision: workspaceSync.revision, dirty: workspaceSync.dirty }));
  }
  function saveWorkspace() {
    localStorage.setItem(workspaceKey(), JSON.stringify(workspace));
    workspaceSync.dirty = true;
    saveWorkspaceMeta();
    void flushWorkspace();
  }
  function loadWorkspace() {
    try {
      const value = JSON.parse(localStorage.getItem(workspaceKey()) || '{}');
      workspace = { classes: Array.isArray(value.classes) ? value.classes : [], tests: Array.isArray(value.tests) ? value.tests : [], links: value.links || {} };
    } catch { workspace = { classes: [], tests: [], links: {} }; }
    activeClass = null;
    activeTest = '';
    workspaceDelete = null;
  }
  function workspaceHasData(value) {
    return value.classes.length > 0 || value.tests.length > 0 || Object.keys(value.links).length > 0;
  }
  async function initializeWorkspace() {
    const session = api.getSession(), sync = workspaceSync;
    sync.error = '';
    if (state.screen === 'home') render();
    try {
      const remote = await api.request('/v1/workspace');
      if (session !== api.getSession() || sync !== workspaceSync) return;
      const localExists = localStorage.getItem(workspaceKey()) !== null;
      let meta = null;
      try { meta = JSON.parse(localStorage.getItem(workspaceMetaKey()) || 'null'); } catch { /* preserve local data */ }
      const remoteData = remote.workspace;
      const same = remoteData && JSON.stringify(remoteData) === JSON.stringify(workspace);
      const localUnsent = meta?.dirty === true || (!meta && localExists && workspaceHasData(workspace));
      if (remoteData && localUnsent && !same && (!meta || meta.revision !== remote.revision)) {
        sync.conflict = remote;
        sync.revision = Number.isInteger(meta?.revision) ? meta.revision : 0;
        sync.dirty = true;
        return;
      }
      sync.revision = remote.revision;
      if (remoteData && (!localUnsent || same)) {
        workspace = remoteData;
        localStorage.setItem(workspaceKey(), JSON.stringify(workspace));
        sync.dirty = false;
      } else {
        sync.dirty = true;
      }
      sync.ready = true;
      saveWorkspaceMeta();
      if (sync.dirty) void flushWorkspace();
    } catch (error) {
      if (session !== api.getSession() || sync !== workspaceSync) return;
      sync.error = errorText(error);
    } finally {
      if (session === api.getSession() && sync === workspaceSync && state.screen === 'home') render();
    }
  }
  async function flushWorkspace() {
    const session = api.getSession(), sync = workspaceSync;
    if (!session || !sync.ready || sync.running || sync.conflict) return;
    const hadError = !!sync.error;
    sync.running = true;
    try {
      while (sync.dirty && sync.ready && session === api.getSession() && sync === workspaceSync) {
        const snapshot = JSON.stringify(workspace);
        const result = await api.request('/v1/workspace', { method: 'PUT', body: { revision: sync.revision, workspace: JSON.parse(snapshot) } });
        if (session !== api.getSession() || sync !== workspaceSync) return;
        sync.revision = result.revision;
        sync.dirty = JSON.stringify(workspace) !== snapshot;
        sync.error = '';
        saveWorkspaceMeta();
      }
    } catch (error) {
      if (session !== api.getSession() || sync !== workspaceSync) return;
      sync.dirty = true;
      try { saveWorkspaceMeta(); } catch { /* local copy remains available */ }
      if (error.code === 'workspace_conflict') {
        try {
          const remote = await api.request('/v1/workspace');
          if (session !== api.getSession() || sync !== workspaceSync) return;
          sync.conflict = remote;
          sync.ready = false;
          toast('Данные изменились на другом устройстве. Откройте главную и выберите версию.', true);
        } catch { sync.error = 'Не удалось проверить изменения на сервере.'; }
      } else {
        sync.error = 'Не удалось синхронизировать данные с сервером. Изменения сохранены на этом устройстве.';
        toast(sync.error, true);
      }
    } finally {
      if (session === api.getSession() && sync === workspaceSync) {
        sync.running = false;
        if (state.screen === 'home' && (hadError || sync.error || sync.conflict)) render();
      }
    }
  }
  const newId = () => `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;

  function workspaceForm(kind) {
    if (addingWorkspaceItem !== kind) return '';
    return `<form id="workspace-form" class="card card-pad stack" style="margin-top:10px">
      <label class="form-field">${kind === 'class' ? 'Название класса' : 'Название работы'}
        <input name="name" maxlength="100" required autocomplete="off" placeholder="${kind === 'class' ? 'Например, 5 А' : 'Например, Дроби'}">
      </label>
      ${workspaceError ? `<p class="form-error" role="alert">${esc(workspaceError)}</p>` : ''}
      <button type="submit" class="btn btn-primary btn-block">Сохранить</button>
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
    if (!workspaceSync.ready) {
      if (workspaceSync.conflict) return `<div class="card card-pad stack"><h1 class="section-title">Выберите версию данных</h1>
        <p>На сервере и на этом устройстве есть разные несохранённые изменения. Какую версию использовать?</p>
        ${btn('workspace-use-server', 'Загрузить с сервера', 'btn-ghost btn-block')}
        ${btn('workspace-use-local', 'Сохранить версию устройства', 'btn-primary btn-block')}</div>`;
      if (workspaceSync.error) return `<div class="card card-pad stack"><h1 class="section-title">Не удалось загрузить кабинет</h1>
        <p class="form-error" role="alert">${esc(workspaceSync.error)}</p>${btn('workspace-retry', 'Попробовать снова', 'btn-ghost btn-block')}</div>`;
      return '<p class="empty" role="status">Загружаем классы и работы…</p>';
    }
    const classes = workspace.classes.map(item => {
      const count = Object.values(workspace.links).filter(link => link.classId === item.id).length;
      if (workspaceDelete?.kind === 'class' && workspaceDelete.id === item.id) {
        return `<div class="home-class-confirm" role="alertdialog" aria-labelledby="workspace-delete-title" aria-describedby="class-delete-description">
          <div id="workspace-delete-title" class="home-class-confirm-title" tabindex="-1">Удалить «${esc(item.name)}»?</div>
          <p id="class-delete-description">Вместе с классом удалятся листы и результаты: ${count}.</p>
          <div class="home-class-confirm-actions">
            <button type="button" class="home-class-confirm-delete" data-act="confirm-delete-workspace" ${state.pending ? 'disabled' : ''}>${state.pending ? 'Удаляем…' : 'Удалить'}</button>
            <button type="button" class="home-class-confirm-cancel" data-act="cancel-delete-workspace" ${state.pending ? 'disabled' : ''}>Отмена</button>
          </div>
        </div>`;
      }
      return `<div class="home-class-wrap"><button type="button" class="home-class" data-act="open-class" data-id="${esc(item.id)}">
        <span class="home-class-top">${icon('folder', 21)}</span>
        <span class="home-class-name">${esc(item.name)}</span><span class="home-class-count">Работ: ${count}</span>
      </button><button type="button" class="home-delete home-delete-class" data-act="ask-delete-workspace" data-kind="class" data-id="${esc(item.id)}" aria-label="Удалить класс ${esc(item.name)}">${icon('trash', 17)}</button></div>`;
    }).join('');
    const tests = workspace.tests.map(item => workspaceDelete?.kind === 'test' && workspaceDelete.id === item.id
      ? `<div class="home-work-confirm" role="alertdialog" aria-labelledby="workspace-delete-title" aria-describedby="work-delete-description">
          <div id="workspace-delete-title" class="home-class-confirm-title" tabindex="-1">Удалить «${esc(item.name)}»?</div>
          <p id="work-delete-description">Задания и ответы будут удалены. Результаты проверенных листов сохранятся.</p>
          <div class="home-class-confirm-actions">
            <button type="button" class="home-class-confirm-delete" data-act="confirm-delete-workspace">Удалить</button>
            <button type="button" class="home-class-confirm-cancel" data-act="cancel-delete-workspace">Отмена</button>
          </div>
        </div>`
      : `<div class="home-work-wrap"><button type="button" class="home-work" data-act="edit-test" data-id="${esc(item.id)}">
      <span class="home-work-icon">${icon('folder', 22)}</span><span class="home-work-copy"><b>${esc(item.name)}</b>
      <span>Заданий: ${Array.isArray(item.questions) ? item.questions.length : 0}</span></span>${icon('chevRight', 18)}</button><button type="button" class="home-delete home-delete-work" data-act="ask-delete-workspace" data-kind="test" data-id="${esc(item.id)}" aria-label="Удалить работу ${esc(item.name)}">${icon('trash', 17)}</button></div>`).join('');
    const unassigned = Object.values(workspace.links).filter(link => !link.classId).length;
    return `<div class="teacher-home">
      ${workspaceSync.error ? `<div class="callout blue"><p>${esc(workspaceSync.error)}</p>${btn('workspace-retry-save', 'Повторить синхронизацию', 'btn-ghost btn-block')}</div>` : ''}
      <div class="home-intro"><p>Личный кабинет учителя</p><h1>Всё по папкам</h1>
        <p>Классы, задания и работы учеников — в одном месте.</p></div>
      <section class="home-section" aria-labelledby="classes-heading">
        <div class="home-section-head"><div class="home-heading"><h2 id="classes-heading">Мои классы</h2><span>${workspace.classes.length}</span></div>
          ${btn('add-class', 'Создать класс', 'home-create', 'plus')}</div>
        ${workspaceForm('class')}
        ${classes ? `<div class="home-classes">${classes}</div>` : '<div class="home-empty">Добавьте класс, чтобы собирать и проверять работы учеников.</div>'}
        ${unassigned ? `<button type="button" class="home-unassigned" data-act="open-unassigned">Ранее загруженные листы · ${unassigned} ${icon('chevRight', 16)}</button>` : ''}
      </section>
      <section class="home-section" aria-labelledby="works-heading">
        <div class="home-section-head"><div class="home-heading"><h2 id="works-heading">Мои работы</h2><span>${workspace.tests.length}</span></div>
          ${btn('add-test', 'Создать работу', 'home-create', 'plus')}</div>
        ${workspaceForm('test')}
        ${tests ? `<div class="home-works">${tests}</div>` : '<div class="home-empty">Создайте работу и добавьте к ней задания и ответы.</div>'}
        <p class="home-note">${icon('info', 16)}<span>Откройте работу, чтобы добавить задания и ответы. Для загрузки листов выберите класс.</span></p>
      </section>
    </div>`;
  }

  function animateWorkspaceList(kind, previousHeight) {
    const grid = document.querySelector(kind === 'class' ? '.home-classes' : '.home-works');
    if (!grid || !previousHeight || matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    const nextHeight = grid.getBoundingClientRect().height;
    if (Math.abs(previousHeight - nextHeight) < 1) return;
    grid.animate([
      { height: `${previousHeight}px`, overflow: 'clip' },
      { height: `${nextHeight}px`, overflow: 'clip' },
    ], { duration: 480, easing: 'cubic-bezier(.22, .61, .36, 1)' });
  }
  function viewTest() {
    const test = workspace.tests.find(item => item.id === editingTest);
    if (!test) return `<p>Работа не найдена.</p>${btn('home', 'На главную', 'btn-ghost')}`;
    const questions = Array.isArray(test.questions) ? test.questions : [];
    const legacy = LEGACY_SUBJECTS.find(([value]) => value === test.subject);
    const subjectName = legacy?.[1] || test.subject || '';
    const category = subjectCategory(test);
    const imported = questionImports.get(test.id);
    return `${back('home', test.name, 'Задания и правильные ответы')}
      <div class="card card-pad" style="margin-bottom:14px">
        <label class="form-field">Предмет
          <input id="test-subject" type="text" list="subject-suggestions" maxlength="100" autocomplete="off" value="${esc(subjectName)}" placeholder="Например, физика или русский язык">
          <datalist id="subject-suggestions">${subjectCatalog.map(item => `<option value="${esc(item.name)}"></option>`).join('')}</datalist>
        </label>
        <label class="form-field" style="margin-top:10px">Тип предмета
          <select id="test-subject-category"><option value="">Выберите тип</option><option value="stem" ${category === 'stem' ? 'selected' : ''}>Математика и естественные науки</option><option value="humanities" ${category === 'humanities' ? 'selected' : ''}>Гуманитарные предметы и языки</option></select>
        </label>
        <p class="page-sub">Выберите подсказку для автоматического определения типа или укажите его вручную для своего предмета.</p>
      </div>
      ${questions.length > 1 ? '<p class="question-order-hint">Порядок определяет номера заданий при проверке.</p>' : ''}
      ${questions.map((q, i) => `<div class="card card-pad question-card" style="margin-bottom:10px">
        <div class="question-card-head"><b>Задание ${i + 1}</b><div class="question-order" aria-label="Изменить порядок задания ${i + 1}">
          <button type="button" class="question-move" data-act="move-question" data-index="${i}" data-step="-1" aria-label="Переместить задание ${i + 1} вверх" ${i === 0 ? 'disabled' : ''}>↑</button>
          <button type="button" class="question-move" data-act="move-question" data-index="${i}" data-step="1" aria-label="Переместить задание ${i + 1} вниз" ${i === questions.length - 1 ? 'disabled' : ''}>↓</button>
        </div></div><p style="white-space:pre-wrap">${mathText(q.question)}</p><p class="page-sub">Правильный ответ: ${mathText(q.answer)}</p><button type="button" class="btn btn-quiet" data-act="remove-question" data-index="${i}">Удалить</button></div>`).join('')}
      ${imported?.questions?.length ? `<form id="import-answers-form" class="card card-pad stack" style="margin-bottom:14px">
        <h2>Задания с фотографии</h2>
        <p class="page-sub">Проверьте условия и их порядок, затем впишите правильный ответ под каждым заданием.</p>
        ${imported.questions.map((item, i) => `<div class="import-question stack-sm">
          <label class="form-field">Задание ${questions.length + i + 1}<textarea data-import-index="${i}" data-import-field="question" maxlength="8000" rows="3" required>${esc(item.question)}</textarea></label>
          <label class="form-field">Правильный ответ<textarea data-import-index="${i}" data-import-field="answer" maxlength="3000" rows="2" required>${esc(item.answer)}</textarea></label>
          <button type="button" class="btn btn-ghost btn-sm import-remove" data-act="remove-import-question" data-index="${i}">${icon('trash', 17)} Убрать задание</button>
        </div>`).join('')}
        ${imported.error ? `<p class="form-error" role="alert">${esc(imported.error)}</p>` : ''}
        <button type="submit" class="btn btn-primary btn-block">Сохранить задания</button>
        ${btn('cancel-question-import', 'Отменить распознавание', 'btn-ghost btn-block')}
      </form>` : `<form id="question-photo-form" class="card card-pad stack" style="margin-bottom:14px">
        <h2>Задания с фотографии</h2>
        <p class="page-sub">Выберите фотографию печатных заданий. Она будет отправлена для распознавания; запрос к модели может быть платным.</p>
        <label class="form-field">Фотография<input type="file" name="photo" accept="image/jpeg,image/png,image/heic,image/heif,.jpg,.jpeg,.png,.heic,.heif" required ${imported?.pending ? 'disabled' : ''}></label>
        ${imported?.error ? `<p class="form-error" role="alert">${esc(imported.error)}</p>` : ''}
        <button type="submit" class="btn btn-ghost btn-block" ${imported?.pending ? 'disabled' : ''}>${imported?.pending ? 'Распознаём задания…' : 'Распознать задания'}</button>
        ${imported?.pending ? '<p class="page-sub" role="status">Ожидаем ответ модели. Не запускайте повторно.</p>' : ''}
      </form>`}
      <form id="question-form" class="card card-pad stack">
        <h2>Новое задание</h2>
        <label class="form-field">Условие<textarea name="question" maxlength="8000" required rows="3"></textarea></label>
        <label class="form-field">Правильный ответ<textarea name="answer" maxlength="3000" required rows="2"></textarea></label>
        ${workspaceError ? `<p class="form-error" role="alert">${esc(workspaceError)}</p>` : ''}
        <button type="submit" class="btn btn-primary btn-block">Добавить задание</button>
      </form>`;
  }
  function subjectCategory(test) {
    const legacy = LEGACY_SUBJECTS.find(([value]) => value === test.subject);
    const known = subjectCatalog.find(item => item.name.toLocaleLowerCase('ru') === String(test.subject || '').trim().toLocaleLowerCase('ru'));
    return known?.category || (legacy ? (legacy[0] === 'math' ? 'stem' : 'humanities') : test.subjectCategory || '');
  }

  async function loadSubjectCatalog(signal) {
    try {
      const result = await api.request('/v1/subjects', { signal });
      if (signal.aborted) return;
      subjectCatalog = result;
      const suggestions = document.getElementById('subject-suggestions');
      if (suggestions) suggestions.innerHTML = result.map(item => `<option value="${esc(item.name)}"></option>`).join('');
      const selector = document.getElementById('test-subject-category');
      const test = workspace.tests.find(item => item.id === editingTest);
      if (selector && test) selector.value = subjectCategory(test);
    } catch (error) {
      if (!signal.aborted) toast('Подсказки предметов недоступны. Можно ввести предмет и выбрать тип вручную.', true);
    }
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

  async function importQuestionPhoto(form) {
    const file = form.elements.photo.files[0];
    const testId = editingTest;
    if (!file || questionImports.get(testId)?.pending) return;
    const draft = { pending: true, questions: [], error: '' };
    questionImports.set(testId, draft);
    const body = new FormData();
    body.append('file', file);
    render();
    try {
      const result = await api.request('/v1/question-import', { method: 'POST', body });
      if (questionImports.get(testId) !== draft) return;
      draft.questions = result.questions.map(item => ({ question: item.question, answer: '' }));
      if (!draft.questions.length) draft.error = 'Задания не найдены. Попробуйте фотографию получше или добавьте задание вручную.';
      if (result.demonstration) draft.error = 'Демонстрационный результат: фотография не распознавалась.';
    } catch (error) {
      if (questionImports.get(testId) !== draft) return;
      draft.error = error?.code === 'network_error' || error?.code === 'api_unavailable'
        ? 'Связь прервалась. Запрос мог быть платным; проверьте состояние у провайдера перед повтором.'
        : errorText(error);
    } finally {
      draft.pending = false;
      if (state.screen === 'test' && editingTest === testId && questionImports.get(testId) === draft) render();
    }
  }

  function saveImportedQuestions() {
    const test = workspace.tests.find(item => item.id === editingTest);
    const draft = questionImports.get(editingTest);
    if (!test || !draft?.questions.length) return;
    const additions = draft.questions.map(item => ({ question: item.question.trim(), answer: item.answer.trim() }));
    if (additions.some(item => !item.question || !item.answer)) return;
    const previous = test.questions;
    const combined = [...(Array.isArray(previous) ? previous : []), ...additions];
    if (combined.length > 50 || JSON.stringify(combined).length > 30000) {
      draft.error = 'В работе допускается до 50 заданий и 30 000 символов условий и ответов.';
      render();
      return;
    }
    test.questions = combined;
    try { saveWorkspace(); } catch {
      test.questions = previous;
      draft.error = 'Не удалось сохранить задания в браузере.';
      render();
      return;
    }
    questionImports.delete(editingTest);
    render();
  }
  const selectedPreviews = new Map();
  const selectedPreviewRequests = new Map();
  const selectedPreviewErrors = new Map();
  const workPreviews = new Map();

  function clearPreviews(cache) {
    for (const url of cache.values()) URL.revokeObjectURL(url);
    cache.clear();
  }

  function selectedPreview(file) {
    if (selectedPreviews.has(file)) return selectedPreviews.get(file);
    if (isHeifFile(file)) return '';
    if (!file.type.startsWith('image/')) return '';
    if (!selectedPreviews.has(file)) selectedPreviews.set(file, URL.createObjectURL(file));
    return selectedPreviews.get(file);
  }

  const isHeifFile = file => /\.(?:heic|heif)$/i.test(file.name) || ['image/heic', 'image/heif'].includes(file.type);

  function clearSelectedPreviews() {
    for (const controller of selectedPreviewRequests.values()) controller.abort();
    selectedPreviewRequests.clear();
    selectedPreviewErrors.clear();
    clearPreviews(selectedPreviews);
  }

  function updateSelectedPreview(file) {
    const index = state.files.indexOf(file);
    if (state.screen !== 'scan' || index < 0) return;
    const holder = document.querySelector(`[data-selected-preview="${index}"]`);
    const error = document.querySelector(`[data-selected-preview-error="${index}"]`);
    const url = selectedPreviews.get(file);
    if (holder && url) {
      const img = document.createElement('img');
      img.src = url;
      img.alt = '';
      holder.replaceChildren(img);
    }
    if (error) error.textContent = selectedPreviewErrors.get(file) || '';
  }

  function loadSelectedPreviews() {
    const session = api.getSession();
    for (const file of state.files) {
      if (!isHeifFile(file) || selectedPreviews.has(file) || selectedPreviewRequests.has(file) || selectedPreviewErrors.has(file)) continue;
      const controller = new AbortController();
      selectedPreviewRequests.set(file, controller);
      const form = new FormData();
      form.append('file', file);
      api.request('/v1/submissions/preview', { method: 'POST', body: form, response: 'blob', signal: controller.signal })
        .then(blob => {
          if (controller.signal.aborted || session !== api.getSession() || !state.files.includes(file)) return;
          selectedPreviews.set(file, URL.createObjectURL(blob));
        })
        .catch(error => {
          if (!controller.signal.aborted && session === api.getSession() && state.files.includes(file)) {
            selectedPreviewErrors.set(file, errorText(error));
          }
        })
        .finally(() => {
          if (selectedPreviewRequests.get(file) === controller) selectedPreviewRequests.delete(file);
          updateSelectedPreview(file);
        });
    }
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
    if (sub.sheet_billing) return sub.checks_remaining <= 0 ? 'Лимит листов исчерпан.' : '';
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
        ${signup ? `<label class="form-field">Фамилия, имя и отчество
          <input name="full_name" type="text" autocomplete="name" minlength="2" maxlength="160" required>
        </label>` : ''}
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

  function viewProfile() {
    const profile = state.profile;
    return `${back(profileBack, 'Аккаунт учителя')}
      <div class="card card-pad stack">
        <div><div class="eyebrow">ФИО</div><p>${esc(profile?.full_name || 'Не указано')}</p></div>
        <div><div class="eyebrow">Электронная почта</div><p>${esc(profile?.email || 'Загружаем…')}</p></div>
        <form id="profile-form" class="stack-sm">
          <label class="form-field">${profile?.full_name ? 'Изменить ФИО' : 'Укажите ФИО'}
            <input name="full_name" type="text" autocomplete="name" minlength="2" maxlength="160" required value="${esc(profile?.full_name || '')}">
          </label>
          <button type="submit" class="btn btn-ghost btn-block" ${profile ? '' : 'disabled'}>Сохранить</button>
        </form>
        ${alert(state.profileError)}
        ${btn('logout', 'Выйти из аккаунта', 'btn-ghost btn-block')}
      </div>`;
  }

  function viewScan() {
    const block = subscriptionBlock();
    const sub = state.subscription;
    const countText = state.worksLoaded ? `${state.hasMore ? 'Показано работ' : 'Загружено работ'}: ${state.works.length}` :
      state.worksError ? 'Откройте список, чтобы обновить' : 'Ваши загруженные работы';
    const chosen = state.files.map((file, index) => `
      <li class="file-row between">
        <span class="selected-file"><span class="selected-thumbnail" data-selected-preview="${index}">${selectedPreview(file) ? `<img src="${esc(selectedPreview(file))}" alt="">` : icon('doc', 24)}</span><span>${esc(file.name)}<small class="preview-error" data-selected-preview-error="${index}" role="status">${esc(selectedPreviewErrors.get(file) || '')}</small></span></span>
        <button type="button" class="iconbtn" data-act="remove-file" data-index="${index}"
          aria-label="Убрать файл ${esc(file.name)}" ${state.pending ? 'disabled' : ''}>${icon('x', 17)}</button>
      </li>`).join('');
    return `
      <div style="padding:6px 2px 16px">
        <button type="button" class="btn btn-quiet" data-act="home">← Все классы</button>
        <h1 class="page-title">${esc(workspace.classes.find(item => item.id === activeClass)?.name || 'Работы')}</h1>
        <p class="page-sub">Сфотографируйте листы одной работы — соберём ответы и найдём ошибки.</p>
      </div>
      <div class="card card-pad stack-sm" style="margin-bottom:14px">
        <h2 class="section-title">Telegram для учеников</h2>
        <p class="page-sub">Выдайте ученикам код класса. Каждый один раз введёт его и своё ФИО в боте.</p>
        <p class="page-sub">Сообщайте код только ученикам этого класса: регистрация использует введённое ими ФИО.</p>
        ${telegramClassInfo?.classId === activeClass && telegramClassInfo.exists && !telegramClassCode?.code ? '<p class="page-sub">Код уже создан. Новый код заменит предыдущий для новых регистраций; зарегистрированные ученики сохранятся.</p>' : ''}
        ${telegramClassInfo?.classId === activeClass && !telegramClassInfo.bot_configured ? '<p class="form-error">Бот ещё не подключён: на сервере нужен TELEGRAM_BOT_TOKEN.</p>' : ''}
        ${telegramClassCode?.classId === activeClass && telegramClassCode.code ? `<label class="form-field">Код класса
          <input value="${esc(telegramClassCode.code)}" readonly aria-label="Код класса для Telegram"></label>
          <p class="page-sub">Команда ученика: /link КОД_КЛАССА Фамилия Имя. Новый код заменит предыдущий; зарегистрированные ученики сохранятся.</p>
          ${!telegramClassInfo && !telegramClassCode.configured ? '<p class="form-error">Бот ещё не подключён: на сервере нужен TELEGRAM_BOT_TOKEN.</p>' : ''}` : ''}
        ${telegramClassCode?.classId === activeClass && telegramClassCode.error ? `<p class="form-error" role="alert">${esc(telegramClassCode.error)}</p>` : ''}
        ${btn('telegram-class-code', (telegramClassCode?.classId === activeClass && telegramClassCode.code) || (telegramClassInfo?.classId === activeClass && telegramClassInfo.exists) ? 'Создать новый код' : 'Создать код класса', 'btn-ghost', '', state.pending ? 'disabled' : '')}
        ${telegramClassInfo?.classId === activeClass && telegramClassInfo.exists ? btn('telegram-refresh-class', 'Обновить список учеников', 'btn-quiet btn-sm') : ''}
        ${telegramClassInfo?.classId === activeClass && telegramClassInfo.students.length ? `<div class="stack-sm"><b>Зарегистрированные ученики</b>
          ${telegramClassInfo.students.map(student => `<div class="between"><span>${esc(student.name)}</span>${telegramStudentToRemove === student.id
            ? `<span>${btn('telegram-remove-student', 'Отключить', 'btn-ghost btn-sm', '', `data-id="${esc(student.id)}"`)} ${btn('telegram-cancel-remove', 'Отмена', 'btn-quiet btn-sm')}</span>`
            : btn('telegram-ask-remove', 'Отвязать', 'btn-quiet btn-sm', '', `data-id="${esc(student.id)}"`)}</div>`).join('')}</div>` : ''}
      </div>
      <label class="form-field" style="margin-bottom:14px">Работа
        <select id="work-test"><option value="">Без выбранной работы</option>${workspace.tests.map(item => `<option value="${esc(item.id)}" ${activeTest === item.id ? 'selected' : ''}>${esc(item.name)}</option>`).join('')}</select>
      </label>
      <div class="capture">
        <div class="cam">${icon('camera', 28)}</div>
        <div class="cap-title">Сфотографируйте лист</div>
        <div class="cap-hint">Целиком, при ровном свете · JPG, PNG, HEIC или PDF</div>
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
      </div>` : sub ? `<p class="page-sub" style="margin:12px 2px">${sub.sheet_billing ? 'Осталось листов' : 'Осталось проверок'}: ${esc(sub.checks_remaining)} · до ${esc(sub.max_pages)} стр. в работе</p>` : ''}`;
  }

  function viewSheets() {
    const groups = new Map();
    for (const work of state.works.filter(item => (workspace.links[item.id]?.classId || null) === activeClass)) {
      const testId = workspace.links[work.id]?.testId || '';
      if (!groups.has(testId)) groups.set(testId, []);
      groups.get(testId).push(work);
    }
    const sheetCard = work => state.sheetDeleteId === work.id ? `
      <div class="sheet-delete-confirm" role="alertdialog" aria-labelledby="sheet-delete-title" aria-describedby="sheet-delete-description">
        <b id="sheet-delete-title" tabindex="-1">Удалить «${esc(workName(work))}»?</b>
        <p id="sheet-delete-description">Будут удалены все страницы и результаты этой работы.</p>
        ${btn('confirm-delete-sheet', state.pending ? 'Удаляем…' : 'Удалить', 'btn-primary btn-block', '', state.pending ? 'disabled' : '')}
        ${btn('cancel-delete-sheet', 'Отмена', 'btn-ghost btn-block', '', state.pending ? 'disabled' : '')}
      </div>` : `
      <div class="sheet-card-wrap"><button type="button" class="sheet-card" data-act="open-sheet" data-id="${esc(work.id)}">
        <div class="sheet sheet-placeholder"><img data-work-preview="${esc(work.id)}" alt="Страница работы" loading="lazy"></div>
        <div class="meta">
          <div class="nm">${esc(workName(work))}</div><div class="st">${esc(workspace.tests.find(item => item.id === workspace.links[work.id]?.testId)?.name || 'Без выбранной работы')}</div>
          <div class="st ${work.status === 'failed' ? 'err' : work.status === 'completed' ? 'ok' : ''}">
            ${icon(work.status === 'failed' ? 'alert' : work.status === 'completed' ? 'check' : 'clock', 13)}
            ${esc(STATUS[work.status] || '')} · ${esc(work.page_count)} стр.
          </div>
        </div>
      </button><button type="button" class="sheet-delete" data-act="ask-delete-sheet" data-id="${esc(work.id)}" aria-label="Удалить ${esc(workName(work))}" ${state.pending ? 'disabled' : ''}>${icon('trash', 18)}</button></div>`;
    const grouped = [...groups].map(([id, works]) => {
      const title = workspace.tests.find(item => item.id === id)?.name || 'Без выбранной работы';
      return `<section class="sheet-group"><h2>${esc(title)}</h2><div class="sheet-grid">${works.map(sheetCard).join('')}</div></section>`;
    }).join('');
    return `${back(activeClass ? 'scan' : 'home', activeClass ? 'Листы класса' : 'Ранее загруженные листы', '')}
      ${grouped || (state.worksLoaded ? empty('Здесь появятся ваши загруженные работы.') : '')}
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
        <div id="telegram-delivery-status" class="card card-pad stack-sm">${telegramDeliveryContent(work.id)}</div>
        ${btn('preview', 'Открыть исходную страницу', 'btn-ghost btn-block', 'image')}
        ${editingStudentName ? `<form id="student-name-form" class="card card-pad stack-sm"><label class="form-field">Фамилия и имя ученика
          <input name="student_name" maxlength="120" value="${esc(work.student_reference || '')}" required></label>
          <p class="page-sub">Для Telegram ФИО должно совпадать с регистрацией ученика в этом классе.</p>
          <button class="btn btn-primary" type="submit" ${state.pending ? 'disabled' : ''}>Сохранить ФИО</button>
          ${btn('cancel-student-name', 'Отмена', 'btn-ghost')}</form>` : btn('edit-student-name', 'Исправить ФИО ученика', 'btn-ghost btn-block')}
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

  function textDetail(label, content, attribute = '') {
    return `<div class="task-detail">
      <div class="eyebrow">${esc(label)}</div><p ${attribute}>${mathText(content || '—')}</p>
    </div>`;
  }

  function findAnswerFragment(root, fragment) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    const matches = [];
    let node;
    while ((node = walker.nextNode())) {
      if (node.parentElement?.closest('.katex')) continue;
      let position = -1;
      while ((position = node.data.indexOf(fragment, position + 1)) !== -1) {
        const before = node.data[position - 1] || '';
        const after = node.data[position + fragment.length] || '';
        if ((/[\p{L}\p{N}]/u.test(fragment[0]) && /[\p{L}\p{N}]/u.test(before)) ||
            (/[\p{L}\p{N}]/u.test(fragment.at(-1)) && /[\p{L}\p{N}]/u.test(after))) continue;
        matches.push({ node, position });
      }
    }
    return matches.length === 1 ? matches[0] : null;
  }

  function linkExplanationReferences(body) {
    const answer = body.querySelector('[data-student-answer]');
    const explanation = body.querySelector('[data-review-explanation]');
    if (!answer || !explanation) return;
    const walker = document.createTreeWalker(explanation, NodeFilter.SHOW_TEXT);
    const nodes = [];
    while (walker.nextNode()) if (!walker.currentNode.parentElement?.closest('.katex')) nodes.push(walker.currentNode);
    for (const node of nodes) {
      const quote = /«([^«»\n]{1,200})»|“([^“”\n]{1,200})”/g;
      let match, last = 0;
      const replacement = document.createDocumentFragment();
      let linked = false;
      while ((match = quote.exec(node.data))) {
        const fragment = match[1] || match[2];
        const preceding = node.data.slice(Math.max(0, match.index - 80), match.index);
        if (/(?:вместо|эталон(?:е|а)?|правильно|пропущен(?:о|а|ы)?(?:\s+слово)?)\s*[:—-]?\s*$/iu.test(preceding)) continue;
        if (!findAnswerFragment(answer, fragment)) continue;
        replacement.append(document.createTextNode(node.data.slice(last, match.index) + match[0][0]));
        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'answer-reference';
        button.textContent = fragment;
        button.setAttribute('aria-label', `Показать в ответе: ${fragment}`);
        button.addEventListener('click', () => {
          const old = answer.querySelector('mark.answer-highlight');
          if (old) { old.replaceWith(document.createTextNode(old.textContent)); answer.normalize(); }
          const target = findAnswerFragment(answer, fragment);
          if (!target) return;
          const range = document.createRange();
          range.setStart(target.node, target.position);
          range.setEnd(target.node, target.position + fragment.length);
          const mark = document.createElement('mark');
          mark.className = 'answer-highlight';
          mark.tabIndex = -1;
          range.surroundContents(mark);
          mark.scrollIntoView({ behavior: matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth', block: 'center' });
          mark.focus({ preventScroll: true });
        });
        replacement.append(button, document.createTextNode(match[0].at(-1)));
        last = quote.lastIndex;
        linked = true;
      }
      if (linked) {
        replacement.append(document.createTextNode(node.data.slice(last)));
        node.replaceWith(replacement);
      }
    }
  }

  function detailContent(task) {
    const review = task.preliminary;
    const rec = task.recognition;
    const original = task.original_recognition || rec;
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
            <div class="l">${editedAnswer ? 'Исходное распознавание ответа' : 'Ответ ученика'}</div><div class="v" ${editedAnswer ? '' : 'data-student-answer'}>${mathText(rec.answer || '—')}</div>
          </div>
          ${review.expected_answer ? `<div class="cell ref">
            <div class="l">${review.expected_answer_source === 'teacher_test' ? 'Ответ учителя в работе' : 'Предполагаемый верный ответ'}</div><div class="v">${mathText(review.expected_answer)}</div>
          </div>` : ''}
        </div>
        ${original.answer && original.answer !== rec.answer ? textDetail('Первоначальное распознавание ответа', original.answer) : ''}
        ${textDetail('Объяснение', review.explanation, 'data-review-explanation')}
        ${review.uncertainty ? textDetail('Причина неопределённости', review.uncertainty) : ''}
        ${rec.ambiguity && rec.ambiguity !== review.uncertainty ? textDetail('Сомнения при распознавании', rec.ambiguity) : ''}
        ${original.ambiguity && original.ambiguity !== rec.ambiguity && original.ambiguity !== review.uncertainty ? textDetail('Сомнения при первоначальном распознавании', original.ambiguity) : ''}
        ${rec.corrections?.length ? textDetail('Исправления на листе', rec.corrections.map((item, index) => `${index + 1}. ${item}`).join('\n')) : ''}
        ${review.missing_data?.length ? textDetail('Не хватает данных', review.missing_data.join(' · ')) : ''}
        ${Object.hasOwn(teacher, 'condition') ? textDetail('Условие от учителя', teacher.condition) : ''}
        ${editedAnswer ? textDetail('Ответ после исправления', teacher.answer, 'data-student-answer') : ''}
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
    if (modal.mode === 'detail') linkExplanationReferences(body);
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

  const VIEWS = { auth: viewAuth, home: viewHome, profile: viewProfile, test: viewTest, scan: viewScan, sheets: viewSheets, check: viewCheck, result: viewResult };

  function focusScreenHeading() {
    const title = $('#view h1');
    if (!title) return;
    title.tabIndex = -1;
    title.focus({ preventScroll: true });
  }

  function render() {
    const session = api.getSession();
    $('.app').dataset.screen = state.screen;
    for (const action of ['billing', 'profile']) {
      const button = document.querySelector(`.appbar [data-act="${action}"]`);
      if (button) button.hidden = !session;
    }
    const profileButton = document.querySelector('.appbar [data-act="profile"]');
    if (profileButton) {
      const initials = (state.profile?.full_name || '').split(/\s+/).filter(Boolean).slice(0, 2).map(part => part[0]).join('').toUpperCase();
      profileButton.innerHTML = initials ? esc(initials) : icon('user', 20);
    }
    const view = $('#view');
    const restoreHeading = document.activeElement === view.querySelector('h1');
    view.dataset.screen = state.screen;
    if (state.screen !== 'billing') view.innerHTML = VIEWS[state.screen]();
    if (state.screen === 'sheets') void loadWorkPreviews();
    if (state.screen === 'scan') loadSelectedPreviews();
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
    if (state.screen !== 'sheets') state.sheetDeleteId = null;
    state.checkError = '';
    state.deleteOpen = false;
    state.worksLoading = false;
    render();
    $('#main').scrollTop = 0;
    if (state.screen !== 'billing') focusScreenHeading();

    if (state.screen === 'auth') {
      refreshAuthOptions(signal);
    } else if (state.screen === 'profile') {
      refreshProfile(signal);
    } else if (state.screen === 'test') {
      loadSubjectCatalog(signal);
    } else if (state.screen === 'scan') {
      refreshSubscription();
      loadWorks(signal);
      void refreshTelegramClass(signal);
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
    if (state.screen === 'result') void refreshTelegramDelivery(signal);
  }

  async function refreshProfile(signal) {
    try {
      const profile = await api.request('/v1/auth/profile', { signal });
      if (signal.aborted) return;
      state.profile = profile;
      state.profileError = '';
    } catch (error) {
      if (signal.aborted) return;
      state.profileError = errorText(error);
    }
    if (state.screen === 'profile') render();
  }

  async function saveProfile(form) {
    const session = api.getSession();
    const name = form.elements.full_name.value.trim();
    if (!name) return;
    try {
      const profile = await api.request('/v1/auth/profile', { method: 'PATCH', body: { full_name: name } });
      if (session !== api.getSession()) return;
      state.profile = profile;
      state.profileError = '';
      render();
      toast('ФИО сохранено');
    } catch (error) { state.profileError = errorText(error); render(); }
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
    if (state.work?.id !== work.id) { state.results = null; editingStudentName = false; telegramDelivery = null; }
    state.work = work;
    setScreen('check', { reopen });
  }

  async function refreshTelegramClass(signal) {
    const classId = activeClass;
    if (!classId) return;
    try {
      const result = await api.request(`/v1/telegram/classes/${encodeURIComponent(classId)}`, { signal });
      if (signal?.aborted || activeClass !== classId) return;
      telegramClassInfo = { classId, ...result };
      if (state.screen === 'scan') render();
    } catch (error) {
      if (!signal?.aborted && activeClass === classId) toast('Не удалось загрузить регистрации Telegram.', true);
    }
  }

  function telegramDeliveryContent(workId) {
    const data = telegramDelivery?.workId === workId ? telegramDelivery : null;
    const labels = {
      no_class: 'Работа не связана с классом.', no_class_code: 'Создайте код Telegram в настройках класса.',
      student_not_linked: 'Ученик не зарегистрирован в боте или ФИО не совпадает.',
      not_scheduled: 'Результат пока не поставлен в очередь.', pending: 'Ожидает отправки.',
      sent: 'Отправлено ученику.', failed: 'Не удалось отправить. Проверьте, что ученик не заблокировал бота.',
      error: 'Не удалось получить состояние отправки.',
    };
    return `<h2 class="section-title">Результат в Telegram</h2><p class="page-sub">${esc(data ? (labels[data.status] || 'Состояние неизвестно.') : 'Проверяем состояние…')}</p>
      ${data && !data.bot_configured ? '<p class="form-error">Бот ещё не запущен: укажите TELEGRAM_BOT_TOKEN на сервере.</p>' : ''}
      ${data?.status === 'failed' ? btn('telegram-retry-delivery', 'Повторить отправку', 'btn-ghost btn-sm') : ''}
      ${btn('telegram-refresh-delivery', 'Обновить статус', 'btn-ghost btn-sm')}`;
  }

  async function refreshTelegramDelivery(signal) {
    const workId = state.work?.id;
    if (!workId) return;
    try {
      const result = await api.request(`/v1/telegram/submissions/${encodeURIComponent(workId)}/delivery`, { signal });
      if (signal?.aborted || state.work?.id !== workId) return;
      telegramDelivery = { workId, ...result };
    } catch {
      if (signal?.aborted || state.work?.id !== workId) return;
      telegramDelivery = { workId, status: 'error', bot_configured: true };
    }
    const box = document.getElementById('telegram-delivery-status');
    if (box) box.innerHTML = telegramDeliveryContent(workId);
  }

  const fileKind = file => {
    if (file.type === 'application/pdf') return 'pdf';
    if (['image/jpeg', 'image/png', 'image/heic', 'image/heif'].includes(file.type)) return 'photo';
    if (file.type && file.type !== 'application/octet-stream') return '';
    if (/\.pdf$/i.test(file.name)) return 'pdf';
    if (/\.(?:jpe?g|png|heic|heif)$/i.test(file.name)) return 'photo';
    return '';
  };

  function invalidFiles(files, existingPages = 0) {
    if (files.some(file => !file.size || !fileKind(file))) return 'Выберите непустые файлы JPG, PNG, HEIC, HEIF или PDF.';
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
      if (activeClass) body.append('class_reference', activeClass);
      const test = workspace.tests.find(item => item.id === activeTest);
      if (test) {
        if (!Array.isArray(test.questions) || !test.questions.length) {
          throw new Error('Добавьте задания и ответы в выбранную работу.');
        }
        const questions = JSON.stringify(test.questions);
        if (questions.length > 30000) throw new Error('Работа слишком большая для одной проверки.');
        body.append('test_questions', questions);
        if (test.subject) {
          const category = subjectCategory(test);
          if (!category) throw new Error('Укажите тип предмета в настройках работы.');
          body.append('test_subject', test.subject);
          body.append('test_subject_category', category);
        }
      }
      const work = await api.request('/v1/submissions', { method: 'POST', body });
      if (session !== api.getSession()) return;
      workspace.links[work.id] = { classId: activeClass, testId: activeTest };
      saveWorkspace();
      state.files = [];
      clearSelectedPreviews();
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
      if (state.authMode === 'register') credentials.full_name = fields.get('full_name').trim();
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
          state.worksOffset = Math.max(0, state.worksOffset - 1);
          if (workPreviews.has(target.id)) URL.revokeObjectURL(workPreviews.get(target.id));
          workPreviews.delete(target.id);
          delete workspace.links[target.id];
          try { saveWorkspace(); } catch { toast('Работа удалена, но не удалось обновить список в браузере.', true); }
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

  async function removeSheet() {
    const target = state.works.find(work => work.id === state.sheetDeleteId);
    if (!target || state.pending) return;
    const session = api.getSession();
    state.pending = true;
    render();
    let removed = false;
    try {
      await api.request(workPath(target), { method: 'DELETE' });
      removed = true;
    } catch (error) {
      if (session !== api.getSession()) return;
      removed = error.code === 'deletion_pending' || error.status === 404;
      if (!removed) toast(errorText(error), true);
    } finally {
      if (session === api.getSession()) {
        state.pending = false;
        if (removed) {
          state.works = state.works.filter(work => work.id !== target.id);
          state.worksOffset = Math.max(0, state.worksOffset - 1);
          if (workPreviews.has(target.id)) URL.revokeObjectURL(workPreviews.get(target.id));
          workPreviews.delete(target.id);
          delete workspace.links[target.id];
          try { saveWorkspace(); } catch { toast('Лист удалён, но не удалось обновить список в браузере.', true); }
          state.sheetDeleteId = null;
          refreshSubscription();
        }
        render();
      }
    }
  }

  const actions = {
    home: () => setScreen('home'),
    'workspace-retry': () => { void initializeWorkspace(); },
    'workspace-retry-save': () => { void flushWorkspace(); },
    'workspace-use-server': () => {
      const remote = workspaceSync.conflict;
      if (!remote?.workspace) return;
      try {
        localStorage.setItem(`${workspaceKey()}.conflict-backup`, JSON.stringify(workspace));
        localStorage.setItem(workspaceKey(), JSON.stringify(remote.workspace));
        workspace = remote.workspace;
        Object.assign(workspaceSync, { revision: remote.revision, dirty: false, conflict: null, ready: true, error: '' });
        saveWorkspaceMeta();
        render();
      } catch { toast('Не удалось сохранить резервную копию на устройстве.', true); }
    },
    'workspace-use-local': () => {
      const remote = workspaceSync.conflict;
      if (!remote) return;
      Object.assign(workspaceSync, { revision: remote.revision, dirty: true, conflict: null, ready: true, error: '' });
      try { saveWorkspaceMeta(); } catch { toast('Не удалось сохранить данные на устройстве.', true); return; }
      render();
      void flushWorkspace();
    },
    profile: () => { if (state.screen !== 'profile') { profileBack = state.screen; state.profileError = ''; setScreen('profile'); } },
    'add-class': () => { addingWorkspaceItem = 'class'; workspaceError = ''; render(); $('#workspace-form input')?.focus(); },
    'add-test': () => { addingWorkspaceItem = 'test'; workspaceError = ''; render(); $('#workspace-form input')?.focus(); },
    'cancel-workspace': () => { addingWorkspaceItem = ''; workspaceError = ''; render(); },
    'ask-delete-workspace': element => {
      const kind = element.dataset.kind, id = element.dataset.id;
      const list = kind === 'class' ? workspace.classes : kind === 'test' ? workspace.tests : [];
      if (!list.some(item => item.id === id)) return;
      const previousHeight = document.querySelector(kind === 'class' ? '.home-classes' : '.home-works')?.getBoundingClientRect().height;
      workspaceDelete = { kind, id };
      addingWorkspaceItem = '';
      render();
      animateWorkspaceList(kind, previousHeight);
      document.getElementById('workspace-delete-title')?.focus({ preventScroll: true });
    },
    'cancel-delete-workspace': () => {
      const kind = workspaceDelete?.kind;
      const previousHeight = kind ? document.querySelector(kind === 'class' ? '.home-classes' : '.home-works')?.getBoundingClientRect().height : 0;
      workspaceDelete = null;
      render();
      if (kind) animateWorkspaceList(kind, previousHeight);
    },
    'confirm-delete-workspace': async () => {
      if (!workspaceDelete) return;
      const { kind, id } = workspaceDelete;
      const list = kind === 'class' ? workspace.classes : workspace.tests;
      if (!list.some(item => item.id === id)) { workspaceDelete = null; render(); return; }
      if (kind === 'class') {
        const session = api.getSession();
        const previousHeight = document.querySelector('.home-classes')?.getBoundingClientRect().height;
        const ids = Object.entries(workspace.links).filter(([, link]) => link.classId === id).map(([workId]) => workId);
        const removed = [];
        const failed = [];
        let telegramFailed = false;
        state.pending = true;
        render();
        try {
          for (const workId of ids) {
            if (session !== api.getSession()) return;
            try {
              await api.request(`/v1/submissions/${encodeURIComponent(workId)}`, { method: 'DELETE' });
              removed.push(workId);
            } catch (error) {
              if (session !== api.getSession()) return;
              if (error.status === 404 || error.code === 'deletion_pending') removed.push(workId);
              else failed.push(workId);
            }
          }
          if (session !== api.getSession()) return;
          if (!failed.length) {
            try { await api.request(`/v1/telegram/classes/${encodeURIComponent(id)}`, { method: 'DELETE' }); }
            catch { telegramFailed = true; }
          }
          const previous = workspace;
          const links = { ...workspace.links };
          for (const workId of removed) delete links[workId];
          workspace = { ...workspace, links,
            classes: failed.length || telegramFailed ? workspace.classes : workspace.classes.filter(item => item.id !== id) };
          try { saveWorkspace(); } catch {
            workspace = previous;
            toast('Не удалось сохранить изменения в браузере. Повторите удаление.', true);
            return;
          }
          state.works = state.works.filter(work => !removed.includes(work.id));
          state.worksOffset = Math.max(0, state.worksOffset - removed.length);
          for (const workId of removed) {
            if (workPreviews.has(workId)) URL.revokeObjectURL(workPreviews.get(workId));
            workPreviews.delete(workId);
          }
          if (telegramFailed) {
            toast('Не удалось удалить регистрации Telegram. Класс сохранён; попробуйте снова.', true);
          } else if (failed.length) {
            toast(`Не удалось удалить листов: ${failed.length}. Класс сохранён; попробуйте снова.`, true);
          } else {
            if (activeClass === id) activeClass = null;
            workspaceDelete = null;
          }
          refreshSubscription();
        } finally {
          if (session === api.getSession()) { state.pending = false; render(); animateWorkspaceList('class', previousHeight); }
        }
        return;
      }
      const previous = workspace;
      const links = Object.fromEntries(Object.entries(workspace.links).map(([workId, link]) => {
        if (kind === 'test' && link.testId === id) return [workId, { ...link, testId: '' }];
        return [workId, link];
      }));
      workspace = { ...workspace, links,
        classes: kind === 'class' ? workspace.classes.filter(item => item.id !== id) : workspace.classes,
        tests: kind === 'test' ? workspace.tests.filter(item => item.id !== id) : workspace.tests };
      try { saveWorkspace(); } catch { workspace = previous; toast('Не удалось сохранить изменения.', true); return; }
      if (kind === 'class' && activeClass === id) activeClass = null;
      if (kind === 'test' && activeTest === id) activeTest = '';
      workspaceDelete = null;
      render();
    },
    'open-unassigned': () => { activeClass = null; setScreen('sheets'); },
    'open-class': element => { activeClass = element.dataset.id; activeTest = ''; telegramStudentToRemove = null; setScreen('scan'); },
    'edit-student-name': () => { editingStudentName = true; render(); document.querySelector('#student-name-form input')?.focus(); },
    'cancel-student-name': () => { editingStudentName = false; render(); },
    'telegram-refresh-delivery': () => { void refreshTelegramDelivery(screenController.signal); },
    'telegram-retry-delivery': async () => {
      const workId = state.work?.id;
      if (!workId) return;
      try {
        await api.request(`/v1/telegram/submissions/${encodeURIComponent(workId)}/retry-delivery`, { method: 'POST' });
        await refreshTelegramDelivery(screenController.signal);
      } catch (error) { toast(errorText(error), true); }
    },
    'telegram-ask-remove': element => { telegramStudentToRemove = element.dataset.id; render(); },
    'telegram-refresh-class': () => { void refreshTelegramClass(screenController.signal); },
    'telegram-cancel-remove': () => { telegramStudentToRemove = null; render(); },
    'telegram-remove-student': async element => {
      const classId = activeClass, session = api.getSession();
      try {
        await api.request(`/v1/telegram/classes/${encodeURIComponent(classId)}/students/${encodeURIComponent(element.dataset.id)}`, { method: 'DELETE' });
        if (session !== api.getSession() || activeClass !== classId) return;
        telegramStudentToRemove = null;
        await refreshTelegramClass(screenController.signal);
      } catch (error) { toast(errorText(error), true); }
    },
    'telegram-class-code': async () => {
      const cls = workspace.classes.find(item => item.id === activeClass);
      if (!cls || state.pending) return;
      const session = api.getSession();
      const classId = cls.id;
      const submissionIds = Object.entries(workspace.links).filter(([, link]) => link.classId === classId).map(([id]) => id);
      state.pending = true;
      telegramClassCode = { classId, code: '', error: '' };
      render();
      try {
        const result = await api.request(`/v1/telegram/classes/${encodeURIComponent(classId)}/code`, {
          method: 'POST', body: { name: cls.name, submission_ids: submissionIds },
        });
        if (session !== api.getSession()) return;
        telegramClassCode = { classId, code: result.code, configured: result.bot_configured, error: '' };
      } catch (error) {
        if (session !== api.getSession()) return;
        telegramClassCode = { classId, code: '', error: errorText(error) };
      } finally {
        if (session === api.getSession()) { state.pending = false; if (state.screen === 'scan') render(); }
      }
    },
    'edit-test': element => { editingTest = element.dataset.id; workspaceError = ''; setScreen('test'); },
    'cancel-question-import': () => { questionImports.delete(editingTest); render(); },
    'remove-import-question': element => {
      const draft = questionImports.get(editingTest);
      const index = Number(element.dataset.index);
      if (!draft || !Number.isInteger(index) || index < 0 || index >= draft.questions.length) return;
      draft.questions.splice(index, 1);
      if (!draft.questions.length) questionImports.delete(editingTest);
      render();
    },
    'move-question': element => {
      const test = workspace.tests.find(item => item.id === editingTest);
      const index = Number(element.dataset.index), step = Number(element.dataset.step);
      if (!test || !Array.isArray(test.questions) || !Number.isInteger(index) || ![-1, 1].includes(step)) return;
      const target = index + step;
      if (index < 0 || index >= test.questions.length || target < 0 || target >= test.questions.length) return;
      const previous = test.questions;
      const reordered = previous.slice();
      [reordered[index], reordered[target]] = [reordered[target], reordered[index]];
      test.questions = reordered;
      try { saveWorkspace(); } catch { test.questions = previous; toast('Не удалось сохранить порядок.', true); return; }
      render();
      const focus = document.querySelector(`[data-act="move-question"][data-index="${target}"][data-step="${step}"]:not(:disabled)`)
        || document.querySelector(`[data-act="move-question"][data-index="${target}"]:not(:disabled)`);
      focus?.focus({ preventScroll: true });
    },
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
      selectedPreviewRequests.get(file)?.abort();
      selectedPreviewRequests.delete(file);
      selectedPreviewErrors.delete(file);
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
    'ask-delete-sheet': element => {
      if (!state.works.some(work => work.id === element.dataset.id)) return;
      state.sheetDeleteId = element.dataset.id;
      render();
      document.getElementById('sheet-delete-title')?.focus({ preventScroll: true });
    },
    'cancel-delete-sheet': () => { state.sheetDeleteId = null; render(); },
    'confirm-delete-sheet': removeSheet,
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

  document.addEventListener('input', event => {
    const field = event.target.dataset?.importField;
    if (!['question', 'answer'].includes(field)) return;
    const index = Number(event.target.dataset.importIndex);
    const draft = questionImports.get(editingTest);
    if (draft?.questions[index]) draft.questions[index][field] = event.target.value;
  });

  document.addEventListener('change', event => {
    if (event.target.id === 'work-test') activeTest = event.target.value;
    if (event.target.id === 'test-subject') {
      const test = workspace.tests.find(item => item.id === editingTest);
      if (!test) return;
      const previous = test.subject, previousCategory = test.subjectCategory;
      test.subject = event.target.value.trim();
      const known = subjectCatalog.find(item => item.name.toLocaleLowerCase('ru') === test.subject.toLocaleLowerCase('ru'));
      test.subjectCategory = known?.category || '';
      try { saveWorkspace(); } catch {
        test.subject = previous;
        test.subjectCategory = previousCategory;
        event.target.value = LEGACY_SUBJECTS.find(([value]) => value === previous)?.[1] || previous || '';
        toast('Не удалось сохранить предмет.', true);
      }
      const selector = document.getElementById('test-subject-category');
      if (selector) selector.value = subjectCategory(test);
    }
    if (event.target.id === 'test-subject-category') {
      const test = workspace.tests.find(item => item.id === editingTest);
      if (!test) return;
      const known = subjectCatalog.find(item => item.name.toLocaleLowerCase('ru') === String(test.subject || '').trim().toLocaleLowerCase('ru'));
      const legacy = LEGACY_SUBJECTS.find(([value]) => value === test.subject);
      const fixedCategory = known?.category || (legacy ? (legacy[0] === 'math' ? 'stem' : 'humanities') : '');
      if (fixedCategory && event.target.value !== fixedCategory) {
        event.target.value = fixedCategory;
        toast('Для этого предмета тип определяется автоматически.', true);
        return;
      }
      const previous = test.subjectCategory;
      test.subjectCategory = event.target.value;
      try { saveWorkspace(); } catch { test.subjectCategory = previous; event.target.value = previous || ''; toast('Не удалось сохранить тип предмета.', true); }
    }
  });

  document.addEventListener('click', event => {
    const element = event.target.closest('[data-act]');
    const action = element?.dataset.act;
    if (!Object.hasOwn(actions, action)) return;
    event.preventDefault();
    if (state.pending && action !== 'logout') return;
    actions[action](element);
  });

  async function saveStudentName(form) {
    const name = form.elements.student_name.value.trim(), work = state.work, session = api.getSession();
    if (!name || !work) return;
    state.pending = true;
    try {
      const updated = await api.request(workPath(work), { method: 'PATCH', body: { student_reference: name } });
      if (session !== api.getSession() || state.work?.id !== work.id) return;
      state.work = updated;
      editingStudentName = false;
      toast('ФИО сохранено.');
      void refreshTelegramDelivery(screenController.signal);
    } catch (error) { if (session === api.getSession()) toast(errorText(error), true); }
    finally { if (session === api.getSession()) { state.pending = false; render(); } }
  }

  const formActions = { 'auth-form': submitAuth, 'correction-form': saveCorrection, 'clarification-form': submitClarification, 'workspace-form': addWorkspaceItem, 'question-form': saveQuestion, 'question-photo-form': importQuestionPhoto, 'import-answers-form': saveImportedQuestions, 'profile-form': saveProfile, 'student-name-form': saveStudentName };
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

  $('#modalClose').innerHTML = icon('x', 20);
  $('#modal').inert = true;
  $('#modal').setAttribute('aria-hidden', 'true');
  $('#modal').removeAttribute('aria-label');
  $('#modal').setAttribute('aria-labelledby', 'modalTitle');

  for (const camera of [true, false]) {
    const input = document.createElement('input');
    input.id = camera ? 'capture-input' : 'gallery-input';
    input.type = 'file';
    input.accept = camera ? 'image/jpeg,image/png,image/heic,image/heif' : FILE_TYPES;
    if (camera) input.setAttribute('capture', 'environment');
    else input.multiple = true;
    input.hidden = true;
    input.addEventListener('change', () => selectFiles(input));
    $('.app').append(input);
  }

  api.onSessionChange((session, reason) => {
    telegramClassCode = null;
    telegramClassInfo = null;
    telegramStudentToRemove = null;
    telegramDelivery = null;
    editingStudentName = false;
    questionImports.clear();
    workspaceSync = { ready: false, revision: 0, dirty: false, running: false, error: '', conflict: null };
    subscriptionEpoch++;
    clearSelectedPreviews();
    clearPreviews(workPreviews);
    Object.assign(state, {
      authMode: 'login', authEmail: '',
      authError: reason === 'expired' ? 'Срок сессии истёк. Войдите снова.' : '',
      files: [], uploadError: '', pending: false,
      subscription: null, subscriptionError: '',
      works: [], worksOffset: 0, worksLoaded: false, worksLoading: false, worksError: '', hasMore: false,
      work: null, results: null,
      sheetDeleteId: null,
      profile: null, profileError: '',
    });
    $('#toasts').replaceChildren();
    if (session) loadWorkspace();
    setScreen(session ? 'home' : 'auth');
    if (session) void initializeWorkspace();
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
  if (api.getSession()) void initializeWorkspace();
})();
