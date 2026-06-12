import type { PageServerLoad } from './$types';
import { ApiError, fetchTrendComparison } from '$lib/api/client';
import { readComparisonItems } from '$lib/features/trends/params';
import { apiBaseUrl } from '$lib/server/backend';

export const load: PageServerLoad = async ({ fetch, request, url }) => {
  const items = readComparisonItems(url.searchParams);
  const cookieHeader = request.headers.get('cookie') ?? undefined;

  try {
    const comparison = await fetchTrendComparison({ fetch, baseUrl: apiBaseUrl(), items, cookieHeader });
    return { items, comparison, errorMessage: null };
  } catch (error) {
    return {
      items,
      comparison: { items: [], requested_items: items, max_items: 6 },
      errorMessage: error instanceof ApiError ? error.message : 'Could not reach the trend comparison API.'
    };
  }
};
