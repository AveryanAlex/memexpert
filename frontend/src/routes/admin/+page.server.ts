import { env } from '$env/dynamic/private';
import { fail } from '@sveltejs/kit';
import type { Actions, PageServerLoad } from './$types';
import {
  addSourceChannel,
  ApiError,
  createBlockedPerceptualHash,
  createMemeTemplate,
  deactivateBlockedPerceptualHash,
  deleteBlockedPerceptualHash,
  deleteMemeTemplate,
  fetchAdminDashboard,
  markSourceChannelDead,
  mergeMemeTemplate,
  regenerateMemeSeoPage,
  reviewChannelSuggestion,
  resolveModerationReport,
  setSourceChannelPaused,
  updateMemeModeration,
  updateBlockedPerceptualHash,
  updateMemeSeoPage,
  updateMemeTemplate
} from '$lib/api/client';
import { buildBlockedPhashPayload } from '$lib/admin/blockedPhash';

export const load: PageServerLoad = async ({ fetch, request }) => {
  const cookieHeader = request.headers.get('cookie') ?? undefined;
  try {
    return {
      dashboard: await fetchAdminDashboard({ fetch, baseUrl: apiBaseUrl(), cookieHeader }),
      loadError: null
    };
  } catch (caught) {
    return {
      dashboard: {
        suggestions: [],
        sourceChannels: [],
        templates: [],
        blockedPerceptualHashes: [],
        memes: [],
        seoReviews: [],
        reports: [],
        decisions: []
      },
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
          orphaned: true,
          catchup_message_limit: readInt(data, 'catchup_message_limit', 500),
          catchup_enabled: data.get('catchup_enabled') === 'on',
          live_enabled: data.get('live_enabled') === 'on',
          engagement_enabled: data.get('engagement_enabled') === 'on'
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
        readRequired(data, 'channel_id'),
        readRequired(data, 'confirmation')
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
  createBlockedPerceptualHash: async ({ fetch, request }) => {
    const data = await request.formData();
    return runAction(async () => {
      await createBlockedPerceptualHash({
        fetch,
        baseUrl: apiBaseUrl(),
        cookieHeader: request.headers.get('cookie') ?? undefined,
        body: blockedPhashPayloadFromForm(data)
      });
      return { message: 'Blocked perceptual hash created.' };
    });
  },
  updateBlockedPerceptualHash: async ({ fetch, request }) => {
    const data = await request.formData();
    return runAction(async () => {
      await updateBlockedPerceptualHash(
        {
          fetch,
          baseUrl: apiBaseUrl(),
          cookieHeader: request.headers.get('cookie') ?? undefined,
          body: blockedPhashPayloadFromForm(data)
        },
        readRequired(data, 'blocked_hash_id')
      );
      return { message: 'Blocked perceptual hash updated.' };
    });
  },
  deactivateBlockedPerceptualHash: async ({ fetch, request }) => {
    const data = await request.formData();
    return runAction(async () => {
      await deactivateBlockedPerceptualHash(
        {
          fetch,
          baseUrl: apiBaseUrl(),
          cookieHeader: request.headers.get('cookie') ?? undefined,
          body: { note: readOptional(data, 'note') }
        },
        readRequired(data, 'blocked_hash_id')
      );
      return { message: 'Blocked perceptual hash deactivated.' };
    });
  },
  deleteBlockedPerceptualHash: async ({ fetch, request }) => {
    const data = await request.formData();
    return runAction(async () => {
      const result = await deleteBlockedPerceptualHash(
        { fetch, baseUrl: apiBaseUrl(), cookieHeader: request.headers.get('cookie') ?? undefined },
        readRequired(data, 'blocked_hash_id')
      );
      return { message: result.message };
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
  updateSeoPage: async ({ fetch, request }) => {
    const data = await request.formData();
    return runAction(async () => {
      await updateMemeSeoPage(
        {
          fetch,
          baseUrl: apiBaseUrl(),
          cookieHeader: request.headers.get('cookie') ?? undefined,
          body: seoPagePayloadFromForm(data)
        },
        readRequired(data, 'meme_id')
      );
      return { message: 'SEO page saved.' };
    });
  },
  regenerateSeoPage: async ({ fetch, request }) => {
    const data = await request.formData();
    const memeId = readRequired(data, 'meme_id');
    return runAction(async () => {
      await regenerateMemeSeoPage(
        {
          fetch,
          baseUrl: apiBaseUrl(),
          cookieHeader: request.headers.get('cookie') ?? undefined,
          body: { confirmation: readRequired(data, 'confirmation') }
        },
        memeId
      );
      return { message: 'SEO page regenerated and manual edits were overwritten.' };
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
    if (caught instanceof Error) {
      return fail(400, { message: caught.message });
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
  const raw = String(data.get(name) ?? '').trim();
  if (!raw) {
    return fallback;
  }
  if (!/^\d+$/.test(raw)) {
    throw new Error(`${name} must be a whole number.`);
  }
  return Number(raw);
}

function blockedPhashPayloadFromForm(data: FormData) {
  return buildBlockedPhashPayload({
    perceptualHash: readRequired(data, 'perceptual_hash'),
    hashAlgorithm: readOptional(data, 'hash_algorithm'),
    maxHammingDistance: readInt(data, 'max_hamming_distance', 0),
    reason: readRequired(data, 'reason'),
    note: readOptional(data, 'note'),
    isActive: data.get('is_active') === 'on'
  });
}

function seoPagePayloadFromForm(data: FormData) {
  return {
    slug: readOptional(data, 'slug'),
    page_title: readOptional(data, 'page_title'),
    meta_description: readOptional(data, 'meta_description'),
    alt_text: readOptional(data, 'alt_text'),
    caption: readOptional(data, 'caption'),
    body_text: readOptional(data, 'body_text'),
    tags: readOptional(data, 'tags')
  };
}

function apiBaseUrl(): string {
  return env.API_BASE_URL || 'http://localhost:8000';
}
