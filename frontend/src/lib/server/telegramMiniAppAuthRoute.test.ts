import type { Cookies, RequestEvent } from '@sveltejs/kit';
import { describe, expect, it, vi } from 'vitest';

vi.mock('$env/dynamic/private', () => ({
  env: { API_BASE_URL: 'https://api.memexpert.test' }
}));

import { POST } from '../../routes/telegram-miniapp/auth/+server';

describe('/telegram-miniapp/auth proxy route', () => {
  it('forwards initData and cookies while stripping access tokens from the public response', async () => {
    const cookies = { set: vi.fn() } as unknown as Cookies;
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const headers = new Headers(init?.headers);

      expect(url.toString()).toBe('https://api.memexpert.test/api/v1/auth/telegram-miniapp');
      expect(init?.method).toBe('POST');
      expect(headers.get('accept')).toBe('application/json');
      expect(headers.get('content-type')).toBe('application/json');
      expect(headers.get('x-requested-with')).toBe('XMLHttpRequest');
      expect(headers.get('cookie')).toBe('memexpert_access_token=old');
      expect(JSON.parse(String(init?.body))).toEqual({ initData: 'query_id=abc&user=%7B%7D' });

      return new Response(
        JSON.stringify({
          access_token: 'secret-upstream-token',
          user: { id: 'user-1', access_token: 'nested-secret' },
          linked: true
        }),
        {
          status: 200,
          headers: {
            'content-type': 'application/json',
            'set-cookie': 'memexpert_access_token=new-token; Path=/; HttpOnly; SameSite=Lax'
          }
        }
      );
    }) satisfies RequestEvent['fetch'];

    const response = await POST(
      minimalEvent({
        cookies,
        fetch: mockFetch,
        request: new Request('https://web.memexpert.test/telegram-miniapp/auth', {
          method: 'POST',
          headers: {
            cookie: 'memexpert_access_token=old',
            'content-type': 'application/json'
          },
          body: JSON.stringify({ initData: 'query_id=abc&user=%7B%7D' })
        })
      })
    );

    expect(response.status).toBe(200);
    expect(cookies.set).toHaveBeenCalledWith(
      'memexpert_access_token',
      'new-token',
      expect.objectContaining({ path: '/', httpOnly: true, secure: false, sameSite: 'lax' })
    );
    await expect(response.json()).resolves.toEqual({ user: { id: 'user-1' }, linked: true });
    expect(mockFetch).toHaveBeenCalledOnce();
  });
});

function minimalEvent(input: { cookies: Cookies; fetch: RequestEvent['fetch']; request: Request }): RequestEvent {
  return input as RequestEvent;
}
