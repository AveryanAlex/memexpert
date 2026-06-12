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
        can_set_active_save: true
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
      props: { data: { session: null, sessionError: null, detail, errorMessage: null } }
    });

    expect(body).toContain('Launch saves');
    expect(body).toContain('Collection details');
    expect(body).toContain('Invite link');
    expect(body).toContain('Danger zone');
    expect(body).toContain('Launch reaction');
    expect(body).toContain('Remove');
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
    collection: {
      id: '11111111-1111-4111-8111-111111111111',
      owner_id: '33333333-3333-4333-8333-333333333333',
      title: 'Launch saves',
      description: 'For launch prep',
      kind: 'custom',
      visibility: 'private',
      memberships: [
        {
          collection_id: '11111111-1111-4111-8111-111111111111',
          user_id: '33333333-3333-4333-8333-333333333333',
          role: 'owner',
          joined_at: '2026-01-01T00:00:00Z'
        }
      ],
      invites: [],
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
    active_save_collection_id: '11111111-1111-4111-8111-111111111111',
    saved_memes: [],
    ...overrides
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
