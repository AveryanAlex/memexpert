import type { PageServerLoad } from './$types';
import { DEFAULT_PAGE_SIZE, ApiError, emptyMemePage, fetchCollections, fetchMemePage, type ApiFetch } from '$lib/api/client';
import { ACCESS_COOKIE_NAME, apiBaseUrl, cookieHeaderWithAccessToken, forwardBackendAccessCookie } from '$lib/server/backend';
import { canonicalPublicOrigin } from '$lib/server/canonicalOrigin';
import { parseSearchParams } from '$lib/searchParams';

export const load: PageServerLoad = async ({ cookies, fetch, parent, request, setHeaders, url }) => {
  setHeaders({ 'cache-control': 'private, no-store' });
  const filters = parseSearchParams(url.searchParams);
  const upstreamFetch: ApiFetch = (input, init) => fetch(input, { ...init, signal: request.signal });
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
    const [page, collectionState] = await Promise.all([
      fetchMemePage({
        fetch: upstreamFetch,
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
            fetch: upstreamFetch,
            baseUrl: apiBaseUrl(),
            cookieHeader,
            onResponse: (response) => {
              forwardBackendAccessCookie(response, cookies);
            }
          })
            .then((collections) => ({ collections, collectionErrorMessage: null }))
            .catch((error) => ({
              collections: null,
              collectionErrorMessage: error instanceof ApiError ? error.message : 'Collection filters are unavailable right now.'
            }))
        : Promise.resolve({ collections: null, collectionErrorMessage: null })
    ]);

    return { page, collections: collectionState.collections, filters, seo, errorMessage: null, collectionErrorMessage: collectionState.collectionErrorMessage };
  } catch (error) {
    return {
      page: emptyMemePage(DEFAULT_PAGE_SIZE, filters.offset),
      collections: null,
      filters,
      seo,
      errorMessage: error instanceof ApiError ? error.message : 'Could not reach the meme catalog API.',
      collectionErrorMessage: null
    };
  }
};
