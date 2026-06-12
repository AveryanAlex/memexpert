import { describe, expect, it } from 'vitest';

import type { PublicMemeCardRead, WebCollectionListRead } from '$lib/api/types';
import {
  bulkCollectionOptions,
  bulkDownloadItems,
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
    expect(bulkGuestGuidance('guest', false)).toContain('Guests can bulk-save into Favorites');
    expect(bulkGuestGuidance('full', true)).toBeNull();
    expect(bulkToolbarSummary(12, 0, 0)).toBe('12 memes available for selection.');
    expect(bulkToolbarSummary(12, 2, 1)).toBe('2 selected. 1 has a media URL for download.');
  });
});

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
        can_set_active_save: collection.can_write
      },
      active_save_collection_id: favorite.id
    }))
  };
}
