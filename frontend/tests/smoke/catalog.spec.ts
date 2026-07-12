import { expect, test } from '@playwright/test';

const seededCollectionId = 'smoke-private-team-saves';
const seededCollectionQuery = 'vault reaction';

test.describe('public masonry feed smoke', () => {
  test('desktop feed keeps keyboard order and accessible Load more', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await disableIntersectionObserver(page);
    await page.goto('/');

    const motd = page.getByRole('region', { name: 'Meme of the Day' });
    await expect(motd).toBeVisible();
    await expect(motd.getByRole('link', { name: 'Open Smoke test cat reaction' })).toBeVisible();

    const feed = page.getByRole('list', { name: 'Meme results' });
    await expect(feed).toBeVisible();
    await expect(feed).toHaveAttribute('data-column-count', /^[2-4]$/);

    const firstCardLink = feed.getByRole('link', { name: 'Open Smoke test cat reaction' });
    const firstCardMenu = feed.getByRole('button', { name: 'Actions for Smoke test cat reaction' });
    await expect(firstCardLink).toBeVisible();
    await expect(firstCardMenu).toBeVisible();

    await expect(firstCardLink).toHaveAccessibleName('Open Smoke test cat reaction');
    await expect(firstCardLink).toHaveJSProperty('tabIndex', 0);
    await expect(firstCardLink).not.toHaveAttribute('tabindex', '-1');
    await firstCardLink.focus();
    await expect(firstCardLink).toBeFocused();
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
    await expect(feed.getByRole('link', { name: 'Open Smoke test cat reaction' })).toBeVisible();
    await expect(feed.getByRole('button', { name: 'Actions for Smoke test cat reaction' })).toBeVisible();
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

test.describe('collection-scoped search smoke', () => {
  test('authorized user can search a seeded private shared collection', async ({ baseURL, page }) => {
    await page.context().addCookies([
      {
        name: 'memexpert_access_token',
        value: 'miniapp-full',
        url: baseURL ?? 'http://127.0.0.1:4174',
        httpOnly: true,
        sameSite: 'Lax'
      }
    ]);

    await page.goto(collectionSearchPath());

    await expect(page.getByRole('radio', { name: /Specific collections/ })).toBeChecked();
    await expect(page.getByRole('checkbox', { name: /Smoke private team saves/ })).toBeChecked();
    await expect(page.getByText('1 selected from the current URL')).toBeVisible();
    await expect(page.getByRole('link', { name: 'Open Smoke test vault reaction' })).toBeVisible();
    await expect(page.getByText('Shared', { exact: true })).toBeVisible();
  });

  test('anonymous user cannot see the same seeded collection result', async ({ page }) => {
    await page.goto(collectionSearchPath());

    await expect(page.getByRole('link', { name: 'Open Smoke test vault reaction' })).toHaveCount(0);
    await expect(page.getByText('Sign in with access to this collection to search it.')).toBeVisible();
    await expect(page.getByText('Sign in to load collection choices. Public search remains available.')).toBeVisible();
    await expect(page.getByText('Showing 0 of 0')).toBeVisible();
  });
});

function collectionSearchPath() {
  const params = new URLSearchParams({
    scope: 'collections',
    collection_ids: seededCollectionId,
    q: seededCollectionQuery
  });
  return `/search?${params.toString()}`;
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
