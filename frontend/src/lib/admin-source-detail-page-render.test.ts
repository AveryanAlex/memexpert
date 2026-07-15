import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import type { AdminSourceBackfillRead, AdminSourceChannelRead, AdminSourcePostPageRead, AdminSourcePostRead, AdminTelegramSessionRead } from '$lib/api/types';
import SourceDetailWorkspace from '$lib/features/admin/sources/SourceDetailWorkspace.svelte';

describe('/admin/sources/[channelId] detail', () => {
  it('renders indexing totals, per-message pipeline truth, pagination, and bounded backfill controls', () => {
    const { body } = render(SourceDetailWorkspace, {
      props: {
        source: sourceChannel(),
        postPage: sourcePostPage(),
        recoveryRequestIds: sourceRecoveryRequestIds(),
        telegramAccounts: [telegramAccount()],
        paging: { page: 1, snapshotAt: '2026-07-13T12:00:00Z', hasPrevious: false, hasNext: true },
        loadError: null,
        form: null
      }
    });

    expect(body).toContain('Source indexing');
    expect(body).toContain('Indexing summary');
    expect(body).toContain('Fetched messages');
    expect(body).toContain('Fetched</span>');
    expect(body).toContain('Indexed</span>');
    expect(body).toContain('Partially indexed');
    expect(body).toContain('Processing');
    expect(body).toContain('Failed');
    expect(body).toContain('Not indexable');
    expect(body).toContain('Materialized');
    expect(body).toContain('Qdrant');
    expect(body).toContain('Meilisearch');
    expect(body).toContain('Embedding provider unavailable.');
    expect(body).toContain('action="?/backfillSourceChannel"');
    expect(body).toMatch(/name="message_limit"[^>]*min="1"[^>]*max="50000"[^>]*value="5000"/);
    expect(body).toContain('Fetch older messages');
    expect(body).toContain('action="?/replaySourcePost"');
    expect(body).toContain('name="request_id" value="11111111-1111-4111-8111-111111111111"');
    expect(body).toContain('aria-label="Source message pagination"');
    expect(body).toContain('Page 1');
    expect(body).toContain('Show latest messages');
    expect(body).toContain('resets to page 1 and includes newly fetched or backfilled rows');
    expect(body).toContain('href="/admin/sources/source-id"');
    expect(body).toContain('data-sveltekit-reload=""');
    expect(body).toContain('href="/admin/sources/source-id?page=2&amp;snapshot_at=2026-07-13T12%3A00%3A00Z"');
  });

  it('shows durable backfill progress and failure state while disabling conflicting work', () => {
    const queued = render(SourceDetailWorkspace, {
      props: {
        source: sourceChannel({ backfill_status: 'running', backfill_requested_count: 5000, backfill_scanned_count: 1250 }),
        postPage: sourcePostPage(),
        recoveryRequestIds: sourceRecoveryRequestIds(),
        telegramAccounts: [telegramAccount()],
        paging: { page: 1, snapshotAt: '2026-07-13T12:00:00Z', hasPrevious: false, hasNext: false },
        loadError: null,
        form: null
      }
    }).body;
    expect(queued).toContain('1,250 of 5,000 scanned');
    expect(queued).toContain('Older-message backfill is running.');
    expect(queued).toContain('Use “Show latest messages” below to reload the ledger with newly fetched rows');
    expect(queued).not.toContain('Refresh this page');
    expect(queued).toMatch(/name="message_limit"[^>]*disabled=""/);

    const failed = render(SourceDetailWorkspace, {
      props: {
        source: sourceChannel({ backfill_status: 'failed', backfill_error: 'Flood wait until tomorrow.' }),
        postPage: sourcePostPage(),
        backfills: { items: [sourceBackfill()] },
        recoveryRequestIds: sourceRecoveryRequestIds({
          'backfill-id': '22222222-2222-4222-8222-222222222222'
        }),
        telegramAccounts: [telegramAccount()],
        paging: { page: 1, snapshotAt: '2026-07-13T12:00:00Z', hasPrevious: false, hasNext: false },
        loadError: null,
        form: {
          message: 'Failed backfill queued to resume from its durable cursor.',
          recoveryJobId: '33333333-3333-4333-8333-333333333333'
        }
      }
    }).body;
    expect(failed).toContain('Backfill failed: Flood wait until tomorrow.');
    expect(failed).toMatch(/name="message_limit"[^>]*disabled=""/);
    expect(failed).toContain('Resume backfill');
    expect(failed).toContain('action="?/resumeSourceBackfill"');
    expect(failed).toContain('name="request_id" value="22222222-2222-4222-8222-222222222222"');
    expect(failed).toContain('href="/admin/recovery/batches/33333333-3333-4333-8333-333333333333"');

    const catchupDisabled = render(SourceDetailWorkspace, {
      props: {
        source: sourceChannel({ catchup_enabled: false }),
        postPage: sourcePostPage(),
        recoveryRequestIds: sourceRecoveryRequestIds(),
        telegramAccounts: [telegramAccount()],
        paging: { page: 1, snapshotAt: '2026-07-13T12:00:00Z', hasPrevious: false, hasNext: false },
        loadError: null,
        form: null
      }
    }).body;
    expect(catchupDisabled).toMatch(/name="message_limit"[^>]*disabled=""/);
    expect(catchupDisabled).toContain('Enable source catch-up');

    const unavailableAccount = render(SourceDetailWorkspace, {
      props: {
        source: sourceChannel(),
        postPage: sourcePostPage(),
        recoveryRequestIds: sourceRecoveryRequestIds(),
        telegramAccounts: [telegramAccount({ flood_wait_until: '2099-01-01T00:00:00Z' })],
        paging: { page: 1, snapshotAt: '2026-07-13T12:00:00Z', hasPrevious: false, hasNext: false },
        loadError: null,
        form: null
      }
    }).body;
    expect(unavailableAccount).toMatch(/name="message_limit"[^>]*disabled=""/);
    expect(unavailableAccount).toContain('assigned Telegram account is not ready');

    const initialFetchPending = render(SourceDetailWorkspace, {
      props: {
        source: sourceChannel({ oldest_observed_post_id: null, initial_catchup_completed: false }),
        postPage: sourcePostPage(),
        recoveryRequestIds: sourceRecoveryRequestIds(),
        telegramAccounts: [telegramAccount()],
        paging: { page: 1, snapshotAt: '2026-07-13T12:00:00Z', hasPrevious: false, hasNext: false },
        loadError: null,
        form: null
      }
    }).body;
    expect(initialFetchPending).toMatch(/name="message_limit"[^>]*disabled=""/);
    expect(initialFetchPending).toContain('Initial fetch pending');
    expect(initialFetchPending).toContain('Wait for the initial latest-message catch-up');
  });
});

