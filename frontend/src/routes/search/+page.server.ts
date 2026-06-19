import type { PageServerLoad } from './$types';
import { DEFAULT_PAGE_SIZE, ApiError, emptyMemePage, fetchCollections, fetchMemePage } from '$lib/api/client';
import { apiBaseUrl, forwardBackendAccessCookie } from '$lib/server/backend';
import { canonicalPublicOrigin } from '$lib/server/canonicalOrigin';
import { parseSearchParams } from '$lib/searchParams';

export const load: PageServerLoad = async ({ cookies, fetch, parent, request, url }) => {
  const filters = parseSearchParams(url.searchParams);
  const cookieHeader = request.headers.get('cookie') ?? undefined;
  const { session } = await parent();
  const seo = {
    canonicalUrl: `${canonicalPublicOrigin()}/search`,
    noindex: Boolean(filters.query)
  };

  try {
    const [page, collectionState] = await Promise.all([
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
