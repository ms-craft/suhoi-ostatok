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
  await page.locator('#question-photo-form [name=photo]').setInputFiles({
    name: 'fixture.png', mimeType: 'image/png', buffer: Buffer.from(image, 'base64'),
  });
  await page.locator('#question-photo-form [type=submit]').click();
  await page.locator('#import-answers-form').waitFor();
  assert.equal(await page.locator('[data-import-field=question]').count(), 2);
  await page.locator('#import-answers-form [type=submit]').click();
  assert.equal(await page.locator('.question-card').count(), 0);
  assert.equal(await page.locator('[data-import-field=answer]:invalid').count(), 2);
  await page.locator('[data-import-field=question]').first().fill('Вычислите 2 + 3');
  await page.locator('[data-import-field=answer]').nth(0).fill('5');
  await page.locator('[data-import-field=answer]').nth(1).fill('2');
  await page.locator('#import-answers-form [type=submit]').click();
  assert.equal(await page.locator('.question-card').count(), 2);
  assert.match(await page.locator('.question-card').first().innerText(), /Вычислите 2 \+ 3/);
  assert.match(await page.locator('.question-card').first().innerText(), /Правильный ответ: 5/);
  await page.reload();
  await page.getByRole('button', { name: /Тестовая работа Заданий: 2/ }).click();
  assert.equal(await page.locator('.question-card').count(), 2);
  assert.match(await page.locator('.question-card').nth(1).innerText(), /Правильный ответ: 2/);
  assert.deepEqual(errors, []);
  console.log('OK photo import, editing, answers, saving and reload');
} finally {
  await browser.close();
}
