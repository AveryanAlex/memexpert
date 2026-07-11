import { describe, expect, it, vi } from 'vitest';
import type { ApiFetch } from '$lib/api/client';
import { actions } from '../../../routes/admin/memes/[id]/+page.server';

describe('admin meme detail destructive actions', () => {
  it('requires human phrases and does not call the backend when they are wrong', async () => {
    const fetch = vi.fn(async () => jsonResponse({})) satisfies ApiFetch;

    await expect(runAction('deleteMeme', actionEvent({ confirmation_phrase: 'delete', note: 'cleanup' }, fetch)))
      .resolves.toMatchObject({ status: 400, data: { message: 'Type DELETE to confirm this action.', error: true } });
    await expect(runAction('mergeMeme', actionEvent({ confirmation_phrase: 'DELETE', target_meme_id: 'target', note: 'duplicate' }, fetch)))
      .resolves.toMatchObject({ status: 400, data: { message: 'Type MERGE to confirm this action.', error: true } });
    expect(fetch).not.toHaveBeenCalled();
  });

  it('uses the route meme ID for backend confirmation and redirects deletion to moderation', async () => {
    const sourceId = '11111111-1111-4111-8111-111111111111';
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(new URL(String(input)).pathname).toBe(`/api/v1/admin/memes/${sourceId}`);
      expect(init?.method).toBe('DELETE');
      expect(JSON.parse(String(init?.body))).toEqual({ confirmation: sourceId, note: 'Policy removal' });
      return jsonResponse({ message: 'deleted' });
    }) satisfies ApiFetch;

    await expect(
      runAction('deleteMeme', actionEvent({ confirmation_phrase: 'DELETE', note: 'Policy removal' }, fetch, sourceId))
    ).rejects.toMatchObject({ status: 303, location: '/admin/moderation' });
    expect(fetch).toHaveBeenCalledOnce();
  });

  it('uses MERGE only as human confirmation while retaining the backend UUID contract', async () => {
    const sourceId = '11111111-1111-4111-8111-111111111111';
    const targetId = '22222222-2222-4222-8222-222222222222';
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      expect(new URL(String(input)).pathname).toBe(`/api/v1/admin/memes/${sourceId}/merge`);
      expect(init?.method).toBe('POST');
      expect(JSON.parse(String(init?.body))).toEqual({
        target_meme_id: targetId,
        confirmation: sourceId,
        note: 'Same media'
      });
      return jsonResponse({ message: 'merged' });
    }) satisfies ApiFetch;

    await expect(
      runAction(
        'mergeMeme',
        actionEvent({ confirmation_phrase: 'MERGE', target_meme_id: targetId, note: 'Same media' }, fetch, sourceId)
      )
    ).rejects.toMatchObject({ status: 303, location: `/admin/memes/${targetId}` });
    expect(fetch).toHaveBeenCalledOnce();
  });
});

function runAction(name: 'deleteMeme' | 'mergeMeme', event: ReturnType<typeof actionEvent>) {
  const action = actions[name];
  if (!action) throw new Error(`Missing ${name} action.`);
  return action(event as never);
}

function actionEvent(values: Record<string, string>, fetch: ApiFetch, id = '11111111-1111-4111-8111-111111111111') {
  const formData = new FormData();
  for (const [name, value] of Object.entries(values)) formData.set(name, value);
  return {
    fetch,
    params: { id },
    request: new Request(`http://frontend.test/admin/memes/${id}`, {
      method: 'POST',
      headers: { cookie: 'memexpert_access_token=token' },
      body: formData
    })
  };
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), { status, headers: { 'content-type': 'application/json' } });
}
