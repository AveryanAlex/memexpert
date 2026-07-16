import { describe, expect, it, vi } from 'vitest';
import {
  fetchAdminSearchSynonymCatalog,
  fetchAdminSearchSynonymSyncState,
  importAdminSearchSynonymSeed,
  publishAdminSearchSynonymDraft,
  resetAdminSearchSynonymDraft,
  retryAdminSearchSynonymSync,
  updateAdminSearchSynonymDraft,
  type ApiFetch
} from './client';

describe('admin search synonym API client', () => {
  it('uses the catalog, revision, seed, publish, reset, and durable sync endpoints', async () => {
    const calls: Array<{ path: string; method: string; body: unknown; headers: Headers }> = [];
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({
        path: new URL(String(input)).pathname,
        method: init?.method ?? 'GET',
        body: init?.body ? JSON.parse(String(init.body)) : null,
        headers: new Headers(init?.headers)
      });
      return new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } });
    }) satisfies ApiFetch;
    const request = { fetch: mockFetch, baseUrl: 'https://api.memexpert.test', cookieHeader: 'sid=admin' };
    const mutation = {
      request_id: '11111111-1111-4111-8111-111111111111',
      version: 'draft-version',
      reason: 'Review synonym coverage.'
    };

    await fetchAdminSearchSynonymCatalog(request, 'ru');
    await updateAdminSearchSynonymDraft({ ...request, body: { ...mutation, source_text: 'жаба,лягушка' } }, 'ru');
    await importAdminSearchSynonymSeed({ ...request, body: mutation }, 'en');
    await publishAdminSearchSynonymDraft({ ...request, body: { ...mutation, confirm_destructive: false } }, 'ru');
    await resetAdminSearchSynonymDraft({ ...request, body: { ...mutation, revision_id: '22222222-2222-4222-8222-222222222222' } }, 'ru');
    await fetchAdminSearchSynonymSyncState(request);
    await retryAdminSearchSynonymSync({ ...request, body: mutation });

    expect(calls.map(({ path, method }) => `${method} ${path}`)).toEqual([
      'GET /api/v1/admin/search-synonyms/ru',
      'PUT /api/v1/admin/search-synonyms/ru/draft',
      'POST /api/v1/admin/search-synonyms/en/draft/import-seed',
      'POST /api/v1/admin/search-synonyms/ru/draft/publish',
      'POST /api/v1/admin/search-synonyms/ru/draft/reset',
      'GET /api/v1/admin/search-synonyms/sync',
      'POST /api/v1/admin/search-synonyms/sync/retry'
    ]);
    expect(calls[1].body).toMatchObject({ source_text: 'жаба,лягушка', version: 'draft-version' });
    expect(calls[3].body).toMatchObject({ confirm_destructive: false });
    expect(calls[4].body).toMatchObject({ revision_id: '22222222-2222-4222-8222-222222222222' });
    for (const call of calls) {
      expect(call.headers.get('cookie')).toBe('sid=admin');
      if (call.method !== 'GET') expect(call.headers.get('x-requested-with')).toBe('XMLHttpRequest');
    }
  });

  it('surfaces structured publish guard messages from the API', async () => {
    const fetch = vi.fn(async () => new Response(JSON.stringify({
      detail: {
        message: 'Publishing would remove more than 25% of the current synonym keys.',
        previous_key_count: 100,
        new_key_count: 20,
        reduction_fraction: 0.8
      }
    }), { status: 409, headers: { 'content-type': 'application/json' } })) satisfies ApiFetch;

    await expect(publishAdminSearchSynonymDraft({
      fetch,
      baseUrl: 'https://api.memexpert.test',
      body: {
        request_id: '11111111-1111-4111-8111-111111111111',
        version: '2',
        reason: 'Publish the reviewed synonym reduction.',
        confirm_destructive: false
      }
    }, 'en')).rejects.toMatchObject({
      status: 409,
      message: 'Publishing would remove more than 25% of the current synonym keys.'
    });
  });
});
