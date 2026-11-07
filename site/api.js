/* Fetch and per-tab credentials for the existing /v1 API. */
(function () {
  const KEY = 'stopkadr.session.v1';
  const listeners = new Set();
  let session = restore();
  let expiryTimer;

  class ApiError extends Error {
    constructor(message, status, code) {
      super(message);
      this.name = 'ApiError';
      this.status = status;
      this.code = code;
    }
  }

  function storage(write) {
    try {
      return write(sessionStorage);
    } catch {
      // Storage can be unavailable in a private window or under browser policy.
      return null;
    }
  }

  function valid(value) {
    return typeof value?.access_token === 'string' &&
      value.access_token.length > 0 &&
      typeof value.user_id === 'string' && value.user_id.length > 0 &&
      Number.isFinite(Date.parse(value.expires_at)) &&
      Date.parse(value.expires_at) > Date.now();
  }

  function restore() {
    const stored = storage(store => store.getItem(KEY));
    if (stored) {
      try {
        const value = JSON.parse(stored);
        if (valid(value)) return value;
      } catch {
        // Ignore a corrupted entry.
      }
      storage(store => store.removeItem(KEY));
    }
    return null;
  }

  function scheduleExpiry() {
    clearTimeout(expiryTimer);
    if (!session) return;
    const remaining = Date.parse(session.expires_at) - Date.now();
    expiryTimer = setTimeout(() => {
      if (valid(session)) scheduleExpiry();
      else clearSession('expired');
    }, Math.max(0, Math.min(remaining, 2147483647)));
  }

  function setSession(value) {
    if (!valid(value)) throw new ApiError('Сервер не выдал действующую сессию.', 0, 'invalid_session');
    session = { access_token: value.access_token, expires_at: value.expires_at, user_id: value.user_id };
    storage(store => store.setItem(KEY, JSON.stringify(session)));
    scheduleExpiry();
    for (const listener of listeners) listener(session, 'authenticated');
  }

  function clearSession(reason = 'logout') {
    if (!session) return;
    session = null;
    clearTimeout(expiryTimer);
    storage(store => store.removeItem(KEY));
    for (const listener of listeners) listener(null, reason);
  }

  function getSession() {
    if (session && !valid(session)) clearSession('expired');
    return session;
  }

  function onSessionChange(listener) {
    listeners.add(listener);
    return () => listeners.delete(listener);
  }

  async function request(path, {
    method = 'GET', body, signal, response = 'json', auth = true,
  } = {}) {
    if (typeof path !== 'string' || !path.startsWith('/v1/')) {
      throw new ApiError('Некорректный адрес API.', 0, 'invalid_api_path');
    }
    const url = new URL(path, location.origin);
    if (url.origin !== location.origin || !url.pathname.startsWith('/v1/')) {
      throw new ApiError('Некорректный адрес API.', 0, 'invalid_api_path');
    }
    const current = auth ? getSession() : null;
    if (auth && !current) throw new ApiError('Войдите в аккаунт учителя.', 401, 'authentication_required');

    const headers = { Accept: response === 'blob' ? 'image/*' : 'application/json' };
    if (current) headers.Authorization = `Bearer ${current.access_token}`;
    if (body !== undefined && !(body instanceof FormData)) {
      headers['Content-Type'] = 'application/json';
      body = JSON.stringify(body);
    }

    let reply;
    try {
      reply = await fetch(url, {
        method, headers, body, signal, cache: 'no-store', credentials: 'omit', redirect: 'error',
      });
    } catch (error) {
      if (signal?.aborted || error.name === 'AbortError') throw error;
      throw new ApiError('Нет связи с сервером. Проверьте подключение и попробуйте снова.', 0, 'network_error');
    }

    if (!reply.ok) {
      const error = await reply.json().catch(() => null);
      if (reply.status === 401 && current === session) clearSession('expired');
      throw new ApiError(
        error?.error?.message || 'Сервер не смог выполнить запрос. Попробуйте снова.',
        reply.status,
        error?.error?.code || 'request_failed',
      );
    }
    if (reply.status === 204) return null;
    if (response === 'blob') {
      if (!reply.headers.get('Content-Type')?.startsWith('image/')) {
        throw new ApiError('Сервер вернул страницу в неожиданном формате.', 0, 'invalid_response');
      }
      return reply.blob();
    }
    try {
      return await reply.json();
    } catch (error) {
      if (signal?.aborted) throw error;
      throw new ApiError('Сервер вернул неожиданный ответ.', 0, 'invalid_response');
    }
  }

  scheduleExpiry();
  window.SK = window.SK || {};
  window.SK.api = { request, getSession, setSession, clearSession, onSessionChange };
})();
