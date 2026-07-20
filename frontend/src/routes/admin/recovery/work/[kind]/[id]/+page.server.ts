import { env } from '$env/dynamic/private';
import { error } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import type { AdminRecoveryWorkKind } from '$lib/api/types';
import { ApiError, fetchAdminRecoveryCandidate, fetchAdminRecoveryWorkDetail } from '$lib/api/client';
import { actRecoveryWork, retryRecoveryWork } from '$lib/server/admin/recoveryActions';

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
    const api = {
      fetch,
      baseUrl: env.API_BASE_URL || 'http://localhost:8000',
      cookieHeader: request.headers.get('cookie') ?? undefined
    };
    let candidate = null;
    try {
      candidate = await fetchAdminRecoveryCandidate(
        api,
        params.kind as AdminRecoveryWorkKind,
        params.id
      );
    } catch (caught) {
      if (!(caught instanceof ApiError) || (caught.status !== 404 && caught.status !== 405)) throw caught;
    }
    const work = candidate?.work ?? await fetchAdminRecoveryWorkDetail(
      api,
      params.kind as AdminRecoveryWorkKind,
      params.id
    );
    return {
      requestId: crypto.randomUUID(),
      work,
      candidate,
      loadError: null
    };
  } catch (caught) {
    return {
      requestId: crypto.randomUUID(),
      work: null,
      candidate: null,
      loadError: caught instanceof ApiError ? caught.message : 'Could not load recovery work details.'
    };
  }
};

export const actions: Actions = { actRecoveryWork, retryRecoveryWork };
