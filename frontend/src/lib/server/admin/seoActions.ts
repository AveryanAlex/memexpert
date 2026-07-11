import type { RequestEvent } from '@sveltejs/kit';
import { regenerateMemeSeoPage, updateMemeSeoPage } from '$lib/api/client';
import { apiRequest, readOptional, readRequired, requireConfirmation, runAction } from './actionUtils';

type SeoActionEvent = Pick<RequestEvent, 'fetch' | 'request'>;

export async function updateSeoPage({ fetch, request }: SeoActionEvent) {
  const data = await request.formData();
  return runAction(async () => {
    const memeId = readRequired(data, 'meme_id');
    await updateMemeSeoPage(
      {
        ...apiRequest(fetch, request),
        body: seoPagePayloadFromForm(data)
      },
      memeId
    );
    return { message: 'SEO page saved.' };
  });
}

export async function regenerateSeoPage({ fetch, request }: SeoActionEvent) {
  const data = await request.formData();
  return runAction(async () => {
    const memeId = readRequired(data, 'meme_id');
    requireConfirmation(
      readRequired(data, 'confirmation_phrase'),
      'REGENERATE',
      'Type REGENERATE to confirm this action.'
    );
    await regenerateMemeSeoPage(
      {
        ...apiRequest(fetch, request),
        body: { confirmation: memeId }
      },
      memeId
    );
    return { message: 'SEO page regenerated and manual edits were overwritten.' };
  });
}

function seoPagePayloadFromForm(data: FormData) {
  return {
    slug: readRequired(data, 'slug'),
    page_title: readRequired(data, 'page_title'),
    meta_description: readRequired(data, 'meta_description'),
    alt_text: readRequired(data, 'alt_text'),
    caption: readOptional(data, 'caption'),
    body_text: readOptional(data, 'body_text'),
    tags: readOptional(data, 'tags')
  };
}

export const seoActions = { updateSeoPage, regenerateSeoPage };
