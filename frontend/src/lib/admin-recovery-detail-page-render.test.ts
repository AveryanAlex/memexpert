import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';
import type { AdminRecoveryBatchRead, AdminRecoveryWorkRead } from '$lib/api/types';
import RecoveryBatchDetail from '$lib/features/admin/recovery/RecoveryBatchDetail.svelte';
import RecoveryWorkDetail from '$lib/features/admin/recovery/RecoveryWorkDetail.svelte';

describe('admin recovery detail views', () => {
  it('shows canonical state, safe details, and one backend-declared primary action', () => {
    const body = render(RecoveryWorkDetail, {
      props: {
        work: workDetail(),
        requestId: '11111111-1111-4111-8111-111111111111',
        loadError: null,
        form: null
      }
    }).body;

    expect(body).toContain('Canonical work state');
    expect(body).toContain('Work details');
    expect(body).toContain('OCR exceeded its 120 second deadline.');
    expect(body).toContain('image/jpeg');
    expect(body).toContain('Retry stage');
    expect(body).toContain('action="?/retryRecoveryWork"');
    expect(body).toContain('name="request_id" value="11111111-1111-4111-8111-111111111111"');
    expect(body.match(/data-recovery-action="retry_stage"/g)).toHaveLength(1);
  });

  it('links a queued single recovery action to its durable job', () => {
    const body = render(RecoveryWorkDetail, {
      props: {
        work: workDetail(),
        requestId: '11111111-1111-4111-8111-111111111111',
        loadError: null,
        form: {
          message: 'Stage retry queued.',
          recoveryJobId: '22222222-2222-4222-8222-222222222222'
        }
      }
    }).body;

    expect(body).toContain('Open recovery job');
    expect(body).toContain('href="/admin/recovery/batches/22222222-2222-4222-8222-222222222222"');
  });

  it('requires typed scheduling for previews and exposes bounded cancellation after queueing', () => {
    const preview = render(RecoveryBatchDetail, {
      props: { batch: batch(), loadError: null, form: null }
    }).body;
    expect(preview).toContain('Type SCHEDULE');
    expect(preview).toContain('action="?/scheduleRecoveryBatch"');

    const running = render(RecoveryBatchDetail, {
      props: { batch: batch({ status: 'running' }), loadError: null, form: null }
    }).body;
    expect(running).toContain('Only undispatched items are cancelled.');
    expect(running).toContain('action="?/cancelRecoveryBatch"');
    expect(running).toContain('Cancel undispatched items');
  });
});

function workDetail(): AdminRecoveryWorkRead {
  return {
    kind: 'pipeline_stage',
    id: 'file-1:ocr',
    bucket: 'retryable',
    title: 'OCR for file-1',
    source_label: '@log4inpowerken',
    source_channel_id: 'source-1',
    post_id: '100',
    meme_file_id: 'file-1',
    stage: 'ocr',
    target: null,
    status: 'failed',
    reason: 'ocr_timeout',
    safe_error: 'OCR exceeded its 120 second deadline.',
    error_code: 'ocr_timeout',
    is_retryable: true,
    attempt_count: 3,
    occurred_at: '2026-07-15T11:00:00Z',
    next_attempt_at: null,
    version: 'version-1',
    capabilities: ['retry_stage'],
    blocked_reason: null,
    details: { mime_type: 'image/jpeg' }
  };
}

function batch(overrides: Partial<AdminRecoveryBatchRead> = {}): AdminRecoveryBatchRead {
  return {
    id: 'batch-1',
    request_id: '11111111-1111-4111-8111-111111111111',
    status: 'preview',
    action: 'retry_stage',
    reason: 'OCR capacity is healthy.',
    total_count: 20,
    completed_count: 3,
    failed_count: 1,
    created_at: '2026-07-15T12:00:00Z',
    updated_at: '2026-07-15T12:01:00Z',
    expires_at: '2026-07-15T12:05:00Z',
    scheduled_at: null,
    completed_at: null,
    cancelled_at: null,
    version: 'batch-version',
    items: [
      {
        id: 'item-1',
        work_kind: 'pipeline_stage',
        work_id: 'file-1:ocr',
        action: 'retry_stage',
        status: 'queued',
        normalized_reason: null,
        safe_error: null,
        dispatched_at: null,
        finished_at: null
      }
    ],
    ...overrides
  };
}
