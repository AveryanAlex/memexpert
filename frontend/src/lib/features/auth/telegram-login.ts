import type { CurrentSessionRead, TelegramLinkStartRead } from '$lib/api/types';

export const TELEGRAM_START_PREFIX = 'link_';
export const TELEGRAM_LOGIN_POLL_INTERVAL_MS = 1000;

export interface LoginProviderOption {
  id: 'telegram' | 'google' | 'email';
  label: string;
  description: string;
  status: 'available' | 'coming_later';
}

export const LOGIN_PROVIDER_OPTIONS: LoginProviderOption[] = [
  { id: 'telegram', label: 'Telegram', description: 'Fast bot handoff, no password.', status: 'available' },
  { id: 'google', label: 'Google', description: 'OAuth sign-in will be available later.', status: 'coming_later' },
  { id: 'email', label: 'Email', description: 'Magic links and password login will be available later.', status: 'coming_later' }
];

export function buildTelegramStartCommand(link: Pick<TelegramLinkStartRead, 'code'>): string {
  return `/start ${TELEGRAM_START_PREFIX}${link.code}`;
}

export function telegramExpiryLabel(link: Pick<TelegramLinkStartRead, 'expires_in_seconds'>): string {
  if (link.expires_in_seconds < 60) return 'Expires in less than 1 minute';
  return `Expires in about ${Math.ceil(link.expires_in_seconds / 60)} minutes`;
}

export function isFullSession(session: CurrentSessionRead | null): boolean {
  return session?.user.account_type === 'full';
}
