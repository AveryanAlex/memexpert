import { env } from '$env/dynamic/private';
import type { Actions, PageServerLoad } from './$types';
import { ApiError, fetchAdminChannelSuggestions, fetchAdminSourceChannels, fetchAdminTelegramSessions } from '$lib/api/client';
import { sourceActions } from '$lib/server/admin/sourceActions';

export const load: PageServerLoad = async ({ fetch, request }) => {
  const api = {
    fetch,
    baseUrl: env.API_BASE_URL || 'http://localhost:8000',
    cookieHeader: request.headers.get('cookie') ?? undefined
  };

  try {
    const [suggestions, sourceChannels, telegramAccounts] = await Promise.all([
      fetchAdminChannelSuggestions(api),
      fetchAdminSourceChannels(api),
      fetchAdminTelegramSessions(api)
    ]);
    return { sourceAdmin: { suggestions, sourceChannels, telegramAccounts }, loadError: null };
  } catch (caught) {
    return {
      sourceAdmin: { suggestions: [], sourceChannels: [], telegramAccounts: [] },
      loadError: caught instanceof ApiError ? caught.message : 'Could not load source management.'
    };
  }
};

export const actions: Actions = sourceActions;
