import { expect, test } from '@playwright/test';

test.describe('public masonry feed smoke', () => {
  test('desktop feed keeps keyboard order and accessible Load more', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await disableIntersectionObserver(page);
    await page.goto('/');

    const feed = page.getByRole('list', { name: 'Meme results' });
    await expect(feed).toBeVisible();
    await expect(feed).toHaveAttribute('data-column-count', /^[2-4]$/);

    const firstCardLink = page.getByRole('link', { name: 'Open Smoke test cat reaction' });
    const firstCardMenu = page.getByRole('button', { name: 'Actions for Smoke test cat reaction' });
    await expect(firstCardLink).toBeVisible();
    await expect(firstCardMenu).toBeVisible();

    await tabUntilFocused(page, firstCardLink);
    await page.keyboard.press('Tab');
    await expect(firstCardMenu).toBeFocused();
    await firstCardMenu.press('Enter');
    await expect(firstCardMenu).toHaveAttribute('aria-expanded', 'true');
    await expect(page.getByRole('menuitem', { name: /Like meme|Unlike meme/ })).toBeVisible();
    await page.keyboard.press('Escape');

    const loadMore = page.getByRole('button', { name: 'Load more' });
    await expect(loadMore).toBeVisible();
    await expect(loadMore).toHaveAccessibleDescription(/More results also load automatically|Automatic loading is unavailable/);
    await loadMore.click();
    await expect(page.getByRole('link', { name: 'Open Smoke test deploy mood' })).toBeVisible();
    await expect(page.getByText('Showing 2 of 2')).toBeVisible();
  });

  test('mobile viewport exposes the feed without layout breakage', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await disableIntersectionObserver(page);
    await page.goto('/');

    const feed = page.getByRole('list', { name: 'Meme results' });
    await expect(feed).toBeVisible();
    await expect(feed).toHaveAttribute('data-column-count', '1');
    await expect(page.getByRole('link', { name: 'Open Smoke test cat reaction' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Actions for Smoke test cat reaction' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Load more' })).toBeVisible();
  });
});

test('search result opens detail with media and actions', async ({ page }) => {
  await page.goto('/');

  const searchInput = page.getByLabel('Search memes');
  await searchInput.fill('cat reaction');
  await searchInput.press('Enter');

  await expect(page.getByText('Results for “cat reaction”')).toBeVisible();

  const result = page.getByRole('link', { name: 'Open Smoke test cat reaction' });
  await expect(result).toBeVisible();
  await result.click();

  await expect(page).toHaveURL(/\/memes\/smoke-test-cat-reaction$/);
  await expect(page.getByRole('heading', { name: 'Smoke test cat reaction' })).toBeVisible();
  await expect(page.getByRole('img', { name: 'Smoke test cat reaction' })).toBeVisible();

  await expect(page.getByRole('button', { name: 'Like (7)' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Save', exact: true })).toBeVisible();
  await expect(page.getByText('Pin requires a full account')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Pin', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Meme actions' })).toBeVisible();
});

async function tabUntilFocused(page: import('@playwright/test').Page, locator: import('@playwright/test').Locator) {
  for (let attempt = 0; attempt < 20; attempt += 1) {
    await page.keyboard.press('Tab');
    if (await locator.evaluate((element) => element === document.activeElement)) return;
  }

  await expect(locator).toBeFocused();
}

async function disableIntersectionObserver(page: import('@playwright/test').Page) {
  await page.addInitScript(() => {
    Object.defineProperty(window, 'IntersectionObserver', {
      configurable: true,
      value: class NoopIntersectionObserver {
        observe() {}
        disconnect() {}
      }
    });
  });
}
