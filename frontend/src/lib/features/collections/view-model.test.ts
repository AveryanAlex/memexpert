import { describe, expect, it } from 'vitest';

import type { CurrentSessionRead, WebCollectionDetailRead } from '$lib/api/types';
import {
  collectionAccessSummary,
  collectionInviteRows,
  collectionManagementNotice,
  collectionMemberRows
} from './view-model';

describe('collection management view model', () => {
  it('maps member and invite rows with terminal invite state', () => {
    const detail = collectionDetail();

    expect(collectionMemberRows(detail).map((member) => [member.label, member.role, member.isOwner])).toEqual([
      ['owner-us...0001', 'owner', true],
      ['editor-u...0002', 'editor', false]
    ]);
    expect(collectionMemberRows(detail)[0].joined).toBe('Jan 1, 2026 UTC');
    const inviteRows = collectionInviteRows(detail.collection.invites, new Date('2026-01-03T00:00:00Z'));
    expect(inviteRows.map((invite) => [invite.label, invite.role, invite.statusLabel, invite.usage, invite.isTerminal])).toEqual([
      ['Launch team', 'editor', 'pending', '0/2 used', false],
      ['Used invite', 'viewer', 'accepted', '1/1 used', true]
    ]);
    expect(inviteRows[0]).toMatchObject({ expires: 'Jan 8, 2099 UTC', created: 'Jan 1, 2026 UTC' });
  });

  it('treats the exact invite expiry instant as terminal and keeps the prior instant pending', () => {
    const invite = {
      ...collectionDetail().collection.invites[0],
      expires_at: '2026-01-08T00:00:00.000Z'
    };

    expect(collectionInviteRows([invite], new Date('2026-01-07T23:59:59.999Z'))[0]).toMatchObject({
      statusLabel: 'pending',
      isTerminal: false
    });
    expect(collectionInviteRows([invite], new Date('2026-01-08T00:00:00.000Z'))[0]).toMatchObject({
      statusLabel: 'expired',
      isTerminal: true
    });
    expect(collectionInviteRows([invite], new Date('2026-01-08T00:00:00.001Z'))[0]).toMatchObject({
      statusLabel: 'expired',
      isTerminal: true
    });
  });

  it('formats backend calendar dates in UTC across an offset boundary', () => {
    const invite = {
      ...collectionDetail().collection.invites[0],
      expires_at: '2026-01-01T00:30:00+14:00',
      created_at: '2026-01-01T00:30:00+14:00'
    };

    expect(collectionInviteRows([invite], new Date('2025-12-01T00:00:00Z'))[0]).toMatchObject({
      expires: 'Dec 31, 2025 UTC',
      created: 'Dec 31, 2025 UTC'
    });
  });

  it('explains collaboration boundaries based on role and account state', () => {
    const detail = collectionDetail();

    expect(collectionManagementNotice(detail, sessionPayload('full'))).toContain('Owners can update collection details');
    expect(collectionAccessSummary(detail)).toContain('private custom collection');

    const editor = collectionDetail({
      viewer_role: 'editor',
      capabilities: { ...detail.capabilities, can_manage_members: false, can_rename: false, can_delete: false }
    });
    expect(collectionManagementNotice(editor, sessionPayload('full'))).toContain('Editors can create and revoke');

    const viewOnly = collectionDetail({
      viewer_role: 'viewer',
      capabilities: {
        ...detail.capabilities,
        can_add_memes: false,
        can_create_invites: false,
        can_revoke_invites: false,
        can_manage_members: false
      }
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
          expires_at: '2099-01-08T00:00:00Z',
          last_used_at: null,
          revoked_at: null,
          recipient_email: null,
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-01T00:00:00Z'
        },
        {
          id: 'accepted-invite-id',
          collection_id: 'collection-id',
          created_by_user_id: 'owner-user-0001',
          role: 'viewer',
          channel: 'direct_link',
          label: 'Used invite',
          status: 'accepted',
          max_uses: 1,
          use_count: 1,
          expires_at: null,
          last_used_at: '2026-01-02T00:00:00Z',
          revoked_at: null,
          recipient_email: null,
          created_at: '2026-01-01T00:00:00Z',
          updated_at: '2026-01-02T00:00:00Z'
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
      can_revoke_invites: true,
      can_manage_members: true,
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
