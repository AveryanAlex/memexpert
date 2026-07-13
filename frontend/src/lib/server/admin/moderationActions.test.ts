import { describe, expect, it, vi } from 'vitest';
import type { ApiFetch } from '$lib/api/client';
import { moderationActions } from './moderationActions';

describe('moderation admin actions', () => {
  it('preserves report resolution and direct override payloads', async () => {
    const reportId = '11111111-1111-4111-8111-111111111111';
    const memeId = '22222222-2222-4222-8222-222222222222';
    const calls: Array<{ path: string; method: string; body: unknown }> = [];
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({
        path: new URL(String(input)).pathname,
        method: init?.method ?? 'GET',
        body: init?.body ? JSON.parse(String(init.body)) : null
      });
      return jsonResponse({});
    }) satisfies ApiFetch;

    await expect(
      moderationActions.resolveModerationReport(
        actionEvent({ report_id: reportId, action: 'hide', reason: 'spam', note: 'Confirmed duplicate spam' }, fetch)
      )
    ).resolves.toEqual({ message: 'Report resolved and decision recorded.' });
    await expect(
      moderationActions.updateMemeModeration(
        actionEvent(
          { meme_id: memeId, visibility_mode: 'force_public', reason: 'other', note: 'Manual review' },
          fetch
        )
      )
    ).resolves.toEqual({ message: 'Meme visibility and safety settings saved.' });

    expect(calls).toEqual([
      {
        path: `/api/v1/admin/moderation-reports/${reportId}/resolve`,
        method: 'POST',
        body: { action: 'hide', reason: 'spam', note: 'Confirmed duplicate spam' }
      },
      {
        path: `/api/v1/admin/memes/${memeId}/moderation`,
        method: 'PATCH',
        body: { is_nsfw: false, visibility_mode: 'force_public', reason: 'other', note: 'Manual review' }
      }
    ]);
  });

  it('maps backend failures to action errors', async () => {
    const result = await moderationActions.resolveModerationReport(
      actionEvent(
        { report_id: '11111111-1111-4111-8111-111111111111', action: 'hide' },
        (async () => jsonResponse({ detail: 'Report is already closed.' }, 409)) satisfies ApiFetch
      )
    );

    expect(result).toMatchObject({ status: 409, data: { message: 'Report is already closed.', error: true } });
  });

  it('rejects malformed forms before making API requests', async () => {
    const fetch = vi.fn(async () => jsonResponse({})) satisfies ApiFetch;

    await expect(
      moderationActions.resolveModerationReport(actionEvent({ action: 'hide' }, fetch))
    ).resolves.toMatchObject({ status: 400, data: { message: 'report_id is required.', error: true } });
    await expect(
      moderationActions.updateMemeModeration(actionEvent({ visibility_mode: 'auto' }, fetch))
    ).resolves.toMatchObject({ status: 400, data: { message: 'meme_id is required.', error: true } });
    await expect(
      moderationActions.updateMemeModeration(
        actionEvent({ meme_id: '22222222-2222-4222-8222-222222222222' }, fetch)
      )
    ).resolves.toMatchObject({ status: 400, data: { message: 'visibility_mode is required.', error: true } });
    expect(fetch).not.toHaveBeenCalled();
  });
});

function actionEvent(values: Record<string, string>, fetch: ApiFetch) {
  const formData = new FormData();
  for (const [name, value] of Object.entries(values)) formData.set(name, value);
  return {
    fetch,
    request: new Request('http://frontend.test/admin/moderation', {
      method: 'POST',
      headers: { cookie: 'memexpert_access_token=token' },
      body: formData
    })
  } as never;
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), { status, headers: { 'content-type': 'application/json' } });
}
