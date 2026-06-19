import { describe, expect, it } from 'vitest';

import type { CurrentSessionRead, PublicMemeCardRead, WebCollectionListRead } from '$lib/api/types';
import {
  bulkCollectionOptions,
  bulkDownloadItems,
  bulkGuidanceFromSessionAndCollections,
  bulkGuestGuidance,
  bulkToolbarSummary,
  collectionListBulkOptions,
  selectedMemes
} from './bulk-view-model';

describe('bulk meme grid view model', () => {
  it('filters selected memes in source order', () => {
    const memes = [memeCard('meme-1'), memeCard('meme-2'), memeCard('meme-3')];

    expect(selectedMemes(memes, ['meme-3', 'meme-1']).map((meme) => meme.id)).toEqual(['meme-1', 'meme-3']);
  });

  it('uses download or render URLs for bulk downloads', () => {
    const items = bulkDownloadItems([
      memeCard('with-download', { download_url: 'https://cdn.test/download.jpg' }),
      memeCard('with-render', { render_url: 'https://cdn.test/render.jpg' }),
      memeCard('without-url')
    ]);

    expect(items.map((item) => [item.id, item.url])).toEqual([
      ['with-download', 'https://cdn.test/download.jpg'],
      ['with-render', 'https://cdn.test/render.jpg']
    ]);
  });

  it('maps writable collection options from library and collection list payloads', () => {
    expect(
      bulkCollectionOptions([
        collectionSummary('favorites', 'Favorites', true),
        collectionSummary('viewer', 'Shared', false)
      ]).map((collection) => collection.title)
    ).toEqual(['Favorites']);

    expect(collectionListBulkOptions(collectionList()).map((collection) => collection.title)).toEqual(['Favorites', 'Team']);
  });

  it('summarizes guest boundaries and selected downloads', () => {
    expect(bulkGuestGuidance(sessionPayload('guest'), false)).toContain('Guests can bulk-save into Favorites');
    expect(bulkGuestGuidance(sessionPayload('full'), true)).toBeNull();
    expect(bulkGuidanceFromSessionAndCollections(sessionPayload('guest'), bulkCollectionOptions([collectionSummary('favorites', 'Favorites', true)]))).toContain(
      'Guests can bulk-save into Favorites'
    );
    expect(bulkToolbarSummary(12, 0, 0)).toBe('12 memes available for selection.');
    expect(bulkToolbarSummary(12, 2, 1)).toBe('2 selected. 1 has a media URL for download.');
  });
});

function sessionPayload(accountType: 'full' | 'guest'): CurrentSessionRead {
  return {
    user: {
      id: 'user-id',
      account_type: accountType,
      telegram_id: accountType === 'full' ? 123 : null,
      google_id: null,
      email: accountType === 'full' ? 'user@example.com' : null,
      email_verified_at: null,
      language: 'en',
      nsfw_enabled: false,
      token_nonce: 1,
      status: 'active',
      guest_expires_at: accountType === 'guest' ? '2026-07-12T00:00:00Z' : null,
      active_save_collection_id: 'favorites',
      is_admin: false,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z'
    },
    linked_providers: {
      email: accountType === 'full' ? 'user@example.com' : null,
      email_verified_at: null,
      has_password: false,
      google_linked: false,
      telegram_linked: accountType === 'full'
    }
  };
}

function memeCard(id: string, overrides: Partial<PublicMemeCardRead> = {}): PublicMemeCardRead {
  return {
    id,
    media_type: 'image',
    language: 'en',
    is_nsfw: false,
    popularity_score: 1,
    like_count: 1,
    tags: ['reaction'],
    primary_file: null,
    caption: id,
    seo_page_slug: null,
    viewer_has_favorited: false,
    viewer_has_saved: false,
    viewer_has_pinned: false,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides
  };
}

function collectionSummary(id: string, title: string, canWrite: boolean) {
  return {
    id,
    owner_id: 'owner-id',
    title,
    description: null,
    kind: id === 'favorites' ? ('favorites' as const) : ('custom' as const),
    visibility: 'private' as const,
    role: canWrite ? ('owner' as const) : ('viewer' as const),
    can_write: canWrite,
    saved_meme_count: 0,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z'
  };
}

function collectionList(): WebCollectionListRead {
  const favorite = collectionSummary('favorites', 'Favorites', true);
  const shared = collectionSummary('shared', 'Shared', false);
  const team = collectionSummary('team', 'Team', true);
  return {
    active_save_collection_id: favorite.id,
    collections: [favorite, shared, team].map((collection) => ({
      collection: {
        id: collection.id,
        owner_id: collection.owner_id,
        title: collection.title,
        description: collection.description,
        kind: collection.kind,
        visibility: collection.visibility,
        memberships: [],
        invites: [],
        created_at: collection.created_at,
        updated_at: collection.updated_at
      },
      viewer_role: collection.role,
      capabilities: {
        can_view: true,
        can_add_memes: collection.can_write,
        can_remove_memes: collection.can_write,
        can_rename: collection.role === 'owner',
        can_delete: collection.role === 'owner',
        can_create_invites: collection.role === 'owner',
        can_revoke_invites: collection.can_write,
        can_manage_members: collection.role === 'owner',
        can_set_active_save: collection.can_write
      },
      active_save_collection_id: favorite.id
    }))
  };
}
