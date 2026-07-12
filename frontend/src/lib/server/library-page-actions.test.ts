import type { Cookies } from '@sveltejs/kit';
import { describe, expect, it, vi } from 'vitest';

import type { ApiFetch } from '$lib/api/client';
import { actions } from '../../routes/library/+page.server';

describe('/library createCollection action', () => {
  it('preserves the collection payload, cookie forwarding, and created-collection feedback', async () => {
    const cookies = { set: vi.fn() } as unknown as Cookies;
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const headers = new Headers(init?.headers);

      expect(new URL(String(input)).pathname).toBe('/api/v1/collections');
      expect(init?.method).toBe('POST');
      expect(headers.get('cookie')).toBe('memexpert_access_token=guest');
      expect(headers.get('x-requested-with')).toBe('XMLHttpRequest');
      expect(JSON.parse(String(init?.body))).toEqual({
        title: 'Launch saves',
        description: 'Memes for launch day.',
        visibility: 'unlisted'
      });

      return new Response(JSON.stringify({ collection: { id: 'new-collection-id' } }), {
        headers: {
          'content-type': 'application/json',
          'set-cookie': 'memexpert_access_token=updated; Path=/; HttpOnly; SameSite=Lax'
        }
      });
    }) satisfies ApiFetch;

    await expect(
      createCollectionAction()(actionEvent({ title: 'Launch saves', description: 'Memes for launch day.', visibility: 'unlisted' }, fetch, cookies))
    ).resolves.toEqual({ collectionCreatedId: 'new-collection-id', successMessage: 'Collection created.' });

    expect(cookies.set).toHaveBeenCalledWith(
      'memexpert_access_token',
      'updated',
      expect.objectContaining({ path: '/', httpOnly: true, sameSite: 'lax' })
    );
  });

  it('retains the ApiError status mapping for forbidden collection creation', async () => {
    const result = await createCollectionAction()(
      actionEvent(
        { title: 'Launch saves', description: '', visibility: 'private' },
        (async () => new Response(JSON.stringify({ detail: 'Only connected accounts can create collections.' }), { status: 403 })) satisfies ApiFetch,
        { set: vi.fn() } as unknown as Cookies
      )
    );

    expect(result).toMatchObject({
      status: 403,
      data: { collectionError: 'Only connected accounts can create collections.' }
    });
  });
});

function createCollectionAction() {
  const action = actions.createCollection;
  if (!action) throw new Error('Missing createCollection action.');
  return action;
}

function actionEvent(values: Record<string, string>, fetch: ApiFetch, cookies: Cookies) {
  const formData = new FormData();
  for (const [name, value] of Object.entries(values)) formData.set(name, value);

  return {
    cookies,
    fetch,
    request: new Request('http://frontend.test/library?/createCollection', {
      method: 'POST',
      headers: { cookie: 'memexpert_access_token=guest' },
      body: formData
    })
  } as never;
}
