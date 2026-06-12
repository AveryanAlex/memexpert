import { env } from '$env/dynamic/private';
import { error, fail } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import {
  ApiError,
  fetchAdminMemeDetail,
  fetchAdminMemeTemplates,
  resolveModerationReport,
  updateMemeModeration
} from '$lib/api/client';

export const load: PageServerLoad = async ({ fetch, params, request }) => {
  const requestConfig = {
    fetch,
    baseUrl: apiBaseUrl(),
    cookieHeader: request.headers.get('cookie') ?? undefined
  };

  try {
    const [detail, templates] = await Promise.all([
      fetchAdminMemeDetail(requestConfig, params.id),
      fetchAdminMemeTemplates(requestConfig)
    ]);

    return { detail, templates, loadError: null };
  } catch (caught) {
    if (caught instanceof ApiError && caught.status === 404) {
      throw error(404, caught.message);
    }
    return { detail: null, templates: [], loadError: caught instanceof ApiError ? caught.message : 'Could not load meme.' };
  }
};

export const actions: Actions = {
  updateMeme: async ({ fetch, params, request }) => {
    const data = await request.formData();
    return runAction(async () => {
      const templateId = readOptional(data, 'template_id');
      await updateMemeModeration(
        {
          fetch,
          baseUrl: apiBaseUrl(),
          cookieHeader: request.headers.get('cookie') ?? undefined,
          body: {
            is_nsfw: data.get('is_nsfw') === 'on',
            is_public: data.get('is_public') === 'on',
            template_id: templateId,
            reason: readOptional(data, 'reason'),
            note: readOptional(data, 'note')
          }
        },
        params.id
      );
      return { message: 'Meme overrides saved.' };
    });
  },
  resolveReport: async ({ fetch, request }) => {
    const data = await request.formData();
    return runAction(async () => {
      await resolveModerationReport(
        {
          fetch,
          baseUrl: apiBaseUrl(),
          cookieHeader: request.headers.get('cookie') ?? undefined,
          body: {
            action: readRequired(data, 'action'),
            reason: readOptional(data, 'reason'),
            note: readOptional(data, 'note')
          }
        },
        readRequired(data, 'report_id')
      );
      return { message: 'Moderation report resolved.' };
    });
  }
};

async function runAction(operation: () => Promise<{ message: string }>) {
  try {
    return await operation();
  } catch (caught) {
    if (caught instanceof ApiError) {
      return fail(caught.status, { message: caught.message });
    }
    return fail(500, { message: 'Admin meme operation failed.' });
  }
}

function readRequired(data: FormData, name: string): string {
  const value = String(data.get(name) ?? '').trim();
  if (!value) {
    throw new ApiError(400, `${name} is required.`);
  }
  return value;
}

function readOptional(data: FormData, name: string): string | null {
  const value = String(data.get(name) ?? '').trim();
  return value || null;
}

function apiBaseUrl(): string {
  return env.API_BASE_URL || 'http://localhost:8000';
}
