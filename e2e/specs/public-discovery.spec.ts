import type { Locator, Page } from '@playwright/test';
import { expect, test } from '../fixtures/app';
import { publicTrendsFixture, seededByCategory, type SeededPublicTrendsFixture } from '../helpers/seed';

test('guest discovers a public meme with URL-backed filters and imgproxy media', async ({ app, seed }) => {
  const cat = seededByCategory(seed, 'cat');
  const nsfwCat = seededByCategory(seed, 'cat-nsfw');

  await app.home.goto();
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

  await app.detail.likeAndExpectAttribution(cat, attribution);
  await app.detail.saveAndExpectAttribution(cat, attribution);
  await app.detail.downloadAndExpectAttribution(cat, attribution);
  await app.detail.shareToTelegramAndExpectAttribution(cat, attribution);
});

test('guest explores seeded public trend aggregates, comparison, and timeline', async ({ page, seed }) => {
  const trends = publicTrendsFixture(seed);
  const representative = seededByCategory(seed, trends.representative_meme.category);

  await page.goto(trends.trend_path);
  await expect(page.getByRole('heading', { name: 'Public meme trends.' })).toBeVisible();
  await expect(page.getByRole('link', { name: `Open ${representative.title}` }).first()).toBeVisible();
  await expect(page.getByRole('link', { name: new RegExp(escapeRegExp(trends.tag.title)) })).toBeVisible();
  await expect(page.getByRole('link', { name: new RegExp(escapeRegExp(trends.template.title)) })).toBeVisible();
  await expect(page.getByText(`${trends.tag.history_points.length} history points`).first()).toBeVisible();

  await expectAggregateLanding(page, trends.tag, 'Tag');
  await expectAggregateLanding(page, trends.template, 'Template');

  await page.goto(trends.compare.path);
  await expect(page.getByRole('heading', { name: 'Compare public trends.' })).toBeVisible();
  await expect(page.locator('figcaption').getByText('Trend comparison line chart', { exact: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Data table' })).toBeVisible();
  const comparisonTable = page.getByRole('table');
  await expect(comparisonTable).toBeVisible();
  await expectComparisonRow(comparisonTable, representative.title, 'meme', 'Per-meme snapshots', 'Real per-meme snapshot history.');
  await expectComparisonRow(comparisonTable, trends.tag.title, 'tag', 'Aggregate history points', 'Real aggregate history points.');
  await expectComparisonRow(comparisonTable, trends.template.title, 'template', 'Aggregate history points', 'Real aggregate history points.');
  await expect(page.getByText('Current-window aggregate fallback')).toHaveCount(0);
  await expect(page.getByText('No comparable history yet')).toHaveCount(0);

  await page.goto(trends.timeline.path);
  await expect(page.getByRole('heading', { name: 'Meme timeline.' })).toBeVisible();
  await expect(page.getByRole('heading', { name: trends.timeline.period_label })).toBeVisible();
  await expect(page.getByText(`${trends.timeline.snapshot_count} real snapshots`)).toBeVisible();
  await expect(page.getByText(trends.timeline.period, { exact: true })).toBeVisible();
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
  await expect(page.getByLabel(`${label} trend summary`)).toBeVisible();
  await expect(page.getByText(`${firstHistoryPoint.meme_count} public memes in this aggregate.`)).toBeVisible();
  await expect(page.getByText('Exact values are listed in the table below.')).toBeVisible();
  await expect(page.getByText('Aggregate history unavailable')).toHaveCount(0);
  await expect(page.getByText('Insufficient aggregate history')).toHaveCount(0);

  const table = page.getByRole('table', { name: `Exact aggregate history values for ${landing.title}` });
  await expect(table).toBeVisible();
  for (const point of landing.history_points) {
    await expect(table.getByText(formatObservedAt(point.observed_at))).toBeVisible();
    await expect(table.getByText(point.value.toFixed(1), { exact: true })).toBeVisible();
    await expect(table.getByText(String(point.source_views), { exact: true })).toBeVisible();
  }
}

async function expectComparisonRow(table: Locator, title: string, kind: string, basis: string, status: string) {
  const row = table.getByRole('row').filter({ hasText: title });
  await expect(row).toHaveCount(1);
  await expect(row).toBeVisible();
  await expect(row.getByRole('cell', { name: kind, exact: true })).toBeVisible();
  await expect(row.getByRole('cell', { name: basis, exact: true })).toBeVisible();
  await expect(row.getByRole('cell', { name: status, exact: true })).toBeVisible();
}

function formatObservedAt(raw: string): string {
  return new Intl.DateTimeFormat('en', { month: 'short', day: 'numeric', year: 'numeric' }).format(new Date(raw));
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}
