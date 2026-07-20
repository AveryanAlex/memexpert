import { env } from '$env/dynamic/private';
import type { Actions, PageServerLoad } from './$types';
import {
  ApiError,
  fetchAdminRecoveryJobs,
  fetchAdminRecoverySummary,
  fetchAdminRecoveryWork
} from '$lib/api/client';
import {
  recoveryFiltersFromUrl,
  recoveryWorkRequestKey,
  RECOVERY_JOB_PAGE_SIZE,
  RECOVERY_PAGE_SIZE
} from '$lib/features/admin/recovery/view-model';
import { recoveryActions } from '$lib/server/admin/recoveryActions';

export const load: PageServerLoad = async ({ fetch, request, url }) => {
  const filters = recoveryFiltersFromUrl(url);
  const api = {
    fetch,
    baseUrl: env.API_BASE_URL || 'http://localhost:8000',
    cookieHeader: request.headers.get('cookie') ?? undefined
  };

  const [summaryResult, workResult, jobsResult] = await Promise.allSettled([
    fetchAdminRecoverySummary(api),
    fetchAdminRecoveryWork(api, {
      bucket: filters.bucket,
      kind: filters.kind,
      source: filters.source,
      stage: filters.stage,
      reason: filters.reason,
      query: filters.query,
      cursor: filters.cursor,
      limit: RECOVERY_PAGE_SIZE
    }),
    fetchAdminRecoveryJobs(api, { cursor: filters.jobCursor, limit: RECOVERY_JOB_PAGE_SIZE })
  ]);
  const snapshotAt = new Date().toISOString();
  const summary = settledValue(summaryResult, {
    retryable_count: 0,
    blocked_count: 0,
    stuck_count: 0,
    dead_lettered_count: 0,
    snapshot_at: snapshotAt
  });
  const workPage = settledValue(workResult, { items: [], next_cursor: null, snapshot_at: snapshotAt });
  const jobsPage = settledValue(jobsResult, { items: [], next_cursor: null });
  const attentionErrors = [
    settledError(summaryResult, 'Could not load recovery summary.'),
    settledError(workResult, 'Could not load recovery work.')
  ].filter((value): value is string => value !== null);

  return {
    summary,
    workPage,
    jobsPage,
    filters,
    requestIds: {
      batchPreview: crypto.randomUUID(),
      allMatchingPreview: crypto.randomUUID(),
      outdatedVideoPreview: crypto.randomUUID(),
      successfulStagePreview: crypto.randomUUID(),
      work: Object.fromEntries(
        workPage.items.map((work) => [recoveryWorkRequestKey(work), crypto.randomUUID()])
      )
    },
    loadError: attentionErrors.length ? [...new Set(attentionErrors)].join(' ') : null,
    jobsLoadError: settledError(jobsResult, 'Could not load replay and repair jobs.')
  };
};

export const actions: Actions = recoveryActions;

function settledValue<T>(result: PromiseSettledResult<T>, fallback: T): T {
  return result.status === 'fulfilled' ? result.value : fallback;
}

function settledError(result: PromiseSettledResult<unknown>, fallback: string): string | null {
  if (result.status === 'fulfilled') return null;
  return result.reason instanceof ApiError ? result.reason.message : fallback;
}
