import type { CollectionSummaryRead, CurrentSessionRead, MemeLibraryRead, UserRead } from '$lib/api/types';

export interface ProfileCapabilityView {
  accountLabel: string;
  persistenceText: string;
  pinText: string;
  collectionText: string;
  showConnectTelegram: boolean;
}

export interface ProfileStatView {
  label: string;
  value: string;
  detail: string;
}

export interface ProfilePreferenceView {
  label: string;
  value: string;
  detail: string;
}

export function profileCapabilities(session: CurrentSessionRead | null): ProfileCapabilityView {
  if (!session) {
    return {
      accountLabel: 'Session unavailable',
      persistenceText: 'Public browsing still works while the account session reconnects.',
      pinText: 'Pins load after your account session is available.',
      collectionText: 'Collections load after your account session is available.',
      showConnectTelegram: false
    };
  }

  if (session.user.account_type === 'full') {
    return {
      accountLabel: 'Connected profile',
      persistenceText: 'Favorites, saves, pins, and active collection follow this connected account.',
      pinText: 'Pins are enabled for quick access.',
      collectionText: 'Custom collections and active save routing are enabled.',
      showConnectTelegram: false
    };
  }

  return {
    accountLabel: 'Guest library',
    persistenceText: 'Favorites stay in this browser session. Connect Telegram to carry them across devices.',
    pinText: 'Pins unlock after connecting Telegram.',
    collectionText: 'Guests save into Favorites only; connected profiles can choose custom collections.',
    showConnectTelegram: !session.linked_providers.telegram_linked
  };
}

export function writableCollectionOptions(library: MemeLibraryRead | null): CollectionSummaryRead[] {
  return library?.collections.filter((collection) => collection.can_write) ?? [];
}

export function activeCollectionId(library: MemeLibraryRead | null): string {
  return library?.active_save_collection?.id ?? '';
}

export function libraryEmptyText(section: 'favorites' | 'pins', session: CurrentSessionRead | null): string {
  if (section === 'favorites') {
    return 'Like or save memes from the catalog and they will appear here.';
  }

  return session?.user.account_type === 'full'
    ? 'Pin favorite reaction memes from any card action menu.'
    : 'Connect Telegram to unlock pinned memes for your profile.';
}

export function profileStats(library: MemeLibraryRead | null): ProfileStatView[] {
  if (!library) {
    return [
      {
        label: 'Library data',
        value: 'Unavailable',
        detail: 'Stats load after the library API responds.'
      }
    ];
  }

  const customCollections = library.collections.filter((collection) => collection.kind === 'custom');
  const writableCollections = library.collections.filter((collection) => collection.can_write);
  const savedTotal = library.collections.reduce((total, collection) => total + collection.saved_meme_count, 0);

  return [
    {
      label: 'Favorites',
      value: String(library.favorites.length),
      detail: library.favorites.length > 0 ? 'Memes you liked or saved to Favorites.' : 'No favorite meme rows yet.'
    },
    {
      label: 'Pins',
      value: String(library.pinned_memes.length),
      detail: library.pinned_memes.length > 0 ? 'Quick-access memes on this profile.' : 'No pinned memes yet.'
    },
    {
      label: 'Collections',
      value: String(library.collections.length),
      detail:
        customCollections.length > 0
          ? `${customCollections.length} custom, ${writableCollections.length} writable.`
          : 'Only Favorites is available until a custom collection exists.'
    },
    {
      label: 'Saved rows',
      value: String(savedTotal),
      detail: savedTotal > 0 ? 'Total saved meme rows across collections.' : 'No saved collection rows yet.'
    }
  ];
}

export function profilePreferences(user: UserRead | null): ProfilePreferenceView[] {
  if (!user) {
    return [
      {
        label: 'Preferences',
        value: 'Unavailable',
        detail: 'Language and NSFW settings load with the account session.'
      }
    ];
  }

  return [
    {
      label: 'Language',
      value: languageLabel(user.language),
      detail: 'Used where account-aware browsing or bot surfaces support a language preference.'
    },
    {
      label: 'NSFW',
      value: user.nsfw_enabled ? 'Enabled' : 'Hidden by default',
      detail: 'Current backend account preference. This web page does not expose a preference mutation endpoint yet.'
    },
    {
      label: 'Account status',
      value: user.status,
      detail: user.guest_expires_at ? `Guest session expires ${formatDate(user.guest_expires_at)}.` : `Created ${formatDate(user.created_at)}.`
    }
  ];
}

function languageLabel(language: UserRead['language']): string {
  switch (language) {
    case 'any':
      return 'Any language';
    case 'en':
      return 'English';
    case 'ru':
      return 'Russian';
    case 'mixed':
      return 'Mixed';
    case 'none':
      return 'No text';
    default:
      return language;
  }
}

function formatDate(value: string): string {
  return new Intl.DateTimeFormat('en', { dateStyle: 'medium' }).format(new Date(value));
}
