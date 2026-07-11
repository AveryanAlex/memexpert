import type { RequestEvent } from '@sveltejs/kit';
import { resolveModerationReport as resolveReportRequest, updateMemeModeration as updateMemeRequest } from '$lib/api/client';
import { apiRequest, readOptional, readRequired, runAction } from './actionUtils';

export async function resolveModerationReport({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  return runAction(async () => {
    await resolveReportRequest(
      {
        ...apiRequest(fetch, request),
        body: {
          action: readRequired(data, 'action'),
          reason: readOptional(data, 'reason'),
          note: readOptional(data, 'note')
        }
      },
      readRequired(data, 'report_id')
    );
    return { message: 'Report resolved and decision recorded.' };
  });
}

export async function updateMemeModeration({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  return runAction(async () => {
    await updateMemeRequest(
      {
        ...apiRequest(fetch, request),
        body: {
          is_nsfw: data.get('is_nsfw') === 'on',
          is_public: data.get('is_public') === 'on',
          reason: readOptional(data, 'reason'),
          note: readOptional(data, 'note')
        }
      },
      readRequired(data, 'meme_id')
    );
    return { message: 'Meme visibility and safety settings saved.' };
  });
}

export const moderationActions = { resolveModerationReport, updateMemeModeration };
