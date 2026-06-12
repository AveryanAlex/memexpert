import { fail, redirect } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import { ApiError, favoriteMeme, fetchCurrentSession, fetchMemeDetail, fetchMemePopularitySummary } from '$lib/api/client';
import { apiBaseUrl, cookieHeaderWithAccessToken, forwardBackendAccessCookie } from '$lib/server/backend';

export const load: PageServerLoad = async ({ fetch, params, request }) => {
  let meme: Awaited<ReturnType<typeof fetchMemeDetail>>;
  try {
    meme = await fetchMemeDetail({
      fetch,
      baseUrl: apiBaseUrl(),
      memeId: params.id,
      cookieHeader: request.headers.get('cookie') ?? undefined
    });
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      return {
        meme: null,
        popularity: null,
        unavailableMessage: 'This meme is not available. It may be private, removed, or filtered by safety settings.'
      };
    }

    if (error instanceof ApiError && error.status === 422) {
      return {
        meme: null,
        popularity: null,
        unavailableMessage: 'This meme link is invalid.'
      };
    }

    return {
      meme: null,
      popularity: null,
      unavailableMessage: 'Could not reach the meme catalog API.'
    };
  }

  const popularity = await fetchMemePopularitySummary({
    fetch,
    baseUrl: apiBaseUrl(),
    memeId: meme.id,
    cookieHeader: request.headers.get('cookie') ?? undefined
  }).catch(() => null);

  if (meme.seo_page_slug && params.id !== meme.seo_page_slug) {
    throw redirect(308, `/memes/${meme.seo_page_slug}`);
  }

  return { meme, popularity, unavailableMessage: null };
};

export const actions: Actions = {
  favorite: async ({ cookies, fetch, request }) => {
    let issuedAccessToken: string | null = null;
    const requestCookieHeader = request.headers.get('cookie') ?? undefined;
    const formData = await request.formData();
    const memeId = String(formData.get('memeId') ?? '');

    if (!memeId) {
      return fail(400, { status: 'error', message: 'Could not identify this meme.' });
    }

    try {
      await favoriteMeme({
        fetch,
        baseUrl: apiBaseUrl(),
        memeId,
        cookieHeader: requestCookieHeader,
        onResponse: (response) => {
          issuedAccessToken = forwardBackendAccessCookie(response, cookies);
        }
      });

      const session = await fetchCurrentSession({
        fetch,
        baseUrl: apiBaseUrl(),
        cookieHeader: cookieHeaderWithAccessToken(requestCookieHeader, issuedAccessToken)
      });

      return {
        status: 'saved',
        message: 'Saved to favorites.',
        showConnectTelegram: session.user.account_type === 'guest'
      };
    } catch (error) {
      return fail(400, {
        status: 'error',
        message: error instanceof ApiError ? error.message : 'Could not save this meme yet.'
      });
    }
  }
};
