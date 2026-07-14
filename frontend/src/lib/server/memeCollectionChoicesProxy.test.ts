import { describe, expect, it, vi } from 'vitest';

import { proxyMemeCollectionChoices, type ProxyFetch } from './memeCollectionChoicesProxy';

describe('proxyMemeCollectionChoices', () => {
  it('forwards the viewer cookie to the meme-scoped collection endpoint', async () => {
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(new URL(String(input)).pathname).toBe('/api/v1/collections/meme-choices/meme%20one');
      expect(new Headers(init?.headers).get('cookie')).toBe('memexpert_access_token=viewer');
      return new Response(JSON.stringify({ collections: [] }), {
        headers: { 'content-type': 'application/json' }
      });
    }) as ProxyFetch;

    const response = await proxyMemeCollectionChoices({
      fetch,
      request: new Request('https://frontend.test/api/v1/collections/meme-choices/meme%20one', {
        headers: { cookie: 'memexpert_access_token=viewer' }
      }),
      apiBaseUrl: 'https://api.test',
      memeId: 'meme one'
    });

    expect(response.status).toBe(200);
    expect(fetch).toHaveBeenCalledOnce();
  });
});
