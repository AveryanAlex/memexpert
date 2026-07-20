import { describe, expect, it, vi } from 'vitest';

import {
  proxyHomeFeedReauthorization,
  type ProxyFetch
} from './homeFeedReauthorizationProxy';

describe('Home feed reauthorization proxy', () => {
  it('forwards normalized Home filters, viewer state, body, and cancellation', async () => {
    const body = {
      items: [{ meme_id: 'meme-1', attribution_token: 'signed-home-token' }]
    };
    const request = new Request(
      'https://web.memexpert.test/api/v1/memes/home-feed/reauthorize' +
        '?tags=Reaction&tags=cat%2Cwork&include_nsfw=true&media_type=video&language=ru' +
        '&scope=all&offset=200',
      {
        method: 'POST',
        headers: {
          cookie: 'memexpert_access_token=viewer',
          'content-type': 'application/json',
          'x-requested-with': 'XMLHttpRequest'
        },
        body: JSON.stringify(body)
      }
    );
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      expect(url.origin).toBe('https://api.memexpert.test');
      expect(url.pathname).toBe('/api/v1/memes/home-feed/reauthorize');
      expect(url.searchParams.getAll('tags')).toEqual(['reaction', 'cat', 'work']);
      expect(url.searchParams.get('include_nsfw')).toBe('true');
      expect(url.searchParams.get('media_type')).toBe('video');
      expect(url.searchParams.get('language')).toBe('ru');
      expect(url.searchParams.has('scope')).toBe(false);
      expect(url.searchParams.has('offset')).toBe(false);
      expect(init?.method).toBe('POST');
      expect(init?.signal).toBe(request.signal);
      expect(new Headers(init?.headers).get('cookie')).toBe('memexpert_access_token=viewer');
      expect(new Headers(init?.headers).get('x-requested-with')).toBe('XMLHttpRequest');
      expect(JSON.parse(String(init?.body))).toEqual(body);
      return new Response(JSON.stringify({ items: [] }), {
        headers: { 'content-type': 'application/json' }
      });
    }) satisfies ProxyFetch;

    const response = await proxyHomeFeedReauthorization({
      fetch,
      request,
      apiBaseUrl: 'https://api.memexpert.test'
    });

    expect(response.status).toBe(200);
    expect(response.headers.get('cache-control')).toBe('private, no-store');
    expect(fetch).toHaveBeenCalledOnce();
  });
});
