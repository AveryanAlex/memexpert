import { env } from '$env/dynamic/private';
import type { Actions, PageServerLoad } from './$types';
import type { AdminRecoveryJobItemStatus } from '$lib/api/types';
import { ApiError, fetchAdminRecoveryBatch, fetchAdminRecoveryJobItems } from '$lib/api/client';
import {
  cancelRecoveryBatch,
  handoffRecoveryBatch,
  retryFailedRecoveryBatch,
  scheduleRecoveryBatch
} from '$lib/server/admin/recoveryActions';
import { RECOVERY_JOB_ITEM_PAGE_SIZE } from '$lib/features/admin/recovery/view-model';

const ITEM_STATUSES = new Set<AdminRecoveryJobItemStatus>([
  'cancelled',
  'dispatched',
  'failed',
  'queued',
  'skipped_dependency',
  'skipped_stale',
  'succeeded',
  'waiting_capacity',
  'waiting_dependency'
]);

export const load: PageServerLoad = async ({ depends, fetch, params, request, url }) => {
  depends(`app:admin-recovery-job:${params.jobId}`);
  const api = {
    fetch,
    baseUrl: env.API_BASE_URL || 'http://localhost:8000',
    cookieHeader: request.headers.get('cookie') ?? undefined
  };
  const cursor = clean(url.searchParams.get('item_cursor'), 2048);
  const status = recoveryItemStatus(url.searchParams.get('item_status'));
  const [batchResult, itemsResult] = await Promise.allSettled([
    fetchAdminRecoveryBatch(api, params.jobId),
    fetchAdminRecoveryJobItems(api, params.jobId, {
      cursor,
      status,
      limit: RECOVERY_JOB_ITEM_PAGE_SIZE,
      order: 'failed_first'
    })
  ]);
  const batch = batchResult.status === 'fulfilled' ? batchResult.value : null;
  const legacyItems = batch?.items ?? [];
  const itemsPage = itemsResult.status === 'fulfilled'
    ? itemsResult.value
    : { items: legacyItems, next_cursor: null, total: legacyItems.length };

  return {
    batch,
    itemsPage,
    itemFilters: { cursor, status },
    retryFailedRequestId: crypto.randomUUID(),
    loadError: resultError(batchResult, 'Could not load the replay and repair job.'),
    itemsLoadError: itemsResult.status === 'rejected' && !isCompatibilityMiss(itemsResult.reason)
      ? errorMessage(itemsResult.reason, 'Could not load job items.')
      : null
  };
};

export const actions: Actions = {
  scheduleRecoveryBatch,
  cancelRecoveryBatch,
  handoffRecoveryBatch,
  retryFailedRecoveryBatch
};

function recoveryItemStatus(value: string | null): AdminRecoveryJobItemStatus | null {
  return value && ITEM_STATUSES.has(value as AdminRecoveryJobItemStatus)
    ? value as AdminRecoveryJobItemStatus
    : null;
}

function clean(value: string | null, maxLength: number): string | null {
  const normalized = value?.trim();
  return normalized ? normalized.slice(0, maxLength) : null;
}

function resultError(result: PromiseSettledResult<unknown>, fallback: string): string | null {
  return result.status === 'fulfilled' ? null : errorMessage(result.reason, fallback);
}

function errorMessage(caught: unknown, fallback: string): string {
  return caught instanceof ApiError ? caught.message : fallback;
}

function isCompatibilityMiss(caught: unknown): boolean {
  return caught instanceof ApiError && (caught.status === 404 || caught.status === 405);
}
