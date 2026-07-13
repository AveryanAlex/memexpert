import { describe, expect, it } from 'vitest';
import type { AdminSourceChannelRead, AdminTelegramSessionRead } from '$lib/api/types';
import {
  clearSourceSuggestionPrefill,
  defaultTelegramAccountId,
  lastFetchLabel,
  readyTelegramAccounts,
  relativeTimestamp,
  sourceSuggestionPrefill,
  toSourceCardViewModel
} from './view-model';

describe('source view model', () => {
  it('maps source health into plain-language status and default pause/resume controls', () => {
    const now = new Date('2026-01-01T01:00:00Z');
    const accounts = [telegramAccount()];
    const healthy = toSourceCardViewModel(sourceChannel(), accounts, now);
    const paused = toSourceCardViewModel(sourceChannel({ is_paused: true, operational_status: 'paused' }), accounts, now);
    const needsAccount = toSourceCardViewModel(sourceChannel({ telegram_session_id: null, telegram_session_name: null, is_orphaned: true, is_indexable: false }), accounts, now);
    const needsAttention = toSourceCardViewModel(sourceChannel({ freshness_status: 'stale', seconds_since_last_fetch: 7_200 }), accounts, now);
    const removed = toSourceCardViewModel(sourceChannel({ is_active: false, operational_status: 'inactive' }), accounts, now);

    expect(healthy).toMatchObject({ status: 'Healthy', assignedAccountLabel: 'Primary ingest', toggleLabel: 'Pause' });
    expect(paused).toMatchObject({ status: 'Paused', toggleLabel: 'Resume' });
    expect(needsAccount.status).toBe('Needs account');
    expect(needsAttention).toMatchObject({ status: 'Needs attention', lastFetchLabel: 'Last fetched 2h ago' });
    expect(removed).toMatchObject({ status: 'Removed', canToggle: false, toggleLabel: null });
  });

  it('uses account readiness for Telegram sources and keeps unassigned sources needing an account', () => {
    const now = new Date('2026-01-01T01:00:00Z');
    const unavailable = telegramAccount({ enabled: false, display_name: 'Unavailable account' });

    expect(toSourceCardViewModel(sourceChannel(), [unavailable], now)).toMatchObject({
      status: 'Needs attention',
      assignedAccountLabel: 'Unavailable account (unavailable)'
    });
    expect(toSourceCardViewModel(sourceChannel({ telegram_session_id: null, telegram_session_name: null, is_orphaned: true, is_indexable: false }), [telegramAccount()], now).status).toBe('Needs account');
  });

  it('lets a ready assigned account activate a manual source whose ingestion flags are all off', () => {
    const sourceAfterAssignment = sourceChannel({
      is_orphaned: false,
      is_indexable: false,
      catchup_enabled: false,
      live_enabled: false,
      engagement_enabled: false
    });

    expect(toSourceCardViewModel(sourceAfterAssignment, [telegramAccount()], new Date('2026-01-01T01:00:00Z'))).toMatchObject({
      status: 'Needs attention',
      statusDetail: 'Ingestion is off.'
    });
  });

  it('keeps new sources in the first-fetch grace window and marks unsupported platforms clearly', () => {
    const now = new Date('2026-01-01T01:00:00Z');
    const account = telegramAccount();
    const waiting = sourceChannel({ freshness_status: 'never_fetched', seconds_since_last_fetch: null, created_at: '2026-01-01T00:45:01Z' });
    const overdue = sourceChannel({ freshness_status: 'never_fetched', seconds_since_last_fetch: null, created_at: '2026-01-01T00:45:00Z' });
    const reddit = sourceChannel({ platform: 'reddit', telegram_session_id: null, telegram_session_name: null, is_orphaned: true, is_indexable: false });

    expect(toSourceCardViewModel(waiting, [account], now).status).toBe('Waiting to fetch');
    expect(toSourceCardViewModel(overdue, [account], now).status).toBe('Needs attention');
    expect(toSourceCardViewModel(reddit, [account], now)).toMatchObject({
      status: 'Crawler unavailable',
      assignedAccountLabel: 'Not applicable',
      canToggle: false
    });
  });

  it('formats relative fetch and suggestion ages for operators', () => {
    expect(lastFetchLabel(sourceChannel({ seconds_since_last_fetch: 65 }))).toBe('Last fetched 1m ago');
    expect(lastFetchLabel(sourceChannel({ seconds_since_last_fetch: null, freshness_status: 'never_fetched' }))).toBe('Not fetched yet');
    expect(relativeTimestamp('2026-01-01T00:00:00Z', new Date('2026-01-02T03:00:00Z'))).toBe('1d ago');
  });

  it('auto-selects exactly one ready Telegram account and requires a choice otherwise', () => {
    const now = new Date('2026-01-01T01:00:00Z');
    const primary = telegramAccount();
    const backup = telegramAccount({ id: '33333333-3333-4333-8333-333333333333', display_name: 'Backup ingest' });
    const unavailable = telegramAccount({ id: '44444444-4444-4444-8444-444444444444', enabled: false });

    expect(readyTelegramAccounts([primary, unavailable], now)).toEqual([primary]);
    expect(defaultTelegramAccountId([primary, unavailable], now)).toBe(primary.id);
    expect(defaultTelegramAccountId([primary, backup], now)).toBe('');
    expect(defaultTelegramAccountId([unavailable], now)).toBe('');
  });

  it('sets and fully clears the suggestion quick-add prefill', () => {
    expect(sourceSuggestionPrefill('https://t.me/public_channel', 'suggestion-id')).toEqual({
      reference: 'https://t.me/public_channel',
      suggestionId: 'suggestion-id'
    });
    expect(clearSourceSuggestionPrefill()).toEqual({ reference: '', suggestionId: '' });
  });
});

function sourceChannel(overrides: Partial<AdminSourceChannelRead> = {}): AdminSourceChannelRead {
  return {
    id: '11111111-1111-4111-8111-111111111111',
    platform: 'telegram',
    platform_id: '-1001234',
    username: 'source_handle',
    title: 'Source title',
    subscriber_count: 100,
    is_active: true,
    is_paused: false,
    catchup_enabled: true,
    live_enabled: true,
    engagement_enabled: true,
    catchup_message_limit: 5000,
    telegram_session_id: '22222222-2222-4222-8222-222222222222',
    telegram_session_name: 'Primary ingest',
    is_orphaned: false,
    is_indexable: true,
    last_read_post_id: '99',
    oldest_observed_post_id: '12',
    initial_catchup_completed: true,
    history_exhausted: false,
    backfill_status: 'idle',
    backfill_requested_count: 0,
    backfill_scanned_count: 0,
    backfill_error: null,
    last_fetched_at: '2026-01-01T00:00:00Z',
    operational_status: 'active',
    freshness_status: 'fresh',
    seconds_since_last_fetch: 300,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...overrides
  };
}

function telegramAccount(overrides: Partial<AdminTelegramSessionRead> = {}): AdminTelegramSessionRead {
  return {
    id: '22222222-2222-4222-8222-222222222222',
    name: 'primary',
    display_name: 'Primary ingest',
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
