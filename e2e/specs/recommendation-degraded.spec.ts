import { expect, test } from '../fixtures/app';
import type { HomeFeedPayload } from '../helpers/api';
import type { SeedArtifact } from '../helpers/seed';

const phase = process.env.E2E_RECOMMENDATION_DEGRADED_PHASE;
const personalizedSources = new Set([
  'short_term',
  'current_intent',
  'long_term_global',
  'long_term_cluster',
  'multi_positive'
]);

test('Home stays safe and usable from PostgreSQL candidates while Qdrant is unavailable', async ({ api, seed }) => {
  onlyDuring('qdrant-unavailable');
  const publicIds = safePublicMemeIds(seed);
  await api.expectNoAccessCookieStored();

  // A fresh guest has no profile, so this first request is PostgreSQL-only
  // even though Qdrant is already stopped. Keep at least one uncooled item in
  // reserve, then turn an indexed result into an exact strong-positive signal.
  const cold = await api.homeFeed({ limit: '10' });
  const guestToken = api.expectAccessCookieSet(cold.response);
  await api.expectAccessCookieStored(guestToken);
  expect(cold.payload.items.length, 'The seeded PostgreSQL pool must contain reserve candidates').toBeGreaterThan(1);
  for (const item of cold.payload.items) expectSafePostgresCandidate(item, publicIds, { profiled: false });
  const positive = cold.payload.items[0];
  if (!positive) throw new Error('Expected an indexed PostgreSQL candidate to become a positive signal.');
  await api.favoriteHomeItem(positive);

  // The favorite produces short-term, long-term, and multi-positive Qdrant
  // requests. A serving profile proves the second request exercised that path
  // before it degraded back to PostgreSQL candidates.
  const { payload } = await api.homeFeed({ limit: '10' });
  expect(payload.feed_session_id).not.toMatch(/^fallback:/);
  expect(payload.items.length, 'PostgreSQL trending/exploration candidates should keep Home usable').toBeGreaterThan(0);
  expect(payload.items.map((item) => item.meme.id)).not.toContain(positive.meme.id);

  for (const item of payload.items) expectSafePostgresCandidate(item, publicIds, { profiled: true });
});

test('Home uses a safe PostgreSQL keyset cursor while Redis is unavailable', async ({ api, seed }) => {
  onlyDuring('redis-unavailable');
  const publicIds = safePublicMemeIds(seed);
  const nsfwIds = new Set(seed.seeded_memes.filter((meme) => meme.is_nsfw).map((meme) => meme.meme_id));
  await api.expectNoAccessCookieStored();

  const first = await api.homeFeed({ limit: '1' });
  const guestToken = api.expectAccessCookieSet(first.response);
  await api.expectAccessCookieStored(guestToken);
  expectPostgresKeysetPage(first.payload, publicIds, nsfwIds);
  expect(first.payload.next_cursor).toEqual(expect.any(String));
  expect(first.payload.has_more).toBe(true);
  const firstId = first.payload.items[0]?.meme.id;
  if (!first.payload.next_cursor || !firstId) throw new Error('Expected a first fallback row and keyset cursor.');

  const second = await api.homeFeed({ limit: '1', cursor: first.payload.next_cursor });
  api.expectAccessCookieNotSet(second.response);
  await api.expectAccessCookieStored(guestToken);
  expectPostgresKeysetPage(second.payload, publicIds, nsfwIds);
  expect(second.payload.offset).toBe(1);
  expect(second.payload.items.map((item) => item.meme.id)).not.toContain(firstId);
});

function onlyDuring(expectedPhase: string) {
  test.skip(phase !== expectedPhase, `Harness phase is ${phase ?? 'normal'}, not ${expectedPhase}.`);
}

function safePublicMemeIds(seed: SeedArtifact): Set<string> {
  return new Set([
    ...seed.seeded_memes.filter((meme) => !meme.is_nsfw).map((meme) => meme.meme_id),
    seed.created_meme.meme_id
  ]);
}

function expectSafePostgresCandidate(
  item: HomeFeedPayload['items'][number],
  publicIds: Set<string>,
  { profiled }: { profiled: boolean }
) {
  expect(publicIds.has(item.meme.id), `Home leaked non-public meme ${item.meme.id}`).toBe(true);
  expect(item.meme.is_nsfw).toBe(false);
  expect(item.attribution.algorithm_version).toBe('personalized_v2');
  expect(item.attribution.profile_version).toEqual(profiled ? expect.stringMatching(/^taste_v2:/) : null);
  expect(item.attribution.candidate_sources.length).toBeGreaterThan(0);
  expect(
    item.attribution.candidate_sources.some((source) => personalizedSources.has(source.source)),
    'Qdrant-backed sources must be absent while the dependency is stopped'
  ).toBe(false);
  expect(
    item.attribution.candidate_sources.every((source) => ['trending', 'exploration'].includes(source.source))
  ).toBe(true);
}

function expectPostgresKeysetPage(
  payload: HomeFeedPayload,
  publicIds: Set<string>,
  nsfwIds: Set<string>
) {
  expect(payload.feed_session_id).toMatch(/^fallback:/);
  expect(payload.items).toHaveLength(1);
  for (const item of payload.items) {
    expect(publicIds.has(item.meme.id), `Fallback leaked non-public meme ${item.meme.id}`).toBe(true);
    expect(nsfwIds.has(item.meme.id), `Fallback leaked NSFW meme ${item.meme.id}`).toBe(false);
    expect(item.meme.is_nsfw).toBe(false);
    expect(item.attribution).toEqual(
      expect.objectContaining({
        surface: 'web_home',
        source_algorithm: 'personalized_recommendations',
        algorithm_version: 'public_trending_keyset_v1',
        profile_version: null,
        reason: 'redis_or_personalization_fallback'
      })
    );
    expect(item.attribution.candidate_sources.map((source) => source.source)).toEqual(['trending']);
    expect(item.attribution.attribution_token).toEqual(expect.any(String));
  }
}
