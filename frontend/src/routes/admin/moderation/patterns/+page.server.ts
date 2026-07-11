import { env } from '$env/dynamic/private';
import type { Actions, PageServerLoad } from './$types';
import { ApiError, fetchAdminBlockedPerceptualHashes } from '$lib/api/client';
import { blockedPatternActions } from '$lib/server/admin/blockedPatternActions';

export const load: PageServerLoad = async ({ fetch, request }) => {
  try {
    return {
      patterns: await fetchAdminBlockedPerceptualHashes({
        fetch,
        baseUrl: env.API_BASE_URL || 'http://localhost:8000',
        cookieHeader: request.headers.get('cookie') ?? undefined
      }),
      loadError: null
    };
  } catch (caught) {
    return {
      patterns: [],
      loadError: caught instanceof ApiError ? caught.message : 'Could not load blocked media patterns.'
    };
  }
};

export const actions: Actions = blockedPatternActions;
