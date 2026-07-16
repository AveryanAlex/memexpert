import type { AdminSourceChannelRead, AdminTelegramSessionRead } from '$lib/api/types';
import { isReadyTelegramAccount } from '$lib/features/admin/telegram/readiness';

export type SourcePlainStatus = 'Healthy' | 'Paused' | 'Waiting to fetch' | 'Needs account' | 'Needs attention' | 'Crawler unavailable' | 'Removed';
export type SourceInventorySortKey = 'source' | 'health' | 'latest_post' | 'last_fetched' | 'memes' | 'posts' | 'subscribers';
export type SourceInventorySortDirection = 'ascending' | 'descending';

export interface SourceInventorySort {
  key: SourceInventorySortKey;
  direction: SourceInventorySortDirection;
}

export const DEFAULT_SOURCE_INVENTORY_SORT: SourceInventorySort = {
  key: 'health',
  direction: 'ascending'
};

const SOURCE_TITLE_COLLATOR = new Intl.Collator('en', {
  numeric: true,
  sensitivity: 'base'
});

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

export interface SourceSuggestionPrefill {
  reference: string;
  suggestionId: string;
}

export function sourceSuggestionPrefill(reference: string, suggestionId: string): SourceSuggestionPrefill {
  return { reference, suggestionId };
}

export function clearSourceSuggestionPrefill(): SourceSuggestionPrefill {
  return { reference: '', suggestionId: '' };
}

export function readyTelegramAccounts(accounts: AdminTelegramSessionRead[], now = new Date()): AdminTelegramSessionRead[] {
  return accounts.filter((account) => isReadyTelegramAccount(account, now));
}

export function defaultTelegramAccountId(accounts: AdminTelegramSessionRead[], now = new Date()): string {
  const readyAccounts = readyTelegramAccounts(accounts, now);
  return readyAccounts.length === 1 ? readyAccounts[0].id : '';
}

export function nextSourceInventorySort(
  current: SourceInventorySort,
  key: SourceInventorySortKey
): SourceInventorySort {
  if (current.key === key) {
    return {
      key,
      direction: current.direction === 'ascending' ? 'descending' : 'ascending'
    };
  }
  return { key, direction: sourceInventoryDefaultDirection(key) };
}

export function sortSourceInventory(
  sources: readonly AdminSourceChannelRead[],
  telegramAccounts: readonly AdminTelegramSessionRead[],
  sort: SourceInventorySort,
  now = new Date()
): AdminSourceChannelRead[] {
  const accountList = [...telegramAccounts];
  const direction = sort.direction === 'ascending' ? 1 : -1;
  return [...sources].sort((left, right) => {
    const leftModel = toSourceCardViewModel(left, accountList, now);
    const rightModel = toSourceCardViewModel(right, accountList, now);
    let comparison = 0;

    switch (sort.key) {
      case 'source':
        comparison = compareText(left.title, right.title);
        break;
      case 'health':
        comparison = sourceAttentionRank(left, leftModel.status) - sourceAttentionRank(right, rightModel.status);
        if (comparison === 0) comparison = compareText(leftModel.assignedAccountLabel, rightModel.assignedAccountLabel);
        break;
      case 'latest_post':
        comparison = compareNullableTimestamp(left.latest_post_at, right.latest_post_at, sort.direction);
        break;
      case 'last_fetched':
        comparison = compareNullableTimestamp(left.last_fetched_at, right.last_fetched_at, sort.direction);
        break;
      case 'memes':
        comparison = left.meme_count - right.meme_count;
        break;
      case 'posts':
        comparison = left.observed_post_count - right.observed_post_count;
        break;
      case 'subscribers':
        comparison = compareNullableNumber(left.subscriber_count, right.subscriber_count, sort.direction);
        break;
    }

    if (sort.key !== 'latest_post' && sort.key !== 'last_fetched' && sort.key !== 'subscribers') {
      comparison *= direction;
    }
    if (comparison !== 0) return comparison;

    const titleComparison = compareText(left.title, right.title);
    return titleComparison !== 0 ? titleComparison : compareText(left.id, right.id);
  });
}

export function sourceInventoryDefaultDirection(key: SourceInventorySortKey): SourceInventorySortDirection {
  return key === 'source' || key === 'health' || key === 'last_fetched' ? 'ascending' : 'descending';
}

export function mergeSourceProjections(
  loaded: AdminSourceChannelRead[],
  optimistic: AdminSourceChannelRead[]
): AdminSourceChannelRead[] {
  const optimisticById = new Map(optimistic.map((source) => [source.id, source]));
  const loadedIds = new Set(loaded.map((source) => source.id));
  return [
    ...loaded.map((source) => optimisticById.get(source.id) ?? source),
    ...optimistic.filter((source) => !loadedIds.has(source.id))
  ];
}

export function isSourceAtLeastExpected(
  actual: AdminSourceChannelRead | undefined,
  expected: AdminSourceChannelRead
): boolean {
  if (!actual || actual.id !== expected.id) return false;
  const actualUpdatedAt = Date.parse(actual.updated_at);
  const expectedUpdatedAt = Date.parse(expected.updated_at);
  if (Number.isFinite(actualUpdatedAt) && Number.isFinite(expectedUpdatedAt)) {
    if (actualUpdatedAt > expectedUpdatedAt) return true;
    if (actualUpdatedAt < expectedUpdatedAt) return false;
  }
  return sourceMutationProjectionMatches(actual, expected);
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

function sourceAttentionRank(source: AdminSourceChannelRead, status: SourcePlainStatus): number {
  if (source.backfill_status === 'failed') return 0;
  switch (status) {
    case 'Needs attention': return 1;
    case 'Needs account': return 2;
    case 'Waiting to fetch': return 3;
    case 'Crawler unavailable': return 4;
    case 'Removed': return 5;
    case 'Paused': return 6;
    case 'Healthy': return 7;
  }
}

function compareText(left: string, right: string): number {
  return SOURCE_TITLE_COLLATOR.compare(left, right);
}

function compareNullableTimestamp(
  left: string | null,
  right: string | null,
  direction: SourceInventorySortDirection
): number {
  const leftTimestamp = left === null ? null : Date.parse(left);
  const rightTimestamp = right === null ? null : Date.parse(right);
  return compareNullableNumber(
    Number.isFinite(leftTimestamp) ? leftTimestamp : null,
    Number.isFinite(rightTimestamp) ? rightTimestamp : null,
    direction
  );
}

function compareNullableNumber(
  left: number | null,
  right: number | null,
  direction: SourceInventorySortDirection
): number {
  if (left === null && right === null) return 0;
  if (left === null) return 1;
  if (right === null) return -1;
  return (left - right) * (direction === 'ascending' ? 1 : -1);
}

function sourceMutationProjectionMatches(actual: AdminSourceChannelRead, expected: AdminSourceChannelRead): boolean {
  return actual.platform === expected.platform
    && actual.platform_id === expected.platform_id
    && actual.username === expected.username
    && actual.title === expected.title
    && actual.subscriber_count === expected.subscriber_count
    && actual.is_active === expected.is_active
    && actual.is_paused === expected.is_paused
    && actual.catchup_enabled === expected.catchup_enabled
    && actual.live_enabled === expected.live_enabled
    && actual.engagement_enabled === expected.engagement_enabled
    && actual.catchup_message_limit === expected.catchup_message_limit
    && actual.telegram_session_id === expected.telegram_session_id
    && actual.telegram_session_name === expected.telegram_session_name
    && actual.is_orphaned === expected.is_orphaned
    && actual.is_indexable === expected.is_indexable
    && actual.operational_status === expected.operational_status;
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
