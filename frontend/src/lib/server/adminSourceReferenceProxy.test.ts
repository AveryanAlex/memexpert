import { describe, expect, it, vi } from 'vitest';

import { proxyAdminSourceReference } from './adminSourceReferenceProxy';
import type { ProxyFetch } from './proxyResponse';

describe('admin source reference proxy', () => {
  it('forwards the browser JSON request, authentication cookie, and CSRF header', async () => {
    const body = { reference: '@memach', telegram_session_id: 'session-id', catchup_message_limit: 5_000 };
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const headers = new Headers(init?.headers);
      expect(String(input)).toBe('https://api.memexpert.test/api/v1/admin/telegram/channels/from-reference');
      expect(init?.method).toBe('POST');
      expect(headers.get('cookie')).toBe('memexpert_access_token=token');
      expect(headers.get('content-type')).toBe('application/json');
      expect(headers.get('x-requested-with')).toBe('XMLHttpRequest');
      expect(init?.body).toBe(JSON.stringify(body));
      return jsonResponse({ id: 'source-id' }, 201);
    }) satisfies ProxyFetch;

    const response = await proxyAdminSourceReference({
      fetch,
      request: new Request('https://web.memexpert.test/api/v1/admin/telegram/channels/from-reference', {
        method: 'POST',
        headers: {
          cookie: 'memexpert_access_token=token',
          'content-type': 'application/json',
          'x-requested-with': 'XMLHttpRequest'
        },
        body: JSON.stringify(body)
      }),
      apiBaseUrl: 'https://api.memexpert.test'
    });

    expect(response.status).toBe(201);
    await expect(response.json()).resolves.toEqual({ id: 'source-id' });
    expect(fetch).toHaveBeenCalledOnce();
  });
});

function jsonResponse(payload: unknown, status: number): Response {
  return new Response(JSON.stringify(payload), { status, headers: { 'content-type': 'application/json' } });
}
