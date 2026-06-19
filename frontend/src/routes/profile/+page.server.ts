import type { PageServerLoad } from './$types';
import { ApiError, fetchMemeLibrary, fetchProfileStats } from '$lib/api/client';
import { ACCESS_COOKIE_NAME, apiBaseUrl, cookieHeaderWithAccessToken, forwardBackendAccessCookie } from '$lib/server/backend';

export const load: PageServerLoad = async ({ cookies, fetch, parent, request }) => {
  await parent();

  const backendRequest = {
    fetch,
    baseUrl: apiBaseUrl(),
    cookieHeader: cookieHeaderWithAccessToken(
      request.headers.get('cookie') ?? undefined,
      cookies.get(ACCESS_COOKIE_NAME) ?? null
    ),
    onResponse: (response: Response) => {
      forwardBackendAccessCookie(response, cookies);
    }
  };

  const [libraryResult, profileStatsResult] = await Promise.allSettled([
    fetchMemeLibrary(backendRequest),
    fetchProfileStats(backendRequest)
  ]);

  return {
    library: libraryResult.status === 'fulfilled' ? libraryResult.value : null,
    libraryError: libraryResult.status === 'rejected' ? loadErrorMessage(libraryResult.reason, 'Could not reach the meme library API.') : null,
    profileStats: profileStatsResult.status === 'fulfilled' ? profileStatsResult.value : null,
    profileStatsError:
      profileStatsResult.status === 'rejected'
        ? loadErrorMessage(profileStatsResult.reason, 'Could not reach the profile stats API.')
        : null
  };
};

function loadErrorMessage(error: unknown, fallback: string): string {
  return error instanceof ApiError ? error.message : fallback;
}
