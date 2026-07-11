import { env } from '$env/dynamic/private';
import type { Actions, PageServerLoad } from './$types';
import {
  ApiError,
  fetchAdminModerationDecisions,
  fetchAdminModerationMemes,
  fetchAdminModerationReports
} from '$lib/api/client';
import { moderationActions } from '$lib/server/admin/moderationActions';

export const load: PageServerLoad = async ({ fetch, request }) => {
  const api = {
    fetch,
    baseUrl: env.API_BASE_URL || 'http://localhost:8000',
    cookieHeader: request.headers.get('cookie') ?? undefined
  };

  try {
    const [reports, decisions, memes] = await Promise.all([
      fetchAdminModerationReports(api, 50),
      fetchAdminModerationDecisions(api, 20),
      fetchAdminModerationMemes(api, 20)
    ]);
    return { moderation: { reports, decisions, memes }, loadError: null };
  } catch (caught) {
    return {
      moderation: { reports: [], decisions: [], memes: [] },
      loadError: caught instanceof ApiError ? caught.message : 'Could not load moderation work.'
    };
  }
};

export const actions: Actions = moderationActions;
