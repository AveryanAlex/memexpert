import { env } from '$env/dynamic/private';
import { error } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import type { AdminRecoveryWorkKind } from '$lib/api/types';
import { ApiError, fetchAdminRecoveryWorkDetail } from '$lib/api/client';
import { retryRecoveryWork } from '$lib/server/admin/recoveryActions';

const WORK_KINDS = new Set<AdminRecoveryWorkKind>([
  'backfill',
  'dead_letter',
  'ingest_request',
  'outbox',
  'pipeline_stage',
  'source_post',
  'sync_target'
]);

export const load: PageServerLoad = async ({ fetch, params, request }) => {
  if (!WORK_KINDS.has(params.kind as AdminRecoveryWorkKind)) {
    throw error(404, 'Recovery work was not found.');
  }

  try {
    return {
      requestId: crypto.randomUUID(),
      work: await fetchAdminRecoveryWorkDetail(
        {
          fetch,
          baseUrl: env.API_BASE_URL || 'http://localhost:8000',
          cookieHeader: request.headers.get('cookie') ?? undefined
        },
        params.kind as AdminRecoveryWorkKind,
        params.id
      ),
      loadError: null
    };
  } catch (caught) {
    return {
      requestId: crypto.randomUUID(),
      work: null,
      loadError: caught instanceof ApiError ? caught.message : 'Could not load recovery work details.'
    };
  }
};

export const actions: Actions = { retryRecoveryWork };
