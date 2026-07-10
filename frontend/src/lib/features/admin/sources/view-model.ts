import type { AdminSourceChannelRead, AdminTelegramSessionRead } from '$lib/api/types';
import { isReadyTelegramAccount } from '$lib/features/admin/telegram/readiness';

export type SourcePlainStatus = 'Healthy' | 'Paused' | 'Waiting to fetch' | 'Needs account' | 'Needs attention' | 'Crawler unavailable' | 'Removed';

export interface SourceCardViewModel {
  source: AdminSourceChannelRead;
  platformLabel: string;
  handleLabel: string;
  status: SourcePlainStatus;
  statusDetail: string;
  lastFetchLabel: string;
  assignedAccountLabel: string;
  canToggle: boolean;
  toggleLabel: 'Pause' | 'Resume' | null;
}

export function toSourceCardViewModel(source: AdminSourceChannelRead, telegramAccounts: AdminTelegramSessionRead[] = [], now = new Date()): SourceCardViewModel {
  const status = sourcePlainStatus(source, telegramAccounts, now);
  return {
    source,
    platformLabel: platformLabel(source.platform),
    handleLabel: source.username ? `@${source.username}` : source.platform_id,
    status,
    statusDetail: sourceStatusDetail(source, status, telegramAccounts),
    lastFetchLabel: lastFetchLabel(source),
    assignedAccountLabel: assignedAccountLabel(source, telegramAccounts),
    canToggle: source.platform === 'telegram' && source.is_active,
    toggleLabel: source.platform === 'telegram' && source.is_active ? (source.is_paused ? 'Resume' : 'Pause') : null
  };
}

export function sourcePlainStatus(source: AdminSourceChannelRead, telegramAccounts: AdminTelegramSessionRead[] = [], now = new Date()): SourcePlainStatus {
  if (source.platform !== 'telegram') return 'Crawler unavailable';
  if (!source.is_active) return 'Removed';
  if (source.is_paused) return 'Paused';
  if (source.is_orphaned || !source.telegram_session_id) return 'Needs account';
  const assignedAccount = telegramAccounts.find((account) => account.id === source.telegram_session_id);
  if (!assignedAccount || !isReadyTelegramAccount(assignedAccount, now)) return 'Needs attention';
  if (!hasEnabledIngestion(source)) return 'Needs attention';
  if (source.freshness_status === 'never_fetched' && isWithinFirstFetchGrace(source.created_at, now)) return 'Waiting to fetch';
  if (source.freshness_status === 'stale' || source.freshness_status === 'never_fetched' || source.freshness_status === 'checkpoint_only') {
    return 'Needs attention';
  }
  return 'Healthy';
}

export function lastFetchLabel(source: Pick<AdminSourceChannelRead, 'freshness_status' | 'seconds_since_last_fetch'>): string {
  if (source.seconds_since_last_fetch !== null) {
    return `Last fetched ${relativeDuration(source.seconds_since_last_fetch)} ago`;
  }
  if (source.freshness_status === 'checkpoint_only') return 'Last fetch is unavailable; checkpoint only';
  return 'Not fetched yet';
}

export function relativeDuration(seconds: number): string {
  if (seconds >= 86_400) return `${Math.floor(seconds / 86_400)}d`;
  if (seconds >= 3_600) return `${Math.floor(seconds / 3_600)}h`;
  if (seconds >= 60) return `${Math.floor(seconds / 60)}m`;
  return `${Math.max(0, Math.floor(seconds))}s`;
}

export function relativeTimestamp(timestamp: string, now = new Date()): string {
  const then = new Date(timestamp).getTime();
  if (!Number.isFinite(then)) return 'unknown age';
  const seconds = Math.max(0, Math.floor((now.getTime() - then) / 1_000));
  return `${relativeDuration(seconds)} ago`;
}

function sourceStatusDetail(source: AdminSourceChannelRead, status: SourcePlainStatus, telegramAccounts: AdminTelegramSessionRead[]): string {
  if (status === 'Crawler unavailable') return `${platformLabel(source.platform)} crawler support is unavailable.`;
  if (status === 'Removed') return 'Removed from crawling; checkpoint history is preserved.';
  if (status === 'Paused') return 'Paused intentionally.';
  if (status === 'Needs account') return 'Assign a Telegram account before this source can be indexed.';
  if (status === 'Waiting to fetch') return 'Waiting for its first fetch.';
  if (status === 'Needs attention') {
    if (source.telegram_session_id && !telegramAccounts.some((account) => account.id === source.telegram_session_id && isReadyTelegramAccount(account))) {
      return 'Its assigned Telegram account is unavailable.';
    }
    if (!hasEnabledIngestion(source)) return 'Ingestion is off.';
    return source.freshness_status === 'never_fetched' ? 'This source has not fetched yet.' : 'Check its recent fetch activity.';
  }
  return 'Fetching normally.';
}

function hasEnabledIngestion(source: Pick<AdminSourceChannelRead, 'catchup_enabled' | 'live_enabled' | 'engagement_enabled'>): boolean {
  return source.catchup_enabled || source.live_enabled || source.engagement_enabled;
}

function assignedAccountLabel(source: AdminSourceChannelRead, telegramAccounts: AdminTelegramSessionRead[]): string {
  if (source.platform !== 'telegram') return 'Not applicable';
  if (!source.telegram_session_id) return 'No account assigned';
  const account = telegramAccounts.find((candidate) => candidate.id === source.telegram_session_id);
  if (!account) return `${source.telegram_session_name ?? 'Assigned account'} (unavailable)`;
  return isReadyTelegramAccount(account) ? account.display_name : `${account.display_name} (unavailable)`;
}

function isWithinFirstFetchGrace(createdAt: string, now: Date): boolean {
  const createdAtMs = new Date(createdAt).getTime();
  if (!Number.isFinite(createdAtMs)) return false;
  const ageMs = now.getTime() - createdAtMs;
  return ageMs >= 0 && ageMs < 15 * 60 * 1_000;
}

function platformLabel(platform: AdminSourceChannelRead['platform']): string {
  if (platform === 'telegram') return 'Telegram';
  if (platform === 'reddit') return 'Reddit';
  return 'VK';
}
