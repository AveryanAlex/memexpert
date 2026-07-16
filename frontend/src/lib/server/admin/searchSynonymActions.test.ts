import { describe, expect, it, vi } from 'vitest';
import type { ApiFetch } from '$lib/api/client';
import { actions as routeActions } from '../../../routes/admin/search/synonyms/+page.server';
import { searchSynonymActions } from './searchSynonymActions';

describe('search synonym admin actions', () => {
  it('exports the focused named actions from the synonym route', () => {
    const names = [
      'saveSearchSynonymDraft',
      'importSearchSynonymSeed',
      'publishSearchSynonymDraft',
      'resetSearchSynonymDraft',
      'retrySearchSynonymSync'
    ];

    expect(Object.keys(searchSynonymActions)).toEqual(names);
    expect(Object.keys(routeActions)).toEqual(names);
  });

  it('normalizes audited fields and browser newlines while preserving source whitespace', async () => {
    const calls: Array<{ method: string; path: string; body: unknown }> = [];
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({
        method: init?.method ?? 'GET',
        path: new URL(String(input)).pathname,
        body: init?.body ? JSON.parse(String(init.body)) : null
      });
      return jsonResponse({});
    }) satisfies ApiFetch;
    const revisionId = '77777777-7777-4777-8777-777777777777';

    await expect(searchSynonymActions.saveSearchSynonymDraft(actionEvent({
      locale: 'ru',
      request_id: '11111111-1111-4111-8111-111111111111',
      version: ' 4 ',
      source_text: ' жаба,лягушка\nfrog,жаба ',
      reason: '  Add useful one-word aliases.  '
    }, fetch))).resolves.toEqual({ message: 'Russian synonym draft saved.', locale: 'ru' });
    await expect(searchSynonymActions.importSearchSynonymSeed(actionEvent({
      locale: 'en',
      request_id: '22222222-2222-4222-8222-222222222222',
      version: '5',
      reason: ' Load the reviewed English seed. '
    }, fetch))).resolves.toEqual({ message: 'English research seed loaded into the draft.', locale: 'en' });
    await expect(searchSynonymActions.publishSearchSynonymDraft(actionEvent({
      locale: 'ru',
      request_id: '33333333-3333-4333-8333-333333333333',
      version: '6',
      reason: ' Publish the reviewed Russian catalog. ',
      confirm_destructive: 'true'
    }, fetch))).resolves.toEqual({ message: 'Russian synonym revision published.', locale: 'ru' });
    await expect(searchSynonymActions.resetSearchSynonymDraft(actionEvent({
      locale: 'en',
      request_id: '44444444-4444-4444-8444-444444444444',
      version: '7',
      reason: ' Reset to the current published revision. '
    }, fetch))).resolves.toEqual({ message: 'English draft reset to the published revision.', locale: 'en' });
    await expect(searchSynonymActions.resetSearchSynonymDraft(actionEvent({
      locale: 'en',
      request_id: '55555555-5555-4555-8555-555555555555',
      version: '8',
      revision_id: revisionId,
      reason: ' Restore the previous reviewed revision. '
    }, fetch))).resolves.toEqual({ message: 'English revision restored into the draft.', locale: 'en' });
    await expect(searchSynonymActions.retrySearchSynonymSync(actionEvent({
      request_id: '66666666-6666-4666-8666-666666666666',
      version: '9',
      reason: ' Retry after Meilisearch recovered. '
    }, fetch))).resolves.toEqual({ message: 'Meilisearch synonym reconciliation requested.' });

    expect(calls).toEqual([
      {
        method: 'PUT',
        path: '/api/v1/admin/search-synonyms/ru/draft',
        body: {
          request_id: '11111111-1111-4111-8111-111111111111',
          version: '4',
          source_text: ' жаба,лягушка\nfrog,жаба ',
          reason: 'Add useful one-word aliases.'
        }
      },
      {
        method: 'POST',
        path: '/api/v1/admin/search-synonyms/en/draft/import-seed',
        body: {
          request_id: '22222222-2222-4222-8222-222222222222',
          version: '5',
          reason: 'Load the reviewed English seed.'
        }
      },
      {
        method: 'POST',
        path: '/api/v1/admin/search-synonyms/ru/draft/publish',
        body: {
          request_id: '33333333-3333-4333-8333-333333333333',
          version: '6',
          reason: 'Publish the reviewed Russian catalog.',
          confirm_destructive: true
        }
      },
      {
        method: 'POST',
        path: '/api/v1/admin/search-synonyms/en/draft/reset',
        body: {
          request_id: '44444444-4444-4444-8444-444444444444',
          version: '7',
          reason: 'Reset to the current published revision.',
          revision_id: null
        }
      },
      {
        method: 'POST',
        path: '/api/v1/admin/search-synonyms/en/draft/reset',
        body: {
          request_id: '55555555-5555-4555-8555-555555555555',
          version: '8',
          reason: 'Restore the previous reviewed revision.',
          revision_id: revisionId
        }
      },
      {
        method: 'POST',
        path: '/api/v1/admin/search-synonyms/sync/retry',
        body: {
          request_id: '66666666-6666-4666-8666-666666666666',
          version: '9',
          reason: 'Retry after Meilisearch recovered.'
        }
      }
    ]);
  });

  it('rejects malformed locale, UUID, boolean, source, and audit fields before calling the API', async () => {
    const fetch = vi.fn(async () => jsonResponse({})) satisfies ApiFetch;
    const valid = {
      locale: 'en',
      request_id: '11111111-1111-4111-8111-111111111111',
      version: '1',
      reason: 'Review synonym coverage.'
    };
    const results = [
      searchSynonymActions.saveSearchSynonymDraft(actionEvent({ ...valid, locale: 'de', source_text: '' }, fetch)),
      searchSynonymActions.saveSearchSynonymDraft(actionEvent({ ...valid, request_id: 'not-a-uuid', source_text: '' }, fetch)),
      searchSynonymActions.saveSearchSynonymDraft(actionEvent({ ...valid, reason: 'x', source_text: '' }, fetch)),
      searchSynonymActions.saveSearchSynonymDraft(actionEvent({ ...valid, source_text: 'x'.repeat(1_000_001) }, fetch)),
      searchSynonymActions.publishSearchSynonymDraft(actionEvent({ ...valid, confirm_destructive: 'yes' }, fetch)),
      searchSynonymActions.resetSearchSynonymDraft(actionEvent({ ...valid, revision_id: 'not-a-uuid' }, fetch))
    ];

    for (const result of results) {
      await expect(result).resolves.toMatchObject({ status: 400, data: { error: true } });
    }
    expect(fetch).not.toHaveBeenCalled();
  });

  it('sends false when the destructive publish override is not checked', async () => {
    const bodies: unknown[] = [];
    const fetch = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      bodies.push(init?.body ? JSON.parse(String(init.body)) : null);
      return jsonResponse({});
    }) satisfies ApiFetch;

    await searchSynonymActions.publishSearchSynonymDraft(actionEvent({
      locale: 'en',
      request_id: '11111111-1111-4111-8111-111111111111',
      version: '1',
      reason: 'Publish the reviewed English catalog.'
    }, fetch));

    expect(bodies).toEqual([{
      request_id: '11111111-1111-4111-8111-111111111111',
      version: '1',
      reason: 'Publish the reviewed English catalog.',
      confirm_destructive: false
    }]);
  });

  it('preserves structured publish validation issues for actionable rendering', async () => {
    const validation = {
      valid: false,
      group_count: 1,
      compiled_key_count: 2,
      edge_count: 2,
      payload_bytes: 24,
      issues: [{
        level: 'error',
        code: 'cross_locale_key_collision',
        message: 'The normalized key is already published in another locale catalog.',
        line_number: null,
        term: '123'
      }]
    };
    const fetch = vi.fn(async () => jsonResponse({
      detail: {
        message: 'The synonym draft contains publish-blocking validation errors.',
        validation
      }
    }, 422)) satisfies ApiFetch;

    const result = await searchSynonymActions.publishSearchSynonymDraft(actionEvent({
      locale: 'ru',
      request_id: '11111111-1111-4111-8111-111111111111',
      version: '1',
      reason: 'Publish the reviewed Russian catalog.'
    }, fetch));

    expect(result).toEqual({
      status: 422,
      data: {
        message: 'The synonym draft contains publish-blocking validation errors.',
        error: true,
        locale: 'ru',
        publishValidation: validation
      }
    });
  });
});

function actionEvent(values: Record<string, string>, fetch: ApiFetch) {
  const formData = new FormData();
  for (const [name, value] of Object.entries(values)) formData.set(name, value);
  return {
    fetch,
    request: new Request('http://frontend.test/admin/search/synonyms', {
      method: 'POST',
      headers: { cookie: 'memexpert_access_token=token' },
      body: formData
    })
  } as never;
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'content-type': 'application/json' }
  });
}
