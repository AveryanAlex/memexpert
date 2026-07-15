import { describe, expect, it, vi } from 'vitest';
import type { ApiFetch } from '$lib/api/client';
import { recoveryActions } from './recoveryActions';

describe('admin recovery actions', () => {
  it('sends idempotent, version-fenced, audited single and batch mutations', async () => {
    const calls: Array<{ path: string; method: string; body: unknown }> = [];
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input)).pathname;
      calls.push({ path, method: init?.method ?? 'GET', body: init?.body ? JSON.parse(String(init.body)) : null });
      return jsonResponse(batch(path.endsWith('/preview') ? 'preview' : path.endsWith('/cancel') ? 'cancelled' : 'queued'));
    }) satisfies ApiFetch;

    await expect(recoveryActions.retryRecoveryWork(actionEvent({
      request_id: '11111111-1111-4111-8111-111111111111',
      kind: 'pipeline_stage',
      work_id: 'file-1:ocr',
      version: 'version-1',
      capability: 'retry_stage',
      reason: 'OCR capacity is healthy again.'
    }, fetch))).resolves.toEqual({ message: 'Stage retry queued.', recoveryJobId: 'batch-1' });

    const preview = await recoveryActions.previewRecoveryBatch(actionEvent({
      request_id: '22222222-2222-4222-8222-222222222222',
      capability: 'retry_stage',
      reason: 'Retry a bounded OCR cohort.',
      item: JSON.stringify({ kind: 'pipeline_stage', id: 'file-1:ocr', version: 'version-1' })
    }, fetch));
    expect(preview).toMatchObject({ message: 'Preview created for 20 items. Review it before scheduling.' });

    await expect(recoveryActions.scheduleRecoveryBatch(actionEvent({
      request_id: '33333333-3333-4333-8333-333333333333',
      job_id: 'batch-1',
      version: 'batch-version',
      reason: 'Retry a bounded OCR cohort.',
      confirmation_phrase: 'SCHEDULE'
    }, fetch))).resolves.toMatchObject({ message: 'Recovery batch scheduled.' });

    await expect(recoveryActions.cancelRecoveryBatch(actionEvent({
      request_id: '44444444-4444-4444-8444-444444444444',
      job_id: 'batch-1',
      version: 'batch-version-2',
      reason: 'Failure rate increased.',
      confirmation_phrase: 'CANCEL'
    }, fetch))).resolves.toMatchObject({ message: 'Undispatched recovery items cancelled.' });

    expect(calls).toEqual([
      {
        path: '/api/v1/admin/recovery/work/pipeline_stage/file-1%3Aocr/retry',
        method: 'POST',
        body: { request_id: '11111111-1111-4111-8111-111111111111', version: 'version-1', reason: 'OCR capacity is healthy again.', capability: 'retry_stage' }
      },
      {
        path: '/api/v1/admin/recovery/batches/preview',
        method: 'POST',
        body: {
          request_id: '22222222-2222-4222-8222-222222222222',
          reason: 'Retry a bounded OCR cohort.',
          capability: 'retry_stage',
          items: [{ kind: 'pipeline_stage', id: 'file-1:ocr', version: 'version-1' }]
        }
      },
      {
        path: '/api/v1/admin/recovery/batches/batch-1/schedule',
        method: 'POST',
        body: { version: 'batch-version', reason: 'Retry a bounded OCR cohort.' }
      },
      {
        path: '/api/v1/admin/recovery/batches/batch-1/cancel',
        method: 'POST',
        body: { version: 'batch-version-2', reason: 'Failure rate increased.' }
      }
    ]);
  });

  it('rejects unknown capabilities, weak reasons, and missing typed confirmations', async () => {
    const fetch = vi.fn(async () => jsonResponse({})) satisfies ApiFetch;

    const cases = [
      recoveryActions.retryRecoveryWork(actionEvent({ kind: 'pipeline_stage', work_id: '1', version: 'v', capability: 'restart_service', reason: 'valid reason' }, fetch)),
      recoveryActions.retryRecoveryWork(actionEvent({ kind: 'pipeline_stage', work_id: '1', version: 'v', capability: 'retry_stage', reason: 'x' }, fetch)),
      recoveryActions.previewRecoveryBatch(actionEvent({ capability: 'archive_dead_letter', reason: 'Archive malformed payload.' }, fetch)),
      recoveryActions.scheduleRecoveryBatch(actionEvent({ job_id: 'batch-1', version: 'v', reason: 'ready', confirmation_phrase: 'schedule' }, fetch)),
      recoveryActions.cancelRecoveryBatch(actionEvent({ job_id: 'batch-1', version: 'v', reason: 'stop', confirmation_phrase: 'cancel' }, fetch))
    ];

    for (const result of cases) await expect(result).resolves.toMatchObject({ status: 400, data: { error: true } });
    expect(fetch).not.toHaveBeenCalled();
  });

  it('forwards submitted request ids unchanged across repeated retry and preview submissions', async () => {
    const requestBodies: unknown[] = [];
    const fetch = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      requestBodies.push(init?.body ? JSON.parse(String(init.body)) : null);
      return jsonResponse(batch('queued'));
    }) satisfies ApiFetch;
    const values = {
      request_id: '88888888-8888-4888-8888-888888888888',
      kind: 'pipeline_stage',
      work_id: 'file-1:ocr',
      version: 'version-1',
      capability: 'retry_stage',
      reason: 'Repeat the same browser submission safely.'
    };

    await recoveryActions.retryRecoveryWork(actionEvent(values, fetch));
    await recoveryActions.retryRecoveryWork(actionEvent(values, fetch));

    const previewValues = {
      request_id: '99999999-9999-4999-8999-999999999999',
      capability: 'retry_stage',
      reason: 'Repeat the same preview submission safely.',
      item: JSON.stringify({ kind: 'pipeline_stage', id: 'file-1:ocr', version: 'version-1' })
    };
    await recoveryActions.previewRecoveryBatch(actionEvent(previewValues, fetch));
    await recoveryActions.previewRecoveryBatch(actionEvent(previewValues, fetch));

    expect(requestBodies).toEqual([
      {
        request_id: values.request_id,
        version: values.version,
        reason: values.reason,
        capability: values.capability
      },
      {
        request_id: values.request_id,
        version: values.version,
        reason: values.reason,
        capability: values.capability
      },
      {
        request_id: previewValues.request_id,
        reason: previewValues.reason,
        capability: previewValues.capability,
        items: [{ kind: 'pipeline_stage', id: 'file-1:ocr', version: 'version-1' }]
      },
      {
        request_id: previewValues.request_id,
        reason: previewValues.reason,
        capability: previewValues.capability,
        items: [{ kind: 'pipeline_stage', id: 'file-1:ocr', version: 'version-1' }]
      }
    ]);
  });

  it('requires a valid submitted request id for retry and preview mutations', async () => {
    const fetch = vi.fn(async () => jsonResponse(batch('queued'))) satisfies ApiFetch;

    const retry = recoveryActions.retryRecoveryWork(actionEvent({
      kind: 'pipeline_stage',
      work_id: 'file-1:ocr',
      version: 'version-1',
      capability: 'retry_stage',
      reason: 'OCR capacity is healthy again.'
    }, fetch));
    const preview = recoveryActions.previewRecoveryBatch(actionEvent({
      capability: 'retry_stage',
      reason: 'Retry a bounded OCR cohort.',
      item: JSON.stringify({ kind: 'pipeline_stage', id: 'file-1:ocr', version: 'version-1' })
    }, fetch));
    const malformed = recoveryActions.retryRecoveryWork(actionEvent({
      request_id: 'not-a-uuid',
      kind: 'pipeline_stage',
      work_id: 'file-1:ocr',
      version: 'version-1',
      capability: 'retry_stage',
      reason: 'OCR capacity is healthy again.'
    }, fetch));

    await expect(retry).resolves.toMatchObject({
      status: 400,
      data: { message: 'request_id is required.', error: true }
    });
    await expect(preview).resolves.toMatchObject({
      status: 400,
      data: { message: 'request_id is required.', error: true }
    });
    await expect(malformed).resolves.toMatchObject({
      status: 400,
      data: { message: 'request_id must be a UUID.', error: true }
    });
    expect(fetch).not.toHaveBeenCalled();
  });
});

function actionEvent(values: Record<string, string>, fetch: ApiFetch) {
  const formData = new FormData();
  for (const [name, value] of Object.entries(values)) formData.set(name, value);
  return {
    fetch,
    request: new Request('http://frontend.test/admin/recovery', {
      method: 'POST',
      headers: { cookie: 'memexpert_access_token=token' },
      body: formData
    })
  } as never;
}

function batch(status: string) {
  return {
    id: 'batch-1',
    request_id: '11111111-1111-4111-8111-111111111111',
    status,
    action: 'retry_stage',
    reason: 'Retry a bounded OCR cohort.',
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
    items: []
  };
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), { status, headers: { 'content-type': 'application/json' } });
}
