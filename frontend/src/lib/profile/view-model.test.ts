import { describe, expect, it } from 'vitest';

import {
  activeCollectionId,
  libraryEmptyText,
  movePinnedMemeId,
  movePinnedMemeIdToTarget,
  orderPinnedMemesByIds,
  profileCapabilities,
  profilePreferences,
  profileProviderStatuses,
  profileStats,
  writableCollectionOptions
} from './view-model';
import type { CurrentSessionRead, MemeLibraryRead, ProfileStatsRead } from '$lib/api/types';

describe('profile view model', () => {
  it('shows guest differences and Telegram CTA', () => {
    const capabilities = profileCapabilities(sessionPayload('guest'));

    expect(capabilities.accountLabel).toBe('Guest library');
    expect(capabilities.persistenceText).toContain('Connect Telegram');
    expect(capabilities.pinText).toContain('unlock');
    expect(capabilities.showConnectTelegram).toBe(true);
    expect(libraryEmptyText('pins', sessionPayload('guest'))).toContain('Connect Telegram');
  });

  it('shows full account capabilities without connect CTA', () => {
    const capabilities = profileCapabilities(sessionPayload('full'));

    expect(capabilities.accountLabel).toBe('Connected profile');
    expect(capabilities.collectionText).toContain('Custom collections');
    expect(capabilities.showConnectTelegram).toBe(false);
    expect(libraryEmptyText('pins', sessionPayload('full'))).toContain('Pin favorite');
  });

  it('maps active and writable collection selector support', () => {
    const library = libraryPayload();

    expect(activeCollectionId(library)).toBe('favorites-id');
    expect(writableCollectionOptions(library).map((collection) => collection.title)).toEqual(['Favorites', 'Team']);
  });

  it('builds explicit provider status rows without unsupported linking CTAs', () => {
    const statuses = profileProviderStatuses(sessionPayload('guest'));

    expect(statuses.map((status) => [status.label, status.value])).toEqual([
      ['Telegram', 'Not connected'],
      ['Google', 'Not connected'],
      ['Email', 'No email on file'],
      ['Password', 'Password not set']
    ]);
    expect(statuses.find((status) => status.label === 'Telegram')?.detail).toContain('Connect Telegram');
    expect(statuses.find((status) => status.label === 'Google')?.detail).toContain('not available from Profile');
  });

  it('builds interaction stats and preference rows from loaded data', () => {
    const session = sessionPayload('guest');
    session.user.nsfw_enabled = true;
    session.user.language = 'ru';

    expect(profileStats(profileStatsPayload()).map((stat) => [stat.label, stat.value])).toEqual([
      ['Viewed', '12'],
      ['Sent', '3'],
      ['Saved', '4'],
      ['Downloaded', '2'],
      ['Days active', '5']
    ]);
    expect(profilePreferences(session.user).map((preference) => [preference.label, preference.value])).toContainEqual([
      'Language',
      'Russian'
    ]);
    expect(profilePreferences(session.user).map((preference) => [preference.label, preference.value])).toContainEqual(['NSFW', 'Enabled']);
    expect(profilePreferences(session.user).find((preference) => preference.label === 'NSFW')?.detail).toContain('Search can include');
  });

  it('builds zero-count stats from empty interaction history', () => {
    const rows = profileStats({ ...profileStatsPayload(), viewed: 0, sent: 0, saved: 0, downloaded: 0, days_active: 0 });

    expect(rows.map((stat) => [stat.label, stat.value])).toEqual([
      ['Viewed', '0'],
      ['Sent', '0'],
      ['Saved', '0'],
      ['Downloaded', '0'],
      ['Days active', '0']
    ]);
    expect(rows.every((stat) => stat.detail.includes('No ') || stat.detail.includes('No active'))).toBe(true);
  });

  it('points disabled NSFW users to the search confirmation flow', () => {
    const nsfwPreference = profilePreferences(sessionPayload('guest').user).find((preference) => preference.label === 'NSFW');

    expect(nsfwPreference?.value).toBe('Hidden by default');
    expect(nsfwPreference?.detail).toContain('Enable from the Search NSFW filter confirmation');
  });

  it('reorders pinned meme ids for button and drag controls', () => {
    expect(movePinnedMemeId(['a', 'b', 'c'], 'b', -1)).toEqual(['b', 'a', 'c']);
    expect(movePinnedMemeId(['a', 'b', 'c'], 'a', -1)).toEqual(['a', 'b', 'c']);
    expect(movePinnedMemeIdToTarget(['a', 'b', 'c'], 'c', 'a')).toEqual(['c', 'a', 'b']);
    expect(orderPinnedMemesByIds([meme('a'), meme('b'), meme('c')], ['c', 'a']).map((item) => item.id)).toEqual(['c', 'a', 'b']);
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

function libraryPayload(): MemeLibraryRead {
  const favorites = collection('favorites-id', 'Favorites', true);
  return {
    favorites: [],
    pinned_memes: [],
    collections: [favorites, collection('viewer-id', 'Shared', false), collection('team-id', 'Team', true)],
    active_save_collection: favorites
  };
}

function profileStatsPayload(): ProfileStatsRead {
  return {
    viewed: 12,
    sent: 3,
    saved: 4,
    downloaded: 2,
    days_active: 5,
    top_tags: [{ tag: 'frog', count: 7 }],
    top_templates: [
      {
        template_id: '44444444-4444-4444-8444-444444444444',
        slug: 'frog-template',
        name: 'Frog Template',
        count: 5
      }
    ],
    metadata: { notes: ['Top tags require tagged meme rows.'] }
  };
}

function collection(id: string, title: string, canWrite: boolean): MemeLibraryRead['collections'][number] {
  return {
    id,
    owner_id: '22222222-2222-4222-8222-222222222222',
    title,
    description: null,
    kind: id === 'favorites-id' ? 'favorites' : 'custom',
    visibility: 'private',
    role: canWrite ? 'owner' : 'viewer',
    can_write: canWrite,
    saved_meme_count: 0,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z'
  };
}

function meme(id: string): MemeLibraryRead['pinned_memes'][number] {
  return {
    id,
    media_type: 'image',
    language: 'en',
    is_nsfw: false,
    popularity_score: 1,
    like_count: 0,
    tags: [],
    primary_file: null,
    caption: id,
    seo_page_slug: null,
    viewer_has_favorited: false,
    viewer_has_saved: false,
    viewer_has_pinned: true,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z'
  };
}
