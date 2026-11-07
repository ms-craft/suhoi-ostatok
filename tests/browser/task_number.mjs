import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { randomUUID } from 'node:crypto';
const require = createRequire(import.meta.url);
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const browser = await chromium.launch({ executablePath: process.env.BROWSER_BIN || undefined, headless: true });
const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
try {
  await page.goto(process.env.SITE_URL || 'http://127.0.0.1:58101');
  await page.locator('[data-act=auth-mode]').click();
  await page.locator('[name=full_name]').fill('Тестовый учитель');
  await page.locator('[name=email]').fill(`number-${randomUUID()}@example.invalid`);
  await page.locator('[name=password]').fill('browser-test-only-passphrase');
  await page.locator('#auth-form [type=submit]').click();
  await page.locator('[data-act=add-class]').waitFor();
  await page.locator('[data-act=add-class]').click();
  await page.locator('#workspace-form [name=name]').fill('7 А');
  await page.locator('#workspace-form [type=submit]').click();
  await page.locator('[data-act=open-class]').click();
  const image = await page.evaluate(() => {
    const canvas = document.createElement('canvas'); canvas.width = canvas.height = 100;
    const ctx = canvas.getContext('2d'); ctx.fillStyle = '#b5d7f2'; ctx.fillRect(0, 0, 100, 100);
    return canvas.toDataURL('image/png').split(',')[1];
  });
  await page.locator('#gallery-input').setInputFiles(['page1.png', 'page2.png'].map(name => ({
    name, mimeType: 'image/png', buffer: Buffer.from(image, 'base64'),
  })));
  await page.locator('[data-act=start-check]').click();
  await page.locator('#view[data-screen=result]').waitFor({ timeout: 30000 });
  await page.locator('[data-act=open-task]').first().click();
  await page.locator('[data-act=edit]').click();
  await page.locator('#correction-form [name=number]').fill('2');
  await page.locator('[form=correction-form]').click();
  await page.getByText('Первоначально распознанный номер').waitFor();
  assert.match(await page.locator('#modalBody').innerText(), /Задание № 2/);
  assert.match(await page.locator('#modalBody').innerText(), /Перепроверьте задание/);
  await page.locator('[data-act=recheck]').click();
  await page.getByText('Первоначально распознанный номер').waitFor({ timeout: 30000 });
  assert.match(await page.locator('#modalBody').innerText(), /Задание № 2/);
  await page.locator('[data-act=confirm]').click();
  await page.getByText('Результат подтверждён.').waitFor();
  await page.locator('[data-act=close-modal]').last().click();
  const confirmWork = page.locator('[data-act=confirm-work]');
  assert.equal(await confirmWork.isEnabled(), true);
  assert.match(await confirmWork.innerText(), /Подтвердить всю работу/);
  await confirmWork.click();
  await page.getByText('Работа подтверждена.').waitFor();
  assert.equal(await page.locator('[data-act=confirm-work]').count(), 0);
  console.log('OK task number edit and recheck');
} finally { await browser.close(); }
