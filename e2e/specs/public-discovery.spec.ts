import type { Locator, Page } from '@playwright/test';
import { expect, test } from '../fixtures/app';
import { publicTrendsFixture, seededByCategory, type SeededPublicTrendsFixture } from '../helpers/seed';

test('guest home feed API bootstraps cold-start fallback from seeded public memes', async ({ api, seed }) => {
  await api.expectNoAccessCookieStored();
  const seededHomeFeedParams = { limit: '10', tags: 'e2e-prd' };

  const first = await api.homeFeed(seededHomeFeedParams);
  const accessToken = api.expectAccessCookieSet(first.response);
  await api.expectAccessCookieStored(accessToken);
  api.expectHomeFeedFallback(first.payload, seed.seeded_memes);

  const second = await api.homeFeed(seededHomeFeedParams);
  api.expectAccessCookieNotSet(second.response);
  await api.expectAccessCookieStored(accessToken);
  api.expectHomeFeedFallback(second.payload, seed.seeded_memes);
});

test('guest discovers a public meme with URL-backed filters and imgproxy media', async ({ app, seed }) => {
  const cat = seededByCategory(seed, 'cat');
  const nsfwCat = seededByCategory(seed, 'cat-nsfw');

  await app.home.goto();
  await app.home.expectGuestHomeFeedFallback(seed.seeded_memes);
  await app.home.searchFor(cat.query);

  await app.search.expectResultVisible(cat);
  await app.search.expectResultHidden(nsfwCat);

  await app.search.applyFilters({ query: cat.query, tag: 'e2e-prd', mediaType: 'image', language: 'en', includeNsfw: false });
  await app.search.expectUrlFilters({ query: cat.query, tag: 'e2e-prd', mediaType: 'image', language: 'en', includeNsfw: false });
  await app.search.expectResultVisible(cat);
  await app.search.expectResultHidden(nsfwCat);

  await app.search.gotoFilters({ query: cat.query, tag: 'e2e-prd', mediaType: 'image', language: 'en', includeNsfw: true });
  await app.search.expectUrlFilters({ query: cat.query, tag: 'e2e-prd', mediaType: 'image', language: 'en', includeNsfw: true });
  await app.search.expectNsfwUrlRequestNote();
  await app.search.expectNoNsfwOptInPrompt();
  await app.search.expectResultVisible(cat);
  await app.search.expectResultHidden(nsfwCat);

  await app.search.openResult(cat);
  await app.detail.expectOpen(cat);
  await app.detail.expectMediaLoadedThroughImgproxy(cat.title);
});

test('guest opens an attributed search result and exercises detail actions', async ({ app, seed }) => {
  const cat = seededByCategory(seed, 'cat');

  const impressionRequest = app.search.waitForResultImpressionPost(cat);
  await app.search.gotoFilters({ query: cat.query, tag: 'e2e-prd', mediaType: 'image', language: 'en', includeNsfw: false });
  await app.search.expectResultVisible(cat);
  const attribution = await app.search.attributionForResult(cat);
  await app.search.scrollResultIntoView(cat);
  await app.search.expectResultImpressionAttribution(impressionRequest, attribution);

  const detailClickRequest = app.search.waitForResultDetailClickPost(cat);
  await app.search.openResult(cat);
  await app.search.expectResultDetailClickAttribution(detailClickRequest, attribution);
  await app.detail.expectOpen(cat);
  await app.detail.expectAttributionQuery(attribution);

  await app.detail.favoriteAndExpectAttribution(cat, attribution);
  await app.detail.expectGuestSaveChooserExcludesFavorites();
  await app.detail.downloadAndExpectAttribution(cat, attribution);
  await app.detail.shareToTelegramAndExpectAttribution(cat, attribution);
});

test('guest explores seeded public trend aggregates, comparison, and timeline', async ({ page, seed }) => {
  const trends = publicTrendsFixture(seed);
  const representative = seededByCategory(seed, trends.representative_meme.category);

  await page.goto(trends.trend_path);
  await expect(page.getByRole('heading', { name: 'Meme trends', exact: true })).toBeVisible();
  await expect(page.getByText(/Recorded activity adds original-source views/)).toBeVisible();
  await expect(page.getByRole('link', { name: `Open ${representative.title}` }).first()).toBeVisible();
  await expect(page.getByRole('link', { name: new RegExp(escapeRegExp(trends.tag.title)) })).toBeVisible();
  await expect(page.getByRole('link', { name: new RegExp(escapeRegExp(trends.template.title)) })).toBeVisible();

  await expectAggregateLanding(page, trends.tag, 'Tag');
  await expectAggregateLanding(page, trends.template, 'Template');

  await page.goto(trends.compare.path);
  await expect(page.getByRole('heading', { name: 'Compare what is catching on.', exact: true })).toBeVisible();
  await expect(page.locator('figcaption').getByText('Recorded activity comparison', { exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'How they compare', exact: true })).toBeVisible();
  await expect(
    page.getByLabel('Selected items').getByText(`Meme · ${representative.title}`, { exact: true })
  ).toBeVisible();
  await expect(page.getByText('Some picks will join the chart once they have two recorded activity moments.', { exact: true })).toHaveCount(0);
  const comparisonTable = page.getByRole('table', { name: 'Recorded activity details for the comparison' });
  await expect(comparisonTable).toBeVisible();
  await expectComparisonSeries(comparisonTable, representative.title, 'Meme');
  await expectComparisonSeries(comparisonTable, trends.tag.title, 'Tag');
  await expectComparisonSeries(comparisonTable, trends.template.title, 'Template');

  await page.goto(trends.timeline.path);
  await expect(page.getByRole('heading', { name: 'Meme timeline.' })).toBeVisible();
  const timelinePeriod = page
    .getByRole('region', { name: 'Timeline periods' })
    .locator('section')
    .filter({ has: page.getByRole('heading', { name: trends.timeline.period_label }) });
  await expect(timelinePeriod).toHaveCount(1);
  await expect(timelinePeriod.getByRole('heading', { name: trends.timeline.period_label })).toBeVisible();
  await expect(timelinePeriod.getByText(/top memes? to revisit/)).toBeVisible();
  await expect(timelinePeriod.getByText(/Recorded activity · .* signals/).first()).toBeVisible();
  await expect(page.getByRole('link', { name: `Open ${representative.title}` }).first()).toBeVisible();
});

