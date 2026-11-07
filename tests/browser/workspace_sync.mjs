import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { randomUUID } from 'node:crypto';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const browser = await chromium.launch({ executablePath: process.env.BROWSER_BIN || undefined, headless: true });
const origin = process.env.SITE_URL || 'http://127.0.0.1:58101';
const credentials = { email: `sync-${randomUUID()}@example.invalid`, password: 'browser-test-only-passphrase' };
const a = await browser.newContext();
const b = await browser.newContext();
const first = await a.newPage();
const second = await b.newPage();
async function login(page, register) {
  await page.goto(origin);
  if (register) {
    await page.locator('[data-act=auth-mode]').click();
    await page.locator('[name=full_name]').fill('Тестовый учитель');
  }
  await page.locator('[name=email]').fill(credentials.email);
  await page.locator('[name=password]').fill(credentials.password);
  await page.locator('#auth-form [type=submit]').click();
  await page.locator('[data-act=add-class]').waitFor();
}
async function createClass(page, name) {
  await page.locator('[data-act=add-class]').click();
  await page.locator('#workspace-form [name=name]').fill(name);
  await page.locator('#workspace-form [type=submit]').click();
  await page.getByRole('button', { name: new RegExp(name) }).first().waitFor();
}
try {
  await login(first, true);
  const synced = first.waitForResponse(response => response.url().endsWith('/v1/workspace') && response.request().method() === 'PUT' && response.request().postData()?.includes('7 А') && response.ok());
  await createClass(first, '7 А');
  await synced;
  await login(second, false);
  assert.equal(await second.getByRole('button', { name: /7 А Работ/ }).count(), 1);
  const updated = first.waitForResponse(response => response.url().endsWith('/v1/workspace') && response.request().method() === 'PUT' && response.request().postData()?.includes('8 А') && response.ok());
  await createClass(first, '8 А');
  await updated;
  await createClass(second, '9 А');
  await second.getByRole('heading', { name: 'Выберите версию данных' }).waitFor();
  await second.getByRole('button', { name: 'Загрузить с сервера' }).click();
  assert.equal(await second.getByRole('button', { name: /8 А Работ/ }).count(), 1);
  assert.equal(await second.getByRole('button', { name: /9 А Работ/ }).count(), 0);
  const backup = await second.evaluate(() => Object.keys(localStorage).find(key => key.endsWith('.conflict-backup')));
  assert(backup, 'local version must be backed up before conflict resolution');
  const old = await browser.newContext();
  const legacyPage = await old.newPage();
  const registration = await old.request.post(`${origin}/v1/auth/register`, { data: {
    email: `legacy-${randomUUID()}@example.invalid`, password: credentials.password, full_name: 'Другой учитель',
  } });
  assert.equal(registration.status(), 201);
  const session = await registration.json();
  await legacyPage.goto(origin);
  await legacyPage.evaluate(session => {
    sessionStorage.setItem('stopkadr.session.v1', JSON.stringify(session));
    localStorage.setItem(`stopkadr.workspace.${session.user_id}`, JSON.stringify({
      classes: [{id: 'legacy-class', name: 'Старый класс'}], tests: [], links: {},
    }));
  }, session);
  const migration = legacyPage.waitForResponse(response => response.url().endsWith('/v1/workspace') && response.request().method() === 'PUT' && response.ok());
  await legacyPage.reload();
  await legacyPage.getByRole('button', { name: /Старый класс Работ/ }).waitFor();
  await migration;
  const migrated = await old.request.get(`${origin}/v1/workspace`, { headers: { Authorization: `Bearer ${session.access_token}` } });
  assert.equal((await migrated.json()).workspace.classes[0].name, 'Старый класс');
  await old.close();
  console.log('OK cross-device sync, version conflict and local backup');
} finally {
  await browser.close();
}
