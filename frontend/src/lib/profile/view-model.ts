import type { CollectionSummaryRead, CurrentSessionRead, MemeLibraryRead } from '$lib/api/types';

export interface ProfileCapabilityView {
  accountLabel: string;
  persistenceText: string;
  pinText: string;
  collectionText: string;
  showConnectTelegram: boolean;
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
