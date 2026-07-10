import { env } from '$env/dynamic/private';
import type { Actions, PageServerLoad } from './$types';
import {
  ApiError,
  createBlockedPerceptualHash,
  createMemeTemplate,
  deactivateBlockedPerceptualHash,
  deleteBlockedPerceptualHash,
  deleteMemeTemplate,
  fetchAdminOverview,
  mergeMemeTemplate,
  regenerateMemeSeoPage,
  resolveModerationReport,
  updateMemeModeration,
  updateBlockedPerceptualHash,
  updateMemeSeoPage,
  updateMemeTemplate
} from '$lib/api/client';
import { buildBlockedPhashPayload } from '$lib/admin/blockedPhash';
import { readInt, readOptional, readRequired, runAction } from '$lib/server/admin/actionUtils';

export const load: PageServerLoad = async ({ fetch, request }) => {
  const cookieHeader = request.headers.get('cookie') ?? undefined;
  try {
    return {
      overview: await fetchAdminOverview({ fetch, baseUrl: apiBaseUrl(), cookieHeader }),
      loadError: null
    };
  } catch (caught) {
    return {
      overview: {
        open_report_count: 0,
        pending_suggestion_count: 0,
        source_attention_count: 0,
        orphaned_source_count: 0,
        stale_source_count: 0,
        waiting_source_count: 0,
        healthy_source_count: 0,
        telegram_account_attention_count: 0,
        ready_telegram_account_count: 0,
        missing_seo_count: 0,
        uncurated_template_count: 0
      },
      loadError: caught instanceof ApiError ? caught.message : 'Could not load admin tools.'
    };
  }
};

export const actions: Actions = {
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
