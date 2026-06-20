import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import type { CurrentSessionRead, MemeLibraryRead, ProfileStatsRead, PublicMemeCardRead } from '$lib/api/types';
import ProfilePage from '../../routes/profile/+page.svelte';

describe('/profile page', () => {
  it('renders guest account differences and empty library states', () => {
    const { body } = render(ProfilePage, {
      props: {
        data: {
          session: sessionPayload('guest'),
          sessionError: null,
          library: emptyLibrary(),
          libraryError: null,
          profileStats: emptyProfileStats(),
          profileStatsError: null
        }
      }
    });

    expect(body).toContain('Guest library');
    expect(body).toContain('Connect Telegram');
    expect(body).toContain('Telegram');
    expect(body).toContain('Google');
    expect(body).toContain('Email');
    expect(body).toContain('Password not set');
    expect(body).toContain('Google linking is not available from Profile yet.');
    expect(body).toContain('Interaction stats');
    expect(body).not.toContain('Library stats');
    expect(body).toMatch(/Viewed[\s\S]*0/);
    expect(body).toMatch(/Sent[\s\S]*0/);
    expect(body).toMatch(/Saved[\s\S]*0/);
    expect(body).toMatch(/Downloaded[\s\S]*0/);
    expect(body).toMatch(/Days active[\s\S]*0/);
    expect(body).toContain('No interactions yet; stats are zero until this user interacts with memes.');
    expect(body).toContain('Top tags require analytics events with payload.refs.meme_id and tagged meme rows.');
    expect(body).not.toContain('Top tags from your history');
    expect(body).not.toContain('Top templates from your history');
    expect(body).toContain('Language preference');
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
          libraryError: null,
          profileStats: emptyProfileStats(),
          profileStatsError: null
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
    const session = sessionPayload('full');
    session.linked_providers.google_linked = true;
    session.linked_providers.email = 'user@example.com';
    session.linked_providers.email_verified_at = '2026-01-03T00:00:00Z';
    session.linked_providers.has_password = true;
    const { body } = render(ProfilePage, {
      props: {
        data: {
          session,
          sessionError: null,
          library: {
            favorites: [memeCard('11111111-1111-4111-8111-111111111111', 'Favorite reaction')],
            pinned_memes: [memeCard('22222222-2222-4222-8222-222222222222', 'Pinned reply')],
            collections: [favorites, team, shared],
            active_save_collection: team
          },
          libraryError: null,
          profileStats: profileStats(),
          profileStatsError: null
        }
      }
    });

    expect(body).toMatch(/Viewed[\s\S]*12/);
    expect(body).toMatch(/Sent[\s\S]*3/);
    expect(body).toMatch(/Saved[\s\S]*4/);
    expect(body).toMatch(/Downloaded[\s\S]*2/);
    expect(body).toMatch(/Days active[\s\S]*5/);
    expect(body).toContain('#frog');
    expect(body).toContain('Frog Template');
    expect(body).toContain('Google is linked to this account.');
    expect(body).toContain('user@example.com');
    expect(body).toContain('Password set');
    expect(body).toContain('Favorite reaction');
    expect(body).toContain('Pinned reply');
    expect(body).toContain('Pin order');
    expect(body).toContain('data-dnd-sortable="true"');
    expect(body).not.toContain('draggable="true"');
    expect(body).not.toContain('ondragstart');
    expect(body).not.toContain('ondrop');
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

function emptyProfileStats(): ProfileStatsRead {
  return {
    viewed: 0,
    sent: 0,
    saved: 0,
    downloaded: 0,
    days_active: 0,
    top_tags: [],
    top_templates: [],
    metadata: {
      notes: [
        'No interactions yet; stats are zero until this user interacts with memes.',
        'Top tags require analytics events with payload.refs.meme_id and tagged meme rows.',
        'Top templates require analytics events with payload.refs.meme_id and classified template ids.'
      ]
    }
  };
}

function profileStats(): ProfileStatsRead {
  return {
    viewed: 12,
    sent: 3,
    saved: 4,
    downloaded: 2,
    days_active: 5,
    top_tags: [
      { tag: 'frog', count: 7 },
      { tag: 'reaction', count: 5 }
    ],
    top_templates: [
      {
        template_id: '44444444-4444-4444-8444-444444444444',
        slug: 'frog-template',
        name: 'Frog Template',
        count: 5
      }
    ],
    metadata: {
      notes: [
        'Top tags require analytics events with payload.refs.meme_id and tagged meme rows.',
        'Top templates require analytics events with payload.refs.meme_id and classified template ids.'
      ]
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
