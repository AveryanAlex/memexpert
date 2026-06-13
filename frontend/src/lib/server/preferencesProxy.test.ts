import { describe, expect, it, vi } from 'vitest';

import { proxyUserPreferences, type ProxyFetch } from './preferencesProxy';

describe('user preferences proxy', () => {
  it('forwards cookies and JSON body to the backend preferences endpoint', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const headers = new Headers(init?.headers);

      expect(url.toString()).toBe('https://api.memexpert.test/api/v1/auth/preferences');
      expect(init?.method).toBe('PATCH');
      expect(headers.get('cookie')).toBe('memexpert_access_token=guest');
      expect(headers.get('content-type')).toBe('application/json');
      expect(headers.get('x-requested-with')).toBe('XMLHttpRequest');
      expect(JSON.parse(String(init?.body))).toEqual({ nsfw_enabled: true });

      return new Response(JSON.stringify({ id: 'user-id', nsfw_enabled: true }), {
        status: 200,
        headers: { 'content-type': 'application/json', 'set-cookie': 'memexpert_access_token=new; Path=/; HttpOnly' }
      });
    }) satisfies ProxyFetch;

    const response = await proxyUserPreferences({
      fetch: mockFetch,
      request: new Request('https://web.memexpert.test/api/v1/auth/preferences', {
        method: 'PATCH',
        headers: {
          cookie: 'memexpert_access_token=guest',
          'content-type': 'application/json',
          'x-requested-with': 'XMLHttpRequest'
        },
        body: JSON.stringify({ nsfw_enabled: true })
      }),
      apiBaseUrl: 'https://api.memexpert.test'
    });

    expect(response.status).toBe(200);
    expect(response.headers.get('set-cookie')).toContain('memexpert_access_token=new');
    await expect(response.json()).resolves.toEqual({ id: 'user-id', nsfw_enabled: true });
    expect(mockFetch).toHaveBeenCalledOnce();
  });
});
