import { describe, expect, it, vi } from 'vitest';

import { proxyCollectionMemeAction, type ProxyFetch } from './collectionMemeProxy';

describe('collection meme proxy', () => {
  it('forwards cookies and passes through backend set-cookie headers', async () => {
    const upstreamHeaders = new Headers({ 'content-type': 'application/json' });
    upstreamHeaders.append('set-cookie', 'memexpert_access_token=new-token; Path=/; HttpOnly');

    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const headers = new Headers(init?.headers);

      expect(url.toString()).toBe('https://api.memexpert.test/api/v1/collections/collection%2F123/memes/meme%2F456');
      expect(init?.method).toBe('POST');
      expect(headers.get('accept')).toBe('application/json');
      expect(headers.get('cookie')).toBe('memexpert_access_token=old-token; other=1');
      expect(headers.get('x-requested-with')).toBe('XMLHttpRequest');

      return new Response(JSON.stringify({ saved: true }), { status: 201, headers: upstreamHeaders });
    }) satisfies ProxyFetch;

    const response = await proxyCollectionMemeAction({
      fetch: mockFetch,
      request: new Request('https://web.memexpert.test/api/v1/collections/collection%2F123/memes/meme%2F456', {
        method: 'POST',
        headers: {
          cookie: 'memexpert_access_token=old-token; other=1',
          'x-requested-with': 'XMLHttpRequest'
        }
      }),
      apiBaseUrl: 'https://api.memexpert.test',
      collectionId: 'collection/123',
      memeId: 'meme/456',
      method: 'POST'
    });

    expect(response.status).toBe(201);
    expect(response.headers.get('content-type')).toBe('application/json');
    expect(response.headers.get('set-cookie')).toContain('memexpert_access_token=new-token');
    await expect(response.json()).resolves.toEqual({ saved: true });
    expect(mockFetch).toHaveBeenCalledOnce();
  });
});
