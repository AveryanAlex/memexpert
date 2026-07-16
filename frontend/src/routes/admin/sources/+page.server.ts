import { env } from '$env/dynamic/private';
import type { Actions, PageServerLoad } from './$types';
import { ApiError, fetchAdminChannelSuggestions, fetchAdminSourceChannels, fetchAdminTelegramSessions } from '$lib/api/client';
import { sourceActions } from '$lib/server/admin/sourceActions';

export const load: PageServerLoad = async ({ depends, fetch, request }) => {
  depends('app:admin-sources');
  const api = {
    fetch,
    baseUrl: env.API_BASE_URL || 'http://localhost:8000',
    cookieHeader: request.headers.get('cookie') ?? undefined
  };

  const [suggestionsResult, sourceChannelsResult, telegramAccountsResult] = await Promise.allSettled([
    fetchAdminChannelSuggestions(api),
    fetchAdminSourceChannels(api),
    fetchAdminTelegramSessions(api)
  ]);
  const sourceAdminErrors = {
    suggestions: loadErrorMessage(suggestionsResult, 'Could not load source suggestions.'),
    sourceChannels: loadErrorMessage(sourceChannelsResult, 'Could not load sources.'),
    telegramAccounts: loadErrorMessage(telegramAccountsResult, 'Could not load Telegram accounts.')
  };
  const errors = Object.values(sourceAdminErrors).filter((message): message is string => message !== null);

  return {
    sourceAdmin: {
      suggestions: settledValue(suggestionsResult, []),
      sourceChannels: settledValue(sourceChannelsResult, []),
      telegramAccounts: settledValue(telegramAccountsResult, [])
    },
    sourceAdminErrors,
    loadError: errors.length ? [...new Set(errors)].join(' ') : null
  };
};

export const actions: Actions = sourceActions;

function settledValue<T>(result: PromiseSettledResult<T>, fallback: T): T {
  return result.status === 'fulfilled' ? result.value : fallback;
}

function loadErrorMessage(result: PromiseSettledResult<unknown>, fallback: string): string | null {
  if (result.status === 'fulfilled') return null;
  return result.reason instanceof ApiError ? result.reason.message : fallback;
}
