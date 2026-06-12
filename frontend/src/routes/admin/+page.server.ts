import { env } from '$env/dynamic/private';
import { fail } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import {
  addSourceChannel,
  ApiError,
  createMemeTemplate,
  deleteMemeTemplate,
  fetchAdminDashboard,
  markSourceChannelDead,
  mergeMemeTemplate,
  reviewChannelSuggestion,
  resolveModerationReport,
  setSourceChannelPaused,
  updateMemeModeration,
  updateMemeTemplate
} from '$lib/api/client';

export const load: PageServerLoad = async ({ fetch, request }) => {
  const cookieHeader = request.headers.get('cookie') ?? undefined;
  try {
    return {
      dashboard: await fetchAdminDashboard({ fetch, baseUrl: apiBaseUrl(), cookieHeader }),
      loadError: null
    };
  } catch (caught) {
    return {
      dashboard: { suggestions: [], sourceChannels: [], templates: [], memes: [], reports: [], decisions: [] },
      loadError: caught instanceof ApiError ? caught.message : 'Could not load admin tools.'
    };
  }
};

export const actions: Actions = {
  reviewSuggestion: async ({ fetch, request }) => {
    const data = await request.formData();
    const suggestionId = readRequired(data, 'suggestion_id');
    const decision = readRequired(data, 'decision');
    if (decision !== 'approve' && decision !== 'reject') {
      return fail(400, { message: 'Unknown review decision.' });
    }
    return runAction(async () => {
      await reviewChannelSuggestion(
        {
          fetch,
          baseUrl: apiBaseUrl(),
          cookieHeader: request.headers.get('cookie') ?? undefined,
          body: { admin_note: readOptional(data, 'admin_note') }
        },
        suggestionId,
        decision
      );
      return { message: `Suggestion ${decision}d.` };
    });
  },
  addSourceChannel: async ({ fetch, request }) => {
    const data = await request.formData();
    return runAction(async () => {
      await addSourceChannel({
        fetch,
        baseUrl: apiBaseUrl(),
        cookieHeader: request.headers.get('cookie') ?? undefined,
        body: {
          platform: readRequired(data, 'platform'),
          platform_id: readRequired(data, 'platform_id'),
          username: readOptional(data, 'username'),
          title: readRequired(data, 'title'),
          session_id: readOptional(data, 'session_id'),
          catchup_message_limit: readInt(data, 'catchup_message_limit', 500),
          catchup_enabled: data.get('catchup_enabled') === 'on'
        }
      });
      return { message: 'Source channel added.' };
    });
  },
  toggleSourceChannel: async ({ fetch, request }) => {
    const data = await request.formData();
    return runAction(async () => {
      await setSourceChannelPaused(
        { fetch, baseUrl: apiBaseUrl(), cookieHeader: request.headers.get('cookie') ?? undefined },
        readRequired(data, 'channel_id'),
        readRequired(data, 'paused') === 'true'
      );
      return { message: 'Source channel updated.' };
    });
  },
  markSourceChannelDead: async ({ fetch, request }) => {
    const data = await request.formData();
    return runAction(async () => {
      await markSourceChannelDead(
        { fetch, baseUrl: apiBaseUrl(), cookieHeader: request.headers.get('cookie') ?? undefined },
        readRequired(data, 'channel_id')
      );
      return { message: 'Source channel marked dead; crawler checkpoint state was preserved.' };
    });
  },
  createTemplate: async ({ fetch, request }) => {
    const data = await request.formData();
    return runAction(async () => {
      await createMemeTemplate({
        fetch,
        baseUrl: apiBaseUrl(),
        cookieHeader: request.headers.get('cookie') ?? undefined,
        body: {
          slug: readRequired(data, 'slug'),
          name: readRequired(data, 'name'),
          description: readOptional(data, 'description'),
          is_curated: data.get('is_curated') === 'on',
          base_image_url: readOptional(data, 'base_image_url')
        }
      });
      return { message: 'Template created.' };
    });
  },
  updateTemplate: async ({ fetch, request }) => {
    const data = await request.formData();
    return runAction(async () => {
      await updateMemeTemplate(
        {
          fetch,
          baseUrl: apiBaseUrl(),
          cookieHeader: request.headers.get('cookie') ?? undefined,
          body: {
            slug: readRequired(data, 'slug'),
            name: readRequired(data, 'name'),
            description: readOptional(data, 'description'),
            is_curated: data.get('is_curated') === 'on',
            base_image_url: readOptional(data, 'base_image_url')
          }
        },
        readRequired(data, 'template_id')
      );
      return { message: 'Template updated.' };
    });
  },
  mergeTemplate: async ({ fetch, request }) => {
    const data = await request.formData();
    const templateId = readRequired(data, 'template_id');
    return runAction(async () => {
      await mergeMemeTemplate(
        {
          fetch,
          baseUrl: apiBaseUrl(),
          cookieHeader: request.headers.get('cookie') ?? undefined,
          body: {
            target_template_id: readRequired(data, 'target_template_id'),
            confirmation: readRequired(data, 'confirmation'),
            note: readRequired(data, 'note')
          }
        },
        templateId
      );
      return { message: 'Template merged and affected memes audited.' };
    });
  },
  deleteTemplate: async ({ fetch, request }) => {
    const data = await request.formData();
    const templateId = readRequired(data, 'template_id');
    return runAction(async () => {
      await deleteMemeTemplate(
        {
          fetch,
          baseUrl: apiBaseUrl(),
          cookieHeader: request.headers.get('cookie') ?? undefined,
          body: {
            confirmation: readRequired(data, 'confirmation'),
            note: readOptional(data, 'note')
          }
        },
        templateId
      );
      return { message: 'Unreferenced template deleted.' };
    });
  },
  updateMemeModeration: async ({ fetch, request }) => {
    const data = await request.formData();
    return runAction(async () => {
      await updateMemeModeration(
        {
          fetch,
          baseUrl: apiBaseUrl(),
          cookieHeader: request.headers.get('cookie') ?? undefined,
          body: {
            is_nsfw: data.get('is_nsfw') === 'on',
            is_public: data.get('is_public') === 'on',
            reason: readOptional(data, 'reason'),
            note: readOptional(data, 'note')
          }
        },
        readRequired(data, 'meme_id')
      );
      return { message: 'Meme moderation flags updated and audited.' };
    });
  },
  resolveModerationReport: async ({ fetch, request }) => {
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
      return { message: 'Moderation report resolved and audited.' };
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
    return fail(500, { message: 'Admin operation failed.' });
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

function readInt(data: FormData, name: string, fallback: number): number {
  const value = Number.parseInt(String(data.get(name) ?? ''), 10);
  return Number.isFinite(value) ? value : fallback;
}

function apiBaseUrl(): string {
  return env.API_BASE_URL || 'http://localhost:8000';
}
