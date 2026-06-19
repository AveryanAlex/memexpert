import { test } from '../fixtures/app';
import { seededByCategory } from '../helpers/seed';

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

  await app.search.gotoFilters({ query: cat.query, tag: 'e2e-prd', mediaType: 'image', language: 'en', includeNsfw: false });
  await app.search.expectResultVisible(cat);
  const attribution = await app.search.attributionForResult(cat);

  await app.search.openResult(cat);
  await app.detail.expectOpen(cat);
  await app.detail.expectAttributionQuery(attribution);

  await app.detail.likeAndExpectAttribution(cat, attribution);
  await app.detail.saveAndExpectAttribution(cat, attribution);
  await app.detail.downloadAndExpectAttribution(cat, attribution);
  await app.detail.shareToTelegramAndExpectAttribution(cat, attribution);
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
