import type { RequestEvent } from '@sveltejs/kit';
import {
  createMemeTemplate,
  deleteMemeTemplate,
  mergeMemeTemplate,
  updateMemeTemplate
} from '$lib/api/client';
import { apiRequest, readOptional, readRequired, requireConfirmation, runAction } from './actionUtils';

export async function createTemplate({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  return runAction(async () => {
    await createMemeTemplate({
      ...apiRequest(fetch, request),
      body: templatePayloadFromForm(data)
    });
    return { message: 'Template created.' };
  });
}

export async function updateTemplate({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  return runAction(async () => {
    const templateId = readRequired(data, 'template_id');
    await updateMemeTemplate(
      {
        ...apiRequest(fetch, request),
        body: templatePayloadFromForm(data)
      },
      templateId
    );
    return { message: 'Template updated.' };
  });
}

export async function mergeTemplate({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  return runAction(async () => {
    const templateId = readRequired(data, 'template_id');
    requireConfirmation(
      readRequired(data, 'confirmation_phrase'),
      'MERGE',
      'Type MERGE to confirm this action.'
    );
    await mergeMemeTemplate(
      {
        ...apiRequest(fetch, request),
        body: {
          target_template_id: readRequired(data, 'target_template_id'),
          confirmation: templateId,
          note: readRequired(data, 'note')
        }
      },
      templateId
    );
    return { message: 'Template merged and affected meme decisions recorded.' };
  });
}

export async function deleteTemplate({ fetch, request }: RequestEvent) {
  const data = await request.formData();
  return runAction(async () => {
    const templateId = readRequired(data, 'template_id');
    requireConfirmation(
      readRequired(data, 'confirmation_phrase'),
      'DELETE',
      'Type DELETE to confirm this action.'
    );
    await deleteMemeTemplate(
      {
        ...apiRequest(fetch, request),
        body: {
          confirmation: templateId,
          note: null
        }
      },
      templateId
    );
    return { message: 'Unreferenced template deleted.' };
  });
}

function templatePayloadFromForm(data: FormData) {
  return {
    slug: readRequired(data, 'slug'),
    name: readRequired(data, 'name'),
    description: readOptional(data, 'description'),
    is_curated: data.get('is_curated') === 'on',
    base_image_url: readOptional(data, 'base_image_url')
  };
}

export const templateActions = { createTemplate, updateTemplate, mergeTemplate, deleteTemplate };
