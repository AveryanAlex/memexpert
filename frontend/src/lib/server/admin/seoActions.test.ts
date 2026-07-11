import { describe, expect, it, vi } from 'vitest';
import type { ApiFetch } from '$lib/api/client';
import { load as contentRedirectLoad } from '../../../routes/admin/content/+page.server';
import { actions as seoRouteActions } from '../../../routes/admin/content/seo/+page.server';
import { seoActions } from './seoActions';

describe('SEO admin actions', () => {
  it('exports the focused action names and redirects the content index to SEO', async () => {
    expect(Object.keys(seoActions)).toEqual(['updateSeoPage', 'regenerateSeoPage']);
    expect(Object.keys(seoRouteActions)).toEqual(['updateSeoPage', 'regenerateSeoPage']);
    await expect(Promise.resolve().then(() => contentRedirectLoad({} as never))).rejects.toMatchObject({
      status: 303,
      location: '/admin/content/seo'
    });
  });

  it('preserves page-two named-action behavior and endpoint payloads while sending the meme ID internally', async () => {
    const memeId = '11111111-1111-4111-8111-111111111111';
    const calls: Array<{ method: string; path: string; body: unknown }> = [];
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({
        method: init?.method ?? 'GET',
        path: new URL(String(input)).pathname,
        body: init?.body ? JSON.parse(String(init.body)) : null
      });
      return jsonResponse({});
    }) satisfies ApiFetch;

    const updateEvent = actionEvent(
      {
        meme_id: memeId,
        slug: ' launch-reaction ',
        page_title: ' Launch Reaction Meme ',
        meta_description: ' A launch reaction meme for search. ',
        alt_text: ' A reaction about launch day. ',
        caption: ' Launch day mood ',
        body_text: ' Longer search copy. ',
        tags: ' launch, reaction '
      },
      fetch,
      '?page=2&/updateSeoPage'
    );
    const regenerateEvent = actionEvent(
      { meme_id: memeId, confirmation_phrase: 'REGENERATE' },
      fetch,
      '?page=2&/regenerateSeoPage'
    );
    expect(new URL(updateEvent.request.url).search).toBe('?page=2&/updateSeoPage');
    expect(new URL(regenerateEvent.request.url).search).toBe('?page=2&/regenerateSeoPage');

    await expect(seoActions.updateSeoPage(updateEvent)).resolves.toEqual({ message: 'SEO page saved.' });
    await expect(
      seoActions.regenerateSeoPage(regenerateEvent)
    ).resolves.toEqual({ message: 'SEO page regenerated and manual edits were overwritten.' });

    expect(calls).toEqual([
      {
        method: 'PATCH',
        path: `/api/v1/admin/memes/${memeId}/seo-page`,
        body: {
          slug: 'launch-reaction',
          page_title: 'Launch Reaction Meme',
          meta_description: 'A launch reaction meme for search.',
          alt_text: 'A reaction about launch day.',
          caption: 'Launch day mood',
          body_text: 'Longer search copy.',
          tags: 'launch, reaction'
        }
      },
      {
        method: 'POST',
        path: `/api/v1/admin/memes/${memeId}/seo-page/regenerate`,
        body: { confirmation: memeId }
      }
    ]);
  });

  it('rejects malformed forms and wrong regeneration phrases before making requests', async () => {
    const fetch = vi.fn(async () => jsonResponse({})) satisfies ApiFetch;
    const memeId = '11111111-1111-4111-8111-111111111111';

    const malformed = [
      { result: seoActions.updateSeoPage(actionEvent({ page_title: 'Title' }, fetch)), message: 'meme_id is required.' },
      { result: seoActions.updateSeoPage(actionEvent({ meme_id: memeId, page_title: 'Title', meta_description: 'Description', alt_text: 'Alt text' }, fetch)), message: 'slug is required.' },
      { result: seoActions.updateSeoPage(actionEvent({ meme_id: memeId, slug: 'slug', meta_description: 'Description', alt_text: 'Alt text' }, fetch)), message: 'page_title is required.' },
      { result: seoActions.regenerateSeoPage(actionEvent({ confirmation_phrase: 'REGENERATE' }, fetch)), message: 'meme_id is required.' },
      { result: seoActions.regenerateSeoPage(actionEvent({ meme_id: memeId, confirmation_phrase: ' ' }, fetch)), message: 'confirmation_phrase is required.' },
      { result: seoActions.regenerateSeoPage(actionEvent({ meme_id: memeId, confirmation_phrase: 'regenerate' }, fetch)), message: 'Type REGENERATE to confirm this action.' }
    ];

    for (const action of malformed) {
      await expect(action.result).resolves.toMatchObject({ status: 400, data: { message: action.message, error: true } });
    }
    expect(fetch).not.toHaveBeenCalled();
  });

  it('maps backend SEO errors to route action errors', async () => {
    const conflict = await seoActions.updateSeoPage(
      actionEvent(
        {
          meme_id: '11111111-1111-4111-8111-111111111111',
          slug: 'duplicate-title',
          page_title: 'Duplicate title',
          meta_description: 'Description',
          alt_text: 'Alt text'
        },
        (async () => jsonResponse({ detail: 'SEO page slug already exists.' }, 409)) satisfies ApiFetch
      )
    );

    expect(conflict).toMatchObject({ status: 409, data: { message: 'SEO page slug already exists.', error: true } });

    const validation = await seoActions.updateSeoPage(
      actionEvent(
        {
          meme_id: '11111111-1111-4111-8111-111111111111',
          slug: 'valid-slug',
          page_title: 'Valid title',
          meta_description: 'Description',
          alt_text: 'Alt text'
        },
        (async () => jsonResponse({ detail: [{ loc: ['body', 'slug'], msg: 'Slug is already in use' }] }, 422)) satisfies ApiFetch
      )
    );

    expect(validation).toMatchObject({ status: 422, data: { message: 'slug: Slug is already in use', error: true } });
  });
});

function actionEvent(values: Record<string, string>, fetch: ApiFetch, search = ''): { fetch: ApiFetch; request: Request } {
  const formData = new FormData();
  for (const [name, value] of Object.entries(values)) formData.set(name, value);
  return {
    fetch,
    request: new Request(`http://frontend.test/admin/content/seo${search}`, {
      method: 'POST',
      headers: { cookie: 'memexpert_access_token=token' },
      body: formData
    })
  };
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), { status, headers: { 'content-type': 'application/json' } });
}
