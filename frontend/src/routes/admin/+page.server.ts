import { env } from '$env/dynamic/private';
import type { PageServerLoad } from './$types';
import { ApiError, fetchAdminOverview } from '$lib/api/client';

export const load: PageServerLoad = async ({ fetch, request }) => {
  const cookieHeader = request.headers.get('cookie') ?? undefined;
  try {
    return {
      overview: await fetchAdminOverview({ fetch, baseUrl: apiBaseUrl(), cookieHeader }),
      loadError: null
    };
  } catch (caught) {
    return {
      overview: {
        open_report_count: 0,
        pending_suggestion_count: 0,
        source_attention_count: 0,
        orphaned_source_count: 0,
        stale_source_count: 0,
        waiting_source_count: 0,
        healthy_source_count: 0,
        telegram_account_attention_count: 0,
        ready_telegram_account_count: 0,
        missing_seo_count: 0,
        uncurated_template_count: 0
      },
      loadError: caught instanceof ApiError ? caught.message : 'Could not load admin tools.'
    };
  }
};

function apiBaseUrl(): string {
  return env.API_BASE_URL || 'http://localhost:8000';
}