test('guest opts into NSFW from search confirmation and can disable it from profile', async ({ app, seed }) => {
  const cat = seededByCategory(seed, 'cat');
  const nsfwCat = seededByCategory(seed, 'cat-nsfw');
  const filters = { query: cat.query, tag: 'e2e-prd', mediaType: 'image', language: 'en', includeNsfw: false };
  const nsfwFilters = { ...filters, includeNsfw: true };

  await app.search.gotoFilters(filters);
  await app.search.expectResultVisible(cat);
  await app.search.expectResultHidden(nsfwCat);

  await app.search.applyFilters(nsfwFilters);
  await app.search.cancelNsfwOptIn();
  await app.search.expectUrlFilters(filters);
  await app.search.expectResultHidden(nsfwCat);

  await app.search.applyFilters(nsfwFilters);
  await app.search.confirmNsfwOptIn();
  await app.search.expectUrlFilters(nsfwFilters);
  await app.search.expectResultVisible(cat);
  await app.search.expectResultVisible(nsfwCat);

  await app.profile.goto();
  await app.profile.expectNsfwEnabled();
  await app.profile.disableNsfw();

  await app.search.gotoFilters(nsfwFilters);
  await app.search.expectNsfwUrlRequestNote();
  await app.search.expectResultVisible(cat);
  await app.search.expectResultHidden(nsfwCat);
});

async function expectAggregateLanding(
  page: Page,
  landing: SeededPublicTrendsFixture['tag'] | SeededPublicTrendsFixture['template'],
  label: 'Tag' | 'Template'
) {
  const firstHistoryPoint = landing.history_points[0];
  if (!firstHistoryPoint) throw new Error(`Seed artifact did not include history points for ${landing.slug}.`);

  await page.goto(landing.path);
  await expect(page.getByRole('heading', { name: landing.title })).toBeVisible();
  await page.getByText(`About this ${label.toLowerCase()}`, { exact: true }).click();
  await expect(page.getByLabel(`${label} popularity summary`)).toBeVisible();
  await expect(page.getByText(`${firstHistoryPoint.meme_count} memes help shape this ${label.toLowerCase()}'s recent popularity.`)).toBeVisible();
  await expect(page.getByRole('region', { name: `${landing.title} recorded activity over time` })).toBeVisible();

  const table = page.getByRole('table', { name: `Recorded activity details for ${landing.title}` });
  await expect(table).toBeVisible();
  for (const point of landing.history_points) {
    const row = table.getByRole('row').filter({ hasText: formatObservedAt(point.observed_at) });
    await expect(row).toHaveCount(1);
    const cells = row.getByRole('cell');
    await expect(cells.nth(0)).toHaveText(formatCount(recordedActivity(point)));
    await expect(cells.nth(1)).toHaveText(formatCount(sourceActivity(point)));
    await expect(cells.nth(2)).toHaveText(formatCount(memeExpertActivity(point)));
    await expect(cells.nth(3)).toHaveText(formatCount(point.meme_count));
  }
}

async function expectComparisonSeries(table: Locator, title: string, kind: 'Meme' | 'Tag' | 'Template') {
  const rows = table.getByRole('row').filter({ hasText: title });
  await expect.poll(() => rows.count()).toBeGreaterThan(0);
  const cells = rows.first().getByRole('cell');
  await expect(cells.nth(0)).toHaveText(kind);
  await expect(cells.nth(2)).toContainText('signals');
}

function formatObservedAt(raw: string): string {
  return new Intl.DateTimeFormat('en', { month: 'short', day: 'numeric', year: 'numeric', timeZone: 'UTC' }).format(new Date(raw));
}

function recordedActivity(point: SeededPublicTrendsFixture['tag']['history_points'][number]): number {
  return sourceActivity(point) + memeExpertActivity(point);
}

function sourceActivity(point: SeededPublicTrendsFixture['tag']['history_points'][number]): number {
  return point.source_views + point.source_reactions + point.source_reposts;
}

function memeExpertActivity(point: SeededPublicTrendsFixture['tag']['history_points'][number]): number {
  return point.platform_views + point.platform_sends + point.platform_saves + point.platform_likes;
}

function formatCount(value: number): string {
  return new Intl.NumberFormat('en').format(value);
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}
