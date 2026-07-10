import { env } from '$env/dynamic/private';
import { fail } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import {
  ApiError,
  completeAdminTelegramPhoneCodeLogin,
  completeAdminTelegramPhonePasswordLogin,
  completeAdminTelegramQrLogin,
  createAdminTelegramSession,
  deleteAdminTelegramSession,
  fetchAdminSourceChannels,
  fetchAdminTelegramSessions,
  startAdminTelegramPhoneLogin,
  startAdminTelegramQrLogin,
  updateAdminTelegramSession,
  validateAdminTelegramSession
} from '$lib/api/client';
import type { ApiFetch } from '$lib/api/client';
import type { AdminTelegramSessionRead, TelegramSessionStatus } from '$lib/api/types';

interface TelegramAdminPagePayload {
  sessions: AdminTelegramSessionRead[];
  sourceCount: number;
}

export const load: PageServerLoad = async ({ fetch, request }) => {
  const api = apiRequest(fetch, request);
  try {
    const [sessions, sourceChannels] = await Promise.all([
      fetchAdminTelegramSessions(api),
      fetchAdminSourceChannels(api)
    ]);

    const telegramAdmin: TelegramAdminPagePayload = { sessions, sourceCount: sourceChannels.length };

    return {
      telegramAdmin,
      loadError: null
    };
  } catch (caught) {
    return {
      telegramAdmin: emptyTelegramAdmin(),
      loadError: caught instanceof ApiError ? caught.message : 'Could not load Telegram admin tools.'
    };
  }
};

function emptyTelegramAdmin(): TelegramAdminPagePayload {
  return { sessions: [], sourceCount: 0 };
}

export const actions: Actions = {
  createSession: async ({ fetch, request }) => {
    const data = await request.formData();
    return runAction(async () => {
      await createAdminTelegramSession({
        ...apiRequest(fetch, request),
        body: telegramSessionCreateBody(data)
      });
      return { message: 'Telegram login slot created. Start QR or phone login from the session card.' };
    });
  },
  updateSession: async ({ fetch, request }) => {
    const data = await request.formData();
    const sessionId = readRequired(data, 'session_id');
    return runAction(async () => {
      await updateAdminTelegramSession(
        {
          ...apiRequest(fetch, request),
          body: {
            display_name: readRequired(data, 'display_name'),
            enabled: data.get('enabled') === 'on',
            status: readRequired(data, 'status') as TelegramSessionStatus,
            live_enabled: data.get('live_enabled') === 'on',
            catchup_enabled: data.get('catchup_enabled') === 'on',
            engagement_enabled: data.get('engagement_enabled') === 'on',
            max_requests_per_second: readFloat(data, 'max_requests_per_second', 1),
            flood_wait_until: readOptional(data, 'flood_wait_until'),
            last_error_class: readOptional(data, 'last_error_class'),
            last_error_text: readOptional(data, 'last_error_text'),
            clear_error: data.get('clear_error') === 'on',
            note: readOptional(data, 'note')
          }
        },
        sessionId
      );
      return { message: 'Telegram session policy updated.' };
    });
  },
  startQrLogin: async ({ fetch, request }) => {
    const data = await request.formData();
    return runAction(async () => {
      const api = apiRequest(fetch, request);
      const sessionId = await ensureLoginSessionId(api, data);
      const result = await startAdminTelegramQrLogin(api, sessionId);
      return {
        message: 'Waiting for scan…',
        kind: 'qr' as const,
        sessionId,
        attemptId: result.attempt_id,
        qrUrl: result.qr_url,
        expiresAt: result.expires_at
      };
    });
  },
  completeQrLogin: async ({ fetch, request }) => {
    const data = await request.formData();
    const sessionId = readRequired(data, 'session_id');
    const attemptId = readRequired(data, 'attempt_id');
    return runAction(async () => {
      const result = await completeAdminTelegramQrLogin(
        {
          ...apiRequest(fetch, request),
          body: { attempt_id: attemptId, note: readOptional(data, 'note') }
        },
        sessionId
      );
      if (result.status === 'password_required') {
        return {
          message: 'Telegram requires the account password. Enter it to finish.',
          kind: 'password' as const,
          method: 'qr' as const,
          sessionId,
          attemptId
        };
      }
      if (result.status === 'pending') {
        return { message: result.message };
      }
      return { message: result.message };
    });
  },
  startPhoneLogin: async ({ fetch, request }) => {
    const data = await request.formData();
    return runAction(async () => {
      const api = apiRequest(fetch, request);
      const sessionId = await ensureLoginSessionId(api, data);
      const result = await startAdminTelegramPhoneLogin(
        {
          ...api,
          body: { phone_number: readRequired(data, 'phone_number'), note: readOptional(data, 'note') }
        },
        sessionId
      );
      return {
        message: `Code sent to phone ${result.phone_number_hint ?? 'account'}. Enter the code from Telegram.`,
        kind: 'phone_code' as const,
        sessionId,
        attemptId: result.attempt_id,
        phoneHint: result.phone_number_hint,
        expiresAt: result.expires_at
      };
    });
  },
  completePhoneCodeLogin: async ({ fetch, request }) => {
    const data = await request.formData();
    const sessionId = readRequired(data, 'session_id');
    const attemptId = readRequired(data, 'attempt_id');
    return runAction(async () => {
      const result = await completeAdminTelegramPhoneCodeLogin(
        {
          ...apiRequest(fetch, request),
          body: { attempt_id: attemptId, code: readRequired(data, 'code'), note: readOptional(data, 'note') }
        },
        sessionId
      );
      if (result.password_required) {
        return {
          message: 'Telegram requires the account password. Enter it to finish.',
          kind: 'password' as const,
          method: 'phone' as const,
          sessionId,
          attemptId
        };
      }
      return { message: result.message };
    });
  },
  completePhonePasswordLogin: async ({ fetch, request }) => {
    const data = await request.formData();
    const sessionId = readRequired(data, 'session_id');
    return runAction(async () => {
      const result = await completeAdminTelegramPhonePasswordLogin(
        {
          ...apiRequest(fetch, request),
          body: {
            attempt_id: readRequired(data, 'attempt_id'),
            password: readRequired(data, 'password'),
            note: readOptional(data, 'note')
          }
        },
        sessionId
      );
      return { message: result.message };
    });
  },
  validateSession: async ({ fetch, request }) => {
    const data = await request.formData();
    const sessionId = readRequired(data, 'session_id');
    return runAction(async () => {
      const result = await validateAdminTelegramSession(
        {
          ...apiRequest(fetch, request),
          body: {
            source_channel_id: readOptional(data, 'source_channel_id'),
            note: readOptional(data, 'note')
          }
        },
        sessionId
      );
      return {
        message: result.channel_checked
          ? `Telegram session validated with ${result.channel_reference ?? 'selected channel'}.`
          : 'Telegram session validated.'
      };
    });
  },
  deleteSession: async ({ fetch, request }) => {
    const data = await request.formData();
    const sessionId = readRequired(data, 'session_id');
    const confirmation = readRequired(data, 'confirmation');
    return runAction(async () => {
      requireConfirmation(confirmation, sessionId, 'Paste the Telegram session id to delete it.');
      const result = await deleteAdminTelegramSession(
        {
          ...apiRequest(fetch, request),
          body: { confirmation, note: readOptional(data, 'note') }
        },
        sessionId
      );
      return { message: result.message };
    });
  }
};

