import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import type { CurrentSessionRead, MemeLibraryRead, PublicMemeCardRead } from '$lib/api/types';
import ProfilePage from '../../routes/profile/+page.svelte';

describe('/profile page', () => {
  it('renders guest account differences and empty library states', () => {
    const { body } = render(ProfilePage, {
      props: {
        data: {
          session: sessionPayload('guest'),
          sessionError: null,
          library: emptyLibrary(),
          libraryError: null
        }
      }
    });

    expect(body).toContain('Guest library');
    expect(body).toContain('Connect Telegram');
    expect(body).toContain('Guests save into Favorites.');
    expect(body).toContain('To include NSFW results, use the NSFW filter on Search');
    expect(body).toContain('No favorites yet');
    expect(body).toContain('No pinned memes yet');
  });

  it('renders the NSFW disable control when the account preference is enabled', () => {
    const session = sessionPayload('guest');
    session.user.nsfw_enabled = true;
    const { body } = render(ProfilePage, {
      props: {
        data: {
          session,
          sessionError: null,
          library: emptyLibrary(),
          libraryError: null
        }
      }
    });

    expect(body).toContain('NSFW search is enabled.');
    expect(body).toContain('Turn off NSFW');
  });

  it('renders favorites, pins, collections, and active save state', () => {
    const favorites = collection('favorites-id', 'Favorites', true, 1);
    const team = collection('team-id', 'Team saves', true, 2);
    const shared = collection('shared-id', 'Shared jokes', false, 4);
    const { body } = render(ProfilePage, {
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

    expect(body).toContain('Favorite reaction');
    expect(body).toContain('Pinned reply');
    expect(body).toContain('Pin order');
    expect(body).toContain('Up');
    expect(body).toContain('Down');
    expect(body).toContain('Bulk actions');
    expect(body).toContain('Team saves');
    expect(body).toContain('Shared jokes');
    expect(body).toContain('Active save');
    expect(body).toContain('Save into');
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
      is_admin: accountType === 'full',
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
