import { env } from '$env/dynamic/private';
import { fail } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import {
  addAdminTelegramChannel,
  ApiError,
  assignAdminTelegramChannel,
  createAdminTelegramSession,
  deleteAdminTelegramSession,
  fetchAdminTelegramChannelGroups,
  fetchAdminTelegramSessions,
  markSourceChannelDead,
  orphanAdminTelegramChannel,
  setSourceChannelPaused,
  updateAdminTelegramChannel,
  updateAdminTelegramSession,
  validateAdminTelegramSession
} from '$lib/api/client';
import type { ApiFetch } from '$lib/api/client';
import type { AdminTelegramChannelGroupRead, AdminTelegramSessionRead, TelegramSessionStatus } from '$lib/api/types';

interface TelegramAdminPagePayload {
  sessions: AdminTelegramSessionRead[];
  groups: AdminTelegramChannelGroupRead[];
}

export const load: PageServerLoad = async ({ fetch, request }) => {
  const api = apiRequest(fetch, request);
  try {
    const [sessions, groups] = await Promise.all([
      fetchAdminTelegramSessions(api),
      fetchAdminTelegramChannelGroups(api)
    ]);

    const telegramAdmin: TelegramAdminPagePayload = { sessions, groups };

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
  return { sessions: [], groups: [] };
}

export const actions: Actions = {
  createSession: async ({ fetch, request }) => {
    const data = await request.formData();
    return runAction(async () => {
      await createAdminTelegramSession({
        ...apiRequest(fetch, request),
        body: {
          name: readRequired(data, 'name'),
          display_name: readOptional(data, 'display_name'),
          string_session: readOptional(data, 'string_session'),
          validate: data.get('validate') === 'on',
          enabled: data.get('enabled') === 'on',
          live_enabled: data.get('live_enabled') === 'on',
          catchup_enabled: data.get('catchup_enabled') === 'on',
          engagement_enabled: data.get('engagement_enabled') === 'on',
          max_requests_per_second: readFloat(data, 'max_requests_per_second', 1),
          account_user_id: readOptionalInt(data, 'account_user_id'),
          account_username: readOptional(data, 'account_username'),
          account_phone_hint: readOptional(data, 'account_phone_hint'),
          note: readOptional(data, 'note')
        }
      });
      return { message: 'Telegram session created. Secret material was not rendered back.' };
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
  },
  addChannel: async ({ fetch, request }) => {
    const data = await request.formData();
    const assignment = readRequired(data, 'assignment');
    const orphaned = assignment === 'orphaned';
    return runAction(async () => {
      await addAdminTelegramChannel({
        ...apiRequest(fetch, request),
        body: {
          platform: 'telegram',
          platform_id: readRequired(data, 'platform_id'),
          username: readOptional(data, 'username'),
          title: readRequired(data, 'title'),
          subscriber_count: readOptionalInt(data, 'subscriber_count'),
          telegram_session_id: orphaned ? null : assignment,
          orphaned,
          catchup_enabled: orphaned ? false : data.get('catchup_enabled') === 'on',
          live_enabled: orphaned ? false : data.get('live_enabled') === 'on',
          engagement_enabled: orphaned ? false : data.get('engagement_enabled') === 'on',
          catchup_message_limit: readInt(data, 'catchup_message_limit', 500)
        }
      });
      return { message: orphaned ? 'Orphaned Telegram channel added as non-indexable.' : 'Telegram channel added and assigned.' };
    });
  },
  updateChannel: async ({ fetch, request }) => {
    const data = await request.formData();
    const channelId = readRequired(data, 'channel_id');
    return runAction(async () => {
      await updateAdminTelegramChannel(
        {
          ...apiRequest(fetch, request),
          body: {
            catchup_enabled: data.get('catchup_enabled') === 'on',
            live_enabled: data.get('live_enabled') === 'on',
            engagement_enabled: data.get('engagement_enabled') === 'on',
            catchup_message_limit: readInt(data, 'catchup_message_limit', 500)
          }
        },
        channelId
      );
      return { message: 'Telegram channel indexing controls updated.' };
    });
  },
  assignChannel: async ({ fetch, request }) => {
    const data = await request.formData();
    const channelId = readRequired(data, 'channel_id');
    return runAction(async () => {
      await assignAdminTelegramChannel(
        {
          ...apiRequest(fetch, request),
          body: {
            telegram_session_id: readRequired(data, 'telegram_session_id'),
            note: readOptional(data, 'note')
          }
        },
        channelId
      );
      return { message: 'Telegram channel assigned to session.' };
    });
  },
  orphanChannel: async ({ fetch, request }) => {
    const data = await request.formData();
    const channelId = readRequired(data, 'channel_id');
    return runAction(async () => {
      await orphanAdminTelegramChannel(
        {
          ...apiRequest(fetch, request),
          body: { note: readOptional(data, 'note') }
        },
        channelId
      );
      return { message: 'Telegram channel orphaned and made non-indexable.' };
    });
  },
  toggleChannel: async ({ fetch, request }) => {
    const data = await request.formData();
    return runAction(async () => {
      await setSourceChannelPaused(
        apiRequest(fetch, request),
        readRequired(data, 'channel_id'),
        readRequired(data, 'paused') === 'true'
      );
      return { message: 'Telegram channel pause state updated.' };
    });
  },
  markChannelDead: async ({ fetch, request }) => {
    const data = await request.formData();
    const channelId = readRequired(data, 'channel_id');
    const confirmation = readRequired(data, 'confirmation');
    return runAction(async () => {
      requireConfirmation(confirmation, channelId, 'Paste the source channel id to mark it dead.');
      await markSourceChannelDead(apiRequest(fetch, request), channelId, confirmation);
      return { message: 'Telegram channel marked dead; crawler checkpoint state was preserved.' };
    });
  }
};

async function runAction(operation: () => Promise<{ message: string }>) {
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

function readInt(data: FormData, name: string, fallback: number): number {
  const raw = String(data.get(name) ?? '').trim();
  if (!raw) {
    return fallback;
  }
  if (!/^\d+$/.test(raw)) {
    throw new Error(`${name} must be a whole number.`);
  }
  return Number(raw);
}

function readOptionalInt(data: FormData, name: string): number | null {
  const raw = String(data.get(name) ?? '').trim();
  if (!raw) {
    return null;
  }
  if (!/^\d+$/.test(raw)) {
    throw new Error(`${name} must be a whole number.`);
  }
  return Number(raw);
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