function sourceRecoveryRequestIds(backfills: Record<string, string> = {}) {
  return {
    backfills,
    posts: { failed: '11111111-1111-4111-8111-111111111111' }
  };
}

function sourceChannel(overrides: Partial<AdminSourceChannelRead> = {}): AdminSourceChannelRead {
  return {
    id: 'source-id',
    platform: 'telegram',
    platform_id: 'daily_memes',
    username: 'daily_memes',
    title: 'Daily memes',
    subscriber_count: 1200,
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

function telegramAccount(overrides: Partial<AdminTelegramSessionRead> = {}): AdminTelegramSessionRead {
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

function sourcePostPage(): AdminSourcePostPageRead {
  const items = [
    sourcePost({ id: 'indexed', post_id: '10000', index_status: 'indexed', qdrant_status: 'synced', meilisearch_status: 'synced' }),
    sourcePost({ id: 'partial', post_id: '9999', index_status: 'partially_indexed', qdrant_status: 'synced', meilisearch_status: 'processing' }),
    sourcePost({ id: 'processing', post_id: '9998', index_status: 'processing', ingest_status: 'media_inspecting', meme_id: null, meme_file_id: null, pipeline_stage: null, pipeline_status: null, qdrant_status: null, meilisearch_status: null }),
    sourcePost({ id: 'failed', post_id: '9997', index_status: 'failed', pipeline_stage: 'embed', pipeline_status: 'failed', pipeline_error: 'Embedding provider unavailable.', qdrant_status: 'failed', meilisearch_status: 'pending', is_retryable: true, capabilities: ['replay_source_post'] }),
    sourcePost({ id: 'skipped', post_id: '9996', index_status: 'not_indexable', media_type: 'text', fetch_status: 'unsupported', ingest_outcome: 'skipped_unsupported_media', ingest_status: null, meme_id: null, meme_file_id: null, pipeline_stage: null, pipeline_status: null, qdrant_status: null, meilisearch_status: null })
  ];
  return {
    source_channel_id: 'source-id',
    snapshot_at: '2026-07-13T12:00:00Z',
    summary: { observed_count: 5, indexed_count: 1, partially_indexed_count: 1, processing_count: 1, failed_count: 1, not_indexable_count: 1 },
    items,
    total: 55,
    limit: 50,
    offset: 0
  };
}

function sourcePost(overrides: Partial<AdminSourcePostRead> = {}): AdminSourcePostRead {
  return {
    id: 'post-id',
    post_id: '10000',
    telegram_url: 'https://t.me/daily_memes/10000',
    published_at: '2026-07-13T09:30:00Z',
    observed_at: '2026-07-13T09:31:00Z',
    media_type: 'image',
    fetch_status: 'accepted',
    fetch_detail: null,
    ingest_outcome: 'ingested',
    ingest_status: 'materialized',
    meme_id: 'meme-id',
    meme_file_id: 'file-id',
    pipeline_stage: 'sync_meili',
    pipeline_status: 'succeeded',
    pipeline_error: null,
    qdrant_status: 'synced',
    meilisearch_status: 'synced',
    index_status: 'indexed',
    is_retryable: false,
    version: 'post-version',
    capabilities: [],
    blocked_reason: null,
    ...overrides
  };
}

function sourceBackfill(overrides: Partial<AdminSourceBackfillRead> = {}): AdminSourceBackfillRead {
  return {
    id: 'backfill-id',
    source_channel_id: 'source-id',
    status: 'failed',
    requested_count: 5000,
    scanned_count: 1250,
    remaining_count: 3750,
    cursor_post_id: '8750',
    attempt_count: 2,
    quarantined_count: 0,
    last_error_code: 'telegram_provider_unavailable',
    last_error_class: 'TimeoutError',
    safe_error: 'Telegram did not respond before the deadline.',
    is_retryable: true,
    next_attempt_at: null,
    last_progress_at: '2026-07-13T11:00:00Z',
    telegram_session_id: 'account-id',
    telegram_session_name: 'primary',
    created_at: '2026-07-13T10:00:00Z',
    started_at: '2026-07-13T10:01:00Z',
    finished_at: '2026-07-13T11:01:00Z',
    updated_at: '2026-07-13T11:01:00Z',
    version: 'backfill-version',
    capabilities: ['resume_backfill'],
    ...overrides
  };
}
