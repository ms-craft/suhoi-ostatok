import assert from 'node:assert/strict';
import { mkdir, readFile, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import { randomUUID } from 'node:crypto';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const url = process.env.SITE_URL || 'http://127.0.0.1:5173';
const artifacts = process.env.BROWSER_ARTIFACTS;
const collector = process.env.DESIGN_QA_COLLECTOR;
const browser = await chromium.launch({
  executablePath: process.env.BROWSER_BIN || undefined,
  headless: true,
});
const failures = [];

async function openBrowserPage(options) {
  const context = await browser.newContext({ ...options, reducedMotion: 'reduce' });
  await context.addInitScript(() => {
    const revoke = URL.revokeObjectURL.bind(URL);
    window.browserTestRevokedUrls = new Set();
    URL.revokeObjectURL = url => {
      window.browserTestRevokedUrls.add(url);
      revoke(url);
    };
  });
  const page = await context.newPage();
  page.setDefaultTimeout(10000);
  page.on('pageerror', error => failures.push(error.message));
  return { context, page };
}

let { context, page } = await openBrowserPage({ viewport: { width: 1280, height: 900 } });

const act = action => page.locator(`[data-act="${action}"]`);
const billingHeader = () => page.locator('.appbar [data-act="billing"]');
const billing = action => page.locator(`[data-billing-action="${action}"]`);
const screen = name => page.locator(`#view[data-screen="${name}"]`).waitFor();
const apiPath = response => new URL(response.url()).pathname;

async function responseFor(path, method, trigger) {
  const [response] = await Promise.all([
    page.waitForResponse(response =>
      apiPath(response) === path && response.request().method() === method),
    trigger(),
  ]);
  assert(response.ok(), `${method} ${path} returned HTTP ${response.status()}`);
  if (!path.startsWith('/v1/auth/')) {
    const authorization = await response.request().headerValue('authorization');
    assert(authorization?.startsWith('Bearer '), `${method} ${path} omitted authorization`);
    assert(!response.url().includes(authorization.slice(7)), 'Authorization appeared in a request URL');
  }
  if (method === 'POST' && (path === '/v1/submissions' || path.endsWith('/clarifications'))) {
    assert.match(await response.request().headerValue('content-type'), /^multipart\/form-data; boundary=/);
  }
  return response.status() === 204 ? undefined : response.json();
}

async function layout(name) {
  const size = await page.evaluate(() => ({
    root: document.documentElement.scrollWidth,
    view: document.documentElement.clientWidth,
    panel: document.querySelector('#main').scrollWidth,
    panelWidth: document.querySelector('#main').clientWidth,
    dialog: document.querySelector('.modal.open .modal-body')?.scrollWidth ?? 0,
    dialogWidth: document.querySelector('.modal.open .modal-body')?.clientWidth ?? 0,
  }));
  assert(size.root <= size.view + 1, `${name}: horizontal page overflow ${JSON.stringify(size)}`);
  assert(size.panel <= size.panelWidth + 1, `${name}: horizontal content overflow ${JSON.stringify(size)}`);
  assert(size.dialog <= size.dialogWidth + 1, `${name}: horizontal dialog overflow ${JSON.stringify(size)}`);
  if (!artifacts) return;
  await page.screenshot({ path: join(artifacts, `${name}.png`) });
  if (collector) {
    // DevTools evaluation leaves the application's restrictive CSP in effect.
    await page.evaluate(source => (0, eval)(source), await readFile(collector, 'utf8'));
    const snapshot = await page.evaluate(() => window.designQaSnapshot({ focus: false }));
    const visited = new Set();
    let wrapped = false;
    for (let index = 0; index < 60; index++) {
      await page.keyboard.press('Tab');
      const probe = await page.evaluate(() => window.designQaFocusProbe());
      if (!probe) continue;
      if (visited.has(probe.id)) {
        wrapped = true;
        break;
      }
      visited.add(probe.id);
      const element = snapshot.elements.find(candidate => candidate.id === probe.id);
      if (!element) continue;
      const changed = Object.keys(probe.styles).filter(property => element.rest?.[property] !== probe.styles[property]);
      element.focus = {
        indicator: probe.outlineOn || probe.shadowOn || changed.length > 0,
        obscured: probe.obscured, how: 'keyboard', changed,
      };
    }
    snapshot.keyboard_pass = { reached: visited.size, wrapped };
    await writeFile(join(artifacts, `${name}.json`), JSON.stringify(snapshot));
  }
}

async function syntheticImage(filename, color, equation = '') {
  const mimeType = filename.endsWith('.jpg') ? 'image/jpeg' : 'image/png';
  const base64 = await page.evaluate(({ color, equation, mimeType }) => {
    const canvas = document.createElement('canvas');
    canvas.width = 240;
    canvas.height = 280;
    const draw = canvas.getContext('2d');
    draw.fillStyle = color;
    draw.fillRect(0, 0, canvas.width, canvas.height);
    if (equation) {
      draw.fillStyle = '#111111';
      draw.font = '20px sans-serif';
      draw.fillText(equation, 20, 60);
    }
    return canvas.toDataURL(mimeType).split(',')[1];
  }, { color, equation, mimeType });
  return { name: filename, mimeType, buffer: Buffer.from(base64, 'base64') };
}

function blankPdf() {
  const objects = [
    '<< /Type /Catalog /Pages 2 0 R >>',
    '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
    '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 240 280] /Contents 4 0 R >>',
    '<< /Length 0 >>\nstream\n\nendstream',
  ];
  let file = '%PDF-1.4\n';
  const offsets = objects.map((object, index) => {
    const offset = file.length;
    file += `${index + 1} 0 obj\n${object}\nendobj\n`;
    return offset;
  });
  const xref = file.length;
  file += 'xref\n0 5\n0000000000 65535 f \n';
  for (const offset of offsets) file += `${String(offset).padStart(10, '0')} 00000 n \n`;
  file += `trailer\n<< /Size 5 /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return { name: 'blank.pdf', mimeType: 'application/pdf', buffer: Buffer.from(file) };
}

async function showResults(id, trigger) {
  const result = await responseFor(`/v1/submissions/${id}/results`, 'GET', trigger);
  await screen('result');
  return result;
}

async function pickFiles(button, files) {
  const [chooser] = await Promise.all([
    page.waitForEvent('filechooser'),
    act(button).click(),
  ]);
  await chooser.setFiles(files);
}

async function deleteWork(id) {
  await act('delete').click();
  await responseFor(`/v1/submissions/${id}`, 'DELETE', () => act('delete-confirm').click());
  await screen('sheets');
  assert.equal(await act('open-sheet').count(), 0);
}

try {
  if (artifacts) await mkdir(artifacts, { recursive: true });
  await page.goto(url);
  await screen('auth');
  assert.match(await page.title(), /^Сухой остаток/);
  await page.waitForFunction(() => document.querySelector('header img.mark')?.naturalWidth > 0);
  await layout('auth-desktop');
  const credentials = { email: `teacher-${randomUUID()}@example.invalid`, password: 'browser-test-only-passphrase' };
  const fillCredentials = async () => {
    const authForm = page.locator('#auth-form');
    await authForm.locator('[name=email]').fill(credentials.email);
    await authForm.locator('[name=password]').fill(credentials.password);
    return authForm.locator('[type=submit]');
  };
  await act('auth-mode').click();
  const register = await fillCredentials();
  await responseFor('/v1/auth/register', 'POST', () => register.click());
  await screen('scan');
  await act('logout').click();
  await screen('auth');
  const signIn = await fillCredentials();
  const login = await responseFor('/v1/auth/login', 'POST', () => signIn.click());
  const protectedRequest = { headers: { Authorization: `Bearer ${login.access_token}` } };
  await screen('scan');
  assert.doesNotMatch(await page.locator('#view').innerText(), /из 28 загружено|эталон заполнен/);
  await layout('scan-desktop');
  console.log('OK registration, logout, login');

  const inactive = await responseFor('/v1/subscription', 'GET', () => billingHeader().click());
  assert.equal(inactive.status, 'inactive');
  await screen('billing');
  await layout('billing-desktop');
  const checkout = await responseFor('/v1/subscription/checkout', 'POST', () => billing('checkout').click());
  assert.equal(checkout.demonstration, true);
  const confirmation = await responseFor(`/v1/payments/fake/checkout/${checkout.checkout_id}/confirm`, 'POST',
    () => billing('confirm').click());
  assert.equal(confirmation.applied, true);
  assert.equal(confirmation.demonstration, true);
  await page.getByText('2 из 2', { exact: true }).waitFor();
  await billing('back').click();
  await screen('scan');
  console.log('OK test checkout and quota');

  // This fixture color triggers one terminal failure, then succeeds on manual retry.
  const yellow = await syntheticImage('page-one.jpg', '#ffff00', `2 + 2 = 5 · ${randomUUID().slice(0, 8)}`);
  const blue = await syntheticImage('page-two.png', '#b5d7f2', '3 + 3 = 7');
  const white = await syntheticImage('blank-page.png', '#ffffff');
  await pickFiles('gallery', [yellow, blue]);
  await pickFiles('capture', white);
  await act('remove-file').last().click();
  assert.equal(await act('remove-file').count(), 2);

  const statusRoute = /\/v1\/submissions\/[a-f0-9-]+$/;
  let interruptStatus = true;
  let requests = 0;
  const watchStatus = async route => {
    if (route.request().method() === 'GET') {
      requests++;
      if (interruptStatus) {
        interruptStatus = false;
        await route.abort('internetdisconnected');
        return;
      }
    }
    await route.continue();
  };
  await page.route(statusRoute, watchStatus);
  const uploaded = await responseFor('/v1/submissions', 'POST', () => act('start-check').click());
  assert.equal(uploaded.page_count, 2);
  assert.equal(uploaded.status, 'queued');
  await screen('check');
  await act('refresh-work').waitFor();
  assert(await page.locator('.form-error[role=alert]').first().isVisible());
  assert.doesNotMatch(await page.locator('#view').innerText(), /\d+\s*%/);
  await layout('check-network-error-desktop');
  await act('refresh-work').click();
  await act('retry').waitFor();
  await page.getByText('Ошибка проверки', { exact: true }).waitFor();
  assert(await page.locator('.form-error[role=alert]').first().isVisible());
  const released = await context.request.get(new URL('/v1/subscription', url).href, protectedRequest);
  assert.equal((await released.json()).checks_remaining, 2, 'Failed job must release its reservation');
  await layout('check-worker-failure-desktop');
  const afterFailure = requests;
  await page.waitForTimeout(1200);
  assert.equal(requests, afterFailure, 'Status polling continued after failure');
  const results = await showResults(uploaded.id, async () => {
    const retry = await responseFor(`/v1/submissions/${uploaded.id}/retry`, 'POST', () => act('retry').click());
    assert.equal(retry.id, uploaded.id);
    assert.equal(retry.status, 'queued');
  });
  const afterTerminal = requests;
  await page.waitForTimeout(2200);
  assert.equal(requests, afterTerminal, 'Status polling continued after completion');
  await page.unroute(statusRoute, watchStatus);

  assert.equal(results.demonstration, true);
  assert.deepEqual(results.tasks.map(task => task.source_page), [1, 2]);
  await page.getByText(/демонстрационн|тестовые данные/i).first().waitFor();
  await layout('result-desktop');
  const task = results.tasks[0];
  await act('open-task').first().click();
  await page.getByRole('dialog').waitFor();
  const sourcePath = task.source_url;
  const preview = page.locator('img.page-image').first();
  await preview.waitFor();
  await page.waitForFunction(() => document.querySelector('img.page-image')?.naturalWidth > 0);
  const protectedImageUrl = await preview.getAttribute('src');
  assert.match(protectedImageUrl, /^blob:/);
  assert.equal((await context.request.get(new URL(sourcePath, url).href)).status(), 401);
  await layout('source-desktop');
  console.log('OK multipart pages, queued/failed states, network recovery, manual retry, protected page and terminal polling');

  const taskPath = `/v1/submissions/${uploaded.id}/tasks/${task.id}`;
  await act('edit').click();
  const correction = page.locator('#correction-form');
  await correction.locator('[name=number]').fill('2');
  await correction.locator('[name=answer]').fill('4');
  const edited = await responseFor(taskPath, 'PATCH', () => page.locator('[form=correction-form]').click());
  assert.equal(edited.teacher.answer, '4');
  assert.equal(edited.teacher.number, '2');
  assert.equal(edited.preliminary.status, 'needs_review');
  await page.getByText('Первоначально распознанный номер').waitFor();
  assert.equal(edited.confirmed, false);
  const rechecked = await showResults(uploaded.id, () => act('recheck').click());
  assert.equal(rechecked.tasks.find(item => item.id === task.id).preliminary.status, 'correct');
  assert.equal(rechecked.tasks.find(item => item.id === task.id).teacher.number, '2');
  const confirmed = await responseFor(taskPath + '/confirm', 'POST', () => act('confirm').click());
  assert.equal(confirmed.confirmed, true);
  assert.equal(confirmed.teacher.final_status, 'correct');
  if (await page.locator('#modal.open').isVisible()) await act('close-modal').last().click();

  await act('clarify').click();
  const clarification = page.locator('#clarification-form');
  await clarification.locator('[name=text]').fill('Комментарий ученику.');
  await clarification.locator('input[type=file]').setInputFiles(blue);
  const messageResponse = page.waitForResponse(response => response.url().endsWith(`/v1/telegram/submissions/${uploaded.id}/message`) && response.request().method() === 'POST');
  await page.locator('[form=clarification-form]').click();
  assert.equal((await messageResponse).status(), 409);
  await page.locator('#modalError').waitFor();
  await act('close-modal').last().click();

  await page.setViewportSize({ width: 320, height: 740 });
  await layout('result-mobile-320');
  await act('open-task').first().click();
  await preview.waitFor();
  await layout('source-mobile-320');
  await act('close-modal').last().click();
  await deleteWork(uploaded.id);
  await page.waitForFunction(src => window.browserTestRevokedUrls.has(src), protectedImageUrl);
  assert.equal((await context.request.get(new URL(sourcePath, url).href, protectedRequest)).status(), 404);
  console.log('OK correction, recheck, confirmation, Telegram message validation, deletion and mobile result');

  await context.close();
  ({ context, page } = await openBrowserPage({
    viewport: { width: 390, height: 844 },
    screen: { width: 390, height: 844 },
    isMobile: true,
    hasTouch: true,
  }));
  await page.goto(url);
  await screen('auth');
  await layout('auth-mobile-390');
  const mobileSignIn = await fillCredentials();
  await responseFor('/v1/auth/login', 'POST', () => mobileSignIn.click());
  await screen('scan');
  await page.locator('#view [data-act=screen][data-screen=sheets]').first().click();
  await screen('sheets');
  await layout('sheets-mobile-390');
  await page.locator('#view [data-act=screen][data-screen=scan]').first().click();
  await screen('scan');
  await layout('scan-mobile-390');
  await pickFiles('gallery', blankPdf());
  const emptyWork = await responseFor('/v1/submissions', 'POST', () => act('start-check').click());
  await screen('result');
  await page.getByText(/Задания не найдены/).first().waitFor();
  await layout('empty-result-mobile-390');
  await deleteWork(emptyWork.id);
  await page.locator('#view [data-act=screen][data-screen=scan]').first().click();
  await screen('scan');
  await pickFiles('gallery', yellow);
  if (await act('start-check').isEnabled()) await act('start-check').click();
  await page.getByText(/лимит|осталось 0|нет доступных проверок/i).first().waitFor();
  const exhausted = await responseFor('/v1/subscription', 'GET', () => billingHeader().click());
  assert.equal(exhausted.checks_remaining, 0);
  await page.getByText('0 из 2', { exact: true }).waitFor();
  await layout('exhausted-mobile-390');
  const cancelled = await responseFor('/v1/subscription/cancel', 'POST', () => billing('cancel').click());
  assert.equal(cancelled.cancel_at_period_end, true);
  assert.equal(cancelled.status, 'active');
  console.log('OK PDF with no detected tasks, exhausted limit and cancellation');

  await page.route('**/v1/subscription', route => route.fulfill({
    status: 401,
    contentType: 'application/json',
    body: JSON.stringify({ error: { code: 'authentication_required', message: 'Войдите в аккаунт учителя' } }),
  }));
  await billing('back').click();
  if (await billingHeader().isVisible()) await billingHeader().click();
  await screen('auth');
  assert.equal(await page.evaluate(() => sessionStorage.length), 0, 'Expired credentials remained in tab storage');
  assert.deepEqual(failures, [], 'Uncaught browser errors');
  console.log(`OK session expiry; Chromium ${browser.version()}`);
} catch (error) {
  if (artifacts) await page.screenshot({ path: join(artifacts, 'failure.png') }).catch(() => {});
  throw error;
} finally {
  await browser.close();
}

