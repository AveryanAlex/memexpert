import type { CurrentSessionRead } from '$lib/api/types';

export interface NavItem {
  label: string;
  href: string;
  match: 'exact' | 'prefix';
}

export const PRIMARY_NAV_ITEMS: NavItem[] = [
  { label: 'For You', href: '/', match: 'exact' },
  { label: 'Trends', href: '/trends', match: 'prefix' },
  { label: 'Profile', href: '/profile', match: 'prefix' }
];

export function isNavItemActive(item: NavItem, currentPath: string): boolean {
  return item.match === 'exact' ? currentPath === item.href : currentPath === item.href || currentPath.startsWith(`${item.href}/`);
}

export function profileLabel(session: CurrentSessionRead | null): string {
  if (!session) return 'Sign in';
  return session.user.account_type === 'full' ? 'Full profile' : 'Guest';
}

export function providerSummary(session: CurrentSessionRead | null): string {
  if (!session) return 'Session unavailable';
  if (session.linked_providers.telegram_linked) return 'Connected: Telegram';
  if (session.linked_providers.google_linked) return 'Connected: Google';
  if (session.linked_providers.email) return 'Connected: Email';
  return session.user.account_type === 'guest' ? 'Connect to sync' : 'No provider connected';
}
