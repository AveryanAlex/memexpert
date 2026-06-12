import { describe, expect, it } from 'vitest';

import type { CurrentSessionRead, WebCollectionDetailRead } from '$lib/api/types';
import {
  collectionAccessSummary,
  collectionInviteRows,
  collectionManagementNotice,
  collectionMemberRows
} from './view-model';

describe('collection management view model', () => {
  it('maps member and invite rows without unsupported mutations', () => {
    const detail = collectionDetail();

    expect(collectionMemberRows(detail).map((member) => [member.label, member.role, member.isOwner])).toEqual([
      ['owner-us...0001', 'owner', true],
      ['editor-u...0002', 'editor', false]
    ]);
    expect(collectionInviteRows(detail.collection.invites).map((invite) => [invite.label, invite.role, invite.usage])).toEqual([
      ['Launch team', 'editor', '0/2 used']
    ]);
  });

  it('explains collaboration boundaries based on role and account state', () => {
    const detail = collectionDetail();

    expect(collectionManagementNotice(detail, sessionPayload('full'))).toContain('Create direct invite links');
    expect(collectionAccessSummary(detail)).toContain('private custom collection');

    const viewOnly = collectionDetail({
      viewer_role: 'viewer',
      capabilities: { ...detail.capabilities, can_add_memes: false, can_create_invites: false }
    });
    expect(collectionManagementNotice(viewOnly, sessionPayload('guest'))).toContain('Connect Telegram');
  });
});

function collectionDetail(overrides: Partial<WebCollectionDetailRead> = {}): WebCollectionDetailRead {
  return {
    collection: {
      id: 'collection-id',
      owner_id: 'owner-user-0001',
      title: 'Launch saves',
      description: 'For launch prep',
      kind: 'custom',
      visibility: 'private',
      memberships: [
        { collection_id: 'collection-id', user_id: 'owner-user-0001', role: 'owner', joined_at: '2026-01-01T00:00:00Z' },
        { collection_id: 'collection-id', user_id: 'editor-user-0002', role: 'editor', joined_at: '2026-01-02T00:00:00Z' }
      ],
      invites: [
        {
          id: 'invite-id',
          collection_id: 'collection-id',
          created_by_user_id: 'owner-user-0001',
          role: 'editor',
          channel: 'direct_link',
          label: 'Launch team',
          status: 'pending',
          max_uses: 2,
          use_count: 0,
          expires_at: '2026-01-08T00:00:00Z',
          last_used_at: null,
          revoked_at: null,
          recipient_email: null,
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z'
        }
      ],
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-03T00:00:00Z'
    },
    viewer_role: 'owner',
    capabilities: {
      can_view: true,
      can_add_memes: true,
      can_remove_memes: true,
      can_rename: true,
      can_delete: true,
      can_create_invites: true,
      can_set_active_save: true
    },
    active_save_collection_id: 'collection-id',
    saved_memes: [],
    ...overrides
  };
}

function sessionPayload(accountType: 'full' | 'guest'): CurrentSessionRead {
  return {
    user: {
      id: 'user-id',
      account_type: accountType,
      telegram_id: accountType === 'full' ? 1 : null,
      google_id: null,
      email: null,
      email_verified_at: null,
      language: 'any',
      nsfw_enabled: false,
      token_nonce: 0,
      status: 'active',
      guest_expires_at: accountType === 'guest' ? '2026-07-12T00:00:00Z' : null,
      active_save_collection_id: null,
      is_admin: false,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z'
    },
    linked_providers: {
      email: null,
      email_verified_at: null,
      has_password: false,
      google_linked: false,
      telegram_linked: accountType === 'full'
    }
  };
}
