import { expect, test } from '@playwright/test';

test('Telegram login updates the reactive shell without a page reload', async ({ page }) => {
  await page.goto('/');
  await page.waitForLoadState('networkidle');
  await expect(page.locator('.app-shell-sign-in')).toBeVisible();
  await page.evaluate(() => {
    (window as Window & { __authSmokeMarker?: string }).__authSmokeMarker = 'same-document';
  });

  await page.locator('.app-shell-sign-in').click();
  await expect(page.getByRole('heading', { name: 'Sign in to MemeXpert' })).toBeVisible();
  await page.getByRole('button', { name: /Continue with Telegram/ }).click();

  await expect(page.getByRole('heading', { name: 'Sign in to MemeXpert' })).toBeHidden();
  await expect(page.locator('.app-shell-account')).toBeVisible();
  await expect(page.locator('.app-shell-sign-in')).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => (window as Window & { __authSmokeMarker?: string }).__authSmokeMarker)).toBe('same-document');

  const cookie = (await page.context().cookies()).find((item) => item.name === 'memexpert_access_token');
  expect(cookie?.value).toMatch(/^modal-full-/);
});
