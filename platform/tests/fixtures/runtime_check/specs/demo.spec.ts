import { test, expect } from '@playwright/test';

const run = process.env.DEMO_RUN_ID;
if (!run || !/^[a-zA-Z0-9-]{1,20}$/.test(run)) throw new Error('DEMO_RUN_ID must be set by the evaluator');
if (process.env.DEMO_BASE_URL_TEMPLATE) throw new Error('template must stay unset');

test.use({ baseURL: process.env.DEMO_BASE_URL });

test('shows the heading', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Runtime Check Demo' })).toBeVisible();
});

test('intentionally fails', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('heading', { name: 'Missing Heading' })).toBeVisible({ timeout: 1000 });
});
