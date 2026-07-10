import type { Actions, PageServerLoad } from './$types';
import { ApiError, fetchAdminTelegramSessions } from '$lib/api/client';
import type { AdminTelegramSessionRead } from '$lib/api/types';
import { apiRequest } from '$lib/server/admin/actionUtils';
import { telegramAccountActions } from '$lib/server/admin/telegramActions';

interface TelegramAdminPagePayload {
  sessions: AdminTelegramSessionRead[];
}

export const load: PageServerLoad = async ({ fetch, request }) => {
  const api = apiRequest(fetch, request);
  try {
    const sessions = await fetchAdminTelegramSessions(api);
    const telegramAdmin: TelegramAdminPagePayload = { sessions };

    return {
      telegramAdmin,
      loadedAt: new Date().toISOString(),
      loadError: null
    };
  } catch (caught) {
    return {
      telegramAdmin: emptyTelegramAdmin(),
      loadedAt: new Date().toISOString(),
      loadError: caught instanceof ApiError ? caught.message : 'Could not load Telegram accounts.'
    };
  }
};

function emptyTelegramAdmin(): TelegramAdminPagePayload {
  return { sessions: [] };
}

export const actions: Actions = telegramAccountActions;
