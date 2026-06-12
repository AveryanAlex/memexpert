import { expect, request, test } from '@playwright/test';
import { readSeedArtifact } from './seed';

const apiBaseUrl = process.env.E2E_API_BASE_URL ?? 'http://api:8000';
const frontendBaseUrl = process.env.E2E_FRONTEND_BASE_URL ?? 'http://frontend:3000';
const operatorToken = process.env.E2E_OPERATOR_TOKEN ?? 'memexpert-e2e-pipeline-operator-token-min-32';

test('API health is reachable inside the Compose network', async ({ request: api }) => {
  const response = await api.get(`${apiBaseUrl}/health`);
  expect(response.ok()).toBeTruthy();
  expect(await response.json()).toEqual(expect.objectContaining({ status: 'ok' }));
});

test('public search and detail expose the deterministic seeded corpus', async ({ request: api }) => {
  const seed = readSeedArtifact();
  const createdMemeId = seed.created_meme.meme_id;
  const seededMemeIds = seed.seeded_memes.map((item) => item.meme_id);
  expect(new Set(seededMemeIds).size).toBe(seededMemeIds.length);
  expect(seededMemeIds).not.toContain(createdMemeId);

  for (const seeded of seed.seeded_memes) {
    const search = await api.get(`${apiBaseUrl}/api/v1/memes/search`, {
      params: { query: seeded.query, limit: '10', offset: '0' }
    });
    expect(search.ok()).toBeTruthy();
    const searchPayload = await search.json();
    expect(searchPayload.items.map((item: SearchItem) => item.meme.id)).toContain(seeded.meme_id);

    const detail = await api.get(`${apiBaseUrl}/api/v1/memes/slug/${seeded.slug}`);
    expect(detail.ok()).toBeTruthy();
    const detailPayload = await detail.json();
    expect(detailPayload).toEqual(
      expect.objectContaining({
        id: seeded.meme_id,
        seo_page_slug: seeded.slug,
        seo_title: `Deterministic ${seeded.category} smoke meme`
      })
    );
  }
});

test('created meme is public and passes the pipeline dual-target smoke proof', async ({ request: api }) => {
  const seed = readSeedArtifact();
  const created = seed.created_meme;

  const search = await api.get(`${apiBaseUrl}/api/v1/memes/search`, {
    params: { query: created.query, limit: '10', offset: '0' }
  });
  expect(search.ok()).toBeTruthy();
  const searchPayload = await search.json();
  expect(searchPayload.items.map((item: SearchItem) => item.meme.id)).toContain(created.meme_id);

  const detail = await api.get(`${apiBaseUrl}/api/v1/memes/slug/${created.slug}`);
  expect(detail.ok()).toBeTruthy();
  expect(await detail.json()).toEqual(expect.objectContaining({ id: created.meme_id, seo_page_slug: created.slug }));

  const proof = await api.post(`${apiBaseUrl}/api/v1/pipeline/search/smoke`, {
    headers: { 'X-Memexpert-Operator-Token': operatorToken },
    data: { meme_file_id: created.meme_file_id }
  });
  expect(proof.ok()).toBeTruthy();
  expect(await proof.json()).toEqual(expect.objectContaining({ both_targets_searchable: true }));
});

test('frontend proxy can perform a stable guest favorite action', async () => {
  const seed = readSeedArtifact();
  const context = await request.newContext({ baseURL: frontendBaseUrl });
  try {
    const favorite = await context.post(`/api/v1/memes/${seed.created_meme.meme_id}/favorite`, {
      headers: { accept: 'application/json', 'x-requested-with': 'XMLHttpRequest' }
    });
    expect(favorite.ok()).toBeTruthy();
    const favoritePayload = await favorite.json();
    expect(favoritePayload.meme_id).toBe(seed.created_meme.meme_id);

    const remove = await context.delete(`/api/v1/memes/${seed.created_meme.meme_id}/favorite`, {
      headers: { accept: 'application/json', 'x-requested-with': 'XMLHttpRequest' }
    });
    expect(remove.ok()).toBeTruthy();
    expect(await remove.json()).toEqual(expect.objectContaining({ removed: true }));
  } finally {
    await context.dispose();
  }
});

interface SearchItem {
  meme: { id: string };
}
