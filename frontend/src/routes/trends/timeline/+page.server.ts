import type { PageServerLoad } from './$types';
import { ApiError, emptyTrendTimeline, fetchTrendTimeline } from '$lib/api/client';
import { readTimelineGranularity } from '$lib/features/trends/params';
import { apiBaseUrl } from '$lib/server/backend';

const TIMELINE_PAGE_SIZE = 12;

export const load: PageServerLoad = async ({ fetch, request, url }) => {
  const granularity = readTimelineGranularity(url.searchParams.get('granularity'));
  const offset = readOffset(url.searchParams.get('offset'));
  const cookieHeader = request.headers.get('cookie') ?? undefined;

  try {
    const timeline = await fetchTrendTimeline({
      fetch,
      baseUrl: apiBaseUrl(),
      granularity,
      limit: TIMELINE_PAGE_SIZE,
      offset,
      cookieHeader
    });
    return { timeline, granularity, offset, errorMessage: null };
  } catch (error) {
    return {
      timeline: emptyTrendTimeline(granularity, TIMELINE_PAGE_SIZE, offset),
      granularity,
      offset,
      errorMessage: error instanceof ApiError ? error.message : 'Could not reach the trend timeline API.'
    };
  }
};

function readOffset(raw: string | null): number {
  const offset = Number.parseInt(raw ?? '', 10);
  return Number.isFinite(offset) && offset > 0 ? offset : 0;
}
