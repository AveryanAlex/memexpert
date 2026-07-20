import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import type { AdminRecoveryBatchRead, AdminRecoverySummaryRead, AdminRecoveryWorkPageRead, AdminRecoveryWorkRead } from '$lib/api/types';
import RecoveryWorkspace from '$lib/features/admin/recovery/RecoveryWorkspace.svelte';

describe('/admin/recovery workspace', () => {
  it('renders non-overlapping counts, URL filters, safe failures, capabilities, and pagination', () => {
    const { body } = render(RecoveryWorkspace, {
      props: {
        summary: recoverySummary(),
        workPage: recoveryPage(),
        jobsPage: { items: [], next_cursor: null },
        filters: {
          section: 'needs_attention',
          bucket: 'retryable',
          kind: null,
          source: '@memach',
          stage: null,
          reason: null,
          query: null,
          cursor: null,
          jobCursor: null
        },
        requestIds: {
          batchPreview: '11111111-1111-4111-8111-111111111111',
          allMatchingPreview: '11111111-1111-4111-8111-111111111113',
          outdatedVideoPreview: '11111111-1111-4111-8111-111111111112',
          successfulStagePreview: '11111111-1111-4111-8111-111111111114',
          work: {
            'backfill:backfill-1': '22222222-2222-4222-8222-222222222222',
            'source_post:blocked-post': '33333333-3333-4333-8333-333333333333'
          }
        },
        loadError: null,
        jobsLoadError: null,
        form: null
      }
    });

    expect(body).toContain('Replay &amp; Repair');
    expect(body).toContain('Needs attention');
    expect(body).toContain('Regenerate');
    expect(body).toContain('Jobs');
    expect(body).toContain('Retryable');
    expect(body).toContain('Blocked');
    expect(body).toContain('Stuck');
    expect(body).toContain('Dead-lettered');
    expect(body).toContain('action="/admin/recovery"');
    expect(body).toContain('name="source"');
    expect(body).toContain('value="@memach"');
    expect(body).toContain('Telegram did not respond before its deadline.');
    expect(body).toContain('Resume backfill');
    expect(body).toContain('Actions (1)');
    expect(body).toContain('data-recovery-action="resume_backfill"');
    expect(body).toContain('name="request_id" value="11111111-1111-4111-8111-111111111111"');
    expect(body).toContain('Reconnect the source account before retrying.');
    expect(body).toContain('data-recovery-action="replay_source_post"');
    expect(body).toContain('data-recovery-action-blocked');
    expect(body).toContain('Preview selected recovery work');
    expect(body).toContain('action="?/previewRecoveryBatch"');
    expect(body).toContain('name="selector_type" value="explicit"');
    expect(body).toContain('data-all-matching-preview');
    expect(body).toContain('name="request_id" value="11111111-1111-4111-8111-111111111113"');
    expect(body).toContain('name="selector_type" value="query"');
    expect(body).toContain('name="query_filters" value="{&quot;bucket&quot;:&quot;retryable&quot;,&quot;query&quot;:&quot;@memach&quot;}"');
    expect(body).toContain('name="snapshot_at" value="2026-07-15T12:00:00Z"');
    expect(body).toContain('Prepare all matching');
    expect(body).toContain('name="retry_limit"');
    expect(body).toContain('data-recovery-select-all');
    expect(body).toContain('Select all compatible recovery work on this page');
    expect(body).toContain('1 compatible on this page · 0 selected');
    expect(body).toContain('0 of 1 compatible rows selected for Resume backfill.');
    expect(body.indexOf('Batch action')).toBeLessThan(body.indexOf('<table'));
    expect(body.match(/<input[^>]+aria-label="Select @memach backfill for Resume backfill"[^>]*>/)?.[0]).not.toContain('disabled');
    expect(body.match(/<input[^>]+aria-label="Select Telegram post #99 for Resume backfill"[^>]*>/)?.[0]).toContain('disabled');
    expect(body).toContain('href="/admin/recovery?bucket=retryable&amp;source=%40memach&amp;cursor=next-cursor"');
  });

  it('renders a batch preview without dispatching it', () => {
    const body = render(RecoveryWorkspace, {
      props: {
        summary: recoverySummary(),
        workPage: { ...recoveryPage(), items: [], next_cursor: null },
        jobsPage: { items: [], next_cursor: null },
        filters: { section: 'needs_attention', bucket: null, kind: null, source: null, stage: null, reason: null, query: null, cursor: null, jobCursor: null },
        requestIds: { batchPreview: '44444444-4444-4444-8444-444444444444', allMatchingPreview: '44444444-4444-4444-8444-444444444446', outdatedVideoPreview: '44444444-4444-4444-8444-444444444445', successfulStagePreview: '44444444-4444-4444-8444-444444444447', work: {} },
        loadError: null,
        jobsLoadError: null,
        form: { message: 'Preview created.', batch: recoveryBatch() }
      }
    }).body;

    expect(body).toContain('Preview created.');
    expect(body).toContain('20 selected · 0 completed');
    expect(body).not.toContain('Type SCHEDULE');
    expect(body).toContain('action="?/scheduleRecoveryBatch"');
    expect(body).toContain('href="/admin/recovery/batches/batch-1"');
  });

  it('starts an uncapped outdated-video query and exposes preparing progress', () => {
    const summary = { ...recoverySummary(), outdated_web_video_count: 7400 };
    const body = render(RecoveryWorkspace, {
      props: {
        summary,
        workPage: recoveryPage(),
        jobsPage: { items: [], next_cursor: null },
        filters: { section: 'regenerate', bucket: null, kind: null, source: null, stage: null, reason: null, query: null, cursor: null, jobCursor: null },
        requestIds: { batchPreview: '44444444-4444-4444-8444-444444444444', allMatchingPreview: '44444444-4444-4444-8444-444444444446', outdatedVideoPreview: '55555555-5555-4555-8555-555555555555', successfulStagePreview: '66666666-6666-4666-8666-666666666666', work: {} },
        loadError: null,
        jobsLoadError: null,
        form: {
          message: 'Exact preview preparation started.',
          batch: recoveryBatch({
            action: 'regenerate_derivatives',
            status: 'preparing',
            selected_root_count: 1200,
            expanded_execution_count: 1200,
            preparation_scanned_count: 1500,
            excluded_count: 3
          })
        }
      }
    }).body;

    expect(body).toContain('Outdated web videos');
    expect(body).toContain('7,400');
    expect(body).toContain('web-h264-aac-1080p30-v2');
    expect(body).toContain('Select all matching');
    expect(body).toContain('name="selector_type" value="query"');
    expect(body).toContain('outdated_web_video');
    expect(body).toContain('Replay successful stages');
    expect(body).toContain('data-successful-stage-preview');
    expect(body).toContain('name="request_id" value="66666666-6666-4666-8666-666666666666"');
    expect(body).toContain('successful_stage');
    expect(body).toContain('name="action" value="replay_stage"');
    expect(body).toContain('Select all matching successful stages');
    expect(body).toContain('Stage-only replay leaves existing downstream data untouched');
    expect(body).toContain('1,500 scanned · 1,200 matched · 3 excluded');
  });
});

