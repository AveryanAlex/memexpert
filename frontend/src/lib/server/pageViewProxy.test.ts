import { describe, expect, it, vi } from 'vitest';

import { proxyPageView, type ProxyFetch } from './pageViewProxy';

describe('page-view proxy', () => {
  it('forwards the bounded payload, cookies, and CSRF-compatible header', async () => {
    const incomingRequest = new Request('https://web.memexpert.test/api/v1/analytics/page-views', {
      method: 'POST',
      headers: {
        cookie: 'memexpert_access_token=guest-token',
        'content-type': 'application/json',
        'x-requested-with': 'XMLHttpRequest'
      },
      body: JSON.stringify({ surface: 'web_search' })
    });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe('https://api.memexpert.test/api/v1/analytics/page-views');
      expect(init?.method).toBe('POST');
      expect(init?.body).toBe(JSON.stringify({ surface: 'web_search' }));
      expect(init?.signal).toBe(incomingRequest.signal);
      const headers = new Headers(init?.headers);
      expect(headers.get('accept')).toBe('application/json');
      expect(headers.get('content-type')).toBe('application/json');
      expect(headers.get('cookie')).toBe('memexpert_access_token=guest-token');
      expect(headers.get('x-requested-with')).toBe('XMLHttpRequest');
      return new Response(JSON.stringify({ ok: true }), {
        status: 202,
        headers: { 'content-type': 'application/json' }
      });
    }) satisfies ProxyFetch;

    const response = await proxyPageView({
      fetch: fetchMock,
      request: incomingRequest,
      apiBaseUrl: 'https://api.memexpert.test'
    });

    expect(response.status).toBe(202);
    await expect(response.json()).resolves.toEqual({ ok: true });
  });

  it('does not mint a CSRF header for a request that did not supply one', async () => {
    const fetchMock = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      expect(new Headers(init?.headers).get('x-requested-with')).toBeNull();
      return new Response(JSON.stringify({ ok: true }), {
        status: 202,
        headers: { 'content-type': 'application/json' }
      });
    }) satisfies ProxyFetch;

    const response = await proxyPageView({
      fetch: fetchMock,
      request: new Request('https://web.memexpert.test/api/v1/analytics/page-views', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ surface: 'web_search' })
      }),
      apiBaseUrl: 'https://api.memexpert.test'
    });

    expect(response.status).toBe(202);
  });
});
