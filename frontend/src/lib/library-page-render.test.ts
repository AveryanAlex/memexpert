import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import type { CurrentSessionRead, MemeLibraryRead, PublicMemeCardRead } from '$lib/api/types';
import LibraryPage from '../routes/library/+page.svelte';

describe('/library page', () => {
  it('renders Favorites, Collections, Pins, the active save destination, and saved meme grids', () => {
    const favorites = collection('favorites-id', 'Favorites', true, 1);
    const team = collection('team-id', 'Team saves', true, 2);
    const shared = collection('shared-id', 'Shared jokes', false, 4);
    const { body } = render(LibraryPage, {
      props: {
        data: {
          session: sessionPayload('full'),
          sessionError: null,
          library: {
            favorites: [memeCard('11111111-1111-4111-8111-111111111111', 'Favorite reaction')],
            pinned_memes: [memeCard('22222222-2222-4222-8222-222222222222', 'Pinned reply')],
            collections: [favorites, team, shared],
            active_save_collection: team
          },
          libraryError: null
        }
      }
    });

    expect(body).toContain('Favorites');
    expect(body).toContain('Collections');
    expect(body).toContain('Pins');
    expect(body).toContain('href="#favorites"');
    expect(body).toContain('href="#collections"');
    expect(body).toContain('href="#pins"');
    expect(body).toMatch(/href="#favorites"[^>]*focus-visible:outline-accent/);
    expect(body).toContain('<section id="favorites"');
    expect(body).toContain('<section id="collections"');
    expect(body).toContain('<section id="pins"');
    expect(body).toContain('Save into');
    expect(body).toContain('Team saves');
    expect(body).toContain('Active save');
    expect(body).toContain('New collection');
    expect(body).toContain('Create collection');
    expect(body).toContain('action="?/createCollection"');
    expect(body).toContain('name="title"');
    expect(body).toContain('name="description"');
    expect(body).toContain('name="visibility"');
    expect(body).toContain('Shared jokes');
    expect(body).toContain('Favorite reaction');
    expect(body).toContain('Pinned reply');
    expect(body).toContain('role="list"');
    expect(body).toContain('Select items');
    expect(body).toContain('Pin order');
    expect(body).toContain('data-dnd-sortable="true"');
    expect(body).toContain('Up');
    expect(body).toContain('Down');
    expect(body).not.toContain('Google');
    expect(body).not.toContain('Password set');
  });

  it('keeps guest library capabilities and empty states on the saved route', () => {
    const { body } = render(LibraryPage, {
      props: {
        data: {
          session: sessionPayload('guest'),
          sessionError: null,
          library: emptyLibrary(),
          libraryError: null
        }
      }
    });

    expect(body).toContain('No favorites yet');
    expect(body).toContain('No pins yet');
    expect(body).toContain('Connect Telegram');
    expect(body).toContain('Guests save into Favorites.');
    expect(body).not.toContain('New collection');
    expect(body).not.toContain('Create collection');
  });

  it('shows collection creation feedback with a link to the new collection', () => {
    const { body } = render(LibraryPage, {
      props: {
        data: {
          session: sessionPayload('full'),
          sessionError: null,
          library: emptyLibrary(),
          libraryError: null
        },
        form: {
          collectionCreatedId: 'new-collection-id',
          successMessage: 'Collection created.'
        }
      }
    });

    expect(body).toContain('Collection created.');
    expect(body).toContain('Open collection');
    expect(body).toContain('href="/collection/new-collection-id"');
  });

  it('shows a library load error without account provider diagnostics', () => {
    const { body } = render(LibraryPage, {
      props: {
        data: {
          session: sessionPayload('full'),
          sessionError: null,
          library: null,
          libraryError: 'Saved memes are unavailable right now.'
        }
      }
    });

    expect(body).toContain('Saved memes are unavailable right now.');
    expect(body).toContain('Saved');
    expect(body).not.toContain('Google');
    expect(body).not.toContain('Email');
  });
});

function sessionPayload(accountType: 'full' | 'guest'): CurrentSessionRead {
  return {
    user: {
      id: '22222222-2222-4222-8222-222222222222',
      account_type: accountType,
      telegram_id: accountType === 'full' ? 123 : null,
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

function emptyLibrary(): MemeLibraryRead {
  const favorites = collection('favorites-id', 'Favorites', true, 0);
  return {
    favorites: [],
    pinned_memes: [],
    collections: [favorites],
    active_save_collection: favorites
  };
}

function collection(
  id: string,
  title: string,
  canWrite: boolean,
  savedMemeCount: number
): MemeLibraryRead['collections'][number] {
  return {
    id,
    owner_id: '22222222-2222-4222-8222-222222222222',
    title,
    description: null,
    kind: id === 'favorites-id' ? 'favorites' : 'custom',
    visibility: 'private',
    role: canWrite ? 'owner' : 'viewer',
    can_write: canWrite,
    saved_meme_count: savedMemeCount,
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
    like_count: 3,
    tags: ['reaction'],
    primary_file: null,
    caption,
    seo_page_slug: null,
    viewer_has_favorited: false,
    viewer_has_saved: false,
    viewer_has_pinned: false,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z'
  };
}