function recoverySummary(): AdminRecoverySummaryRead {
  return {
    retryable_count: 10,
    blocked_count: 2,
    stuck_count: 3,
    dead_lettered_count: 4
  };
}

function recoveryPage(): AdminRecoveryWorkPageRead {
  return {
    items: [
      recoveryWork(),
      recoveryWork({
        kind: 'source_post',
        id: 'blocked-post',
        bucket: 'blocked',
        title: 'Telegram post #99',
        status: 'failed',
        safe_error: null,
        error_code: 'source_account_unavailable',
        is_retryable: false,
        capabilities: [],
        actions: [
          {
            capability: 'replay_source_post',
            available: false,
            blocked_prerequisites: ['Reconnect the source account before retrying.']
          }
        ],
        blocked_reason: 'Reconnect the source account before retrying.',
        version: 'version-2'
      })
    ],
    next_cursor: 'next-cursor',
    snapshot_at: '2026-07-15T12:00:00Z'
  };
}

function recoveryWork(overrides: Partial<AdminRecoveryWorkRead> = {}): AdminRecoveryWorkRead {
  return {
    kind: 'backfill',
    id: 'backfill-1',
    bucket: 'retryable',
    title: '@memach backfill',
    source_label: '@memach',
    source_channel_id: 'source-1',
    post_id: null,
    meme_file_id: null,
    stage: null,
    target: null,
    status: 'failed',
    reason: 'telegram_provider_unavailable',
    safe_error: 'Telegram did not respond before its deadline.',
    error_code: 'telegram_provider_unavailable',
    is_retryable: true,
    attempt_count: 2,
    occurred_at: '2026-07-15T11:00:00Z',
    next_attempt_at: null,
    version: 'version-1',
    capabilities: ['resume_backfill'],
    actions: [{ capability: 'resume_backfill', available: true }],
    blocked_reason: null,
    details: { requested_count: 5000, scanned_count: 1250 },
    ...overrides
  };
}

function recoveryBatch(overrides: Partial<AdminRecoveryBatchRead> = {}): AdminRecoveryBatchRead {
  return {
    id: 'batch-1',
    request_id: '11111111-1111-4111-8111-111111111111',
    status: 'preview' as const,
    action: 'retry_stage' as const,
    reason: 'OCR capacity is healthy.',
    total_count: 20,
    completed_count: 0,
    failed_count: 0,
    created_at: '2026-07-15T12:00:00Z',
    updated_at: '2026-07-15T12:00:00Z',
    expires_at: '2026-07-15T12:05:00Z',
    scheduled_at: null,
    completed_at: null,
    cancelled_at: null,
    version: 'batch-version',
    items: [],
    ...overrides
  };
}
