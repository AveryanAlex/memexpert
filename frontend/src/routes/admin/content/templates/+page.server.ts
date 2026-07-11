import { env } from '$env/dynamic/private';
import type { Actions, PageServerLoad } from './$types';
import { ApiError, fetchAdminMemeTemplates } from '$lib/api/client';
import { templateActions } from '$lib/server/admin/templateActions';

export const load: PageServerLoad = async ({ fetch, request }) => {
  try {
    return {
      templates: await fetchAdminMemeTemplates({
        fetch,
        baseUrl: env.API_BASE_URL || 'http://localhost:8000',
        cookieHeader: request.headers.get('cookie') ?? undefined
      }),
      loadError: null
    };
  } catch (caught) {
    return {
      templates: [],
      loadError: caught instanceof ApiError ? caught.message : 'Could not load meme templates.'
    };
  }
};

export const actions: Actions = templateActions;
