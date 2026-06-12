import { describe, expect, it } from 'vitest';

import {
  activeCollectionId,
  libraryEmptyText,
  profileCapabilities,
  profilePreferences,
  profileStats,
  writableCollectionOptions
} from './view-model';
import type { CurrentSessionRead, MemeLibraryRead } from '$lib/api/types';

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

  it('builds honest stats and preference rows from loaded data', () => {
    const session = sessionPayload('guest');
    session.user.nsfw_enabled = true;
    session.user.language = 'ru';

    expect(profileStats(libraryPayload()).map((stat) => [stat.label, stat.value])).toEqual([
      ['Favorites', '0'],
      ['Pins', '0'],
      ['Collections', '3'],
      ['Saved rows', '0']
    ]);
    expect(profilePreferences(session.user).map((preference) => [preference.label, preference.value])).toContainEqual([
      'Language',
      'Russian'
    ]);
    expect(profilePreferences(session.user).map((preference) => [preference.label, preference.value])).toContainEqual(['NSFW', 'Enabled']);
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
