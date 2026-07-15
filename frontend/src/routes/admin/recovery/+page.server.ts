import { env } from '$env/dynamic/private';
import type { Actions, PageServerLoad } from './$types';
import {
  ApiError,
  fetchAdminRecoverySummary,
  fetchAdminRecoveryWork
} from '$lib/api/client';
import { recoveryFiltersFromUrl, recoveryWorkRequestKey, RECOVERY_PAGE_SIZE } from '$lib/features/admin/recovery/view-model';
import { recoveryActions } from '$lib/server/admin/recoveryActions';

export const load: PageServerLoad = async ({ fetch, request, url }) => {
  const filters = recoveryFiltersFromUrl(url);
  const api = {
    fetch,
    baseUrl: env.API_BASE_URL || 'http://localhost:8000',
    cookieHeader: request.headers.get('cookie') ?? undefined
  };

  try {
    const [summary, workPage] = await Promise.all([
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
      })
    ]);
    return {
      summary,
      workPage,
      filters,
      requestIds: {
        batchPreview: crypto.randomUUID(),
        work: Object.fromEntries(
          workPage.items.map((work) => [recoveryWorkRequestKey(work), crypto.randomUUID()])
        )
      },
      loadError: null
    };
  } catch (caught) {
    const snapshotAt = new Date().toISOString();
    return {
      summary: {
        retryable_count: 0,
        blocked_count: 0,
        stuck_count: 0,
        dead_lettered_count: 0
      },
      workPage: { items: [], next_cursor: null, snapshot_at: snapshotAt },
      filters,
      requestIds: { batchPreview: crypto.randomUUID(), work: {} },
      loadError: caught instanceof ApiError ? caught.message : 'Could not load recovery work.'
    };
  }
};

export const actions: Actions = recoveryActions;
