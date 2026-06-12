import { expect, test } from '@playwright/test';

test('search result opens detail with media and actions', async ({ page }) => {
  await page.goto('/');

  await page.getByLabel('Search memes').fill('cat reaction');
  await page.getByRole('button', { name: 'Search' }).click();

  await expect(page.getByText('Results for “cat reaction”')).toBeVisible();

  const result = page.getByRole('link', { name: 'Open Smoke test cat reaction' });
  await expect(result).toBeVisible();
  await result.click();

  await expect(page).toHaveURL(/\/memes\/smoke-test-cat-reaction$/);
  await expect(page.getByRole('heading', { name: 'Smoke test cat reaction' })).toBeVisible();
  await expect(page.getByRole('img', { name: 'Smoke test cat reaction' })).toBeVisible();

  await expect(page.getByRole('button', { name: 'Like (7)' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Save', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Pin' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Meme actions' })).toBeVisible();
});
