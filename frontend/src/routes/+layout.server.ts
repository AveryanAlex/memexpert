import type { LayoutServerLoad } from './$types';
import { ApiError, fetchCurrentSession } from '$lib/api/client';
import { apiBaseUrl, forwardBackendAccessCookie } from '$lib/server/backend';

export const load: LayoutServerLoad = async ({ cookies, fetch, request }) => {
  try {
    const session = await fetchCurrentSession({
      fetch,
      baseUrl: apiBaseUrl(),
      cookieHeader: request.headers.get('cookie') ?? undefined,
      onResponse: (response) => {
        forwardBackendAccessCookie(response, cookies);
      }
    });

    return { session, sessionError: null };
  } catch (error) {
    return {
      session: null,
      sessionError: error instanceof ApiError ? error.message : 'Could not reach the account session API.'
    };
  }
};
