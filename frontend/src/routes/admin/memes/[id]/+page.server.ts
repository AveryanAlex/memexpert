import { env } from '$env/dynamic/private';
import { error, fail, redirect } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import type { AdminMemeDetailRead, AdminRecoveryWorkKind } from '$lib/api/types';
import {
  ApiError,
  deleteAdminMeme,
  fetchAdminMemeDetail,
  fetchAdminMemeTemplates,
  mergeAdminMeme,
  resolveModerationReport,
  updateMemeModeration
} from '$lib/api/client';
import { actRecoveryWork } from '$lib/server/admin/recoveryActions';

export const load: PageServerLoad = async ({ fetch, params, request }) => {
  const requestConfig = {
    fetch,
    baseUrl: apiBaseUrl(),
    cookieHeader: request.headers.get('cookie') ?? undefined
  };

  const [detailResult, templatesResult] = await Promise.allSettled([
    fetchAdminMemeDetail(requestConfig, params.id),
    fetchAdminMemeTemplates(requestConfig)
  ]);
  if (detailResult.status === 'rejected' && detailResult.reason instanceof ApiError && detailResult.reason.status === 404) {
    throw error(404, detailResult.reason.message);
  }
  const detail = detailResult.status === 'fulfilled' ? detailResult.value : null;
  return {
    detail,
    templates: templatesResult.status === 'fulfilled' ? templatesResult.value : [],
    processingRequestIds: detail ? processingActionRequestIds(detail) : {},
    loadError: detailResult.status === 'rejected'
      ? detailResult.reason instanceof ApiError ? detailResult.reason.message : 'Could not load meme.'
      : null,
    templatesLoadError: templatesResult.status === 'rejected'
      ? templatesResult.reason instanceof ApiError ? templatesResult.reason.message : 'Could not load meme templates.'
      : null
  };
};

export const actions: Actions = {
  actRecoveryWork,
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
            visibility_mode: readRequired(data, 'visibility_mode'),
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
  },
  deleteMeme: async ({ fetch, params, request }) => {
    const data = await request.formData();
    try {
      requirePhrase(data, 'DELETE');
      await deleteAdminMeme(
        {
          fetch,
          baseUrl: apiBaseUrl(),
          cookieHeader: request.headers.get('cookie') ?? undefined,
          body: {
            confirmation: params.id,
            note: readRequired(data, 'note')
          }
        },
        params.id
      );
    } catch (caught) {
      if (caught instanceof ApiError) {
        return fail(caught.status, { message: caught.message, error: true });
      }
      return fail(500, { message: 'Admin meme deletion failed.', error: true });
    }
    throw redirect(303, '/admin/moderation');
  },
  mergeMeme: async ({ fetch, params, request }) => {
    const data = await request.formData();
    let targetMemeId: string;
    try {
      requirePhrase(data, 'MERGE');
      targetMemeId = readRequired(data, 'target_meme_id');
      await mergeAdminMeme(
        {
          fetch,
          baseUrl: apiBaseUrl(),
          cookieHeader: request.headers.get('cookie') ?? undefined,
          body: {
            target_meme_id: targetMemeId,
            confirmation: params.id,
            note: readRequired(data, 'note')
          }
        },
        params.id
      );
    } catch (caught) {
      if (caught instanceof ApiError) {
        return fail(caught.status, { message: caught.message, error: true });
      }
      return fail(500, { message: 'Admin meme merge failed.', error: true });
    }
    throw redirect(303, `/admin/memes/${targetMemeId}`);
  }
};

function processingActionRequestIds(detail: AdminMemeDetailRead): Record<string, string> {
  const requestIds: Record<string, string> = {};
  for (const file of detail.processing_files ?? []) {
    if (file.actions?.length) {
      const kind = file.work_kind ?? 'pipeline_stage';
      const workId = file.work_id ?? `${file.id}:transcode`;
      requestIds[processingRequestKey(kind, workId)] = crypto.randomUUID();
    }
    for (const stage of file.stages) {
      if (!stage.actions?.length) continue;
      const kind = stage.work_kind ?? 'pipeline_stage';
      const workId = stage.work_id ?? `${file.id}:${stage.stage}`;
      requestIds[processingRequestKey(kind, workId)] = crypto.randomUUID();
    }
  }
  return requestIds;
}

function processingRequestKey(kind: AdminRecoveryWorkKind, workId: string): string {
  return `${kind}:${workId}`;
}

async function runAction(operation: () => Promise<{ message: string }>) {
  try {
    return await operation();
  } catch (caught) {
    if (caught instanceof ApiError) {
      return fail(caught.status, { message: caught.message, error: true });
    }
    return fail(500, { message: 'Admin meme operation failed.', error: true });
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

function requirePhrase(data: FormData, expected: 'DELETE' | 'MERGE'): void {
  const value = String(data.get('confirmation_phrase') ?? '').trim();
  if (value !== expected) {
    throw new ApiError(400, `Type ${expected} to confirm this action.`);
  }
}

function apiBaseUrl(): string {
  return env.API_BASE_URL || 'http://localhost:8000';
}
