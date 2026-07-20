import type { Cookies } from '@sveltejs/kit';
import { describe, expect, it, vi } from 'vitest';

import type { ApiFetch } from '$lib/api/client';
import { proxyMemePage } from './memePageProxy';

describe('meme page proxy', () => {
  it('forwards an opaque home cursor, viewer cookie, and abort signal without an offset', async () => {
    const request = new Request('https://web.memexpert.test/api/v1/memes/home-feed?limit=12&cursor=signed%20cursor', {
      headers: { cookie: 'memexpert_access_token=viewer' }
    });
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      expect(url.pathname).toBe('/api/v1/memes/home-feed');
      expect(url.searchParams.get('cursor')).toBe('signed cursor');
      expect(url.searchParams.has('offset')).toBe(false);
      expect(new Headers(init?.headers).get('cookie')).toBe('memexpert_access_token=viewer');
      expect(init?.signal).toBe(request.signal);
      return new Response(JSON.stringify({
        items: [],
        limit: 12,
        offset: 0,
        total: 0,
        has_more: false,
        request_id: 'request-1',
        feed_session_id: 'feed-1',
        next_cursor: null,
        expires_at: '2026-07-20T12:00:00Z'
      }), { headers: { 'content-type': 'application/json' } });
    }) satisfies ApiFetch;

    const response = await proxyMemePage({
      fetch,
      request,
      cookies: {} as Cookies,
      apiBaseUrl: 'https://api.memexpert.test',
      mode: 'home-feed'
    });

    expect(response.status).toBe(200);
    expect(response.headers.get('cache-control')).toBe('private, no-store');
    expect(fetch).toHaveBeenCalledOnce();
  });

  it('preserves the machine-readable expired-cursor error and private cache policy', async () => {
    const fetch = vi.fn(async () => new Response(
      JSON.stringify({ detail: 'feed_cursor_expired' }),
      { status: 410, headers: { 'content-type': 'application/json' } }
    )) satisfies ApiFetch;

    const response = await proxyMemePage({
      fetch,
      request: new Request('https://web.memexpert.test/api/v1/memes/home-feed?cursor=expired'),
      cookies: {} as Cookies,
      apiBaseUrl: 'https://api.memexpert.test',
      mode: 'home-feed'
    });

    expect(response.status).toBe(410);
    expect(response.headers.get('cache-control')).toBe('private, no-store');
    await expect(response.json()).resolves.toEqual({
      code: 'feed_cursor_expired',
      detail: 'feed_cursor_expired'
    });
  });

  it('rejects mixing cursor and legacy offset before contacting the backend', async () => {
    const fetch = vi.fn() as unknown as ApiFetch;
    const response = await proxyMemePage({
      fetch,
      request: new Request('https://web.memexpert.test/api/v1/memes/home-feed?cursor=signed&offset=12'),
      cookies: {} as Cookies,
      apiBaseUrl: 'https://api.memexpert.test',
      mode: 'home-feed'
    });

    expect(response.status).toBe(400);
    expect(response.headers.get('cache-control')).toBe('private, no-store');
    expect(fetch).not.toHaveBeenCalled();
  });
});
