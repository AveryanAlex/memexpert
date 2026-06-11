import { env } from '$env/dynamic/private';
import type { PageServerLoad } from './$types';
import { DEFAULT_PAGE_SIZE, ApiError, fetchTagLanding } from '$lib/api/client';

export const load: PageServerLoad = async ({ fetch, params, request, url }) => {
  const offset = readOffset(url.searchParams.get('offset'));

  try {
    const landing = await fetchTagLanding({
      fetch,
      baseUrl: apiBaseUrl(),
      slug: params.tag,
      limit: DEFAULT_PAGE_SIZE,
      offset,
      cookieHeader: request.headers.get('cookie') ?? undefined
    });

    return { landing, offset, errorMessage: null };
  } catch (error) {
    return {
      landing: null,
      offset,
      errorMessage: error instanceof ApiError ? error.message : 'Could not reach the meme catalog API.'
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
