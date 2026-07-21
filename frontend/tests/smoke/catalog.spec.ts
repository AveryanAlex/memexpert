import { expect, test } from '@playwright/test';

const seededCollectionId = 'smoke-private-team-saves';
const seededCollectionQuery = 'vault reaction';
const rankedMasonryQuery = 'ranked masonry smoke';
const rankedMasonryTitles = [
  'Ranked portrait one',
  'Ranked ultra wide two',
  'Ranked square three',
  'Ranked landscape four',
  'Ranked captionless five',
  'Ranked missing media six',
  'Ranked video seven',
  'Ranked tall eight with enough caption copy to make its card height distinct',
  'Ranked appended portrait nine',
  'Ranked appended wide ten'
] as const;

interface MasonryHydrationFrame {
  state: string | null;
  order: number[];
  visible: number[];
  positions: Array<{ index: number; left: number; top: number }>;
}
const infiniteFeedSentinel = '[data-infinite-feed-sentinel]';

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
    const firstCardZoom = firstCard.getByRole('button', { name: 'Enlarge Smoke test cat reaction', exact: true });
    const firstCardFavorite = firstCard.getByRole('button', { name: 'Favorite', exact: true });
    const firstCardDownload = firstCard.getByRole('button', { name: 'Download', exact: true });
    const firstCardSave = firstCard.getByRole('button', { name: 'Save to collection', exact: true });
    const firstCardSend = firstCard.getByRole('button', { name: 'Send', exact: true });
    const firstCardMenu = feed.getByRole('button', { name: 'Actions for Smoke test cat reaction' });
    await expect(firstCardLink).toBeVisible();
    await expect(firstCardZoom).toBeVisible();
    await expect(firstCardFavorite).toBeVisible();
    await expect(firstCardDownload).toBeVisible();
    await expect(firstCardSave).toBeVisible();
    await expect(firstCardSend).toBeVisible();
    await expect(firstCardMenu).toBeVisible();
    await expect(page.getByRole('button', { name: 'Select items' })).toHaveCount(0);

    await expect(firstCardLink).toHaveAccessibleName('Open Smoke test cat reaction');
    await expect(firstCardLink).toHaveJSProperty('tabIndex', 0);
    await expect(firstCardLink).not.toHaveAttribute('tabindex', '-1');
    await firstCardLink.focus();
    await expect(firstCardLink).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(firstCardZoom).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(firstCardFavorite).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(firstCardDownload).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(firstCardSave).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(firstCardSend).toBeFocused();
    await page.keyboard.press('Tab');
    await expect(firstCardMenu).toBeFocused();
    await firstCardMenu.press('Enter');
    await expect(firstCardMenu).toHaveAttribute('aria-expanded', 'true');
    await expect(page.getByRole('menuitem', { name: /Favorite meme|Remove favorite/ })).toHaveCount(0);
    await expect(page.getByRole('menuitem', { name: 'Copy link', exact: true })).toBeVisible();
    await expect(page.getByRole('menuitem', { name: 'Report meme', exact: true })).toBeVisible();
    await expect(page.getByRole('menuitem', { name: /Send to Telegram|Download/ })).toHaveCount(0);
    await expect(page.getByRole('menuitem', { name: 'Save', exact: true })).toHaveCount(0);
    await page.keyboard.press('Escape');

    await firstCardZoom.click();
    const zoomDialog = page.getByRole('dialog', { name: 'Smoke test cat reaction' });
    await expect(zoomDialog).toBeVisible();
    await expect(zoomDialog.getByRole('img', { name: 'Enlarged Smoke test cat reaction' })).toBeVisible();
    await zoomDialog.getByRole('button', { name: 'Close enlarged image' }).click();
    await expect(zoomDialog).toHaveCount(0);
    await expect(firstCardZoom).toBeFocused();

    await firstCardFavorite.click();
    const activeFavorite = firstCard.getByRole('button', { name: 'Remove favorite', exact: true });
    const activeMotdFavorite = motd.getByRole('button', { name: 'Remove favorite', exact: true });
    await expect(activeFavorite).toHaveAttribute('aria-pressed', 'true');
    await expect(activeFavorite.locator('svg')).toHaveClass(/text-danger/);
    await expect(activeMotdFavorite).toHaveAttribute('aria-pressed', 'true');
    await expect(activeMotdFavorite.locator('svg')).toHaveClass(/text-danger/);
    await expect(firstCardSave).toHaveAttribute('aria-pressed', 'false');
    await expect(firstCard.getByText('Added to favorites.', { exact: true })).toHaveCount(0);

    const loadMore = page.getByRole('button', { name: 'Load more' });
    await expect(loadMore).toBeVisible();
    await expect(loadMore).toHaveAccessibleDescription(/More results also load automatically|Automatic loading is unavailable/);
    await loadMore.click();
    await expect(page.getByRole('link', { name: 'Open Smoke test deploy mood' })).toBeVisible();
    await expect(page.getByText('Showing 2 of 2')).toBeVisible();

    const cardsFitTheirColumns = await feed.evaluate((element) => {
      const tolerance = 1;
      return Array.from(element.querySelectorAll(':scope > [data-masonry-index]')).every((item) => {
        const itemBox = item.getBoundingClientRect();
        return Array.from(item.querySelectorAll(':scope > div > article, :scope > article')).every((card) => {
          const cardBox = card.getBoundingClientRect();
          return cardBox.left >= itemBox.left - tolerance && cardBox.right <= itemBox.right + tolerance;
        });
      });
    });
    expect(cardsFitTheirColumns).toBe(true);
    await expect(page.locator('html')).toHaveJSProperty('scrollWidth', 1280);
  });

  test('Back restores the frozen Home cursor only after server reauthorization', async ({ page }) => {
    await page.setViewportSize({ width: 900, height: 520 });
    await disableIntersectionObserver(page);
    let reauthorizationRequests = 0;
    page.on('request', (request) => {
      if (new URL(request.url()).pathname === '/api/v1/memes/home-feed/reauthorize') {
        reauthorizationRequests += 1;
      }
    });
    await page.goto('/');
    const feed = page.getByRole('list', { name: 'Meme results' });
    await page.getByRole('button', { name: 'Load more' }).click();
    await expect(feed.getByRole('link', { name: 'Open Smoke test deploy mood' })).toBeVisible();
    await page.evaluate(() => window.scrollTo(0, document.body.scrollHeight));
    const previousScroll = await page.evaluate(() => window.scrollY);

    // Dispatch without Playwright's actionability scroll so this navigation starts at the position being tested.
    await page.getByRole('link', { name: 'Search', exact: true }).first().dispatchEvent('click');
    await expect(page).toHaveURL(/\/search$/);
    const savedScroll = await page.evaluate(() => {
      const key = Object.keys(sessionStorage).find((candidate) => candidate.startsWith('memexpert:home-feed:v2:'));
      return key ? JSON.parse(sessionStorage.getItem(key) ?? '{}').scrollY : null;
    });
    expect(savedScroll).toBeGreaterThanOrEqual(previousScroll - 2);
    await page.goBack();
    await expect(page).toHaveURL(/\/$/);

    await expect.poll(() => reauthorizationRequests).toBeGreaterThan(0);
    await expect(feed.getByRole('link', { name: 'Open Smoke test cat reaction' })).toBeVisible();
    await expect(feed.getByRole('link', { name: 'Open Smoke test deploy mood' })).toBeVisible();
    await expect.poll(() => page.evaluate(() => window.scrollY)).toBeGreaterThanOrEqual(previousScroll - 2);
  });

  test('Back never renders a saved Home item omitted by fresh authorization', async ({ page }) => {
    await disableIntersectionObserver(page);
    await page.goto('/');
    const feed = page.getByRole('list', { name: 'Meme results' });
    await page.getByRole('button', { name: 'Load more' }).click();
    await expect(feed.getByRole('link', { name: 'Open Smoke test deploy mood' })).toBeVisible();
    await page.route('**/api/v1/memes/home-feed/reauthorize**', async (route) => {
      const upstream = await route.fetch();
      const payload = await upstream.json();
      await route.fulfill({
        response: upstream,
        json: { ...payload, items: payload.items.slice(0, 1) }
      });
    });

    await page.getByRole('link', { name: 'Search', exact: true }).first().click();
    await expect(page).toHaveURL(/\/search$/);
    await page.goBack();
    await expect(page).toHaveURL(/\/$/);

    await expect(feed.getByRole('link', { name: 'Open Smoke test cat reaction' })).toBeVisible();
    await expect(feed.getByRole('link', { name: 'Open Smoke test deploy mood' })).toHaveCount(0);
  });

  test('reload and direct Home navigation keep the fresh SSR feed', async ({ baseURL, page }) => {
    await page.context().addCookies([
      {
        name: 'memexpert_access_token',
        value: 'smoke-full-home-refresh',
        url: baseURL ?? 'http://127.0.0.1:4174',
        httpOnly: true,
        sameSite: 'Lax'
      }
    ]);
    await disableIntersectionObserver(page);
    let reauthorizationRequests = 0;
    page.on('request', (request) => {
      if (new URL(request.url()).pathname === '/api/v1/memes/home-feed/reauthorize') {
        reauthorizationRequests += 1;
      }
    });

    await page.goto('/');
    const feed = page.getByRole('list', { name: 'Meme results' });
    await expect(feed.getByRole('link', { name: 'Open Smoke test cat reaction' })).toBeVisible();
    await expect.poll(() => page.evaluate(() => (
      Object.keys(sessionStorage).some((key) => key.startsWith('memexpert:home-feed:v2:'))
    ))).toBe(true);

    await page.reload();
    await page.waitForLoadState('networkidle');
    await expect(feed.getByRole('link', { name: 'Open Smoke test deploy mood' })).toBeVisible();
    await expect(feed.getByRole('link', { name: 'Open Smoke test cat reaction' })).toHaveCount(0);
    expect(reauthorizationRequests).toBe(0);

    await page.getByRole('link', { name: 'Search', exact: true }).first().click();
    await expect(page).toHaveURL(/\/search$/);
    await page.getByRole('link', { name: 'MemeXpert', exact: true }).click();
    await page.waitForLoadState('networkidle');
    await expect(feed.getByRole('link', { name: 'Open Smoke test cat reaction' })).toBeVisible();
    await expect(feed.getByRole('link', { name: 'Open Smoke test deploy mood' })).toHaveCount(0);
    expect(reauthorizationRequests).toBe(0);
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
    await expect(card.locator('button[aria-label="Enlarge Smoke test cat reaction"]')).toBeHidden();
    await expect(card.getByRole('button', { name: 'Favorite', exact: true })).toBeVisible();
    await expect(card.getByRole('button', { name: 'Download', exact: true })).toBeVisible();
    await expect(card.getByRole('button', { name: 'Save to collection', exact: true })).toBeVisible();
    await expect(card.getByRole('button', { name: 'Send', exact: true })).toBeVisible();
    await expect(feed.getByRole('button', { name: 'Actions for Smoke test cat reaction' })).toBeVisible();
    const mediaBox = await card.getByRole('img', { name: 'Smoke test cat reaction' }).boundingBox();
    expect(mediaBox?.y).toBeLessThan(844);
    await expect(page.getByRole('button', { name: 'Load more' })).toBeVisible();
  });

  test('image magnifier follows the masonry one-column threshold', async ({ page }) => {
    await page.setViewportSize({ width: 590, height: 844 });
    await disableIntersectionObserver(page);
    await page.goto('/');

    const feed = page.getByRole('list', { name: 'Meme results' });
    const firstCard = feed.getByRole('link', { name: 'Open Smoke test cat reaction' }).locator('xpath=ancestor::article');
    const zoom = firstCard.locator('button[aria-label="Enlarge Smoke test cat reaction"]');
    await expect(feed).toHaveAttribute('data-column-count', '1');
    await expect(zoom).toBeHidden();

    await page.setViewportSize({ width: 610, height: 844 });
    await expect(feed).toHaveAttribute('data-column-count', '2');
    await expect(zoom).toBeVisible();
  });

  test('image magnifier stays intrinsic-sized and fully visible in a short viewport', async ({ page }) => {
    await page.setViewportSize({ width: 1000, height: 400 });
    await disableIntersectionObserver(page);
    await page.goto('/');

    const feed = page.getByRole('list', { name: 'Meme results' });
    const zoom = feed.getByRole('button', { name: 'Enlarge Smoke test cat reaction', exact: true });
    await zoom.click();

    const dialog = page.getByRole('dialog', { name: 'Smoke test cat reaction' });
    const image = dialog.getByRole('img', { name: 'Enlarged Smoke test cat reaction' });
    await expect(image).toBeVisible();

    const geometry = await dialog.evaluate((element) => {
      const imageElement = element.querySelector('img');
      const closeElement = element.querySelector('button[aria-label="Close enlarged image"]');
      if (!(imageElement instanceof HTMLImageElement) || !(closeElement instanceof HTMLButtonElement)) {
        throw new Error('Zoom dialog media controls are missing.');
      }

      const rect = (node: Element) => {
        const box = node.getBoundingClientRect();
        return { top: box.top, right: box.right, bottom: box.bottom, left: box.left, width: box.width, height: box.height };
      };

      return {
        viewport: { width: window.innerWidth, height: window.innerHeight },
        dialog: rect(element),
        image: rect(imageElement),
        close: rect(closeElement),
        natural: { width: imageElement.naturalWidth, height: imageElement.naturalHeight }
      };
    });
    const tolerance = 2;

    expect(geometry.dialog.left).toBeGreaterThanOrEqual(-tolerance);
    expect(geometry.dialog.top).toBeGreaterThanOrEqual(-tolerance);
    expect(geometry.dialog.right).toBeLessThanOrEqual(geometry.viewport.width + tolerance);
    expect(geometry.dialog.bottom).toBeLessThanOrEqual(geometry.viewport.height + tolerance);
    expect(geometry.image.left).toBeGreaterThanOrEqual(geometry.dialog.left - tolerance);
    expect(geometry.image.top).toBeGreaterThanOrEqual(geometry.dialog.top - tolerance);
    expect(geometry.image.right).toBeLessThanOrEqual(geometry.dialog.right + tolerance);
    expect(geometry.image.bottom).toBeLessThanOrEqual(geometry.dialog.bottom + tolerance);
    expect(geometry.image.width).toBeLessThanOrEqual(geometry.natural.width + tolerance);
    expect(geometry.image.height).toBeLessThanOrEqual(geometry.natural.height + tolerance);
    expect(geometry.image.width / geometry.image.height).toBeCloseTo(geometry.natural.width / geometry.natural.height, 2);
    expect(geometry.close.bottom).toBeLessThanOrEqual(geometry.image.top + tolerance);

    await page.keyboard.press('Escape');
    await expect(dialog).toHaveCount(0);
    await expect(zoom).toBeFocused();
  });

  test('search masonry uses shared column state for its magnifier', async ({ page }) => {
    await page.setViewportSize({ width: 590, height: 844 });
    await disableIntersectionObserver(page);
    await page.goto('/search?q=cat%20reaction');

    const grid = page.getByRole('list', { name: 'Search results' });
    const card = grid.getByRole('link', { name: 'Open Smoke test cat reaction' }).locator('xpath=ancestor::article');
    const zoom = card.locator('button[aria-label="Enlarge Smoke test cat reaction"]');
    await expect(grid).toHaveAttribute('data-layout', 'masonry');
    await expect(grid).toHaveAttribute('data-masonry-state', 'ready');
    await expect(grid).toHaveAttribute('data-column-count', '1');
    await expect(zoom).toBeHidden();

    await page.setViewportSize({ width: 610, height: 844 });
    await expect(grid).toHaveAttribute('data-column-count', '2');
    await expect(zoom).toBeVisible();
  });

  test('ranked search is hydration-stable, densely packed, and stable after append', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await installMasonryHydrationRecorder(page);
    await disableIntersectionObserver(page);
    await page.goto(rankedMasonrySearchPath());

    const grid = page.getByRole('list', { name: 'Search results' });
    const items = grid.locator(':scope > [data-masonry-index]');
    await expect(grid).toHaveAttribute('data-layout', 'masonry');
    await expect(grid).toHaveAttribute('data-column-count', '4');
    await expect(grid).toHaveAttribute('data-masonry-state', 'ready');
    await expect(items).toHaveCount(8);
    await expect(page.getByRole('link', { name: `Open ${rankedMasonryTitles[7]}`, exact: true })).toBeVisible();
    await nextBrowserFrames(page, 2);

    const initialGeometry = await readMasonryGeometry(grid);
    expect(initialGeometry.items.map((item) => item.index)).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
    expect(initialGeometry.items.map((item) => item.position)).toEqual([1, 2, 3, 4, 5, 6, 7, 8]);
    expect(initialGeometry.items.map((item) => item.title)).toEqual(rankedMasonryTitles.slice(0, 8));
    expectRankAwareMasonryGeometry(initialGeometry);

    const hydrationFrames = await page.evaluate(() => {
      return (window as typeof window & { __masonryHydrationFrames?: MasonryHydrationFrame[] }).__masonryHydrationFrames ?? [];
    });
    expect(hydrationFrames.length).toBeGreaterThan(0);
    expect(hydrationFrames.filter((frame) => frame.order.length > 0).every((frame) => frame.order.join(',') === '0,1,2,3,4,5,6,7')).toBe(true);

    const firstVisibleIndex = hydrationFrames.findIndex((frame) => frame.visible.length > 0);
    expect(firstVisibleIndex).toBeGreaterThanOrEqual(0);
    expect(hydrationFrames.slice(0, firstVisibleIndex).every((frame) => frame.visible.length === 0)).toBe(true);
    const firstVisibleFrame = hydrationFrames[firstVisibleIndex];
    expect(firstVisibleFrame?.state).toBe('ready');
    expect(firstVisibleFrame?.visible).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
    for (const item of initialGeometry.items) {
      const firstPosition = firstVisibleFrame?.positions.find((position) => position.index === item.index);
      expect(firstPosition, `initial visible position for ranked item ${item.index}`).toBeDefined();
      expect(Math.abs((firstPosition?.left ?? Number.NaN) - (item.left - initialGeometry.container.left))).toBeLessThanOrEqual(1);
      expect(Math.abs((firstPosition?.top ?? Number.NaN) - (item.top - initialGeometry.container.top))).toBeLessThanOrEqual(1);
    }

    await page.getByRole('button', { name: 'Load more' }).click();
    await expect(items).toHaveCount(10);
    await expect(page.getByRole('link', { name: `Open ${rankedMasonryTitles[9]}`, exact: true })).toBeVisible();
    await expect(grid).toHaveAttribute('data-masonry-state', 'ready');
    await nextBrowserFrames(page, 2);

    const appendedGeometry = await readMasonryGeometry(grid);
    expect(appendedGeometry.items.map((item) => item.index)).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8, 9]);
    expect(appendedGeometry.items.map((item) => item.title)).toEqual([...rankedMasonryTitles]);
    expectRankAwareMasonryGeometry(appendedGeometry);
    for (const item of initialGeometry.items) {
      const afterAppend = appendedGeometry.items.find((candidate) => candidate.index === item.index);
      expect(afterAppend, `appended position for ranked item ${item.index}`).toBeDefined();
      const initialLeft = item.left - initialGeometry.container.left;
      const initialTop = item.top - initialGeometry.container.top;
      const appendedLeft = (afterAppend?.left ?? Number.NaN) - appendedGeometry.container.left;
      const appendedTop = (afterAppend?.top ?? Number.NaN) - appendedGeometry.container.top;
      expect(Math.abs(appendedLeft - initialLeft)).toBeLessThanOrEqual(1);
      expect(Math.abs(appendedTop - initialTop)).toBeLessThanOrEqual(1);
    }
  });

  test('ranked search masonry responds with one through four columns', async ({ page }) => {
    await disableIntersectionObserver(page);
    await page.setViewportSize({ width: 390, height: 900 });
    await page.goto(rankedMasonrySearchPath());

    const grid = page.getByRole('list', { name: 'Search results' });
    for (const [width, columns] of [[390, '1'], [660, '2'], [960, '3'], [1280, '4']] as const) {
      await page.setViewportSize({ width, height: 900 });
      await expect(grid).toHaveAttribute('data-column-count', columns);
      await expect(grid).toHaveAttribute('data-masonry-state', 'ready');
      await nextBrowserFrames(page, 2);
      await expect(grid).toHaveAttribute('data-masonry-state', 'ready');
      const geometry = await readMasonryGeometry(grid);
      expect(new Set(geometry.items.map((item) => item.column)).size).toBe(Number(columns));
      expectRankAwareMasonryGeometry(geometry);
    }
  });

  test('ranked search has a visible ordered fallback without JavaScript', async ({ browser, baseURL }) => {
    const context = await browser.newContext({
      baseURL,
      javaScriptEnabled: false,
      viewport: { width: 960, height: 900 }
    });
    const page = await context.newPage();

    try {
      await page.goto(rankedMasonrySearchPath());
      const grid = page.getByRole('list', { name: 'Search results' });
      const items = grid.locator(':scope > [data-masonry-index]');
      await expect(grid).toHaveAttribute('data-layout', 'masonry');
      await expect(grid).toHaveAttribute('data-masonry-state', 'pending');
      await expect(items).toHaveCount(8);
      await expect(page.getByRole('link', { name: `Open ${rankedMasonryTitles[0]}`, exact: true })).toBeVisible();
      await expect(page.getByRole('link', { name: `Open ${rankedMasonryTitles[7]}`, exact: true })).toBeVisible();
      await expect.poll(async () => items.evaluateAll((nodes) => nodes.map((node) => Number(node.getAttribute('data-masonry-index'))))).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);
    } finally {
      await context.close();
    }
  });

  test('detail automatically extends one ranked Similar masonry page per sentinel crossing', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await installControlledIntersectionObserver(page);
    const pagingRequests: URL[] = [];
    const detailDataRequests: URL[] = [];
    page.on('request', (request) => {
      const url = new URL(request.url());
      if (url.pathname === '/api/v1/memes/smoke-meme-1/similar') pagingRequests.push(url);
      if (url.pathname.endsWith('/__data.json')) detailDataRequests.push(url);
    });

    await page.goto('/');
    await page.getByRole('list', { name: 'Meme results' }).getByRole('link', { name: 'Open Smoke test cat reaction' }).click();
    await expect(page).toHaveURL(/\/memes\/smoke-test-cat-reaction/);

    const grid = page.getByRole('list', { name: 'Similar memes' });
    await expect(grid).toHaveAttribute('data-layout', 'masonry');
    await expect(grid).toHaveAttribute('data-masonry-state', 'ready');
    await expect(grid.getByRole('link', { name: 'Open Smoke similar meme 1', exact: true })).toBeVisible();
    await expect(grid.getByRole('link', { name: 'Open Smoke similar meme 12', exact: true })).toBeVisible();
    await expect(grid.getByRole('link', { name: 'Open Smoke similar meme 13', exact: true })).toHaveCount(0);
    await expect(page.getByText('Showing 12 of 25')).toBeVisible();
    await expect(page.getByRole('button', { name: 'Select items' })).toHaveCount(0);
    await expect.poll(() => controlledObserverCount(page, infiniteFeedSentinel)).toBe(1);

    const offset12Response = page.waitForResponse((response) => isSimilarPageResponse(response, 'smoke-meme-1', 12));
    expect(await triggerControlledIntersection(page, infiniteFeedSentinel, 2)).toEqual([1, 1]);
    await offset12Response;

    await expect(grid.getByRole('link', { name: 'Open Smoke similar meme 24', exact: true })).toBeVisible();
    await expect(page.getByText('Showing 24 of 25')).toBeVisible();
    expect(pagingRequests.map((url) => url.searchParams.get('offset'))).toEqual(['12']);

    const offset24Response = page.waitForResponse((response) => isSimilarPageResponse(response, 'smoke-meme-1', 24));
    expect(await triggerControlledIntersection(page, infiniteFeedSentinel, 2)).toEqual([1, 1]);
    await offset24Response;

    await expect(grid.getByRole('link', { name: 'Open Smoke similar meme 25', exact: true })).toBeVisible();
    await expect(page.getByText('Showing 25 of 25')).toBeVisible();
    await expect(page.getByText('You’re all caught up.')).toBeVisible();
    expect(pagingRequests.map((url) => url.searchParams.get('offset'))).toEqual(['12', '24']);
    expect(pagingRequests.every((url) => url.searchParams.get('limit') === '12')).toBe(true);

    expect(await triggerControlledIntersection(page, infiniteFeedSentinel, 2)).toEqual([1, 1]);
    await page.waitForTimeout(100);
    expect(pagingRequests.map((url) => url.searchParams.get('offset'))).toEqual(['12', '24']);
    await expect(grid.locator('[data-discovery-source="qdrant_similarity"]')).toHaveCount(25);
    await expect(grid.locator('[data-discovery-source-meme-id="smoke-meme-1"]')).toHaveCount(25);
    const orderedLabels = await grid.locator('a[aria-label^="Open Smoke similar meme "]').evaluateAll((links) =>
      links.map((link) => link.getAttribute('aria-label'))
    );
    expect(orderedLabels).toEqual(Array.from({ length: 25 }, (_, index) => `Open Smoke similar meme ${index + 1}`));

    const appendedLink = grid.getByRole('link', { name: 'Open Smoke similar meme 25', exact: true });
    await expect(appendedLink).toHaveAttribute('data-sveltekit-preload-data', 'tap');
    await expect(appendedLink).toHaveAttribute('href', /attribution_rank=25/);
    await expect(appendedLink).toHaveAttribute('href', /attribution_source_meme_id=smoke-meme-1/);
    const initialDetailDataRequestCount = detailDataRequests.length;
    await appendedLink.hover();
    await page.waitForTimeout(300);
    expect(detailDataRequests).toHaveLength(initialDetailDataRequestCount);
  });

  test('changing the Similar source resets and isolates an in-flight page', async ({ page }) => {
    await installControlledIntersectionObserver(page);
    await page.goto('/');
    await page.getByRole('list', { name: 'Meme results' }).getByRole('link', { name: 'Open Smoke test cat reaction' }).click();
    await expect(page).toHaveURL(/\/memes\/smoke-test-cat-reaction/);

    const sourceAGrid = page.getByRole('list', { name: 'Similar memes' });
    await expect(sourceAGrid.getByRole('link', { name: 'Open Smoke similar meme 1', exact: true })).toBeVisible();
    await expect.poll(() => controlledObserverCount(page, infiniteFeedSentinel)).toBe(1);
    const stalePageRequest = page.waitForRequest((request) => {
      const url = new URL(request.url());
      return url.pathname === '/api/v1/memes/smoke-meme-1/similar' && url.searchParams.get('offset') === '12';
    });
    expect(await triggerControlledIntersection(page, infiniteFeedSentinel)).toEqual([1]);
    const staleRequest = await stalePageRequest;

    await sourceAGrid.getByRole('link', { name: 'Open Smoke similar meme 1', exact: true }).click();
    await expect(page).toHaveURL(/\/memes\/smoke-similar-meme-1/);
    await expect(page.getByRole('heading', { name: 'Smoke similar meme 1', level: 1 })).toBeVisible();
    expect(await staleRequest.response()).toBeNull();

    const sourceBGrid = page.getByRole('list', { name: 'Similar memes' });
    await expect(sourceBGrid.getByRole('link', { name: 'Open Smoke test cat reaction', exact: true })).toBeVisible();
    await expect(sourceBGrid.getByRole('link', { name: 'Open Smoke test deploy mood', exact: true })).toBeVisible();
    await expect(page.getByText('Showing 2 of 2')).toBeVisible();
    await expect(sourceBGrid.locator('[data-discovery-source-meme-id="smoke-similar-1"]')).toHaveCount(2);
    await page.waitForTimeout(650);
    await expect(sourceBGrid.locator('a[aria-label^="Open Smoke similar meme "]')).toHaveCount(0);
    await expect(sourceBGrid.getByRole('listitem')).toHaveCount(2);
  });

  test('a failed session revalidation preserves the loaded Similar grid until refresh retry succeeds', async ({ baseURL, page }) => {
    await installControlledIntersectionObserver(page);
    await page.goto('/memes/smoke-test-cat-reaction');

    const grid = page.getByRole('list', { name: 'Similar memes' });
    await expect(grid.getByRole('link', { name: 'Open Smoke similar meme 12', exact: true })).toBeVisible();
    await expect.poll(() => controlledObserverCount(page, infiniteFeedSentinel)).toBe(1);
    const offset12Response = page.waitForResponse((response) => isSimilarPageResponse(response, 'smoke-meme-1', 12));
    expect(await triggerControlledIntersection(page, infiniteFeedSentinel)).toEqual([1]);
    await offset12Response;
    await expect(grid.getByRole('link', { name: 'Open Smoke similar meme 24', exact: true })).toBeVisible();
    await expect(page.getByText('Showing 24 of 25')).toBeVisible();

    await page.context().addCookies([
      {
        name: 'smoke_similar_revalidation_failure',
        value: '1',
        url: baseURL ?? 'http://127.0.0.1:4174'
      }
    ]);
    await page.locator('.app-shell-sign-in').click();
    await expect(page.getByRole('heading', { name: 'Sign in to MemeXpert' })).toBeVisible();
    await page.getByRole('button', { name: /Continue with Telegram/ }).click();

    await expect(page.getByRole('heading', { name: 'Sign in to MemeXpert' })).toBeHidden();
    await expect(page.getByText('Could not load similar memes. Try again.')).toBeVisible();
    await expect(grid.getByRole('link', { name: 'Open Smoke similar meme 24', exact: true })).toBeVisible();
    await expect(page.getByText('Showing 24 of 25')).toBeVisible();

    const refreshRetryRequest = page.waitForRequest((request) => isSimilarPageRequest(request, 'smoke-meme-1', 0));
    await page.getByRole('button', { name: 'Retry refreshing results' }).click();
    await refreshRetryRequest;
    await expect(page.getByText('Could not load similar memes. Try again.')).toHaveCount(0);
    await expect(grid.getByRole('link', { name: 'Open Smoke similar meme 12', exact: true })).toBeVisible();
    await expect(grid.getByRole('link', { name: 'Open Smoke similar meme 13', exact: true })).toHaveCount(0);
    await expect(page.getByText('Showing 12 of 25')).toBeVisible();
  });

  test('card actions do not overlap at the supported 320px viewport', async ({ page }) => {
    await page.setViewportSize({ width: 320, height: 700 });
    await disableIntersectionObserver(page);
    await page.goto('/');

    const feed = page.getByRole('list', { name: 'Meme results' });
    const card = feed.getByRole('link', { name: 'Open Smoke test cat reaction' }).locator('xpath=ancestor::article');
    const actions = [
      card.getByRole('button', { name: 'Favorite', exact: true }),
      card.getByRole('button', { name: 'Download', exact: true }),
      card.getByRole('button', { name: 'Save to collection', exact: true }),
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

  test('save chooser separates saved and available collections and removes in place', async ({ baseURL, page }) => {
    await page.context().addCookies([
      {
        name: 'memexpert_access_token',
        value: 'smoke-full-save-chooser',
        url: baseURL ?? 'http://127.0.0.1:4174',
        httpOnly: true,
        sameSite: 'Lax'
      }
    ]);
    await disableIntersectionObserver(page);
    await page.goto('/');

    const feed = page.getByRole('list', { name: 'Meme results' });
    const card = feed.getByRole('link', { name: 'Open Smoke test cat reaction' }).locator('xpath=ancestor::article');
    await card.getByRole('button', { name: 'Save to collection', exact: true }).click();

    const chooser = page.getByLabel('Collections for Smoke test cat reaction');
    await expect(chooser).toBeVisible();
    await expect(chooser.getByText('Add to', { exact: true })).toBeVisible();
    await expect(chooser.getByRole('button', { name: 'Add to Recent reactions', exact: true })).toBeVisible();
    await expect(chooser.getByRole('button', { name: 'Add to Later ideas', exact: true })).toBeVisible();
    await expect(chooser.getByText('Favorites', { exact: true })).toHaveCount(0);
    await expect(chooser.getByText('Smoke private team saves', { exact: true })).toHaveCount(0);
    expect(await chooser.evaluate((element) => getComputedStyle(element).borderTopWidth)).toBe('0px');

    await chooser.getByRole('button', { name: 'Add to Recent reactions', exact: true }).click();
    await expect(card.getByRole('button', { name: 'Save to collection', exact: true })).toHaveAttribute('aria-pressed', 'true');
    const motdSave = page.getByRole('region', { name: 'Meme of the Day' }).getByRole('button', { name: 'Save to collection', exact: true });
    await expect(motdSave).toHaveAttribute('aria-pressed', 'true');
    await expect(chooser.getByText('Saved in', { exact: true })).toBeVisible();
    await expect(chooser.getByRole('button', { name: 'Remove from Recent reactions', exact: true })).toBeVisible();
    await expect(chooser.getByRole('button', { name: 'Add to Later ideas', exact: true })).toBeVisible();
    await expect(card.getByText('Saved to Recent reactions.', { exact: true })).toHaveCount(0);

    await chooser.getByRole('button', { name: 'Remove from Recent reactions', exact: true }).click();
    await expect(card.getByRole('button', { name: 'Save to collection', exact: true })).toHaveAttribute('aria-pressed', 'false');
    await expect(motdSave).toHaveAttribute('aria-pressed', 'false');
    await expect(chooser.getByRole('button', { name: 'Add to Recent reactions', exact: true })).toBeVisible();

    await chooser.getByRole('button', { name: 'Add to Later ideas', exact: true }).click();
    await expect(chooser.getByRole('button', { name: 'Remove from Later ideas', exact: true })).toBeVisible();
    await page.keyboard.press('Escape');

    const motd = page.getByRole('region', { name: 'Meme of the Day' });
    await motd.getByRole('button', { name: 'Actions for Smoke test cat reaction' }).click();
    await page.getByRole('menuitem', { name: 'Pin', exact: true }).click();

    await card.getByRole('button', { name: 'Actions for Smoke test cat reaction' }).click();
    await expect(page.getByRole('menuitem', { name: 'Unpin', exact: true })).toBeVisible();
  });

  test('save chooser ignores an older refresh after another card saves the same meme', async ({ baseURL, page }) => {
    await page.context().addCookies([
      {
        name: 'memexpert_access_token',
        value: 'smoke-full-save-race',
        url: baseURL ?? 'http://127.0.0.1:4174',
        httpOnly: true,
        sameSite: 'Lax'
      }
    ]);
    await disableIntersectionObserver(page);
    await page.goto('/');

    const feed = page.getByRole('list', { name: 'Meme results' });
    const card = feed.getByRole('link', { name: 'Open Smoke test cat reaction' }).locator('xpath=ancestor::article');
    const feedSave = card.getByRole('button', { name: 'Save to collection', exact: true });
    const motdSave = page
      .getByRole('region', { name: 'Meme of the Day' })
      .getByRole('button', { name: 'Save to collection', exact: true });

    await feedSave.click();
    let chooser = page.getByLabel('Collections for Smoke test cat reaction');
    await expect(chooser.getByRole('button', { name: 'Add to Recent reactions', exact: true })).toBeVisible();
    await page.keyboard.press('Escape');

    const delayedRequestPromise = page.waitForRequest((request) =>
      new URL(request.url()).pathname.endsWith('/api/v1/collections/meme-choices/smoke-meme-1')
    );
    await feedSave.click();
    const delayedRequest = await delayedRequestPromise;
    const delayedResponsePromise = delayedRequest.response();
    chooser = page.getByLabel('Collections for Smoke test cat reaction');
    await expect(chooser.getByText('Loading collections…', { exact: true })).toBeVisible();
    await expect(chooser.getByRole('button', { name: 'Add to Recent reactions', exact: true })).toHaveCount(0);
    await page.keyboard.press('Escape');

    await motdSave.click();
    chooser = page.getByLabel('Collections for Smoke test cat reaction');
    await chooser.getByRole('button', { name: 'Add to Recent reactions', exact: true }).click();
    await expect(motdSave).toHaveAttribute('aria-pressed', 'true');
    await expect(feedSave).toHaveAttribute('aria-pressed', 'true');

    await delayedResponsePromise;
    await expect(motdSave).toHaveAttribute('aria-pressed', 'true');
    await expect(feedSave).toHaveAttribute('aria-pressed', 'true');

    await page.keyboard.press('Escape');
    await feedSave.click();
    chooser = page.getByLabel('Collections for Smoke test cat reaction');
    await expect(chooser.getByRole('button', { name: 'Remove from Recent reactions', exact: true })).toBeVisible();
  });

  test('Meme of the Day favorite survives reload with one authoritative increment', async ({ baseURL, page }) => {
    await page.context().addCookies([
      {
        name: 'memexpert_access_token',
        value: 'smoke-full-motd-like-reload',
        url: baseURL ?? 'http://127.0.0.1:4174',
        httpOnly: true,
        sameSite: 'Lax'
      }
    ]);
    await disableIntersectionObserver(page);
    await page.goto('/');

    const motd = page.getByRole('region', { name: 'Meme of the Day' });
    const feed = page.getByRole('list', { name: 'Meme results' });
    const feedCard = feed.getByRole('link', { name: 'Open Smoke test cat reaction' }).locator('xpath=ancestor::article');
    await motd.getByRole('button', { name: 'Favorite', exact: true }).click();
    await expect(motd.getByRole('button', { name: 'Remove favorite', exact: true })).toBeVisible();
    await expect(feedCard.getByRole('button', { name: 'Remove favorite', exact: true })).toBeVisible();

    await page.reload();
    await expect(motd.getByRole('button', { name: 'Remove favorite', exact: true })).toBeVisible();
    await expect(feedCard.getByRole('button', { name: 'Remove favorite', exact: true })).toBeVisible();

    await feedCard.getByRole('link', { name: 'Open Smoke test cat reaction' }).click();
    await expect(page.getByRole('button', { name: 'Favorite (8)', exact: true })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Favorite (9)', exact: true })).toHaveCount(0);
  });

  test('video previews autoplay in one column and play on hover in wider grids', async ({ page }) => {
    await installMediaSpies(page);
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.goto('/search?q=video');

    const grid = page.getByRole('list', { name: 'Search results' });
    const video = grid.locator('video');
    await expect(grid).toHaveAttribute('data-layout', 'masonry');
    await expect(grid).toHaveAttribute('data-masonry-state', 'ready');
    await expect(grid).toHaveAttribute('data-video-preview-mode', 'hover');
    await expect(video).toHaveAttribute('poster', /smoke-cat\.svg/);
    await expect(video).toHaveJSProperty('muted', true);
    await expect(video).not.toHaveAttribute('data-play-calls');
    await video.hover();
    await expect(video).toHaveAttribute('data-play-calls', '1');
    await expect(video).toHaveAttribute('aria-label', 'Pause Untitled meme');
    await video.click();
    await expect(video).toHaveAttribute('aria-label', 'Play Untitled meme');
    await expect(page.getByRole('link', { name: 'Open Untitled meme', exact: true })).toBeVisible();

    const unmute = page.getByRole('button', { name: 'Unmute Untitled meme', exact: true });
    await unmute.click();
    await expect(page.getByRole('button', { name: 'Mute Untitled meme', exact: true })).toHaveAttribute('aria-pressed', 'true');
    await expect(video).toHaveJSProperty('muted', false);

    await page.setViewportSize({ width: 390, height: 844 });
    await video.scrollIntoViewIfNeeded();
    await expect(grid).toHaveAttribute('data-video-preview-mode', 'viewport');
    await expect(video).toHaveAttribute('data-play-calls', /^[2-9]\d*$/);
    await expect(video).toHaveJSProperty('muted', true);
    await video.click();
    await expect(video).toHaveAttribute('aria-label', 'Play Untitled meme');
  });

  test('search selection controls stay hidden until Select items is chosen', async ({ page }) => {
    await disableIntersectionObserver(page);
    await page.goto('/search?q=cat%20reaction');

    const feed = page.getByRole('list', { name: 'Search results' });
    await expect(feed).toBeVisible();
    await expect(feed.getByRole('checkbox')).toHaveCount(0);

    const selectItems = page.getByRole('button', { name: 'Select items' });
    await expect(selectItems).toBeEnabled();
    await selectItems.click();
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
    await expect(page).toHaveURL(/\/memes\/smoke-test-cat-reaction(?:\?.*)?$/);
  });
});

test('search result opens detail with media and actions', async ({ page, request }, testInfo) => {
  const statsUrl = `http://127.0.0.1:${Number(testInfo.config.metadata.mockApiPort)}/__smoke/stats`;
  const statsBeforeVisit = await readSmokeStats(request, statsUrl);
  await page.goto('/');

  const searchInput = page.getByLabel('Search memes');
  await searchInput.fill('cat reaction');
  await searchInput.press('Enter');

  await expect(page.getByText('Results for “cat reaction”')).toBeVisible();

  const result = page.getByRole('link', { name: 'Open Smoke test cat reaction' });
  await expect(result).toBeVisible();
  await expect(result).toHaveAttribute(
    'href',
    /attribution_impression_id=[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}/
  );
  await result.click();

  await expect(page).toHaveURL(/\/memes\/smoke-test-cat-reaction(?:\?.*)?$/);
  expect(new URL(page.url()).searchParams.get('attribution_impression_id')).toMatch(
    /^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
  );
  await expect(page.getByRole('heading', { name: 'Smoke test cat reaction' })).toBeVisible();
  await expect.poll(async () => (await readSmokeStats(request, statsUrl)).detailViewCount).toBe(
    statsBeforeVisit.detailViewCount + 1
  );
  await expect(page.getByRole('img', { name: 'Smoke test cat reaction' })).toBeVisible();

  const detailActions = page.locator('article').first().locator('[aria-label="Meme actions"]');
  await expect(detailActions.getByRole('button', { name: 'Favorite (7)' })).toBeVisible();
  await expect(detailActions.getByRole('button', { name: 'Save to collection', exact: true })).toBeVisible();
  await expect(detailActions.getByRole('button', { name: 'Send', exact: true })).toBeVisible();
  await expect(page.getByText('Pin requires a full account')).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Pin', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Meme actions' })).toBeVisible();
  await page.getByRole('button', { name: 'Meme actions' }).click();
  await expect(page.getByRole('menuitem', { name: 'Download', exact: true })).toBeVisible();
  await expect(page.getByRole('menuitem', { name: /Favorite meme|Remove favorite|Send to Telegram/ })).toHaveCount(0);
  await page.keyboard.press('Escape');

  const insights = page.locator('details[data-meme-insights]');
  await expect(insights).toBeVisible();
  await insights.locator(':scope > summary').click();
  await expect(insights.getByRole('link', { name: 'Smoke Memes Lab' })).toBeVisible();
  await expect(insights.getByRole('link', { name: 'Open Telegram post' })).toHaveAttribute('href', 'https://t.me/smoke_memes_lab/42');
  await expect(insights.getByText('Unknown', { exact: true }).first()).toBeVisible();

  const professional = insights.locator('details').filter({ hasText: 'Professional analytics' });
  await expect(professional).toBeVisible();
  await professional.locator(':scope > summary').click();
  await expect(professional.getByText('Recorded activity · signals per day')).toBeVisible();
  await expect(professional.getByText('Exposure funnels')).toBeVisible();
  await expect(professional.getByText('Telegram inline')).toBeVisible();

  const statsBeforeRangeChange = await readSmokeStats(request, statsUrl);
  await professional.getByRole('link', { name: '7 days' }).click();
  await expect(page).toHaveURL(/activity_window=7d.*#meme-professional-analytics$/);
  await expect(insights).toHaveAttribute('open', '');
  await expect(professional).toHaveAttribute('open', '');
  await expect(professional.getByText('Recorded activity · signals per day')).toBeVisible();
  const statsAfterRangeChange = await readSmokeStats(request, statsUrl);
  expect(statsAfterRangeChange.detailViewCount).toBe(statsBeforeRangeChange.detailViewCount);
  expect(statsAfterRangeChange.similarInitialReadCount).toBe(statsBeforeRangeChange.similarInitialReadCount);
  await expect(
    page.getByRole('list', { name: 'Similar memes' }).getByRole('link', { name: 'Open Smoke similar meme 1', exact: true })
  ).toHaveCount(1);

  await detailActions.getByRole('button', { name: 'Favorite (7)' }).click();
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

function rankedMasonrySearchPath() {
  return `/search?q=${encodeURIComponent(rankedMasonryQuery)}`;
}

interface MasonryGeometry {
  state: string | null;
  columnCount: number;
  container: MasonryBox;
  items: Array<MasonryBox & { index: number; column: number; position: number; title: string | null }>;
}

interface MasonryBox {
  top: number;
  right: number;
  bottom: number;
  left: number;
  width: number;
  height: number;
}

async function readMasonryGeometry(grid: import('@playwright/test').Locator): Promise<MasonryGeometry> {
  return grid.evaluate((element) => {
    const rect = (node: Element) => {
      const box = node.getBoundingClientRect();
      return {
        top: box.top,
        right: box.right,
        bottom: box.bottom,
        left: box.left,
        width: box.width,
        height: box.height
      };
    };
    const items = Array.from(element.querySelectorAll<HTMLElement>(':scope > [data-masonry-index]'));

    return {
      state: element.getAttribute('data-masonry-state'),
      columnCount: Number(element.getAttribute('data-column-count')),
      container: rect(element),
      items: items.map((item) => {
        const article = item.querySelector('article');
        const openLink = item.querySelector<HTMLElement>('[aria-label^="Open "]');
        return {
          ...rect(item),
          index: Number(item.dataset.masonryIndex),
          column: Number(item.dataset.masonryColumn),
          position: Number(article?.getAttribute('aria-posinset')),
          title: openLink?.getAttribute('aria-label')?.replace(/^Open /, '') ?? null
        };
      })
    };
  });
}

function expectRankAwareMasonryGeometry(geometry: MasonryGeometry) {
  const tolerance = 1.5;
  expect(geometry.state).toBe('ready');
  expect(geometry.items.length).toBeGreaterThan(0);
  expect(new Set(geometry.items.map((item) => item.column)).size).toBe(geometry.columnCount);

  const first = geometry.items[0];
  expect(Math.abs(first.left - geometry.container.left)).toBeLessThanOrEqual(tolerance);
  expect(Math.abs(first.top - geometry.container.top)).toBeLessThanOrEqual(tolerance);

  for (let index = 0; index < geometry.items.length; index += 1) {
    const item = geometry.items[index];
    expect(item.left).toBeGreaterThanOrEqual(geometry.container.left - tolerance);
    expect(item.top).toBeGreaterThanOrEqual(geometry.container.top - tolerance);
    expect(item.right).toBeLessThanOrEqual(geometry.container.right + tolerance);
    expect(item.bottom).toBeLessThanOrEqual(geometry.container.bottom + tolerance);
    if (index > 0) {
      expect(item.top).toBeGreaterThanOrEqual(geometry.items[index - 1].top - tolerance);
    }
  }

  for (let leftIndex = 0; leftIndex < geometry.items.length; leftIndex += 1) {
    for (let rightIndex = leftIndex + 1; rightIndex < geometry.items.length; rightIndex += 1) {
      const left = geometry.items[leftIndex];
      const right = geometry.items[rightIndex];
      const horizontalOverlap = Math.min(left.right, right.right) - Math.max(left.left, right.left);
      const verticalOverlap = Math.min(left.bottom, right.bottom) - Math.max(left.top, right.top);
      expect(horizontalOverlap > tolerance && verticalOverlap > tolerance, `ranked items ${left.index} and ${right.index} overlap`).toBe(false);
    }
  }

  if (geometry.columnCount > 1 && geometry.items.length > geometry.columnCount) {
    const firstRowBottom = Math.max(...geometry.items.slice(0, geometry.columnCount).map((item) => item.bottom));
    expect(geometry.items[geometry.columnCount].top).toBeLessThan(firstRowBottom - 8);
  }
}

async function installMasonryHydrationRecorder(page: import('@playwright/test').Page) {
  await page.addInitScript(() => {
    const recordedWindow = window as typeof window & { __masonryHydrationFrames?: MasonryHydrationFrame[] };
    const frames: MasonryHydrationFrame[] = [];
    recordedWindow.__masonryHydrationFrames = frames;
    let previous = '';
    let remainingFrames = 300;

    const round = (value: number) => Math.round(value * 100) / 100;
    const isVisible = (item: HTMLElement, grid: HTMLElement) => {
      let current: HTMLElement | null = item;
      while (current) {
        const style = getComputedStyle(current);
        if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0 || current.hidden) return false;
        if (current === grid) break;
        current = current.parentElement;
      }
      const box = item.getBoundingClientRect();
      return box.width > 0 && box.height > 0;
    };
    const sample = () => {
      const grid = document.querySelector<HTMLElement>('[aria-label="Search results"][data-layout="masonry"]');
      if (grid) {
        const gridBox = grid.getBoundingClientRect();
        const items = Array.from(grid.querySelectorAll<HTMLElement>(':scope > [data-masonry-index]'));
        if (items.length > 0) {
          const frame: MasonryHydrationFrame = {
            state: grid.getAttribute('data-masonry-state'),
            order: items.map((item) => Number(item.dataset.masonryIndex)),
            visible: items.filter((item) => isVisible(item, grid)).map((item) => Number(item.dataset.masonryIndex)),
            positions: items.map((item) => {
              const box = item.getBoundingClientRect();
              return {
                index: Number(item.dataset.masonryIndex),
                left: round(box.left - gridBox.left),
                top: round(box.top - gridBox.top)
              };
            })
          };
          const serialized = JSON.stringify(frame);
          if (serialized !== previous) {
            frames.push(frame);
            previous = serialized;
          }
        }
      }

      remainingFrames -= 1;
      if (remainingFrames > 0) requestAnimationFrame(sample);
    };

    requestAnimationFrame(sample);
  });
}

async function nextBrowserFrames(page: import('@playwright/test').Page, count: number) {
  await page.evaluate(async (frameCount) => {
    for (let frame = 0; frame < frameCount; frame += 1) {
      await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
    }
  }, count);
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

async function readSmokeStats(
  request: import('@playwright/test').APIRequestContext,
  url: string
): Promise<{ detailReadCount: number; detailViewCount: number; similarInitialReadCount: number }> {
  const response = await request.get(url);
  expect(response.ok()).toBe(true);
  return response.json();
}

async function installControlledIntersectionObserver(page: import('@playwright/test').Page) {
  await page.addInitScript(() => {
    class ControlledIntersectionObserver {
      readonly elements = new Set<Element>();
      readonly root: Element | Document | null;
      readonly rootMargin: string;
      readonly thresholds: number[];

      constructor(
        readonly callback: IntersectionObserverCallback,
        options: IntersectionObserverInit = {}
      ) {
        this.root = options.root ?? null;
        this.rootMargin = options.rootMargin ?? '0px';
        this.thresholds = Array.isArray(options.threshold) ? [...options.threshold] : [options.threshold ?? 0];
        observers.add(this);
      }

      observe(element: Element) {
        this.elements.add(element);
      }

      unobserve(element: Element) {
        this.elements.delete(element);
      }

      disconnect() {
        this.elements.clear();
        observers.delete(this);
      }

      takeRecords(): IntersectionObserverEntry[] {
        return [];
      }
    }

    const observers = new Set<ControlledIntersectionObserver>();
    type ControlledWindow = Window & {
      countSmokeIntersectionObservers?: (selector: string) => number;
      triggerSmokeIntersection?: (selector: string) => number;
    };
    const controlledWindow = window as ControlledWindow;

    Object.defineProperty(window, 'IntersectionObserver', {
      configurable: true,
      value: ControlledIntersectionObserver
    });
    controlledWindow.countSmokeIntersectionObservers = (selector) => {
      const target = document.querySelector(selector);
      return target ? [...observers].filter((observer) => observer.elements.has(target)).length : 0;
    };
    controlledWindow.triggerSmokeIntersection = (selector) => {
      const target = document.querySelector(selector);
      if (!target) return 0;

      const matching = [...observers].filter((observer) => observer.elements.has(target));
      const bounds = target.getBoundingClientRect();
      const entry = {
        boundingClientRect: bounds,
        intersectionRatio: 1,
        intersectionRect: bounds,
        isIntersecting: true,
        rootBounds: null,
        target,
        time: performance.now()
      } as IntersectionObserverEntry;
      for (const observer of matching) {
        observer.callback([entry], observer as unknown as IntersectionObserver);
      }
      return matching.length;
    };
  });
}

async function controlledObserverCount(page: import('@playwright/test').Page, selector: string): Promise<number> {
  return page.evaluate((targetSelector) => {
    const count = (window as Window & { countSmokeIntersectionObservers?: (selector: string) => number })
      .countSmokeIntersectionObservers;
    return count?.(targetSelector) ?? 0;
  }, selector);
}

async function triggerControlledIntersection(
  page: import('@playwright/test').Page,
  selector: string,
  count = 1
): Promise<number[]> {
  return page.evaluate(
    ({ targetSelector, triggerCount }) => {
      const trigger = (window as Window & { triggerSmokeIntersection?: (selector: string) => number })
        .triggerSmokeIntersection;
      return Array.from({ length: triggerCount }, () => trigger?.(targetSelector) ?? 0);
    },
    { targetSelector: selector, triggerCount: count }
  );
}

function isSimilarPageResponse(
  response: import('@playwright/test').Response,
  sourceMemeId: string,
  offset: number
): boolean {
  const url = new URL(response.url());
  return url.pathname === `/api/v1/memes/${sourceMemeId}/similar` && url.searchParams.get('offset') === String(offset);
}

function isSimilarPageRequest(
  request: import('@playwright/test').Request,
  sourceMemeId: string,
  offset: number
): boolean {
  const url = new URL(request.url());
  return url.pathname === `/api/v1/memes/${sourceMemeId}/similar` && url.searchParams.get('offset') === String(offset);
}

async function installMediaSpies(page: import('@playwright/test').Page) {
  await page.addInitScript(() => {
    Object.defineProperty(HTMLMediaElement.prototype, 'play', {
      configurable: true,
      value() {
        const element = this as HTMLMediaElement;
        const calls = Number(element.dataset.playCalls ?? '0') + 1;
        element.dataset.playCalls = String(calls);
        element.dispatchEvent(new Event('play'));
        return Promise.resolve();
      }
    });
    Object.defineProperty(HTMLMediaElement.prototype, 'pause', {
      configurable: true,
      value() {
        const element = this as HTMLMediaElement;
        const calls = Number(element.dataset.pauseCalls ?? '0') + 1;
        element.dataset.pauseCalls = String(calls);
        element.dispatchEvent(new Event('pause'));
      }
    });
  });
}
