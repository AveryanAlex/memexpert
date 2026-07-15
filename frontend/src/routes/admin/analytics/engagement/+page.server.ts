import type { PageServerLoad } from './$types';
import {
  ApiError,
  fetchAdminAnalyticsEngagement,
  fetchAdminAnalyticsSearchQueries,
  fetchAdminAnalyticsSearchQueryDetail
} from '$lib/api/client';
import type { AdminAnalyticsSearchQuerySort } from '$lib/api/types';
import { analyticsRangeParamsFromUrl } from '$lib/features/admin/analytics/range';
import { apiBaseUrl } from '$lib/server/backend';

const QUERY_PAGE_SIZE = 50;

export const load: PageServerLoad = async ({ fetch, request, url }) => {
  const requestedRange = analyticsRangeParamsFromUrl(url);
  const offset = readOffset(url.searchParams.get('offset'));
  const sort = readSort(url.searchParams.get('sort'));
  const selectedQueryKey = cleanQueryKey(url.searchParams.get('query_key'));
  const baseRequest = {
    fetch,
    baseUrl: apiBaseUrl(),
    cookieHeader: request.headers.get('cookie') ?? undefined,
    ...requestedRange
  };

  const [dashboardResult, queriesResult, detailResult] = await Promise.allSettled([
    fetchAdminAnalyticsEngagement(baseRequest),
    fetchAdminAnalyticsSearchQueries({ ...baseRequest, limit: QUERY_PAGE_SIZE, offset, sort }),
    selectedQueryKey ? fetchAdminAnalyticsSearchQueryDetail({ ...baseRequest, queryKey: selectedQueryKey }) : Promise.resolve(null)
  ]);
  const failures = [dashboardResult, queriesResult, detailResult]
    .filter((result) => result.status === 'rejected')
    .map((result) => result.reason)
    .map(analyticsLoadError);

  return {
    dashboard: dashboardResult.status === 'fulfilled' ? dashboardResult.value : null,
    searchQueries: queriesResult.status === 'fulfilled' ? queriesResult.value : null,
    queryDetail: detailResult.status === 'fulfilled' ? detailResult.value : null,
    selectedQueryKey,
    offset,
    sort,
    requestedRange,
    loadError: failures.length > 0 ? [...new Set(failures)].join(' ') : null
  };
};

function readOffset(raw: string | null): number {
  const value = Number.parseInt(raw ?? '', 10);
  return Number.isFinite(value) && value > 0 ? Math.min(value, 1_000_000) : 0;
}

function cleanQueryKey(raw: string | null): string | null {
  const key = raw?.trim();
  return key && /^[0-9a-f]{64}$/.test(key) ? key : null;
}

function readSort(raw: string | null): AdminAnalyticsSearchQuerySort {
  if (raw === 'niche' || raw === 'zero_result_rate' || raw === 'downloads') return raw;
  return 'searches';
}

function analyticsLoadError(caught: unknown): string {
  return caught instanceof ApiError ? caught.message : 'Could not load engagement analytics.';
}
