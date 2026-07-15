import { env } from '$env/dynamic/private';
import type { Actions, PageServerLoad } from './$types';
import { ApiError, fetchAdminRecoveryBatch } from '$lib/api/client';
import { cancelRecoveryBatch, scheduleRecoveryBatch } from '$lib/server/admin/recoveryActions';

export const load: PageServerLoad = async ({ fetch, params, request }) => {
  try {
    return {
      batch: await fetchAdminRecoveryBatch(
        {
          fetch,
          baseUrl: env.API_BASE_URL || 'http://localhost:8000',
          cookieHeader: request.headers.get('cookie') ?? undefined
        },
        params.jobId
      ),
      loadError: null
    };
  } catch (caught) {
    return {
      batch: null,
      loadError: caught instanceof ApiError ? caught.message : 'Could not load the recovery batch.'
    };
  }
};

export const actions: Actions = { scheduleRecoveryBatch, cancelRecoveryBatch };
