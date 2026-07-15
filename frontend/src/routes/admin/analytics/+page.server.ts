import type { PageServerLoad } from './$types';
import { ApiError, fetchAdminAnalyticsOverview } from '$lib/api/client';
import { analyticsRangeParamsFromUrl } from '$lib/features/admin/analytics/range';
import { apiBaseUrl } from '$lib/server/backend';

export const load: PageServerLoad = async ({ fetch, request, url }) => {
  const requestedRange = analyticsRangeParamsFromUrl(url);

  try {
    const dashboard = await fetchAdminAnalyticsOverview({
      fetch,
      baseUrl: apiBaseUrl(),
      cookieHeader: request.headers.get('cookie') ?? undefined,
      ...requestedRange
    });
    return { dashboard, requestedRange, loadError: null };
  } catch (caught) {
    return { dashboard: null, requestedRange, loadError: analyticsLoadError(caught) };
  }
};

function analyticsLoadError(caught: unknown): string {
  return caught instanceof ApiError ? caught.message : 'Could not load the analytics overview.';
}
