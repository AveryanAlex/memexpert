import type { RequestEvent } from '@sveltejs/kit';
import { describe, expect, it, vi } from 'vitest';

vi.mock('$env/dynamic/private', () => ({
  env: { API_BASE_URL: 'http://backend.test' }
}));

import { POST as completeQrLogin } from '../../routes/admin/telegram/api/qr/complete/+server';
import { POST as startQrLogin } from '../../routes/admin/telegram/api/qr/start/+server';
import { DELETE as cancelLoginAttempt } from '../../routes/admin/telegram/api/login-attempts/[attemptId]/+server';

describe('admin Telegram QR proxy routes', () => {
  it('forwards QR start with admin cookies', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe('http://backend.test/api/v1/admin/telegram/login-attempts/qr');
      expect(init?.method).toBe('POST');
      const headers = new Headers(init?.headers);
      expect(headers.get('accept')).toBe('application/json');
      expect(headers.get('content-type')).toBe('application/json');
      expect(headers.get('x-requested-with')).toBe('XMLHttpRequest');
      expect(headers.get('cookie')).toBe('memexpert_access_token=token');
      expect(init?.signal).toBe(request.signal);
      expect(JSON.parse(String(init?.body))).toEqual({ telegram_session_id: 'session-1' });
      return new Response(
        JSON.stringify({ attempt_id: 'attempt-1', qr_url: 'tg://login?token=x', expires_at: '2026-01-01T00:10:00Z', message: 'started' }),
        { status: 200, headers: { 'content-type': 'application/json' } }
      );
    }) satisfies RequestEvent['fetch'];

    const request = jsonRequest('/admin/telegram/api/qr/start', { telegram_session_id: 'session-1' });
    const response = await startQrLogin(
      minimalStartEvent({
        fetch: fetchMock,
        request
      })
    );

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toMatchObject({ attempt_id: 'attempt-1' });
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it('forwards QR complete long-poll payload with admin cookies', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe('http://backend.test/api/v1/admin/telegram/login-attempts/attempt-1/qr/complete');
      expect(init?.method).toBe('POST');
      const headers = new Headers(init?.headers);
      expect(headers.get('accept')).toBe('application/json');
      expect(headers.get('content-type')).toBe('application/json');
      expect(headers.get('x-requested-with')).toBe('XMLHttpRequest');
      expect(headers.get('cookie')).toBe('memexpert_access_token=token');
      expect(init?.signal).toBe(request.signal);
      expect(JSON.parse(String(init?.body))).toEqual({ note: 'operator note' });
      return new Response(
        JSON.stringify({ status: 'pending', telegram_session: null, password_required: false, message: 'Still waiting for Telegram QR scan.' }),
        { status: 200, headers: { 'content-type': 'application/json' } }
      );
    }) satisfies RequestEvent['fetch'];

    const request = jsonRequest('/admin/telegram/api/qr/complete', { attempt_id: 'attempt-1', note: 'operator note' });
    const response = await completeQrLogin(
      minimalCompleteEvent({
        fetch: fetchMock,
        request
      })
    );

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toMatchObject({ status: 'pending' });
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it('starts a standalone QR attempt when no target account id is supplied', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe('http://backend.test/api/v1/admin/telegram/login-attempts/qr');
      expect(JSON.parse(String(init?.body))).toEqual({});
      return jsonResponse({ attempt_id: 'attempt-1' });
    }) satisfies RequestEvent['fetch'];

    const response = await startQrLogin(
      minimalStartEvent({
        fetch: fetchMock,
        request: jsonRequest('/admin/telegram/api/qr/start', {})
      })
    );

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toEqual({ attempt_id: 'attempt-1' });
    expect(fetchMock).toHaveBeenCalledOnce();
  });

  it('rejects QR completion without an opaque attempt id before calling the backend', async () => {
    const fetchMock = vi.fn() satisfies RequestEvent['fetch'];

    const response = await completeQrLogin(
      minimalCompleteEvent({
        fetch: fetchMock,
        request: jsonRequest('/admin/telegram/api/qr/complete', { session_id: 'session-1' })
      })
    );

    expect(response.status).toBe(400);
    await expect(response.json()).resolves.toEqual({ detail: 'attempt_id is required.' });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('forwards best-effort attempt cancellation with admin cookies', async () => {
    const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(String(input)).toBe('http://backend.test/api/v1/admin/telegram/login-attempts/attempt-1');
      expect(init?.method).toBe('DELETE');
      const headers = new Headers(init?.headers);
      expect(headers.get('accept')).toBe('application/json');
      expect(headers.get('x-requested-with')).toBe('XMLHttpRequest');
      expect(headers.get('cookie')).toBe('memexpert_access_token=token');
      return jsonResponse({ attempt_id: 'attempt-1', status: 'cancelled', message: 'cancelled' });
    }) satisfies RequestEvent['fetch'];

    const response = await cancelLoginAttempt({
      fetch: fetchMock,
      request: jsonRequest('/admin/telegram/api/login-attempts/attempt-1', undefined, 'DELETE'),
      params: { attemptId: 'attempt-1' }
    } as never);

    expect(response.status).toBe(200);
    await expect(response.json()).resolves.toMatchObject({ status: 'cancelled' });
  });
});

function jsonRequest(path: string, body: unknown, method = 'POST'): Request {
  return new Request(`https://web.memexpert.test${path}`, {
    method,
    headers: {
      cookie: 'memexpert_access_token=token',
      'content-type': 'application/json'
    },
    body: body === undefined ? undefined : JSON.stringify(body)
  });
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), { status, headers: { 'content-type': 'application/json' } });
}

function minimalStartEvent(input: { fetch: RequestEvent['fetch']; request: Request }): Parameters<typeof startQrLogin>[0] {
  return input as Parameters<typeof startQrLogin>[0];
}

function minimalCompleteEvent(input: { fetch: RequestEvent['fetch']; request: Request }): Parameters<typeof completeQrLogin>[0] {
  return input as Parameters<typeof completeQrLogin>[0];
}
