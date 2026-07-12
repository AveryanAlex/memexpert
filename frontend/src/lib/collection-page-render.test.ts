import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import type { PublicMemeCardRead, WebCollectionDetailRead } from '$lib/api/types';
import CollectionPage from '../routes/collection/[id]/+page.svelte';

describe('/collection/[id] page', () => {
  it('renders owner controls and saved memes', () => {
    const detail = collectionDetail({
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
      collection: {
        ...baseCollection(),
        memberships: [ownerMember(), editorMember()],
        invites: [pendingInvite()]
      },
      saved_memes: [
        {
          save: {
            collection_id: '11111111-1111-4111-8111-111111111111',
            meme_id: '22222222-2222-4222-8222-222222222222',
            added_by_user_id: '33333333-3333-4333-8333-333333333333',
            added_at: '2026-01-02T00:00:00Z'
          },
          meme: memeCard('22222222-2222-4222-8222-222222222222', 'Launch reaction')
        }
      ]
    });

    const { body } = render(CollectionPage, {
      props: {
        data: { session: null, sessionError: null, detail, errorMessage: null },
        form: { successMessage: 'Invite link created.', inviteUrl: 'https://memexpert.test/collection/invite/token' }
      }
    });

    expect(body).toContain('Launch saves');
    expect(body).toContain('Save destination');
    expect(body).toContain('action="?/setActive"');
    expect(body).toContain('Invite link created.');
    expect(body).toContain('Manage collection');
    expect(body).toContain('<details');
    expect(body).toContain('<summary');
    expect(body).toContain('Collection details');
    expect(body).toContain('Invite link');
    expect(body).toContain('Copy');
    expect(body).toContain('Revoke');
    expect(body).toContain('Update role');
    expect(body).toContain('Owner transfer and owner removal are not available here.');
    expect(body).toContain('Danger zone');
    expect(body).toContain('Launch reaction');
    expect(body).toContain('Remove');

    const saveDestinationIndex = body.indexOf('Save destination');
    const savedMemeIndex = body.indexOf('Launch reaction');
    const detailsStart = body.indexOf('<details');
    const detailsEnd = body.indexOf('</details>', detailsStart);
    const managementMarkup = body.slice(detailsStart, detailsEnd);

    expect(saveDestinationIndex).toBeLessThan(savedMemeIndex);
    expect(savedMemeIndex).toBeLessThan(detailsStart);
    expect(detailsEnd).toBeGreaterThan(detailsStart);
    expect(managementMarkup).toContain('Collection details');
    expect(managementMarkup).toContain('Create invite');
    expect(managementMarkup).toContain('Update role');
    expect(managementMarkup).toContain('Danger zone');
    expect(managementMarkup).toContain('action="?/update"');
    expect(managementMarkup).toContain('action="?/createInvite"');
    expect(managementMarkup).toContain('action="?/delete"');
    expect(managementMarkup).toContain('action="?/updateMemberRole"');
    expect(managementMarkup).toContain('action="?/removeMember"');
    expect(managementMarkup).toContain('action="?/revokeInvite"');
    expect(managementMarkup).toContain('name="member_user_id"');
    expect(managementMarkup).toContain('name="invite_id"');
  });

  it('renders view-only empty state without owner controls', () => {
    const detail = collectionDetail({
      viewer_role: 'viewer',
      capabilities: {
        can_view: true,
        can_add_memes: false,
        can_remove_memes: false,
        can_rename: false,
        can_delete: false,
        can_create_invites: false,
        can_revoke_invites: false,
        can_manage_members: false,
        can_set_active_save: false
      },
      saved_memes: []
    });

    const { body } = render(CollectionPage, {
      props: { data: { session: null, sessionError: null, detail, errorMessage: null } }
    });

    expect(body).toContain('No saved memes yet');
    expect(body).toContain('viewer');
    expect(body).not.toContain('Danger zone');
    expect(body).not.toContain('Create invite');
    expect(body).not.toContain('Update role');
    expect(body).not.toContain('Revoke');
  });

  it('renders editor invite controls without member management', () => {
    const detail = collectionDetail({
      viewer_role: 'editor',
      capabilities: {
        can_view: true,
        can_add_memes: true,
        can_remove_memes: true,
        can_rename: false,
        can_delete: false,
        can_create_invites: true,
        can_revoke_invites: true,
        can_manage_members: false,
        can_set_active_save: true
      },
      collection: {
        ...baseCollection(),
        memberships: [ownerMember(), editorMember()],
        invites: [pendingInvite()]
      }
    });

    const { body } = render(CollectionPage, {
      props: { data: { session: null, sessionError: null, detail, errorMessage: null } }
    });

    expect(body).toContain('Create invite');
    expect(body).toContain('Revoke');
    expect(body).not.toContain('Collection details');
    expect(body).not.toContain('Update role');
    expect(body).not.toContain('Danger zone');
  });

  it('renders unavailable state gracefully', () => {
    const { body } = render(CollectionPage, {
      props: {
        data: { session: null, sessionError: null, detail: null, errorMessage: 'Collection was not found.' }
      }
    });

    expect(body).toContain('Collection unavailable');
    expect(body).toContain('Collection was not found.');
  });
});

function collectionDetail(overrides: Partial<WebCollectionDetailRead> = {}): WebCollectionDetailRead {
  return {
    collection: baseCollection(),
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
    active_save_collection_id: '11111111-1111-4111-8111-111111111111',
    saved_memes: [],
    ...overrides
  };
}

function baseCollection(): WebCollectionDetailRead['collection'] {
  return {
    id: '11111111-1111-4111-8111-111111111111',
    owner_id: '33333333-3333-4333-8333-333333333333',
    title: 'Launch saves',
    description: 'For launch prep',
    kind: 'custom',
    visibility: 'private',
    memberships: [ownerMember()],
    invites: [],
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-03T00:00:00Z'
  };
}

function ownerMember(): WebCollectionDetailRead['collection']['memberships'][number] {
  return {
    collection_id: '11111111-1111-4111-8111-111111111111',
    user_id: '33333333-3333-4333-8333-333333333333',
    role: 'owner',
    joined_at: '2026-01-01T00:00:00Z'
  };
}

function editorMember(): WebCollectionDetailRead['collection']['memberships'][number] {
  return {
    collection_id: '11111111-1111-4111-8111-111111111111',
    user_id: '44444444-4444-4444-8444-444444444444',
    role: 'editor',
    joined_at: '2026-01-02T00:00:00Z'
  };
}

function pendingInvite(): WebCollectionDetailRead['collection']['invites'][number] {
  return {
    id: '55555555-5555-4555-8555-555555555555',
    collection_id: '11111111-1111-4111-8111-111111111111',
    created_by_user_id: '33333333-3333-4333-8333-333333333333',
    role: 'viewer',
    channel: 'direct_link',
    label: 'Launch invite',
    status: 'pending',
    max_uses: 1,
    use_count: 0,
    expires_at: '2099-01-08T00:00:00Z',
    last_used_at: null,
    revoked_at: null,
    recipient_email: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z'
  };
}

function memeCard(id: string, caption: string): PublicMemeCardRead {
  return {
    id,
    media_type: 'image',
    language: 'en',
    is_nsfw: false,
    popularity_score: 1,
    like_count: 2,
    tags: ['launch'],
    primary_file: null,
    caption,
    seo_page_slug: null,
    viewer_has_favorited: false,
    viewer_has_saved: true,
    viewer_has_pinned: false,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z'
  };
}
