import type { PageServerLoad } from './$types';
import { ApiError, fetchProfileStats } from '$lib/api/client';
import { ACCESS_COOKIE_NAME, apiBaseUrl, cookieHeaderWithAccessToken, forwardBackendAccessCookie } from '$lib/server/backend';

export const load: PageServerLoad = async ({ cookies, fetch, parent, request }) => {
  await parent();

  try {
    return {
      profileStats: await fetchProfileStats({
        fetch,
        baseUrl: apiBaseUrl(),
        cookieHeader: cookieHeaderWithAccessToken(
          request.headers.get('cookie') ?? undefined,
          cookies.get(ACCESS_COOKIE_NAME) ?? null
        ),
        onResponse: (response) => {
          forwardBackendAccessCookie(response, cookies);
        }
      }),
      profileStatsError: null
    };
  } catch (error) {
    return {
      profileStats: null,
      profileStatsError: error instanceof ApiError ? error.message : 'Could not reach the profile stats API.'
    };
  }
};
