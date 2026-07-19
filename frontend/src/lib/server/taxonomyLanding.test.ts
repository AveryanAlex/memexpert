import { describe, expect, it, vi } from 'vitest';

import type { ApiFetch } from '$lib/api/client';
import { load as loadTagPage } from '../../routes/tags/[tag]/+page.server';
import { load as loadTemplatePage } from '../../routes/templates/[slug]/+page.server';
import { loadTaxonomyLanding } from './taxonomyLanding';

describe('taxonomy landing route loaders', () => {
  it.each([
    {
      kind: 'tag',
      load: loadTagPage,
      params: { tag: 'reaction' },
      routePath: '/tags/reaction?offset=24',
      apiPath: '/api/v1/memes/tags/reaction',
      expectedOffset: 24
    },
    {
      kind: 'template',
      load: loadTemplatePage,
      params: { slug: 'distracted-boyfriend' },
      routePath: '/templates/distracted-boyfriend?offset=-8',
      apiPath: '/api/v1/memes/templates/distracted-boyfriend',
      expectedOffset: 0
    }
  ] as const)('loads the $kind route through the matching API with shared pagination and cookies', async (route) => {
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const apiUrl = new URL(String(input));
      expect(apiUrl.pathname).toBe(route.apiPath);
      expect(apiUrl.searchParams.get('limit')).toBe('12');
      expect(apiUrl.searchParams.get('offset')).toBe(String(route.expectedOffset));
      expect(new Headers(init?.headers).get('cookie')).toBe('memexpert_access_token=guest-token');

      return jsonResponse({
        kind: route.kind,
        slug: route.kind === 'tag' ? route.params.tag : route.params.slug,
        title: 'Taxonomy landing',
        description: null,
        page: {
          items: [],
          limit: 12,
          offset: route.expectedOffset,
          total: 0,
          has_more: false,
          request_id: `req_${route.kind}`
        },
        trend_summary: null
      });
    }) satisfies ApiFetch;
    const url = new URL(route.routePath, 'https://web.memexpert.test');

    const result = await route.load({
      fetch,
      params: route.params,
      request: new Request(url, { headers: { cookie: 'memexpert_access_token=guest-token' } }),
      url
    } as never);

    expect(fetch).toHaveBeenCalledOnce();
    expect(result).toMatchObject({
      landing: { kind: route.kind },
      offset: route.expectedOffset,
      errorMessage: null
    });
  });

  it('keeps API and connectivity failures consumer-safe', async () => {
    const apiFailure = await loadTaxonomyLanding({
      kind: 'tag',
      slug: 'missing',
      rawOffset: 'invalid',
      fetch: (async () => jsonResponse({ detail: 'Tag not found.' }, 404)) satisfies ApiFetch,
      baseUrl: 'https://api.memexpert.test'
    });
    const connectivityFailure = await loadTaxonomyLanding({
      kind: 'template',
      slug: 'offline',
      rawOffset: null,
      fetch: (async () => {
        throw new Error('socket details that should not leak');
      }) satisfies ApiFetch,
      baseUrl: 'https://api.memexpert.test'
    });

    expect(apiFailure).toEqual({ landing: null, offset: 0, errorMessage: 'Tag not found.' });
    expect(connectivityFailure).toEqual({
      landing: null,
      offset: 0,
      errorMessage: 'Could not reach the meme catalog API.'
    });
  });
});

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' }
  });
}
