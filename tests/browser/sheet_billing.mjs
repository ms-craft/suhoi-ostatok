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
  await page.locator('[name=email]').fill(`billing-${randomUUID()}@example.invalid`);
  await page.locator('[name=password]').fill('browser-test-only-passphrase');
  await page.locator('#auth-form [type=submit]').click();
  await page.locator('.appbar [data-act=billing]').click();
  await page.getByText('Бесплатные: 30 · По подписке: 0 · Докупленные: 0').waitFor();
  assert.match(await page.locator('[data-billing-content]').innerText(), /19,90 BYN/);
  assert.match(await page.locator('[data-billing-content]').innerText(), /3,90 BYN/);
  assert.equal(await page.locator('[data-billing-action=checkout][data-plan=topup140]').isDisabled(), true);
  await page.locator('[data-billing-action=checkout][data-plan=monthly1000]').click();
  await page.locator('[data-billing-action=confirm]').click();
  await page.getByText('Бесплатные: 30 · По подписке: 1000 · Докупленные: 0').waitFor();
  assert.equal(await page.locator('[data-billing-action=checkout][data-plan=topup140]').isEnabled(), true);
  await page.locator('[data-billing-action=checkout][data-plan=topup140]').click();
  await page.locator('[data-billing-action=confirm]').click();
  await page.getByText('Бесплатные: 30 · По подписке: 1000 · Докупленные: 140').waitFor();
  assert.deepEqual(errors, []);
  console.log('OK free balance, monthly plan and top-up test checkout');
} finally {
  await browser.close();
}
