import { describe, expect, it, vi } from 'vitest';
import type { ApiFetch } from '$lib/api/client';
import { actions as templateRouteActions } from '../../../routes/admin/content/templates/+page.server';
import { templateActions } from './templateActions';

describe('template admin actions', () => {
  it('exports the focused action names', () => {
    expect(Object.keys(templateActions)).toEqual(['createTemplate', 'updateTemplate', 'mergeTemplate', 'deleteTemplate']);
    expect(Object.keys(templateRouteActions)).toEqual(['createTemplate', 'updateTemplate', 'mergeTemplate', 'deleteTemplate']);
  });

  it('preserves template endpoint payloads while translating human phrases to backend IDs', async () => {
    const templateId = '11111111-1111-4111-8111-111111111111';
    const targetTemplateId = '22222222-2222-4222-8222-222222222222';
    const calls: Array<{ method: string; path: string; body: unknown }> = [];
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({
        method: init?.method ?? 'GET',
        path: new URL(String(input)).pathname,
        body: init?.body ? JSON.parse(String(init.body)) : null
      });
      return jsonResponse({});
    }) satisfies ApiFetch;

    await expect(
      templateActions.createTemplate(
        actionEvent(
          {
            slug: ' launch-reaction ',
            name: ' Launch reaction ',
            description: ' A familiar launch moment. ',
            is_curated: 'on',
            base_image_url: ' https://images.memexpert.test/launch.jpg '
          },
          fetch
        )
      )
    ).resolves.toEqual({ message: 'Template created.' });
    await expect(
      templateActions.updateTemplate(
        actionEvent(
          {
            template_id: templateId,
            slug: 'launch-reaction',
            name: 'Launch reaction',
            description: '',
            base_image_url: ''
          },
          fetch
        )
      )
    ).resolves.toEqual({ message: 'Template updated.' });
    await expect(
      templateActions.mergeTemplate(
        actionEvent(
          {
            template_id: templateId,
            target_template_id: targetTemplateId,
            confirmation_phrase: 'MERGE',
            note: ' Duplicate catalog entry. '
          },
          fetch
        )
      )
    ).resolves.toEqual({ message: 'Template merged and affected meme decisions recorded.' });
    await expect(
      templateActions.deleteTemplate(
        actionEvent({ template_id: templateId, confirmation_phrase: 'DELETE' }, fetch)
      )
    ).resolves.toEqual({ message: 'Unreferenced template deleted.' });

    expect(calls).toEqual([
      {
        method: 'POST',
        path: '/api/v1/admin/meme-templates',
        body: {
          slug: 'launch-reaction',
          name: 'Launch reaction',
          description: 'A familiar launch moment.',
          is_curated: true,
          base_image_url: 'https://images.memexpert.test/launch.jpg'
        }
      },
      {
        method: 'PATCH',
        path: `/api/v1/admin/meme-templates/${templateId}`,
        body: { slug: 'launch-reaction', name: 'Launch reaction', description: null, is_curated: false, base_image_url: null }
      },
      {
        method: 'POST',
        path: `/api/v1/admin/meme-templates/${templateId}/merge`,
        body: { target_template_id: targetTemplateId, confirmation: templateId, note: 'Duplicate catalog entry.' }
      },
      {
        method: 'DELETE',
        path: `/api/v1/admin/meme-templates/${templateId}`,
        body: { confirmation: templateId, note: null }
      }
    ]);
  });

  it('rejects malformed forms and wrong danger phrases before making requests', async () => {
    const fetch = vi.fn(async () => jsonResponse({})) satisfies ApiFetch;
    const templateId = '11111111-1111-4111-8111-111111111111';
    const targetTemplateId = '22222222-2222-4222-8222-222222222222';

    const malformed = [
      { result: templateActions.createTemplate(actionEvent({ name: 'Template' }, fetch)), message: 'slug is required.' },
      { result: templateActions.updateTemplate(actionEvent({ slug: 'template', name: 'Template' }, fetch)), message: 'template_id is required.' },
      { result: templateActions.mergeTemplate(actionEvent({ template_id: templateId, target_template_id: targetTemplateId, confirmation_phrase: 'merge', note: 'Duplicate' }, fetch)), message: 'Type MERGE to confirm this action.' },
      { result: templateActions.mergeTemplate(actionEvent({ template_id: templateId, target_template_id: targetTemplateId, confirmation_phrase: 'MERGE' }, fetch)), message: 'note is required.' },
      { result: templateActions.deleteTemplate(actionEvent({ template_id: templateId, confirmation_phrase: ' ' }, fetch)), message: 'confirmation_phrase is required.' },
      { result: templateActions.deleteTemplate(actionEvent({ template_id: templateId, confirmation_phrase: 'delete' }, fetch)), message: 'Type DELETE to confirm this action.' }
    ];

    for (const action of malformed) {
      await expect(action.result).resolves.toMatchObject({ status: 400, data: { message: action.message, error: true } });
    }
    expect(fetch).not.toHaveBeenCalled();
  });

  it('maps backend template errors to route action errors', async () => {
    const result = await templateActions.deleteTemplate(
      actionEvent(
        { template_id: '11111111-1111-4111-8111-111111111111', confirmation_phrase: 'DELETE' },
        (async () => jsonResponse({ detail: 'Meme template is still referenced by memes; merge it into a target template first.' }, 409)) satisfies ApiFetch
      )
    );

    expect(result).toMatchObject({
      status: 409,
      data: { message: 'Meme template is still referenced by memes; merge it into a target template first.', error: true }
    });
  });
});

function actionEvent(values: Record<string, string>, fetch: ApiFetch) {
  const formData = new FormData();
  for (const [name, value] of Object.entries(values)) formData.set(name, value);
  return {
    fetch,
    request: new Request('http://frontend.test/admin/content/templates', {
      method: 'POST',
      headers: { cookie: 'memexpert_access_token=token' },
      body: formData
    })
  } as never;
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), { status, headers: { 'content-type': 'application/json' } });
}
