import type { PageServerLoad } from './$types';
import {
  DEFAULT_PAGE_SIZE,
  ApiError,
  emptyTrendPage,
  fetchTagTrendSummaries,
  fetchTemplateTrendSummaries,
  fetchTrendPage
} from '$lib/api/client';
import { apiBaseUrl } from '$lib/server/backend';

const SUMMARY_LIMIT = 8;

export const load: PageServerLoad = async ({ fetch, request, url }) => {
  const offset = readOffset(url.searchParams.get('offset'));
  const ranking = readRanking(url.searchParams.get('ranking'));
  const cookieHeader = request.headers.get('cookie') ?? undefined;

  try {
    const [page, tagSummaries, templateSummaries] = await Promise.all([
      fetchTrendPage({
        fetch,
        baseUrl: apiBaseUrl(),
        ranking,
        limit: DEFAULT_PAGE_SIZE,
        offset,
        cookieHeader
      }),
      fetchTagTrendSummaries({ fetch, baseUrl: apiBaseUrl(), limit: SUMMARY_LIMIT, offset: 0, cookieHeader }),
      fetchTemplateTrendSummaries({ fetch, baseUrl: apiBaseUrl(), limit: SUMMARY_LIMIT, offset: 0, cookieHeader })
    ]);

    return { page, tagSummaries, templateSummaries, ranking, offset, errorMessage: null };
  } catch (error) {
    return {
      page: emptyTrendPage(DEFAULT_PAGE_SIZE, offset),
      tagSummaries: [],
      templateSummaries: [],
      ranking,
      offset,
      errorMessage: error instanceof ApiError ? error.message : 'Could not reach the trend analytics API.'
    };
  }
};

function readOffset(raw: string | null): number {
  const offset = Number.parseInt(raw ?? '', 10);
  return Number.isFinite(offset) && offset > 0 ? offset : 0;
}

function readRanking(raw: string | null): 'trending' | 'fastest_rising' | 'most_liked' {
  if (raw === 'fastest_rising' || raw === 'most_liked') {
    return raw;
  }
  return 'trending';
}
