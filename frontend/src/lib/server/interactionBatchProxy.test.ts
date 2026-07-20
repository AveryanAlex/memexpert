import { describe, expect, it, vi } from 'vitest';

import { proxyInteractionBatch, type ProxyFetch } from './interactionBatchProxy';

describe('interaction batch proxy', () => {
  it('forwards the bounded JSON body, cookie, CSRF header, and cancellation', async () => {
    const body = { events: [{ event_id: 'event-1', event_type: 'meme_impression', meme_id: 'meme-1' }] };
    const request = new Request('https://web.memexpert.test/api/v1/analytics/interactions/batch', {
      method: 'POST',
      headers: {
        cookie: 'memexpert_access_token=viewer',
        'content-type': 'application/json',
        'x-requested-with': 'XMLHttpRequest'
      },
      body: JSON.stringify(body)
    });
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe('https://api.memexpert.test/api/v1/analytics/interactions/batch');
      expect(init?.method).toBe('POST');
      expect(init?.signal).toBe(request.signal);
      expect(new Headers(init?.headers).get('cookie')).toBe('memexpert_access_token=viewer');
      expect(new Headers(init?.headers).get('x-requested-with')).toBe('XMLHttpRequest');
      expect(JSON.parse(String(init?.body))).toEqual(body);
      return new Response(JSON.stringify({ recorded: 1, duplicates: 0 }), {
        status: 202,
        headers: { 'content-type': 'application/json' }
      });
    }) satisfies ProxyFetch;

    const response = await proxyInteractionBatch({ fetch, request, apiBaseUrl: 'https://api.memexpert.test' });

    expect(response.status).toBe(202);
    await expect(response.json()).resolves.toEqual({ recorded: 1, duplicates: 0 });
  });
});
