import type { PageServerLoad } from './$types';
import { ApiError, fetchMemeLibrary } from '$lib/api/client';
import { ACCESS_COOKIE_NAME, apiBaseUrl, cookieHeaderWithAccessToken, forwardBackendAccessCookie } from '$lib/server/backend';

export const load: PageServerLoad = async ({ cookies, fetch, parent, request }) => {
  await parent();

  try {
    const library = await fetchMemeLibrary({
      fetch,
      baseUrl: apiBaseUrl(),
      cookieHeader: cookieHeaderWithAccessToken(
        request.headers.get('cookie') ?? undefined,
        cookies.get(ACCESS_COOKIE_NAME) ?? null
      ),
      onResponse: (response) => {
        forwardBackendAccessCookie(response, cookies);
      }
    });

    return { library, libraryError: null };
  } catch (error) {
    return {
      library: null,
      libraryError: error instanceof ApiError ? error.message : 'Could not reach the meme library API.'
    };
  }
};
