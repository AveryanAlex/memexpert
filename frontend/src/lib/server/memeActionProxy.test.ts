import { describe, expect, it, vi } from 'vitest';

import { proxyMemeAction, type ProxyFetch } from './memeActionProxy';

describe('meme action proxy', () => {
  it('forwards method, encoded URL, cookies, and JSON response metadata', async () => {
    const upstreamHeaders = new Headers({ 'content-type': 'application/json' });
    upstreamHeaders.append('set-cookie', 'guest_session=abc; Path=/; HttpOnly');

    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const headers = new Headers(init?.headers);

      expect(url.toString()).toBe('https://api.memexpert.test/api/v1/memes/meme%2F123/favorite');
      expect(init?.method).toBe('POST');
      expect(headers.get('accept')).toBe('application/json');
      expect(headers.get('cookie')).toBe('sid=front; guest=old');

      return new Response(JSON.stringify({ id: 'saved-row' }), {
        status: 201,
        headers: upstreamHeaders
      });
    }) satisfies ProxyFetch;

    const response = await proxyMemeAction({
      fetch: mockFetch,
      request: new Request('https://web.memexpert.test/api/v1/memes/meme%2F123/favorite', {
        method: 'POST',
        headers: { cookie: 'sid=front; guest=old' }
      }),
      apiBaseUrl: 'https://api.memexpert.test',
      memeId: 'meme/123',
      action: 'favorite',
      method: 'POST'
    });

    expect(response.status).toBe(201);
    expect(response.headers.get('content-type')).toBe('application/json');
    expect(response.headers.get('set-cookie')).toContain('guest_session=abc');
    await expect(response.json()).resolves.toEqual({ id: 'saved-row' });
    expect(mockFetch).toHaveBeenCalledOnce();
  });

  it('passes through backend errors without rewriting business rules', async () => {
    const mockFetch = vi.fn(async () => {
      return new Response(JSON.stringify({ detail: 'Full account required.' }), {
        status: 403,
        headers: { 'content-type': 'application/json' }
      });
    }) satisfies ProxyFetch;

    const response = await proxyMemeAction({
      fetch: mockFetch,
      request: new Request('https://web.memexpert.test/api/v1/memes/meme-123/pin', { method: 'DELETE' }),
      apiBaseUrl: 'https://api.memexpert.test',
      memeId: 'meme-123',
      action: 'pin',
      method: 'DELETE'
    });

    expect(response.status).toBe(403);
    await expect(response.json()).resolves.toEqual({ detail: 'Full account required.' });
  });

  it('forwards report JSON bodies and CSRF-compatible request headers', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const headers = new Headers(init?.headers);

      expect(url.toString()).toBe('https://api.memexpert.test/api/v1/memes/meme-123/report');
      expect(init?.method).toBe('POST');
      expect(headers.get('content-type')).toBe('application/json');
      expect(headers.get('x-requested-with')).toBe('XMLHttpRequest');
      expect(init?.body).toBe(JSON.stringify({ reason: 'spam', note: 'bad link' }));

      return new Response(JSON.stringify({ id: 'report-1' }), {
        status: 200,
        headers: { 'content-type': 'application/json' }
      });
    }) satisfies ProxyFetch;

    const response = await proxyMemeAction({
      fetch: mockFetch,
      request: new Request('https://web.memexpert.test/api/v1/memes/meme-123/report', {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          'x-requested-with': 'XMLHttpRequest'
        },
        body: JSON.stringify({ reason: 'spam', note: 'bad link' })
      }),
      apiBaseUrl: 'https://api.memexpert.test',
      memeId: 'meme-123',
      action: 'report',
      method: 'POST'
    });

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({ id: 'report-1' });
  });

  it('forwards list-card telemetry attribution bodies', async () => {
    const calls: Array<{ path: string; body: unknown; contentType: string | null }> = [];
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const headers = new Headers(init?.headers);
      calls.push({ path: url.pathname, body: JSON.parse(String(init?.body)), contentType: headers.get('content-type') });

      return new Response(JSON.stringify({ ok: true }), {
        status: 200,
        headers: { 'content-type': 'application/json' }
      });
    }) satisfies ProxyFetch;
    const body = { attribution: { request_id: 'req-list', impression_id: 'imp-list', rank: 3 } };

    for (const action of ['impression', 'detail-click'] as const) {
      const response = await proxyMemeAction({
        fetch: mockFetch,
        request: new Request(`https://web.memexpert.test/api/v1/memes/meme-123/${action}`, {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(body)
        }),
        apiBaseUrl: 'https://api.memexpert.test',
        memeId: 'meme-123',
        action,
        method: 'POST'
      });
      expect(response.status).toBe(200);
    }

    expect(calls).toEqual([
      { path: '/api/v1/memes/meme-123/impression', body, contentType: 'application/json' },
      { path: '/api/v1/memes/meme-123/detail-click', body, contentType: 'application/json' }
    ]);
  });
});
