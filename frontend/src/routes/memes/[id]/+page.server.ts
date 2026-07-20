import { fail } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import {
  ApiError,
  favoriteMeme,
  fetchCurrentSession,
  fetchMemeAnalytics,
  fetchMemePopularitySummary,
  fetchMemeSources,
  fetchSimilarMemes,
  fetchTagLanding,
  fetchTrendPage
} from '$lib/api/client';
import type { ApiFetch } from '$lib/api/client';
import type { PublicMemeDetailRead } from '$lib/api/types';
import type { MemeDetailRelatedSource } from '$lib/meme-detail-view';
import {
  MEME_SOURCE_PAGE_SIZE,
  parseMemeInsightsParams
} from '$lib/features/memes/meme-insights-params';
import { parseMemeAttributionSearchParams } from '$lib/memeActions';
import { apiBaseUrl, cookieHeaderWithAccessToken, forwardBackendAccessCookie } from '$lib/server/backend';

const RELATED_LIMIT = 7;

export const load: PageServerLoad = async ({ fetch, parent, request, url }) => {
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
      relatedSource: null,
      sourceError: null,
      sourcePage: null
    };
  }

  const [popularity, relatedSource, sourceResult, analyticsResult] = await Promise.all([
    fetchMemePopularitySummary({
      fetch,
      baseUrl: apiBaseUrl(),
      memeId: meme.id,
      cookieHeader
    }).catch(() => null),
    fetchRelatedDiscoverySource(fetch, cookieHeader, meme),
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
    relatedSource,
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

async function fetchRelatedDiscoverySource(
  fetch: ApiFetch,
  cookieHeader: string | undefined,
  meme: PublicMemeDetailRead
): Promise<MemeDetailRelatedSource> {
  try {
    const page = await fetchSimilarMemes({
      fetch,
      baseUrl: apiBaseUrl(),
      memeId: meme.id,
      limit: RELATED_LIMIT,
      offset: 0,
      cookieHeader
    });
    return { kind: 'similar', page };
  } catch {
    // Fall back only when the canonical similar endpoint is unavailable.
  }

  const firstTag = meme.tags[0]?.trim();

  if (firstTag) {
    try {
      const landing = await fetchTagLanding({
        fetch,
        baseUrl: apiBaseUrl(),
        slug: firstTag,
        limit: RELATED_LIMIT,
        offset: 0,
        cookieHeader
      });
      return { kind: 'tag', tag: firstTag, items: landing.page.items };
    } catch {
      // Fall through to public trends so tag API issues do not break detail pages.
    }
  }

  try {
    const trends = await fetchTrendPage({
      fetch,
      baseUrl: apiBaseUrl(),
      ranking: 'trending',
      limit: RELATED_LIMIT,
      offset: 0,
      cookieHeader
    });
    return { kind: 'trending', items: trends.items.map((item) => ({ meme: item.meme, attribution: item.attribution })) };
  } catch {
    return null;
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
