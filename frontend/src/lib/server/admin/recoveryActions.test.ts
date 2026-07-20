import { describe, expect, it, vi } from 'vitest';
import type { ApiFetch } from '$lib/api/client';
import { recoveryActions } from './recoveryActions';

describe('admin recovery actions', () => {
  it('sends scoped, budgeted, acknowledged, version-fenced actions and explicit previews', async () => {
    const calls: Array<{ path: string; method: string; body: unknown }> = [];
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const path = new URL(String(input)).pathname;
      calls.push({ path, method: init?.method ?? 'GET', body: init?.body ? JSON.parse(String(init.body)) : null });
      return jsonResponse(batch(
        path.endsWith('/cancel') ? 'cancelling' : path.endsWith('/preview') || path.endsWith('/retry-failed-preview') ? 'preview' : 'queued'
      ));
    }) satisfies ApiFetch;

    await expect(recoveryActions.actRecoveryWork(actionEvent({
      request_id: '11111111-1111-4111-8111-111111111111',
      kind: 'pipeline_stage',
      work_id: 'file-1:ocr',
      version: 'version-1',
      action: 'replay_stage',
      scope: 'stage_and_dependents',
      retry_limit: '3',
      reason: 'Replay this terminal OCR stage after provider repair.',
      acknowledgement: ['terminal_override', 'stale_dependents']
    }, fetch))).resolves.toMatchObject({ message: 'Stage replay queued.', recoveryJobId: 'batch-1' });

    await expect(recoveryActions.previewRecoveryBatch(actionEvent({
      request_id: '22222222-2222-4222-8222-222222222222',
      action: 'replay_stage',
      scope: 'stage_only',
      retry_limit: '5',
      selector_type: 'explicit',
      reason: 'Preview a reviewed OCR cohort.',
      item: JSON.stringify({ kind: 'pipeline_stage', id: 'file-1:ocr', version: 'version-1' })
    }, fetch))).resolves.toMatchObject({ message: 'Preview created for 20 execution steps. Review it before scheduling.' });

    await expect(recoveryActions.scheduleRecoveryBatch(actionEvent({
      job_id: 'batch-1',
      version: 'batch-version',
      reason: 'Dispatch the reviewed cohort.'
    }, fetch))).resolves.toMatchObject({ message: 'Recovery batch scheduled.' });

    await expect(recoveryActions.cancelRecoveryBatch(actionEvent({
      job_id: 'batch-1',
      version: 'batch-version-2',
      reason: 'Failure rate increased.',
      acknowledge_cancel: 'on'
    }, fetch))).resolves.toMatchObject({ message: 'Cancellation started. Dispatched work will reconcile before totals finalize.' });

    await expect(recoveryActions.handoffRecoveryBatch(actionEvent({
      job_id: 'batch-1',
      version: 'batch-version-2',
      reason: 'Hand this incident to the on-call operator.',
      assigned_admin_user_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
    }, fetch))).resolves.toMatchObject({ message: expect.stringContaining('Operational handoff recorded') });

    await expect(recoveryActions.retryFailedRecoveryBatch(actionEvent({
      request_id: '33333333-3333-4333-8333-333333333333',
      job_id: 'batch-1',
      version: 'batch-version-3',
      reason: 'Retry only failed roots after provider recovery.',
      retry_limit: '1'
    }, fetch))).resolves.toMatchObject({ recoveryJobId: 'batch-1' });

    expect(calls).toEqual([
      {
        path: '/api/v1/admin/recovery/work/pipeline_stage/file-1%3Aocr/actions',
        method: 'POST',
        body: {
          request_id: '11111111-1111-4111-8111-111111111111',
          version: 'version-1',
          reason: 'Replay this terminal OCR stage after provider repair.',
          action: 'replay_stage',
          scope: 'stage_and_dependents',
          retry_limit: 3,
          acknowledgements: ['terminal_override', 'stale_dependents']
        }
      },
      {
        path: '/api/v1/admin/recovery/batches/preview',
        method: 'POST',
        body: {
          request_id: '22222222-2222-4222-8222-222222222222',
          reason: 'Preview a reviewed OCR cohort.',
          action: 'replay_stage',
          scope: 'stage_only',
          retry_limit: 5,
          acknowledgements: [],
          selector: {
            type: 'explicit',
            items: [{ kind: 'pipeline_stage', id: 'file-1:ocr', version: 'version-1' }]
          }
        }
      },
      {
        path: '/api/v1/admin/recovery/batches/batch-1/schedule',
        method: 'POST',
        body: { version: 'batch-version', reason: 'Dispatch the reviewed cohort.' }
      },
      {
        path: '/api/v1/admin/recovery/batches/batch-1/cancel',
        method: 'POST',
        body: { version: 'batch-version-2', reason: 'Failure rate increased.' }
      },
      {
        path: '/api/v1/admin/recovery/batches/batch-1/handoff',
        method: 'POST',
        body: {
          version: 'batch-version-2',
          reason: 'Hand this incident to the on-call operator.',
          assigned_admin_user_id: 'aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa'
        }
      },
      {
        path: '/api/v1/admin/recovery/batches/batch-1/retry-failed-preview',
        method: 'POST',
        body: {
          request_id: '33333333-3333-4333-8333-333333333333',
          version: 'batch-version-3',
          reason: 'Retry only failed roots after provider recovery.',
          retry_limit: 1
        }
      }
    ]);
  });

  it('sends an uncapped query selector and reports asynchronous preparation', async () => {
    let body: unknown;
    const fetch = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      body = JSON.parse(String(init?.body));
      return jsonResponse({
        ...batch('preparing'),
        action: 'regenerate_derivatives',
        preparation_scanned_count: 0,
        selected_root_count: 0,
        expanded_execution_count: 0
      });
    }) satisfies ApiFetch;

    const result = await recoveryActions.previewRecoveryBatch(actionEvent({
      request_id: '44444444-4444-4444-8444-444444444444',
      action: 'regenerate_derivatives',
      scope: 'stage_only',
      retry_limit: '3',
      selector_type: 'query',
      snapshot_at: '2026-07-20T10:00:00Z',
      query_filters: JSON.stringify({ outdated_web_video: true }),
      acknowledgement: 'terminal_override',
      reason: 'Regenerate every outdated web derivative.'
    }, fetch));

    expect(result).toMatchObject({ message: expect.stringContaining('Exact preview preparation started') });
    expect(body).toEqual({
      request_id: '44444444-4444-4444-8444-444444444444',
      reason: 'Regenerate every outdated web derivative.',
      action: 'regenerate_derivatives',
      scope: 'stage_only',
      retry_limit: 3,
      acknowledgements: ['terminal_override'],
      selector: {
        type: 'query',
        filters: { outdated_web_video: true },
        snapshot_at: '2026-07-20T10:00:00Z'
      }
    });
  });

  it('sends an uncapped successful-stage cascade with its terminal override acknowledgement', async () => {
    let body: unknown;
    const fetch = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      body = JSON.parse(String(init?.body));
      return jsonResponse({
        ...batch('preparing'),
        action: 'replay_stage',
        scope: 'stage_and_dependents'
      });
    }) satisfies ApiFetch;

    await recoveryActions.previewRecoveryBatch(actionEvent({
      request_id: '55555555-5555-4555-8555-555555555555',
      action: 'replay_stage',
      scope: 'stage_and_dependents',
      retry_limit: '5',
      selector_type: 'query',
      snapshot_at: '2026-07-20T10:00:00Z',
      query_filters: JSON.stringify({ successful_stage: true, stage: 'ocr' }),
      acknowledgement: 'terminal_override',
      reason: 'Replay every successful OCR stage and its descendants.'
    }, fetch));

    expect(body).toEqual({
      request_id: '55555555-5555-4555-8555-555555555555',
      reason: 'Replay every successful OCR stage and its descendants.',
      action: 'replay_stage',
      scope: 'stage_and_dependents',
      retry_limit: 5,
      acknowledgements: ['terminal_override'],
      selector: {
        type: 'query',
        filters: { successful_stage: true, stage: 'ocr' },
        snapshot_at: '2026-07-20T10:00:00Z'
      }
    });
  });

  it('rejects unknown actions/scopes/budgets, weak reasons, malformed selectors, and missing acknowledgements', async () => {
    const fetch = vi.fn(async () => jsonResponse({})) satisfies ApiFetch;
    const cases = [
      recoveryActions.actRecoveryWork(actionEvent({ request_id: '11111111-1111-4111-8111-111111111111', kind: 'pipeline_stage', work_id: '1', version: 'v', action: 'restart_service', reason: 'valid reason' }, fetch)),
      recoveryActions.actRecoveryWork(actionEvent({ request_id: '11111111-1111-4111-8111-111111111111', kind: 'pipeline_stage', work_id: '1', version: 'v', action: 'replay_stage', scope: 'everything', reason: 'valid reason' }, fetch)),
      recoveryActions.actRecoveryWork(actionEvent({ request_id: '11111111-1111-4111-8111-111111111111', kind: 'pipeline_stage', work_id: '1', version: 'v', action: 'replay_stage', retry_limit: '2', reason: 'valid reason' }, fetch)),
      recoveryActions.previewRecoveryBatch(actionEvent({ request_id: '11111111-1111-4111-8111-111111111111', action: 'regenerate_derivatives', selector_type: 'query', snapshot_at: 'now', query_filters: '[]', reason: 'valid reason' }, fetch)),
      recoveryActions.cancelRecoveryBatch(actionEvent({ job_id: 'batch-1', version: 'v', reason: 'stop safely' }, fetch)),
      recoveryActions.previewRecoveryBatch(actionEvent({ action: 'archive_dead_letter', reason: 'Archive malformed payload.' }, fetch))
    ];

    for (const result of cases) await expect(result).resolves.toMatchObject({ status: 400, data: { error: true } });
    expect(fetch).not.toHaveBeenCalled();
  });

  it('keeps the compatibility retry route idempotent with the submitted request id', async () => {
    const bodies: unknown[] = [];
    const fetch = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      bodies.push(JSON.parse(String(init?.body)));
      return jsonResponse(batch('queued'));
    }) satisfies ApiFetch;
    const values = {
      request_id: '88888888-8888-4888-8888-888888888888',
      kind: 'pipeline_stage',
      work_id: 'file-1:ocr',
      version: 'version-1',
      capability: 'retry_stage',
      reason: 'Repeat the same compatibility submission safely.'
    };

    await recoveryActions.retryRecoveryWork(actionEvent(values, fetch));
    await recoveryActions.retryRecoveryWork(actionEvent(values, fetch));
    expect(bodies).toEqual([
      { request_id: values.request_id, version: values.version, reason: values.reason, capability: values.capability },
      { request_id: values.request_id, version: values.version, reason: values.reason, capability: values.capability }
    ]);
  });
});

function actionEvent(values: Record<string, string | string[]>, fetch: ApiFetch) {
  const formData = new FormData();
  for (const [name, value] of Object.entries(values)) {
    if (Array.isArray(value)) for (const item of value) formData.append(name, item);
    else formData.set(name, value);
  }
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
