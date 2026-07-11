import { describe, expect, it, vi } from 'vitest';
import type { ApiFetch } from '$lib/api/client';
import * as rootAdminPageServer from '../../../routes/admin/+page.server';
import { blockedPatternActions } from './blockedPatternActions';

describe('blocked-pattern admin actions', () => {
  it('exports the focused action names and removes them from the overview route', () => {
    expect(Object.keys(blockedPatternActions)).toEqual([
      'createBlockedPerceptualHash',
      'updateBlockedPerceptualHash',
      'deactivateBlockedPerceptualHash',
      'reactivateBlockedPerceptualHash',
      'deleteBlockedPerceptualHash'
    ]);
    expect(rootAdminPageServer).not.toHaveProperty('actions');
  });

  it('preserves exact create, update, lifecycle, and delete endpoint payloads', async () => {
    const blockedHashId = '11111111-1111-4111-8111-111111111111';
    const calls: Array<{ method: string; path: string; body: unknown }> = [];
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({
        method: init?.method ?? 'GET',
        path: new URL(String(input)).pathname,
        body: init?.body ? JSON.parse(String(init.body)) : null
      });
      return jsonResponse({ action: 'delete' });
    }) satisfies ApiFetch;

    await expect(
      blockedPatternActions.createBlockedPerceptualHash(
        actionEvent(
          {
            perceptual_hash: ' ABCDEF1234567890 ',
            hash_algorithm: 'phash',
            max_hamming_distance: '2',
            reason: 'spam',
            note: ' seed ban ',
            is_active: 'on'
          },
          fetch
        )
      )
    ).resolves.toEqual({ message: 'Blocked media pattern created.' });
    await expect(
      blockedPatternActions.updateBlockedPerceptualHash(
        actionEvent(
          {
            blocked_hash_id: blockedHashId,
            perceptual_hash: 'abcdef1234567891',
            hash_algorithm: 'phash',
            max_hamming_distance: '3',
            reason: 'copyright',
            note: ' tightened pattern ',
            is_active: 'on'
          },
          fetch
        )
      )
    ).resolves.toEqual({ message: 'Blocked media pattern updated.' });
    await expect(
      blockedPatternActions.updateBlockedPerceptualHash(
        actionEvent(
          {
            blocked_hash_id: blockedHashId,
            perceptual_hash: 'abcdef1234567892',
            hash_algorithm: 'phash',
            max_hamming_distance: '0',
            reason: 'other'
          },
          fetch
        )
      )
    ).resolves.toEqual({ message: 'Blocked media pattern updated.' });
    await expect(
      blockedPatternActions.deactivateBlockedPerceptualHash(
        actionEvent(
          { blocked_hash_id: blockedHashId, confirmation_phrase: 'DEACTIVATE', note: ' temporary pause ' },
          fetch
        )
      )
    ).resolves.toEqual({ message: 'Blocked media pattern deactivated.' });
    await expect(
      blockedPatternActions.reactivateBlockedPerceptualHash(
        actionEvent({ blocked_hash_id: blockedHashId, confirmation_phrase: 'REACTIVATE' }, fetch)
      )
    ).resolves.toEqual({ message: 'Blocked media pattern reactivated.' });
    await expect(
      blockedPatternActions.deleteBlockedPerceptualHash(
        actionEvent({ blocked_hash_id: blockedHashId, confirmation_phrase: 'DELETE' }, fetch)
      )
    ).resolves.toEqual({ message: 'Blocked media pattern deleted; audit history preserved.' });

    expect(calls).toEqual([
      {
        method: 'POST',
        path: '/api/v1/admin/blocked-perceptual-hashes',
        body: {
          perceptual_hash: 'abcdef1234567890',
          hash_algorithm: 'phash',
          hash_size: 64,
          max_hamming_distance: 2,
          reason: 'spam',
          note: 'seed ban',
          is_active: true
        }
      },
      {
        method: 'PATCH',
        path: `/api/v1/admin/blocked-perceptual-hashes/${blockedHashId}`,
        body: {
          perceptual_hash: 'abcdef1234567891',
          hash_algorithm: 'phash',
          hash_size: 64,
          max_hamming_distance: 3,
          reason: 'copyright',
          note: 'tightened pattern',
          is_active: true
        }
      },
      {
        method: 'PATCH',
        path: `/api/v1/admin/blocked-perceptual-hashes/${blockedHashId}`,
        body: {
          perceptual_hash: 'abcdef1234567892',
          hash_algorithm: 'phash',
          hash_size: 64,
          max_hamming_distance: 0,
          reason: 'other',
          note: null,
          is_active: false
        }
      },
      {
        method: 'POST',
        path: `/api/v1/admin/blocked-perceptual-hashes/${blockedHashId}/deactivate`,
        body: { note: 'temporary pause' }
      },
      {
        method: 'PATCH',
        path: `/api/v1/admin/blocked-perceptual-hashes/${blockedHashId}`,
        body: { is_active: true }
      },
      {
        method: 'DELETE',
        path: `/api/v1/admin/blocked-perceptual-hashes/${blockedHashId}`,
        body: null
      }
    ]);
  });

  it('rejects malformed forms plus blank and wrong lifecycle confirmations without making requests', async () => {
    const fetch = vi.fn(async () => jsonResponse({})) satisfies ApiFetch;

    await expect(
      blockedPatternActions.createBlockedPerceptualHash(actionEvent({ reason: 'spam' }, fetch))
    ).resolves.toMatchObject({ status: 400, data: { message: 'perceptual_hash is required.', error: true } });
    await expect(
      blockedPatternActions.updateBlockedPerceptualHash(
        actionEvent({ perceptual_hash: 'abcdef1234567890', reason: 'spam' }, fetch)
      )
    ).resolves.toMatchObject({ status: 400, data: { message: 'blocked_hash_id is required.', error: true } });
    await expect(
      blockedPatternActions.deactivateBlockedPerceptualHash(
        actionEvent({ blocked_hash_id: 'pattern-id', confirmation_phrase: ' ' }, fetch)
      )
    ).resolves.toMatchObject({ status: 400, data: { message: 'confirmation_phrase is required.', error: true } });
    await expect(
      blockedPatternActions.deactivateBlockedPerceptualHash(
        actionEvent({ blocked_hash_id: 'pattern-id', confirmation_phrase: 'deactivate' }, fetch)
      )
    ).resolves.toMatchObject({ status: 400, data: { message: 'Type DEACTIVATE to confirm this action.', error: true } });
    await expect(
      blockedPatternActions.deleteBlockedPerceptualHash(
        actionEvent({ blocked_hash_id: 'pattern-id', confirmation_phrase: ' ' }, fetch)
      )
    ).resolves.toMatchObject({ status: 400, data: { message: 'confirmation_phrase is required.', error: true } });
    await expect(
      blockedPatternActions.deleteBlockedPerceptualHash(
        actionEvent({ blocked_hash_id: 'pattern-id', confirmation_phrase: 'delete' }, fetch)
      )
    ).resolves.toMatchObject({ status: 400, data: { message: 'Type DELETE to confirm this action.', error: true } });
    await expect(
      blockedPatternActions.reactivateBlockedPerceptualHash(actionEvent({ confirmation_phrase: 'REACTIVATE' }, fetch))
    ).resolves.toMatchObject({ status: 400, data: { message: 'blocked_hash_id is required.', error: true } });
    await expect(
      blockedPatternActions.reactivateBlockedPerceptualHash(
        actionEvent({ blocked_hash_id: 'pattern-id', confirmation_phrase: ' ' }, fetch)
      )
    ).resolves.toMatchObject({ status: 400, data: { message: 'confirmation_phrase is required.', error: true } });
    await expect(
      blockedPatternActions.reactivateBlockedPerceptualHash(
        actionEvent({ blocked_hash_id: 'pattern-id', confirmation_phrase: 'reactivate' }, fetch)
      )
    ).resolves.toMatchObject({ status: 400, data: { message: 'Type REACTIVATE to confirm this action.', error: true } });
    expect(fetch).not.toHaveBeenCalled();
  });

  it('maps backend conflicts to the route action error response', async () => {
    const result = await blockedPatternActions.updateBlockedPerceptualHash(
      actionEvent(
        {
          blocked_hash_id: '11111111-1111-4111-8111-111111111111',
          perceptual_hash: 'abcdef1234567890',
          hash_algorithm: 'phash',
          max_hamming_distance: '0',
          reason: 'spam',
          is_active: 'on'
        },
        (async () =>
          jsonResponse({ detail: 'Blocked perceptual hash already exists for that algorithm and hash size.' }, 409)) satisfies ApiFetch
      )
    );

    expect(result).toMatchObject({
      status: 409,
      data: { message: 'Blocked perceptual hash already exists for that algorithm and hash size.', error: true }
    });
  });
});

function actionEvent(values: Record<string, string>, fetch: ApiFetch) {
  const formData = new FormData();
  for (const [name, value] of Object.entries(values)) formData.set(name, value);
  return {
    fetch,
    request: new Request('http://frontend.test/admin/moderation/patterns', {
      method: 'POST',
      headers: { cookie: 'memexpert_access_token=token' },
      body: formData
    })
  } as never;
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), { status, headers: { 'content-type': 'application/json' } });
}
