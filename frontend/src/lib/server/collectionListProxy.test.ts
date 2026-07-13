import { describe, expect, it, vi } from 'vitest';

import { proxyCollectionList, type ProxyFetch } from './collectionListProxy';

describe('collection list proxy', () => {
  it('forwards cookies, request cancellation, and guest bootstrap cookies', async () => {
    const incomingRequest = new Request('https://web.memexpert.test/api/v1/collections', {
      headers: { cookie: 'memexpert_access_token=guest-token; other=1' }
    });
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const headers = new Headers(init?.headers);

      expect(String(input)).toBe('https://api.memexpert.test/api/v1/collections');
      expect(init?.method).toBe('GET');
      expect(init?.signal).toBe(incomingRequest.signal);
      expect(headers.get('accept')).toBe('application/json');
      expect(headers.get('cookie')).toBe('memexpert_access_token=guest-token; other=1');

      return new Response(JSON.stringify({ collections: [], active_save_collection_id: null }), {
        status: 200,
        headers: {
          'content-type': 'application/json',
          'set-cookie': 'memexpert_access_token=bootstrapped; Path=/; HttpOnly; SameSite=Lax'
        }
      });
    }) satisfies ProxyFetch;

    const response = await proxyCollectionList({
      fetch: mockFetch,
      request: incomingRequest,
      apiBaseUrl: 'https://api.memexpert.test'
    });

    expect(response.status).toBe(200);
    expect(response.headers.get('set-cookie')).toContain('memexpert_access_token=bootstrapped');
    await expect(response.json()).resolves.toEqual({ collections: [], active_save_collection_id: null });
    expect(mockFetch).toHaveBeenCalledOnce();
  });
});
