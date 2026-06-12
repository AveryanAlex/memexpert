import { describe, expect, it, vi } from 'vitest';

import { proxyActiveSaveCollection, type ProxyFetch } from './activeSaveProxy';

describe('active save collection proxy', () => {
  it('forwards cookies and JSON body to the backend active-save endpoint', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const headers = new Headers(init?.headers);

      expect(url.toString()).toBe('https://api.memexpert.test/api/v1/memes/active-save-collection');
      expect(init?.method).toBe('PUT');
      expect(headers.get('cookie')).toBe('memexpert_access_token=guest');
      expect(headers.get('content-type')).toBe('application/json');
      expect(JSON.parse(String(init?.body))).toEqual({ collection_id: 'favorites-id' });

      return new Response(JSON.stringify({ active_save_collection_id: 'favorites-id' }), {
        status: 200,
        headers: { 'content-type': 'application/json', 'set-cookie': 'memexpert_access_token=new; Path=/; HttpOnly' }
      });
    }) satisfies ProxyFetch;

    const response = await proxyActiveSaveCollection({
      fetch: mockFetch,
      request: new Request('https://web.memexpert.test/api/v1/memes/active-save-collection', {
        method: 'PUT',
        headers: { cookie: 'memexpert_access_token=guest', 'content-type': 'application/json' },
        body: JSON.stringify({ collection_id: 'favorites-id' })
      }),
      apiBaseUrl: 'https://api.memexpert.test'
    });

    expect(response.status).toBe(200);
    expect(response.headers.get('set-cookie')).toContain('memexpert_access_token=new');
    await expect(response.json()).resolves.toEqual({ active_save_collection_id: 'favorites-id' });
    expect(mockFetch).toHaveBeenCalledOnce();
  });
});
