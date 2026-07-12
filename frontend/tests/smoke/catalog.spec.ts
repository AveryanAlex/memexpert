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
    const firstCard = firstCardLink.locator('xpath=ancestor::article');
    const firstCardFavorite = firstCard.getByRole('button', { name: 'Favorite', exact: true });
    const firstCardSave = firstCard.getByRole('button', { name: 'Save', exact: true });
    const firstCardSend = firstCard.getByRole('button', { name: 'Send', exact: true });
    const firstCardMenu = feed.getByRole('button', { name: 'Actions for Smoke test cat reaction' });
    await expect(firstCardLink).toBeVisible();
    await expect(firstCardFavorite).toBeVisible();
    await expect(firstCardSave).toBeVisible();
    await expect(firstCardSend).toBeVisible();
    await expect(firstCardMenu).toBeVisible();
    await expect(page.getByRole('button', { name: 'Select items' })).toHaveCount(0);

    await tabUntilFocused(page, firstCardLink);
    await page.keyboard.press('Tab');
    await expect(firstCardFavorite).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(firstCardSave).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(firstCardSend).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(firstCardMenu).toBeFocused();
    await firstCardMenu.press('Enter');
    await expect(firstCardMenu).toHaveAttribute('aria-expanded', 'true');
    await expect(page.getByRole('menuitem', { name: /Favorite meme|Remove favorite/ })).toBeVisible();
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
    const cardLink = feed.getByRole('link', { name: 'Open Smoke test cat reaction' });
    const card = cardLink.locator('xpath=ancestor::article');
    await expect(card.getByRole('button', { name: 'Favorite', exact: true })).toBeVisible();
    await expect(card.getByRole('button', { name: 'Save', exact: true })).toBeVisible();
    await expect(card.getByRole('button', { name: 'Send', exact: true })).toBeVisible();
    await expect(feed.getByRole('button', { name: 'Actions for Smoke test cat reaction' })).toBeVisible();
    const mediaBox = await card.getByRole('img', { name: 'Smoke test cat reaction' }).boundingBox();
    expect(mediaBox?.y).toBeLessThan(844);
    await expect(page.getByRole('button', { name: 'Load more' })).toBeVisible();
  });

  test('card actions do not overlap at the supported 320px viewport', async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 700 });
    await disableIntersectionObserver(page);
    await page.goto('/');

    const feed = page.getByRole('list', { name: 'Meme results' });
    const card = feed.getByRole('link', { name: 'Open Smoke test cat reaction' }).locator('xpath=ancestor::article');
    const actions = [
      card.getByRole('button', { name: 'Favorite', exact: true }),
      card.getByRole('button', { name: 'Save', exact: true }),
      card.getByRole('button', { name: 'Send', exact: true }),
      card.getByRole('button', { name: 'Actions for Smoke test cat reaction' })
    ];
    const boxes = [];
    for (const action of actions) {
      await expect(action).toBeVisible();
      boxes.push(await action.boundingBox());
    }

    for (let index = 0; index < boxes.length - 1; index += 1) {
      expect((boxes[index]?.x ?? 0) + (boxes[index]?.width ?? 0)).toBeLessThanOrEqual((boxes[index + 1]?.x ?? 0) + 1);
    }
    await expect(page.locator('html')).toHaveJSProperty('scrollWidth', 320);
  });

  test('search selection controls stay hidden until Select items is chosen', async ({ page }) => {
    await disableIntersectionObserver(page);
    await page.goto('/search?q=cat%20reaction');

    const feed = page.getByRole('list', { name: 'Search results' });
    await expect(feed).toBeVisible();
    await expect(feed.getByRole('checkbox')).toHaveCount(0);

    await page.waitForTimeout(250);
    await page.getByRole('button', { name: 'Select items' }).click();
    await expect(feed.getByRole('checkbox', { name: 'Select Smoke test cat reaction' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Clear', exact: true })).toBeVisible();
  });

  test('search filters mount only while open and do not block result controls', async ({ page }) => {
    await page.goto('/search?q=cat%20reaction');

    const result = page.getByRole('link', { name: 'Open Smoke test cat reaction' });
    const dialogContent = page.locator('[data-dialog-content]');
    await expect(result).toBeVisible();
    await expect(dialogContent).toHaveCount(0);

    await page.getByRole('button', { name: 'Filters', exact: true }).click();
    const filterDialog = page.getByRole('dialog', { name: 'Filters' });
    await expect(filterDialog).toBeVisible();
    await expect(filterDialog.getByRole('radio', { name: /Public memes/ })).toBeVisible();
    await expect(dialogContent).toHaveCount(1);
    await filterDialog.getByRole('radio', { name: /Specific collections/ }).check();
    await expect(filterDialog.getByText('Choose at least one collection before searching.')).toBeVisible();
    await expect(filterDialog.getByRole('button', { name: 'Show results' })).toBeDisabled();

    await filterDialog.getByRole('button', { name: 'Close filters' }).click();
    await expect(filterDialog).toHaveCount(0);
    await expect(dialogContent).toHaveCount(0);

    await result.click();
    await expect(page).toHaveURL(/\/memes\/smoke-test-cat-reaction$/);
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

  await expect(page.getByRole('button', { name: 'Favorite (7)' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Save', exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeVisible();
  await expect(page.getByText('Pin requires a full account')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Pin', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Meme actions' })).toBeVisible();

  await page.getByRole('button', { name: 'Favorite (7)' }).click();
  await expect(page.getByText('Keep this save beyond this browser.')).toBeVisible();
  await expect(page.getByRole('link', { name: 'Connect Telegram to keep saves/favorites' })).toBeVisible();
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

    await expect(page.getByRole('link', { name: 'Remove Smoke private team saves filter' })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Open Smoke test vault reaction' })).toBeVisible();
    await expect(page.getByText('Shared', { exact: true })).toBeVisible();
  });

  test('anonymous user cannot see the same seeded collection result', async ({ page }) => {
    await page.goto(collectionSearchPath());

    await expect(page.getByRole('link', { name: 'Open Smoke test vault reaction' })).toHaveCount(0);
    await expect(page.getByText('Sign in with access to this collection to search it.')).toBeVisible();
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
