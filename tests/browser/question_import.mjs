import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { randomUUID } from 'node:crypto';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const browser = await chromium.launch({ executablePath: process.env.BROWSER_BIN || undefined, headless: true });
const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
const errors = [];
page.on('pageerror', error => errors.push(error.message));
try {
  await page.goto(process.env.SITE_URL || 'http://127.0.0.1:58101');
  await page.locator('[data-act=auth-mode]').click();
  await page.locator('[name=full_name]').fill('Тестовый учитель');
  await page.locator('[name=email]').fill(`fixture-${randomUUID()}@example.invalid`);
  await page.locator('[name=password]').fill('browser-test-only-passphrase');
  await page.locator('#auth-form [type=submit]').click();
  await page.locator('[data-act=add-test]').click();
  await page.locator('#workspace-form [name=name]').fill('Тестовая работа');
  await page.locator('#workspace-form [type=submit]').click();
  await page.getByRole('button', { name: /Тестовая работа Заданий/ }).click();
  const image = await page.evaluate(() => {
    const canvas = document.createElement('canvas');
    canvas.width = 100; canvas.height = 100;
    return canvas.toDataURL('image/png').split(',')[1];
  });
  const photos = ['page-1.png', 'page-2.png'].map(name => ({
    name, mimeType: 'image/png', buffer: Buffer.from(image, 'base64'),
  }));
  const uploadedPages = [];
  page.on('request', request => {
    if (request.method() === 'POST' && request.url().endsWith('/v1/question-import')) uploadedPages.push(request);
  });
  await page.locator('#question-photo-form [name=photo]').setInputFiles(photos);
  await page.locator('#question-photo-form [type=submit]').click();
  await page.locator('#import-answers-form').waitFor();
  assert.equal(await page.locator('[data-import-field=question]').count(), 4);
  assert.equal(uploadedPages.length, 2);
  assert.equal(await page.locator('[data-import-field=question]').nth(0).inputValue(), await page.locator('[data-import-field=question]').nth(2).inputValue());
  const question = page.locator('[data-import-field=question]').first();
  const preview = question.locator('..').locator('[data-math-preview]');
  assert.equal(await preview.isVisible(), false);
  await question.fill('Вычислите $\\frac{2}{3}$');
  assert.equal(await preview.isVisible(), true);
  assert.equal(await preview.locator('.katex').count(), 1);
  await question.fill('Вычислите 2 + 3');
  assert.equal(await preview.isVisible(), false);
  await page.locator('#import-answers-form [type=submit]').click();
  assert.equal(await page.locator('.question-card').count(), 0);
  assert.equal(await page.locator('[data-import-field=answer]:invalid').count(), 4);
  await question.fill('Вычислите 2 + 3');
  await page.locator('[data-import-field=answer]').nth(0).fill('5');
  await page.locator('[data-import-field=answer]').nth(1).fill('2');
  await page.locator('[data-import-field=answer]').nth(2).fill('5');
  await page.locator('[data-import-field=answer]').nth(3).fill('2');
  await page.locator('#import-answers-form [type=submit]').click();
  assert.equal(await page.locator('.question-card').count(), 4);
  assert.match(await page.locator('.question-card').first().innerText(), /Вычислите 2 \+ 3/);
  assert.match(await page.locator('.question-card').first().innerText(), /Правильный ответ: 5/);
  await page.getByRole('button', { name: 'Редактировать задание 1' }).click();
  await page.locator('#edit-question-form [name=answer]').fill('$\\frac{7}{2}$');
  assert.equal(await page.locator('#edit-question-form [data-math-preview] .katex').count(), 1);
  await page.locator('#edit-question-form [type=submit]').click();
  assert.match(await page.locator('.question-card').first().innerText(), /Правильный ответ:/);
  assert.equal(await page.locator('.question-card').first().locator('.katex').count(), 1);
  await page.getByRole('button', { name: 'Редактировать задание 1' }).click();
  await page.locator('#edit-question-form [name=answer]').fill('999');
  await page.locator('[data-act=cancel-edit-question]').click();
  assert.equal(await page.locator('.question-card').first().locator('.katex').count(), 1);
  await page.reload();
  await page.getByRole('button', { name: /Тестовая работа Заданий: 4/ }).click();
  assert.equal(await page.locator('.question-card').count(), 4);
  assert.equal(await page.locator('.question-card').first().locator('.katex').count(), 1);
  assert.match(await page.locator('.question-card').nth(1).innerText(), /Правильный ответ: 2/);
  let attempts = 0;
  await page.route('**/v1/question-import', async route => {
    attempts++;
    if (attempts === 2) return route.fulfill({ status: 502, contentType: 'application/json',
      body: JSON.stringify({ error: { code: 'ai_invalid_response', message: 'Не удалось распознать задания.' } }) });
    return route.continue();
  });
  await page.locator('#question-photo-form [name=photo]').setInputFiles(photos);
  await page.locator('#question-photo-form [type=submit]').click();
  await page.locator('#import-answers-form').waitFor();
  assert.equal(await page.locator('[data-import-field=question]').count(), 2);
  assert.match(await page.locator('#import-answers-form [role=alert]').innerText(), /page-2\.png/);
  assert.match(await page.locator('#import-answers-form [role=alert]').innerText(), /Сохраните найденные задания/);
  assert.deepEqual(errors, []);
  console.log('OK photo import, editing, answers, saving and reload');
} finally {
  await browser.close();
}
