import { fail } from '@sveltejs/kit';
import type { RequestEvent } from '@sveltejs/kit';
import {
  ApiError,
  cancelAdminTelegramLoginAttempt,
  completeAdminTelegramPhoneCodeLogin,
  completeAdminTelegramPhonePasswordLogin,
  createAdminTelegramSession,
  deleteAdminTelegramSession,
  startAdminTelegramPhoneLogin,
  startAdminTelegramQrLogin,
  updateAdminTelegramSession,
  validateAdminTelegramSession
} from '$lib/api/client';
import type { TelegramSessionStatus } from '$lib/api/types';
import { apiRequest, readOptional, readRequired, runAction } from './actionUtils';

type LoginMethod = 'phone' | 'qr';

const RETRYABLE_PHONE_CODE_MESSAGE = 'The Telegram code was incorrect. Try again.';
const RETRYABLE_PASSWORD_MESSAGE = 'The Telegram password was incorrect. Try again.';

interface LoginFailureContext {
  sessionId?: string | null;
  attemptId?: string;
  method: LoginMethod;
  phoneHint?: string | null;
}

export async function createSession({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  return runAction(async () => {
    await createAdminTelegramSession({
      ...apiRequest(fetch, request),
      body: telegramSessionCreateBody(data)
    });
    return { message: 'Telegram account created. Connect it with QR or use the phone alternative.' };
  });
}

export async function updateSession({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  return runAction(async () => {
    const body = {
      display_name: readRequired(data, 'display_name'),
      enabled: data.get('enabled') === 'on',
      status: readRequired(data, 'status') as TelegramSessionStatus,
      live_enabled: data.get('live_enabled') === 'on',
      catchup_enabled: data.get('catchup_enabled') === 'on',
      engagement_enabled: data.get('engagement_enabled') === 'on',
      max_requests_per_second: readFloat(data, 'max_requests_per_second', 1),
      flood_wait_until: readOptional(data, 'flood_wait_until'),
      last_error_class: readOptional(data, 'last_error_class'),
      clear_error: data.get('clear_error') === 'on',
      note: readOptional(data, 'note')
    };
    await updateAdminTelegramSession({ ...apiRequest(fetch, request), body }, readRequired(data, 'session_id'));
    return { message: 'Telegram account settings updated.' };
  });
}

export async function repairSession({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  return runAction(async () => {
    const repair = readRequired(data, 'repair');
    const body = repair === 'enable'
      ? { enabled: true }
      : repair === 'resume'
        ? { status: 'active' as const, clear_error: true }
        : null;
    if (!body) throw new ApiError(400, 'Unknown Telegram account repair action.');
    await updateAdminTelegramSession({ ...apiRequest(fetch, request), body }, readRequired(data, 'session_id'));
    return {
      message: repair === 'enable'
        ? 'Telegram account enabled.'
        : 'Telegram account resumed and saved errors cleared.'
    };
  });
}

export async function startQrLogin({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  const sessionId = readOptional(data, 'session_id');
  try {
    const api = apiRequest(fetch, request);
    const result = await startAdminTelegramQrLogin({
      ...api,
      body: { telegram_session_id: sessionId, note: readOptional(data, 'note') }
    });
    return {
      message: 'Waiting for scan…',
      kind: 'qr' as const,
      sessionId,
      attemptId: result.attempt_id,
      qrUrl: result.qr_url,
      expiresAt: result.expires_at
    };
  } catch (caught) {
    return loginFailure(caught, { sessionId, method: 'qr' });
  }
}

export async function startPhoneLogin({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  const sessionId = readOptional(data, 'session_id');
  try {
    const phoneNumber = readRequired(data, 'phone_number');
    const api = apiRequest(fetch, request);
    const result = await startAdminTelegramPhoneLogin(
      {
        ...api,
        body: { telegram_session_id: sessionId, phone_number: phoneNumber, note: readOptional(data, 'note') }
      }
    );
    return {
      message: 'Telegram sent a verification code. Enter it to continue.',
      kind: 'phone_code' as const,
      method: 'phone' as const,
      sessionId,
      attemptId: result.attempt_id,
      phoneHint: result.phone_number_hint,
      expiresAt: result.expires_at,
      error: false
    };
  } catch (caught) {
    return loginFailure(caught, { sessionId, method: 'phone' });
  }
}

export async function completePhoneCodeLogin({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  const context = loginContext(data, 'phone');
  try {
    const attemptId = readRequired(data, 'attempt_id');
    const result = await completeAdminTelegramPhoneCodeLogin(
      {
        ...apiRequest(fetch, request),
        body: { code: readRequired(data, 'code'), note: readOptional(data, 'note') }
      },
      attemptId
    );
    if (result.password_required) {
      return {
        message: 'Telegram requires the account password. Enter it to finish.',
        kind: 'password' as const,
        method: 'phone' as const,
        sessionId: context.sessionId ?? null,
        attemptId,
        phoneHint: context.phoneHint,
        error: false
      };
    }
    return { message: result.message };
  } catch (caught) {
    return isRetryableCredentialFailure(caught, RETRYABLE_PHONE_CODE_MESSAGE)
      ? phoneCodeFailure(caught, context)
      : loginFailure(caught, context);
  }
}

export async function completePhonePasswordLogin({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  const requestedMethod = readOptional(data, 'method');
  const method = requestedMethod === 'qr' ? 'qr' : 'phone';
  const context = loginContext(data, method);
  try {
    if (requestedMethod !== 'phone' && requestedMethod !== 'qr') {
      throw new ApiError(400, 'method must be phone or qr.');
    }
    const attemptId = readRequired(data, 'attempt_id');
    const result = await completeAdminTelegramPhonePasswordLogin(
      {
        ...apiRequest(fetch, request),
        body: { password: readRequired(data, 'password'), note: readOptional(data, 'note') }
      },
      attemptId
    );
    return { message: result.message };
  } catch (caught) {
    return isRetryableCredentialFailure(caught, RETRYABLE_PASSWORD_MESSAGE)
      ? passwordFailure(caught, context)
      : loginFailure(caught, context);
  }
}

export async function cancelLoginAttempt({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  return runAction(async () => {
    await cancelAdminTelegramLoginAttempt(
      apiRequest(fetch, request),
      readRequired(data, 'attempt_id')
    );
    return { message: 'Telegram sign-in cancelled.' };
  });
}

export async function validateSession({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  return runAction(async () => {
    const result = await validateAdminTelegramSession(
      {
        ...apiRequest(fetch, request),
        body: { source_channel_id: readOptional(data, 'source_channel_id'), note: readOptional(data, 'note') }
      },
      readRequired(data, 'session_id')
    );
    return {
      message: result.channel_checked
        ? `Telegram account validated with ${result.channel_reference ?? 'selected source'}.`
        : 'Telegram account validated.'
    };
  });
}

export async function deleteSession({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  return runAction(async () => {
    const sessionId = readRequired(data, 'session_id');
    if (readRequired(data, 'confirmation') !== 'DISCONNECT') {
      throw new ApiError(400, 'Type DISCONNECT to permanently delete this Telegram account.');
    }
    const result = await deleteAdminTelegramSession(
      { ...apiRequest(fetch, request), body: { confirmation: sessionId, note: readOptional(data, 'note') } },
      sessionId
    );
    const sourceCount = result.orphaned_source_channel_count;
    return {
      message: `Telegram account permanently deleted. ${sourceCount} ${sourceCount === 1 ? 'source is' : 'sources are'} now unassigned and ingestion is disabled.`
    };
  });
}

function telegramSessionCreateBody(data: FormData) {
  return {
    name: readOptional(data, 'name'),
    display_name: readOptional(data, 'display_name'),
    enabled: readCheckbox(data, 'enabled', true),
    live_enabled: readCheckbox(data, 'live_enabled', true),
    catchup_enabled: readCheckbox(data, 'catchup_enabled', true),
    engagement_enabled: readCheckbox(data, 'engagement_enabled', true),
    max_requests_per_second: readFloat(data, 'max_requests_per_second', 1),
    note: readOptional(data, 'note')
  };
}

function loginContext(data: FormData, method: LoginMethod): LoginFailureContext {
  return {
    sessionId: readOptional(data, 'session_id'),
    attemptId: readOptional(data, 'attempt_id') ?? undefined,
    method,
    phoneHint: method === 'phone' ? readOptional(data, 'phone_hint') : undefined
  };
}

function loginFailure(caught: unknown, context: LoginFailureContext) {
  const status = caught instanceof ApiError ? caught.status : 400;
  const message = caught instanceof Error ? caught.message : 'Telegram sign-in could not continue.';
  return fail(status, {
    message,
    error: true,
    kind: 'login_error' as const,
    ...context
  });
}

function phoneCodeFailure(caught: unknown, context: LoginFailureContext) {
  return fail(actionFailureStatus(caught), {
    message: 'Telegram could not verify that code. Check it and try again.',
    error: true,
    kind: 'phone_code' as const,
    ...context
  });
}

function passwordFailure(caught: unknown, context: LoginFailureContext) {
  return fail(actionFailureStatus(caught), {
    message: 'Telegram could not verify that password. Check it and try again.',
    error: true,
    kind: 'password' as const,
    ...context
  });
}

function actionFailureStatus(caught: unknown): number {
  return caught instanceof ApiError ? caught.status : 400;
}

function isRetryableCredentialFailure(caught: unknown, expectedMessage: string): boolean {
  return caught instanceof ApiError && caught.message === expectedMessage;
}

function readCheckbox(data: FormData, name: string, fallback: boolean): boolean {
  return data.has(name) ? data.get(name) === 'on' : fallback;
}

function readFloat(data: FormData, name: string, fallback: number): number {
  const raw = String(data.get(name) ?? '').trim();
  if (!raw) return fallback;
  const value = Number(raw);
  if (!Number.isFinite(value) || value <= 0) throw new Error(`${name} must be a positive number.`);
  return value;
}

export const telegramAccountActions = {
  createSession,
  updateSession,
  repairSession,
  startQrLogin,
  startPhoneLogin,
  completePhoneCodeLogin,
  completePhonePasswordLogin,
  cancelLoginAttempt,
  validateSession,
  deleteSession
};
