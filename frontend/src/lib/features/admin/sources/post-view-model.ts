import type {
  AdminSourceBackfillStatus,
  AdminSourceChannelRead,
  AdminSourcePostIndexStatus,
  AdminSourcePostSyncStatus,
  AdminTelegramSessionRead
} from '$lib/api/types';
import { isReadyTelegramAccount } from '$lib/features/admin/telegram/readiness';

export interface SourceBackfillAvailability {
  canQueue: boolean;
  reason: string | null;
}

export function sourcePostIndexLabel(status: AdminSourcePostIndexStatus): string {
  if (status === 'indexed') return 'Indexed';
  if (status === 'partially_indexed') return 'Partially indexed';
  if (status === 'processing') return 'Processing';
  if (status === 'failed') return 'Failed';
  return 'Not indexable';
}

export function sourcePostIndexTone(status: AdminSourcePostIndexStatus): 'neutral' | 'success' | 'trend' {
  if (status === 'indexed') return 'success';
  if (status === 'processing' || status === 'partially_indexed') return 'trend';
  return 'neutral';
}

export function syncStatusLabel(status: AdminSourcePostSyncStatus | null): string {
  if (status === 'synced') return 'Synced';
  if (status === 'processing') return 'Processing';
  if (status === 'pending') return 'Pending';
  if (status === 'failed') return 'Failed';
  return 'Not started';
}

export function backfillStatusLabel(status: AdminSourceBackfillStatus): string {
  if (status === 'queued') return 'Queued';
  if (status === 'running') return 'Running';
  if (status === 'failed') return 'Failed';
  return 'Idle';
}

export function sourceBackfillAvailability(
  source: AdminSourceChannelRead,
  telegramAccounts: AdminTelegramSessionRead[],
  now = new Date()
): SourceBackfillAvailability {
  if (source.platform !== 'telegram') {
    return blocked('Older-history backfill is available only for Telegram sources.');
  }
  if (!source.is_active) {
    return blocked('This source was removed from crawling, so older messages cannot be fetched.');
  }
  if (source.is_paused) {
    return blocked('Resume this source before fetching older messages.');
  }
  if (!source.telegram_session_id) {
    return blocked('Assign a ready Telegram account before fetching older messages.');
  }

  const assignedAccount = telegramAccounts.find((account) => account.id === source.telegram_session_id);
  if (!assignedAccount) {
    return blocked('The assigned Telegram account no longer exists. Assign a ready account before fetching older messages.');
  }
  if (!isReadyTelegramAccount(assignedAccount, now)) {
    return blocked('The assigned Telegram account is not ready. Choose an enabled, authorized account without a current rate limit.');
  }
  if (!assignedAccount.catchup_enabled) {
    return blocked('Enable catch-up on the assigned Telegram account before fetching older messages.');
  }
  if (!source.catchup_enabled) {
    return blocked('Enable source catch-up before fetching older messages.');
  }
  if (source.history_exhausted) {
    return blocked('Telegram history is already fully scanned for this source.');
  }
  if (!source.initial_catchup_completed) {
    return blocked('Wait for the initial latest-message catch-up before fetching older messages.');
  }
  if (source.backfill_status === 'queued' || source.backfill_status === 'running') {
    return blocked('An older-message backfill is already queued or running for this source.');
  }
  return { canQueue: true, reason: null };
}

export function sourcePostPageHref(channelId: string, page: number, snapshotAt: string): string {
  const params = new URLSearchParams({ page: String(page), snapshot_at: snapshotAt });
  return `/admin/sources/${encodeURIComponent(channelId)}?${params.toString()}`;
}

export function sourcePostLatestHref(channelId: string): string {
  return `/admin/sources/${encodeURIComponent(channelId)}`;
}

export function humanizePipelineValue(value: string | null): string {
  if (!value) return 'Not started';
  return value
    .split('_')
    .filter(Boolean)
    .map((part) => `${part[0]?.toUpperCase() ?? ''}${part.slice(1)}`)
    .join(' ');
}

function blocked(reason: string): SourceBackfillAvailability {
  return { canQueue: false, reason };
}
