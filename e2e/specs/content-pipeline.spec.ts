import { test } from '../fixtures/app';

test('fake-provider upload becomes public and searchable through the website', async ({ api, app, seed }) => {
  const created = seed.created_meme;

  await api.expectHealthy();
  await api.expectDualIndexProof(created.meme_file_id);
  await api.expectMemeInSearch(created.meme_id, { query: created.query, limit: '10', offset: '0' });
  await api.expectMemeDetail(created.slug, created.meme_id);

  await app.home.goto();
  await app.home.searchFor(created.query);
  await app.search.expectResultVisible(created);
  await app.search.openResult(created);
  await app.detail.expectOpen(created);
});
