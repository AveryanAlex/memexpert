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

  await app.search.applyFilters({ query: cat.query, tag: 'e2e-prd', mediaType: 'image', language: 'en', includeNsfw: true });
  await app.search.expectUrlFilters({ query: cat.query, tag: 'e2e-prd', mediaType: 'image', language: 'en', includeNsfw: true });
  await app.search.expectResultVisible(cat);
  await app.search.expectResultHidden(nsfwCat);

  await app.search.openResult(cat);
  await app.detail.expectOpen(cat);
  await app.detail.expectMediaLoadedThroughImgproxy(cat.title);
});
