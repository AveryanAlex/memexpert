import { env } from '$env/dynamic/private';
import type { PageServerLoad } from './$types';
import { ApiError, fetchAdminOverview, fetchAdminRecoverySummary } from '$lib/api/client';

export const load: PageServerLoad = async ({ fetch, request }) => {
  const cookieHeader = request.headers.get('cookie') ?? undefined;
  const api = { fetch, baseUrl: apiBaseUrl(), cookieHeader };
  const [overviewResult, recoveryResult] = await Promise.allSettled([
    fetchAdminOverview(api),
    fetchAdminRecoverySummary(api)
  ]);
  const errors = [overviewResult, recoveryResult]
    .filter((result) => result.status === 'rejected')
    .map((result) => result.reason)
    .map((caught) => caught instanceof ApiError ? caught.message : 'Could not load admin tools.');

  return {
    overview: overviewResult.status === 'fulfilled' ? overviewResult.value : emptyOverview(),
    recovery: recoveryResult.status === 'fulfilled' ? recoveryResult.value : emptyRecoverySummary(),
    loadError: errors.length ? [...new Set(errors)].join(' ') : null
  };
};

function emptyOverview() {
  return {
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
  };
}

function emptyRecoverySummary() {
  return {
    retryable_count: 0,
    blocked_count: 0,
    stuck_count: 0,
    dead_lettered_count: 0
  };
}

function apiBaseUrl(): string {
  return env.API_BASE_URL || 'http://localhost:8000';
}
