import type { Cookies } from '@sveltejs/kit';
import { describe, expect, it, vi } from 'vitest';

import type { ApiFetch } from '$lib/api/client';
import type { PublicMemeSearchPageRead } from '$lib/api/types';
import { proxySimilarMemes } from './similarMemeProxy';

describe('similar meme proxy', () => {
  it('forwards validated pagination, cookies, cancellation, and the backend access cookie', async () => {
    const page: PublicMemeSearchPageRead = {
      items: [],
      limit: 12,
      offset: 24,
      total: 200,
      has_more: true,
      request_id: 'req_similar'
    };
    const request = new Request('https://beta.memexpert.test/api/v1/memes/source/similar?limit=12&offset=24', {
      headers: { cookie: 'memexpert_access_token=viewer; theme=warm' }
    });
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const headers = new Headers(init?.headers);

      expect(url.href).toBe('https://api.memexpert.test/api/v1/memes/source/similar?include_nsfw=false&limit=12&offset=24');
      expect(headers.get('cookie')).toBe('memexpert_access_token=viewer; theme=warm');
      expect(init?.signal).toBe(request.signal);

      return new Response(JSON.stringify(page), {
        headers: {
          'content-type': 'application/json',
          'set-cookie': 'memexpert_access_token=guest-next; Path=/; HttpOnly; SameSite=Lax'
        }
      });
    }) satisfies ApiFetch;
    const set = vi.fn();

    const response = await proxySimilarMemes({
      fetch,
      request,
      cookies: { set } as unknown as Cookies,
      apiBaseUrl: 'https://api.memexpert.test',
      memeId: 'source'
    });

    expect(response.status).toBe(200);
    expect(response.headers.get('cache-control')).toBe('private, no-store');
    await expect(response.json()).resolves.toEqual(page);
    expect(fetch).toHaveBeenCalledOnce();
    expect(set).toHaveBeenCalledWith(
      'memexpert_access_token',
      'guest-next',
      expect.objectContaining({ path: '/', httpOnly: true, sameSite: 'lax' })
    );
  });

  it.each([
    ['limit', '0'],
    ['limit', '101'],
    ['limit', '12items'],
    ['offset', '-1'],
    ['offset', 'later']
  ])('rejects an invalid %s before contacting the backend', async (name, value) => {
    const fetch = vi.fn() as unknown as ApiFetch;
    const request = new Request(`https://beta.memexpert.test/api/v1/memes/source/similar?${name}=${value}`);

    const response = await proxySimilarMemes({
      fetch,
      request,
      cookies: {} as Cookies,
      apiBaseUrl: 'https://api.memexpert.test',
      memeId: 'source'
    });

    expect(response.status).toBe(400);
    expect(response.headers.get('cache-control')).toBe('private, no-store');
    await expect(response.json()).resolves.toEqual({ detail: expect.stringContaining(`${name} must be an integer`) });
    expect(fetch).not.toHaveBeenCalled();
  });

  it('preserves an upstream error status and safe detail', async () => {
    const fetch = vi.fn(async () => {
      return new Response(JSON.stringify({ detail: 'Similar ranking is temporarily unavailable.' }), {
        status: 503,
        headers: { 'content-type': 'application/json' }
      });
    }) satisfies ApiFetch;

    const response = await proxySimilarMemes({
      fetch,
      request: new Request('https://beta.memexpert.test/api/v1/memes/source/similar'),
      cookies: {} as Cookies,
      apiBaseUrl: 'https://api.memexpert.test',
      memeId: 'source'
    });

    expect(response.status).toBe(503);
    expect(response.headers.get('cache-control')).toBe('private, no-store');
    await expect(response.json()).resolves.toEqual({ detail: 'Similar ranking is temporarily unavailable.' });
  });
});
