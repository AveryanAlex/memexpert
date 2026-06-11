import { env } from '$env/dynamic/private';
import type { PageServerLoad } from './$types';
import { DEFAULT_PAGE_SIZE, ApiError, emptyMemePage, fetchMemePage } from '$lib/api/client';

export const load: PageServerLoad = async ({ fetch, request, url }) => {
  const query = (url.searchParams.get('q') ?? '').trim();
  const offset = readOffset(url.searchParams.get('offset'));
  const cookieHeader = request.headers.get('cookie') ?? undefined;

  try {
    const page = await fetchMemePage({
      fetch,
      baseUrl: apiBaseUrl(),
      query,
      limit: DEFAULT_PAGE_SIZE,
      offset,
      cookieHeader
    });

    return { page, query, offset, errorMessage: null };
  } catch (error) {
    if (error instanceof ApiError) {
      return {
        page: emptyMemePage(DEFAULT_PAGE_SIZE, offset),
        query,
        offset,
        errorMessage: error.message
      };
    }

    return {
      page: emptyMemePage(DEFAULT_PAGE_SIZE, offset),
      query,
      offset,
      errorMessage: 'Could not reach the meme catalog API.'
    };
  }
};

function apiBaseUrl(): string {
  return env.API_BASE_URL || 'http://localhost:8000';
}

function readOffset(raw: string | null): number {
  const offset = Number.parseInt(raw ?? '', 10);
  return Number.isFinite(offset) && offset > 0 ? offset : 0;
}
