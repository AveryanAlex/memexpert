import type { CollectionSummaryRead, CurrentSessionRead, MemeLibraryRead, ProfileStatsRead, PublicMemeCardRead, UserRead } from '$lib/api/types';

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

export interface ProfileProviderStatusView {
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

export function profileProviderStatuses(session: CurrentSessionRead | null): ProfileProviderStatusView[] {
  if (!session) {
    return ['Telegram', 'Google', 'Email', 'Password'].map((label) => ({
      label,
      value: 'Unavailable',
      detail: 'Provider status loads with the account session.'
    }));
  }

  const providers = session.linked_providers;

  return [
    {
      label: 'Telegram',
      value: providers.telegram_linked ? 'Connected' : 'Not connected',
      detail: providers.telegram_linked
        ? 'Telegram is linked for cross-device account access.'
        : session.user.account_type === 'guest'
          ? 'Connect Telegram to keep this guest library across devices.'
          : 'Telegram is not linked to this account.'
    },
    {
      label: 'Google',
      value: providers.google_linked ? 'Connected' : 'Not connected',
      detail: providers.google_linked ? 'Google is linked to this account.' : 'Google linking is not available from Profile yet.'
    },
    {
      label: 'Email',
      value: providers.email ? (providers.email_verified_at ? 'Verified' : 'Unverified') : 'No email on file',
      detail: providers.email ?? 'Email linking is not available from Profile yet.'
    },
    {
      label: 'Password',
      value: providers.has_password ? 'Password set' : 'Password not set',
      detail: providers.has_password ? 'Password sign-in is enabled for this account.' : 'Password setup is not available from Profile yet.'
    }
  ];
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

export function profileStats(stats: ProfileStatsRead | null): ProfileStatView[] {
  if (!stats) {
    return [
      {
        label: 'Interaction stats',
        value: 'Unavailable',
        detail: 'Stats load after the profile stats API responds.'
      }
    ];
  }

  return [
    {
      label: 'Viewed',
      value: String(stats.viewed),
      detail: stats.viewed > 0 ? 'Views and detail opens recorded from interaction events.' : 'No viewed meme interactions recorded yet.'
    },
    {
      label: 'Sent',
      value: String(stats.sent),
      detail: stats.sent > 0 ? 'Share and send events from this account.' : 'No sent meme interactions recorded yet.'
    },
    {
      label: 'Saved',
      value: String(stats.saved),
      detail: stats.saved > 0 ? 'Save and favorite events from this account.' : 'No saved meme interactions recorded yet.'
    },
    {
      label: 'Downloaded',
      value: String(stats.downloaded),
      detail: stats.downloaded > 0 ? 'Download events from this account.' : 'No downloaded meme interactions recorded yet.'
    },
    {
      label: 'Days active',
      value: String(stats.days_active),
      detail: stats.days_active > 0 ? 'Distinct calendar days with recorded interactions.' : 'No active interaction days recorded yet.'
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
      detail: user.nsfw_enabled
        ? 'Search can include NSFW memes when the URL filter asks for them.'
        : 'Enable from the Search NSFW filter confirmation when you want to include those results.'
    },
    {
      label: 'Account status',
      value: user.status,
      detail: user.guest_expires_at ? `Guest session expires ${formatDate(user.guest_expires_at)}.` : `Created ${formatDate(user.created_at)}.`
    }
  ];
}

export function movePinnedMemeId(memeIds: string[], memeId: string, direction: -1 | 1): string[] {
  const index = memeIds.indexOf(memeId);
  const nextIndex = index + direction;
  if (index < 0 || nextIndex < 0 || nextIndex >= memeIds.length) {
    return memeIds;
  }

  const next = [...memeIds];
  [next[index], next[nextIndex]] = [next[nextIndex], next[index]];
  return next;
}

export function movePinnedMemeIdToTarget(memeIds: string[], memeId: string, targetMemeId: string): string[] {
  if (memeId === targetMemeId || !memeIds.includes(memeId) || !memeIds.includes(targetMemeId)) {
    return memeIds;
  }

  const next = memeIds.filter((id) => id !== memeId);
  next.splice(next.indexOf(targetMemeId), 0, memeId);
  return next;
}

export function orderPinnedMemesByIds(memes: PublicMemeCardRead[], memeIds: string[]): PublicMemeCardRead[] {
  const byId = new Map(memes.map((meme) => [meme.id, meme]));
  const ordered = memeIds.flatMap((id) => {
    const meme = byId.get(id);
    return meme ? [meme] : [];
  });
  const orderedIds = new Set(ordered.map((meme) => meme.id));
  return [...ordered, ...memes.filter((meme) => !orderedIds.has(meme.id))];
}

export function languageLabel(language: UserRead['language']): string {
  switch (language) {
    case 'any':
      return 'Any language';
    case 'en':
      return 'English';
    case 'ru':
      return 'Russian';
    default:
      return language;
  }
}

function formatDate(value: string): string {
  return `${new Intl.DateTimeFormat('en', { dateStyle: 'medium', timeZone: 'UTC' }).format(new Date(value))} UTC`;
}
