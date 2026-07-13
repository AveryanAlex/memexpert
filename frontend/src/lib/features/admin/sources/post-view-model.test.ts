import { describe, expect, it } from 'vitest';
import type { AdminSourceChannelRead, AdminTelegramSessionRead } from '$lib/api/types';
import {
  backfillStatusLabel,
  humanizePipelineValue,
  sourceBackfillAvailability,
  sourcePostLatestHref,
  sourcePostPageHref,
  sourcePostIndexLabel,
  sourcePostIndexTone,
  syncStatusLabel
} from './post-view-model';

describe('source post view model', () => {
  it('maps durable index and sync states to operator labels', () => {
    expect(sourcePostIndexLabel('indexed')).toBe('Indexed');
    expect(sourcePostIndexLabel('partially_indexed')).toBe('Partially indexed');
    expect(sourcePostIndexLabel('not_indexable')).toBe('Not indexable');
    expect(sourcePostIndexTone('indexed')).toBe('success');
    expect(sourcePostIndexTone('processing')).toBe('trend');
    expect(syncStatusLabel('synced')).toBe('Synced');
    expect(syncStatusLabel(null)).toBe('Not started');
    expect(backfillStatusLabel('running')).toBe('Running');
  });

  it('humanizes raw pipeline enum values without inventing state', () => {
    expect(humanizePipelineValue('sync_qdrant')).toBe('Sync Qdrant');
    expect(humanizePipelineValue(null)).toBe('Not started');
  });

  it('keeps a stable observation snapshot across page links', () => {
    expect(sourcePostPageHref('source/id', 2, '2026-07-13T12:00:00+03:00')).toBe(
      '/admin/sources/source%2Fid?page=2&snapshot_at=2026-07-13T12%3A00%3A00%2B03%3A00'
    );
  });

  it('resets to the source detail URL without a sticky snapshot', () => {
    expect(sourcePostLatestHref('source/id')).toBe('/admin/sources/source%2Fid');
  });

  it('matches the backend prerequisites for queuing older-message backfill', () => {
    const now = new Date('2026-07-13T12:00:00Z');
    expect(sourceBackfillAvailability(source(), [account()], now)).toEqual({ canQueue: true, reason: null });

    expect(sourceBackfillAvailability(source(), [account({ flood_wait_until: '2026-07-13T12:01:00Z' })], now)).toEqual({
      canQueue: false,
      reason: 'The assigned Telegram account is not ready. Choose an enabled, authorized account without a current rate limit.'
    });
    expect(sourceBackfillAvailability(source(), [account({ catchup_enabled: false })], now).reason).toContain('assigned Telegram account');
    expect(sourceBackfillAvailability(source({ catchup_enabled: false }), [account()], now).reason).toContain('Enable source catch-up');
    expect(sourceBackfillAvailability(source({ initial_catchup_completed: false }), [account()], now).reason).toContain('initial latest-message catch-up');
    expect(sourceBackfillAvailability(source({ backfill_status: 'queued' }), [account()], now).reason).toContain('already queued or running');
  });
});

function source(overrides: Partial<AdminSourceChannelRead> = {}): AdminSourceChannelRead {
  return {
    id: 'source-id',
    platform: 'telegram',
    platform_id: 'daily_memes',
    username: 'daily_memes',
    title: 'Daily memes',
    subscriber_count: 100,
    is_active: true,
    is_paused: false,
    catchup_enabled: true,
    live_enabled: true,
    engagement_enabled: true,
    catchup_message_limit: 5000,
    telegram_session_id: 'account-id',
    telegram_session_name: 'primary',
    is_orphaned: false,
    is_indexable: true,
    last_read_post_id: '10000',
    oldest_observed_post_id: '5001',
    initial_catchup_completed: true,
    history_exhausted: false,
    backfill_status: 'idle',
    backfill_requested_count: 0,
    backfill_scanned_count: 0,
    backfill_error: null,
    last_fetched_at: '2026-07-13T10:00:00Z',
    operational_status: 'active',
    freshness_status: 'fresh',
    seconds_since_last_fetch: 10,
    created_at: '2026-07-13T09:00:00Z',
    updated_at: '2026-07-13T10:00:00Z',
    ...overrides
  };
}

function account(overrides: Partial<AdminTelegramSessionRead> = {}): AdminTelegramSessionRead {
  return {
    id: 'account-id',
    name: 'primary',
    display_name: 'Primary account',
    owned_channel_count: 1,
    status: 'active',
    enabled: true,
    flood_wait_until: null,
    live_listener_started_at: null,
    last_heartbeat_at: null,
    last_error_class: null,
    last_error_text: null,
    quarantined_at: null,
    live_enabled: true,
    catchup_enabled: true,
    engagement_enabled: true,
    max_requests_per_second: 1,
    account_user_id: 1,
    account_username: 'primary',
    account_phone_hint: 'ending-1234',
    has_string_session: true,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides
  };
}
