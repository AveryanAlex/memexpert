import type { PageServerLoad } from './$types';
import { DEFAULT_PAGE_SIZE, ApiError, emptyMemePage, fetchCollections, fetchMemePage } from '$lib/api/client';
import { ACCESS_COOKIE_NAME, apiBaseUrl, cookieHeaderWithAccessToken, forwardBackendAccessCookie } from '$lib/server/backend';
import { canonicalPublicOrigin } from '$lib/server/canonicalOrigin';
import { parseSearchParams } from '$lib/searchParams';

export const load: PageServerLoad = async ({ cookies, fetch, parent, request, url }) => {
  const filters = parseSearchParams(url.searchParams);
  const { session } = await parent();
  const cookieHeader = cookieHeaderWithAccessToken(
    request.headers.get('cookie') ?? undefined,
    cookies.get(ACCESS_COOKIE_NAME) ?? null
  );
  const seo = {
    canonicalUrl: `${canonicalPublicOrigin()}/search`,
    noindex: Boolean(filters.query)
  };

  try {
    const [page, collections] = await Promise.all([
      fetchMemePage({
        fetch,
        baseUrl: apiBaseUrl(),
        query: filters.query,
        tags: filters.tags,
        includeNsfw: filters.includeNsfw,
        mediaType: filters.mediaType,
        language: filters.language,
        scope: filters.scope,
        collectionIds: filters.collectionIds,
        limit: DEFAULT_PAGE_SIZE,
        offset: filters.offset,
        cookieHeader
      }),
      session
        ? fetchCollections({
            fetch,
            baseUrl: apiBaseUrl(),
            cookieHeader,
            onResponse: (response) => {
              forwardBackendAccessCookie(response, cookies);
            }
          }).catch(() => null)
        : Promise.resolve(null)
    ]);

    return { page, collections, filters, seo, errorMessage: null };
  } catch (error) {
    return {
      page: emptyMemePage(DEFAULT_PAGE_SIZE, filters.offset),
      collections: null,
      filters,
      seo,
      errorMessage: error instanceof ApiError ? error.message : 'Could not reach the meme catalog API.'
    };
  }
};
