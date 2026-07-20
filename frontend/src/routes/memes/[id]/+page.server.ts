import { fail } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import {
  ApiError,
  DEFAULT_PAGE_SIZE,
  emptyMemePage,
  favoriteMeme,
  fetchCurrentSession,
  fetchMemeAnalytics,
  fetchMemePopularitySummary,
  fetchMemeSources,
  fetchSimilarMemes
} from '$lib/api/client';
import type { ApiFetch } from '$lib/api/client';
import type { PublicMemeSearchPageRead } from '$lib/api/types';
import {
  MEME_SOURCE_PAGE_SIZE,
  parseMemeInsightsParams
} from '$lib/features/memes/meme-insights-params';
import { parseMemeAttributionSearchParams } from '$lib/memeActions';
import { apiBaseUrl, cookieHeaderWithAccessToken, forwardBackendAccessCookie } from '$lib/server/backend';

const SIMILAR_LIMIT = DEFAULT_PAGE_SIZE;

export const load: PageServerLoad = async ({ fetch, isDataRequest, parent, request, url }) => {
  const cookieHeader = request.headers.get('cookie') ?? undefined;
  const attribution = parseMemeAttributionSearchParams(url.searchParams);
  const insightsParams = parseMemeInsightsParams(url.searchParams);
  const { meme } = await parent();

  if (!meme) {
    return {
      attribution,
      analytics: null,
      analyticsError: null,
      insightsParams,
      insightsUrl: { pathname: url.pathname, search: url.search },
      popularity: null,
      retainSimilarPage: false,
      similarPage: emptyMemePage(SIMILAR_LIMIT, 0),
      similarErrorMessage: null,
      sourceError: null,
      sourcePage: null
    };
  }

  const retainSimilarPage = canRetainSimilarPage({ isDataRequest, request, url });
  const similarRequest = retainSimilarPage
    ? Promise.resolve({ page: emptyMemePage(SIMILAR_LIMIT, 0), errorMessage: null })
    : fetchInitialSimilarPage(fetch, cookieHeader, meme.id);
  const [popularity, similar, sourceResult, analyticsResult] = await Promise.all([
    fetchMemePopularitySummary({
      fetch,
      baseUrl: apiBaseUrl(),
      memeId: meme.id,
      cookieHeader
    }).catch(() => null),
    similarRequest,
    settleInsight(
      fetchMemeSources({
        fetch,
        baseUrl: apiBaseUrl(),
        memeId: meme.id,
        cookieHeader,
        sort: insightsParams.sourceSort,
        limit: MEME_SOURCE_PAGE_SIZE,
        offset: insightsParams.sourceOffset,
        snapshotAt: insightsParams.sourceSnapshot
      }),
      'Telegram source posts could not be loaded right now.'
    ),
    settleInsight(
      fetchMemeAnalytics({
        fetch,
        baseUrl: apiBaseUrl(),
        memeId: meme.id,
        cookieHeader,
        window: insightsParams.analyticsWindow
      }),
      'Professional activity analytics could not be loaded right now.'
    )
  ]);

  return {
    attribution,
    analytics: analyticsResult.data,
    analyticsError: analyticsResult.error,
    insightsParams,
    insightsUrl: { pathname: url.pathname, search: url.search },
    popularity,
    retainSimilarPage,
    similarPage: similar.page,
    similarErrorMessage: similar.errorMessage,
    sourceError: sourceResult.error,
    sourcePage: sourceResult.data
  };
};

async function settleInsight<T>(promise: Promise<T>, message: string): Promise<{ data: T | null; error: string | null }> {
  try {
    return { data: await promise, error: null };
  } catch {
    return { data: null, error: message };
  }
}

function canRetainSimilarPage({
  isDataRequest,
  request,
  url
}: {
  isDataRequest: boolean;
  request: Request;
  url: URL;
}): boolean {
  if (!isDataRequest) return false;
  const referer = request.headers.get('referer');
  if (!referer) return false;

  try {
    const previousUrl = new URL(referer);
    return (
      previousUrl.origin === url.origin &&
      previousUrl.pathname === url.pathname &&
      previousUrl.search !== url.search
    );
  } catch {
    return false;
  }
}

async function fetchInitialSimilarPage(
  fetch: ApiFetch,
  cookieHeader: string | undefined,
  memeId: string
): Promise<{ page: PublicMemeSearchPageRead; errorMessage: string | null }> {
  try {
    const page = await fetchSimilarMemes({
      fetch,
      baseUrl: apiBaseUrl(),
      memeId,
      limit: SIMILAR_LIMIT,
      offset: 0,
      cookieHeader
    });
    return { page, errorMessage: null };
  } catch {
    return {
      page: emptyMemePage(SIMILAR_LIMIT, 0),
      errorMessage: 'Could not load similar memes. Try again.'
    };
  }
}

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
