import type { LayoutServerLoad } from './$types';
import { ApiError, fetchCurrentSession } from '$lib/api/client';
import { apiBaseUrl, forwardBackendAccessCookie } from '$lib/server/backend';
import { fetchCachedSeoSummary } from '$lib/server/seoSummary';

export const load: LayoutServerLoad = async ({ cookies, fetch, request }) => {
  const backendBaseUrl = apiBaseUrl();
  const sessionStatePromise = fetchCurrentSession({
    fetch,
    baseUrl: backendBaseUrl,
    cookieHeader: request.headers.get('cookie') ?? undefined,
    onResponse: (response) => {
      forwardBackendAccessCookie(response, cookies);
    }
  })
    .then((session) => ({ session, sessionError: null }))
    .catch((error: unknown) => ({
      session: null,
      sessionError: error instanceof ApiError ? error.message : 'Could not reach the account session API.'
    }));
  const searchMemeCountStatePromise: Promise<{ searchMemeCount?: number }> = fetchCachedSeoSummary(fetch, backendBaseUrl)
    .then((summary) => ({ searchMemeCount: summary.public_safe_meme_count }))
    .catch(() => ({}));
  const [sessionState, searchMemeCountState] = await Promise.all([sessionStatePromise, searchMemeCountStatePromise]);

  return { ...sessionState, ...searchMemeCountState };
};
