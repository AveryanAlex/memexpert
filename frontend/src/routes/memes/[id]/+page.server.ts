import { fail, redirect } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import {
  ApiError,
  favoriteMeme,
  fetchCurrentSession,
  fetchMemeDetail,
  fetchMemePopularitySummary,
  fetchSimilarMemes,
  fetchTagLanding,
  fetchTrendPage
} from '$lib/api/client';
import type { ApiFetch } from '$lib/api/client';
import type { PublicMemeDetailRead } from '$lib/api/types';
import type { MemeDetailRelatedSource } from '$lib/meme-detail-view';
import { parseMemeAttributionSearchParams } from '$lib/memeActions';
import { apiBaseUrl, cookieHeaderWithAccessToken, forwardBackendAccessCookie } from '$lib/server/backend';

const RELATED_LIMIT = 7;

export const load: PageServerLoad = async ({ fetch, params, request, url }) => {
  const cookieHeader = request.headers.get('cookie') ?? undefined;
  const attribution = parseMemeAttributionSearchParams(url.searchParams);
  let meme: Awaited<ReturnType<typeof fetchMemeDetail>>;
  try {
    meme = await fetchMemeDetail({
      fetch,
      baseUrl: apiBaseUrl(),
      memeId: params.id,
      attribution,
      cookieHeader
    });
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      return {
        attribution,
        meme: null,
        popularity: null,
        relatedSource: null,
        unavailableMessage: 'This meme is not available. It may be private, removed, or filtered by safety settings.'
      };
    }

    if (error instanceof ApiError && error.status === 422) {
      return {
        attribution,
        meme: null,
        popularity: null,
        relatedSource: null,
        unavailableMessage: 'This meme link is invalid.'
      };
    }

    return {
      attribution,
      meme: null,
      popularity: null,
      relatedSource: null,
      unavailableMessage: 'Could not reach the meme catalog API.'
    };
  }

  if (meme.seo_page_slug && params.id !== meme.seo_page_slug) {
    throw redirect(308, `/memes/${meme.seo_page_slug}${url.search}`);
  }

  const [popularity, relatedSource] = await Promise.all([
    fetchMemePopularitySummary({
      fetch,
      baseUrl: apiBaseUrl(),
      memeId: meme.id,
      cookieHeader
    }).catch(() => null),
    fetchRelatedDiscoverySource(fetch, cookieHeader, meme)
  ]);

  return { attribution, meme, popularity, relatedSource, unavailableMessage: null };
};

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
