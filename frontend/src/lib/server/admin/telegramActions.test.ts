import { describe, expect, it, vi } from 'vitest';
import type { ApiFetch } from '$lib/api/client';
import { telegramAccountActions } from './telegramActions';

const sessionId = '11111111-1111-4111-8111-111111111111';
const attemptId = '22222222-2222-4222-8222-222222222222';

describe('Telegram account actions', () => {
  it('uses minimal PATCH payloads for enable and resume repairs', async () => {
    const calls: Array<{ path: string; method: string; body: unknown }> = [];
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({
        path: new URL(String(input)).pathname,
        method: init?.method ?? 'GET',
        body: init?.body ? JSON.parse(String(init.body)) : null
      });
      return jsonResponse({});
    }) satisfies ApiFetch;

    await expect(telegramAccountActions.repairSession(actionEvent({ session_id: sessionId, repair: 'enable' }, fetch))).resolves.toEqual({
      message: 'Telegram account enabled.'
    });
    await expect(telegramAccountActions.repairSession(actionEvent({ session_id: sessionId, repair: 'resume' }, fetch))).resolves.toEqual({
      message: 'Telegram account resumed and saved errors cleared.'
    });

    expect(calls).toEqual([
      { path: `/api/v1/admin/telegram/sessions/${sessionId}`, method: 'PATCH', body: { enabled: true } },
      { path: `/api/v1/admin/telegram/sessions/${sessionId}`, method: 'PATCH', body: { status: 'active', clear_error: true } }
    ]);
  });

  it('requires the user-facing disconnect phrase and sends the internal account ID to the backend', async () => {
    const calls: Array<{ path: string; body: unknown }> = [];
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({ path: new URL(String(input)).pathname, body: init?.body ? JSON.parse(String(init.body)) : null });
      return jsonResponse({ action: 'delete', telegram_session_id: sessionId, orphaned_source_channel_count: 2, message: 'internal session wording' });
    }) satisfies ApiFetch;

    await expect(telegramAccountActions.deleteSession(actionEvent({ session_id: sessionId, confirmation: 'DISCONNECT' }, fetch))).resolves.toEqual({
      message: 'Telegram account permanently deleted. 2 sources are now unassigned and ingestion is disabled.'
    });
    await expect(telegramAccountActions.deleteSession(actionEvent({ session_id: sessionId, confirmation: sessionId }, fetch))).resolves.toMatchObject({
      status: 400,
      data: { error: true, message: 'Type DISCONNECT to permanently delete this Telegram account.' }
    });

    expect(calls).toEqual([{ path: `/api/v1/admin/telegram/sessions/${sessionId}`, body: { confirmation: sessionId, note: null } }]);
  });

  it('starts standalone new-account attempts without creating a Telegram session shell', async () => {
    const calls: Array<{ path: string; body: unknown }> = [];
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input)).pathname;
      calls.push({ path, body: init?.body ? JSON.parse(String(init.body)) : null });
      return jsonResponse({ detail: 'Provider start failed.' }, 409);
    }) satisfies ApiFetch;

    await expect(telegramAccountActions.startQrLogin(actionEvent({}, fetch))).resolves.toMatchObject({
      status: 409,
      data: { kind: 'login_error', sessionId: null, method: 'qr', error: true, message: 'Provider start failed.' }
    });
    await expect(telegramAccountActions.startPhoneLogin(actionEvent({ phone_number: '+15551234567' }, fetch))).resolves.toMatchObject({
      status: 409,
      data: { kind: 'login_error', sessionId: null, method: 'phone', error: true, message: 'Provider start failed.' }
    });
    expect(calls).toEqual([
      { path: '/api/v1/admin/telegram/login-attempts/qr', body: { telegram_session_id: null, note: null } },
      { path: '/api/v1/admin/telegram/login-attempts/phone', body: { telegram_session_id: null, phone_number: '+15551234567', note: null } }
    ]);
  });

  it('returns safe context for successful phone-code and 2FA transitions', async () => {
    const fetch = vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input)).pathname;
      if (path.endsWith('/login-attempts/phone')) {
        return jsonResponse({ attempt_id: attemptId, phone_number_hint: 'ending-1234', expires_at: '2026-01-01T00:10:00Z', message: 'sent' });
      }
      return jsonResponse({ password_required: true, message: '2FA required' });
    }) satisfies ApiFetch;

    await expect(
      telegramAccountActions.startPhoneLogin(actionEvent({ session_id: sessionId, phone_number: '+15551234567' }, fetch))
    ).resolves.toMatchObject({
      kind: 'phone_code',
      sessionId,
      attemptId,
      phoneHint: 'ending-1234'
    });
    await expect(
      telegramAccountActions.completePhoneCodeLogin(
        actionEvent({ session_id: sessionId, attempt_id: attemptId, phone_hint: 'ending-1234', code: '12345' }, fetch)
      )
    ).resolves.toMatchObject({
      kind: 'password',
      method: 'phone',
      sessionId,
      attemptId,
      phoneHint: 'ending-1234'
    });
  });

  it('retains safe phone-code and password step context only for retryable credential failures', async () => {
    let codeAttempts = 0;
    let passwordAttempts = 0;
    const retryableFetch = vi.fn(async (input: RequestInfo | URL) => {
      const path = new URL(String(input)).pathname;
      if (path.endsWith('/code')) {
        codeAttempts += 1;
        return codeAttempts === 1
          ? jsonResponse({ detail: 'The Telegram code was incorrect. Try again.' }, 409)
          : jsonResponse({ password_required: false, message: 'Code accepted.' });
      }
      passwordAttempts += 1;
      return passwordAttempts === 1
        ? jsonResponse({ detail: 'The Telegram password was incorrect. Try again.' }, 409)
        : jsonResponse({ message: 'Password accepted.' });
    }) satisfies ApiFetch;

    await expect(
      telegramAccountActions.completePhoneCodeLogin(
        actionEvent({ session_id: sessionId, attempt_id: attemptId, phone_hint: 'ending-1234', code: '12345' }, retryableFetch)
      )
    ).resolves.toMatchObject({
      status: 409,
      data: { kind: 'phone_code', sessionId, attemptId, method: 'phone', phoneHint: 'ending-1234', error: true, message: 'Telegram could not verify that code. Check it and try again.' }
    });
    await expect(
      telegramAccountActions.completePhoneCodeLogin(
        actionEvent({ session_id: sessionId, attempt_id: attemptId, phone_hint: 'ending-1234', code: '12345' }, retryableFetch)
      )
    ).resolves.toEqual({ message: 'Code accepted.' });
    await expect(
      telegramAccountActions.completePhonePasswordLogin(
        actionEvent({ session_id: sessionId, attempt_id: attemptId, method: 'phone', phone_hint: 'ending-1234', password: 'not-rendered' }, retryableFetch)
      )
    ).resolves.toMatchObject({
      status: 409,
      data: { kind: 'password', sessionId, attemptId, method: 'phone', phoneHint: 'ending-1234', error: true, message: 'Telegram could not verify that password. Check it and try again.' }
    });
    await expect(
      telegramAccountActions.completePhonePasswordLogin(
        actionEvent({ session_id: sessionId, attempt_id: attemptId, method: 'phone', phone_hint: 'ending-1234', password: 'not-rendered' }, retryableFetch)
      )
    ).resolves.toEqual({ message: 'Password accepted.' });
  });

  it('maps non-retryable, expired, and malformed credential submissions to restart state', async () => {
    const fetch = vi.fn(async () => jsonResponse({ detail: 'Telegram rejected this step.' }, 409)) satisfies ApiFetch;

    await expect(
      telegramAccountActions.completePhoneCodeLogin(
        actionEvent({ session_id: sessionId, attempt_id: attemptId, phone_hint: 'ending-1234', code: '12345' }, fetch)
      )
    ).resolves.toMatchObject({
      status: 409,
      data: { kind: 'login_error', sessionId, attemptId, method: 'phone', phoneHint: 'ending-1234', error: true, message: 'Telegram rejected this step.' }
    });
    await expect(
      telegramAccountActions.completePhonePasswordLogin(
        actionEvent({ session_id: sessionId, attempt_id: attemptId, method: 'phone', phone_hint: 'ending-1234', password: 'not-rendered' }, fetch)
      )
    ).resolves.toMatchObject({
      status: 409,
      data: { kind: 'login_error', sessionId, attemptId, method: 'phone', phoneHint: 'ending-1234', error: true, message: 'Telegram rejected this step.' }
    });
    await expect(telegramAccountActions.completePhoneCodeLogin(actionEvent({ session_id: sessionId, attempt_id: attemptId }, fetch))).resolves.toMatchObject({
      status: 400,
      data: { kind: 'login_error', sessionId, attemptId, method: 'phone', error: true, message: 'code is required.' }
    });
    await expect(
      telegramAccountActions.completePhonePasswordLogin(actionEvent({ session_id: sessionId, attempt_id: attemptId, password: 'not-rendered' }, fetch))
    ).resolves.toMatchObject({
      status: 400,
      data: { kind: 'login_error', sessionId, attemptId, method: 'phone', error: true, message: 'method must be phone or qr.' }
    });
    expect(fetch).toHaveBeenCalledTimes(2);
  });

  it('cancels a phone or password attempt without requiring an account shell', async () => {
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(new URL(String(input)).pathname).toBe(`/api/v1/admin/telegram/login-attempts/${attemptId}`);
      expect(init?.method).toBe('DELETE');
      return jsonResponse({ attempt_id: attemptId, status: 'cancelled', message: 'cancelled' });
    }) satisfies ApiFetch;

    await expect(
      telegramAccountActions.cancelLoginAttempt(actionEvent({ attempt_id: attemptId }, fetch))
    ).resolves.toEqual({ message: 'Telegram sign-in cancelled.' });
    expect(fetch).toHaveBeenCalledOnce();
  });
});

function actionEvent(values: Record<string, string>, fetch: ApiFetch) {
  const formData = new FormData();
  for (const [name, value] of Object.entries(values)) formData.set(name, value);
  return {
    fetch,
    request: new Request('http://frontend.test/admin/telegram', {
      method: 'POST',
      headers: { cookie: 'memexpert_access_token=token' },
      body: formData
    })
  } as never;
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), { status, headers: { 'content-type': 'application/json' } });
}
