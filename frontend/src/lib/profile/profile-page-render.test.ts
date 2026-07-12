import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import type { CurrentSessionRead, ProfileStatsRead } from '$lib/api/types';
import ProfilePage from '../../routes/profile/+page.svelte';

describe('/profile page', () => {
  it('renders a Telegram connection, preferences, and compact stats without saved meme grids', () => {
    const { body } = render(ProfilePage, {
      props: {
        data: {
          session: sessionPayload('guest'),
          sessionError: null,
          profileStats: profileStats(),
          profileStatsError: null
        }
      }
    });

    expect(body).toContain('Account');
    expect(body).toContain('Connect Telegram');
    expect(body).toContain('Telegram');
    expect(body).toContain('Language preference');
    expect(body).toContain('Sensitive content');
    expect(body).toContain('Interaction stats');
    expect(body).toContain('<details');
    expect(body).toMatch(/Viewed[\s\S]*12/);
    expect(body).toMatch(/Sent[\s\S]*3/);
    expect(body).not.toContain('Google');
    expect(body).not.toContain('Email');
    expect(body).not.toContain('Password');
    expect(body).not.toContain('No favorites yet');
    expect(body).not.toContain('No pins yet');
    expect(body).not.toContain('Pin order');
    expect(body).not.toContain('Save into');
    expect(body).not.toContain('data-dnd-sortable="true"');
  });

  it('renders the sensitive-content disable control when the account preference is enabled', () => {
    const session = sessionPayload('guest');
    session.user.nsfw_enabled = true;
    const { body } = render(ProfilePage, {
      props: {
        data: {
          session,
          sessionError: null,
          profileStats: emptyProfileStats(),
          profileStatsError: null
        }
      }
    });

    expect(body).toContain('Sensitive content is enabled.');
    expect(body).toContain('Turn off sensitive content');
  });

  it('keeps account controls available when profile stats fail', () => {
    const { body } = render(ProfilePage, {
      props: {
        data: {
          session: sessionPayload('full'),
          sessionError: null,
          profileStats: null,
          profileStatsError: 'Interaction stats are unavailable right now.'
        }
      }
    });

    expect(body).toContain('Telegram connected');
    expect(body).toContain('Language preference');
    expect(body).toContain('Sensitive content');
    expect(body).toContain('Interaction stats are unavailable right now.');
    expect(body).not.toContain('Favorite reaction');
    expect(body).not.toContain('Pinned reply');
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
