import { describe, expect, it, vi } from 'vitest';

import { proxyPinReorder, type ProxyFetch } from './pinReorderProxy';

describe('pin reorder proxy', () => {
  it('forwards cookies, requested-with header, and full ordered pin body', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const headers = new Headers(init?.headers);

      expect(url.toString()).toBe('https://api.memexpert.test/api/v1/memes/pins/reorder');
      expect(init?.method).toBe('PUT');
      expect(headers.get('cookie')).toBe('memexpert_access_token=full');
      expect(headers.get('content-type')).toBe('application/json');
      expect(headers.get('x-requested-with')).toBe('XMLHttpRequest');
      expect(JSON.parse(String(init?.body))).toEqual({ meme_ids: ['meme-2', 'meme-1'] });

      return new Response(JSON.stringify([{ meme_id: 'meme-2', position: 1 }]), {
        status: 200,
        headers: { 'content-type': 'application/json' }
      });
    }) satisfies ProxyFetch;

    const response = await proxyPinReorder({
      fetch: mockFetch,
      request: new Request('https://web.memexpert.test/api/v1/memes/pins/reorder', {
        method: 'PUT',
        headers: {
          cookie: 'memexpert_access_token=full',
          'content-type': 'application/json',
          'x-requested-with': 'XMLHttpRequest'
        },
        body: JSON.stringify({ meme_ids: ['meme-2', 'meme-1'] })
      }),
      apiBaseUrl: 'https://api.memexpert.test'
    });

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual([{ meme_id: 'meme-2', position: 1 }]);
    expect(mockFetch).toHaveBeenCalledOnce();
  });
});
