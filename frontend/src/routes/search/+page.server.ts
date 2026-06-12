import type { PageServerLoad } from './$types';
import { DEFAULT_PAGE_SIZE, ApiError, emptyMemePage, fetchMemePage } from '$lib/api/client';
import { apiBaseUrl } from '$lib/server/backend';
import { parseSearchParams } from '$lib/searchParams';

export const load: PageServerLoad = async ({ fetch, request, url }) => {
  const filters = parseSearchParams(url.searchParams);
  const cookieHeader = request.headers.get('cookie') ?? undefined;

  try {
    const page = await fetchMemePage({
      fetch,
      baseUrl: apiBaseUrl(),
      query: filters.query,
      tags: filters.tags,
      includeNsfw: filters.includeNsfw,
      mediaType: filters.mediaType,
      language: filters.language,
      limit: DEFAULT_PAGE_SIZE,
      offset: filters.offset,
      cookieHeader
    });

    return { page, filters, errorMessage: null };
  } catch (error) {
    return {
      page: emptyMemePage(DEFAULT_PAGE_SIZE, filters.offset),
      filters,
      errorMessage: error instanceof ApiError ? error.message : 'Could not reach the meme catalog API.'
    };
  }
};