async function ensureLoginSessionId(api: ReturnType<typeof apiRequest>, data: FormData): Promise<string> {
  const existingSessionId = readOptional(data, 'session_id');
  if (existingSessionId) {
    return existingSessionId;
  }
  const created = await createAdminTelegramSession({
    ...api,
    body: telegramSessionCreateBody(data)
  });
  return created.id;
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

function readCheckbox(data: FormData, name: string, fallback: boolean): boolean {
  return data.has(name) ? data.get(name) === 'on' : fallback;
}

async function runAction<T extends { message: string }>(operation: () => Promise<T>) {
  try {
    return await operation();
  } catch (caught) {
    if (caught instanceof ApiError) {
      return fail(caught.status, { message: caught.message, error: true });
    }
    if (caught instanceof Error) {
      return fail(400, { message: caught.message, error: true });
    }
    return fail(500, { message: 'Telegram admin operation failed.', error: true });
  }
}

function apiRequest(fetch: ApiFetch, request: Request) {
  return {
    fetch,
    baseUrl: apiBaseUrl(),
    cookieHeader: request.headers.get('cookie') ?? undefined
  };
}

function readRequired(data: FormData, name: string): string {
  const value = String(data.get(name) ?? '').trim();
  if (!value) {
    throw new ApiError(400, `${name} is required.`);
  }
  return value;
}

function readOptional(data: FormData, name: string): string | null {
  const value = String(data.get(name) ?? '').trim();
  return value || null;
}

function readFloat(data: FormData, name: string, fallback: number): number {
  const raw = String(data.get(name) ?? '').trim();
  if (!raw) {
    return fallback;
  }
  const value = Number(raw);
  if (!Number.isFinite(value) || value <= 0) {
    throw new Error(`${name} must be a positive number.`);
  }
  return value;
}

function requireConfirmation(actual: string, expected: string, message: string): void {
  if (actual !== expected) {
    throw new ApiError(400, message);
  }
}

function apiBaseUrl(): string {
  return env.API_BASE_URL || 'http://localhost:8000';
}
