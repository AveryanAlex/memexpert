import { describe, expect, it, vi } from 'vitest';

import { proxyAuthSession, proxyTelegramLinkStart } from './authProxy';

function request(url: string, init: RequestInit = {}): Request {
  return new Request(url, {
    ...init,
    headers: {
      cookie: 'memexpert_access_token=guest-token; other=value',
      ...(init.headers as Record<string, string> | undefined)
    }
  });
}

describe('authProxy', () => {
  it('proxies current session reads with incoming cookies and forwards set-cookie', async () => {
    const incomingRequest = request('https://app.test/api/v1/auth/session');
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe('http://backend.test/api/v1/auth/session');
      expect(init?.method).toBe('GET');
      expect(new Headers(init?.headers).get('cookie')).toBe('memexpert_access_token=guest-token; other=value');
      expect(init?.signal).toBe(incomingRequest.signal);
      return new Response(JSON.stringify({ user: { account_type: 'guest' }, linked_providers: { telegram_linked: false } }), {
        status: 200,
        headers: { 'content-type': 'application/json', 'set-cookie': 'memexpert_access_token=repaired; Path=/; HttpOnly; SameSite=Lax' }
      });
    });

    const response = await proxyAuthSession({ fetch: fetchMock, request: incomingRequest, apiBaseUrl: 'http://backend.test' });

    expect(response.status).toBe(200);
    expect(response.headers.get('content-type')).toContain('application/json');
    expect(response.headers.get('set-cookie')).toContain('memexpert_access_token=repaired');
  });

  it('proxies Telegram link start as POST with CSRF header and cookies', async () => {
    const incomingRequest = request('https://app.test/api/v1/auth/link/telegram', { method: 'POST' });
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe('http://backend.test/api/v1/auth/link/telegram');
      expect(init?.method).toBe('POST');
      const headers = new Headers(init?.headers);
      expect(headers.get('accept')).toBe('application/json');
      expect(headers.get('x-requested-with')).toBe('XMLHttpRequest');
      expect(headers.get('cookie')).toBe('memexpert_access_token=guest-token; other=value');
      expect(init?.signal).toBe(incomingRequest.signal);
      return new Response(JSON.stringify({ code: 'abc', deep_link_url: 'https://t.me/bot?start=link_abc', expires_in_seconds: 600 }), {
        status: 201,
        headers: { 'content-type': 'application/json' }
      });
    });

    const response = await proxyTelegramLinkStart({ fetch: fetchMock, request: incomingRequest, apiBaseUrl: 'http://backend.test' });

    expect(response.status).toBe(201);
    expect(await response.json()).toMatchObject({ code: 'abc' });
  });

  it('passes upstream auth errors through unchanged', async () => {
    const fetchMock = vi.fn(async () =>
      new Response(JSON.stringify({ code: 'GUEST_ACCOUNT_REQUIRED', detail: 'Only guest accounts can be linked.' }), {
        status: 403,
        headers: { 'content-type': 'application/json' }
      })
    );

    const response = await proxyTelegramLinkStart({ fetch: fetchMock, request: request('https://app.test/api/v1/auth/link/telegram', { method: 'POST' }), apiBaseUrl: 'http://backend.test' });

    expect(response.status).toBe(403);
    expect(await response.json()).toMatchObject({ detail: 'Only guest accounts can be linked.' });
  });

  it('aborts the upstream auth request when the downstream request is cancelled', async () => {
    const controller = new AbortController();
    const incomingRequest = request('https://app.test/api/v1/auth/session', { signal: controller.signal });
    const forwardedSignals: AbortSignal[] = [];
    const fetchMock = vi.fn((_input: RequestInfo | URL, init?: RequestInit) => {
      const signal = init?.signal;
      if (!signal) throw new Error('Expected the downstream abort signal to be forwarded.');
      forwardedSignals.push(signal);
      return new Promise<Response>((_resolve, reject) => {
        signal.addEventListener('abort', () => reject(signal.reason), { once: true });
      });
    });

    const responsePromise = proxyAuthSession({ fetch: fetchMock, request: incomingRequest, apiBaseUrl: 'http://backend.test' });
    const abortReason = new DOMException('Client disconnected', 'AbortError');
    controller.abort(abortReason);

    expect(forwardedSignals).toEqual([incomingRequest.signal]);
    expect(forwardedSignals[0].aborted).toBe(true);
    await expect(responsePromise).rejects.toBe(abortReason);
  });
});
