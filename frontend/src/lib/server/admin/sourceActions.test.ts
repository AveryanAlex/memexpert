import { describe, expect, it, vi } from 'vitest';
import type { ApiFetch } from '$lib/api/client';
import { sourceActions } from './sourceActions';

describe('source admin actions', () => {
  it('exports the named handlers used by the source workspace', () => {
    expect(Object.keys(sourceActions)).toEqual([
      'reviewSuggestion',
      'addSourceByReference',
      'addSourceChannel',
      'toggleSourceChannel',
      'markSourceChannelDead',
      'updateSourceChannelIngestion',
      'assignSourceChannel',
      'orphanSourceChannel',
      'validateSourceAccount',
      'backfillSourceChannel',
      'resumeSourceBackfill',
      'replaySourcePost'
    ]);
  });

  it('keeps source and suggestion API payloads stable and returns success messages', async () => {
    const channelId = '11111111-1111-4111-8111-111111111111';
    const sessionId = '22222222-2222-4222-8222-222222222222';
    const suggestionId = '33333333-3333-4333-8333-333333333333';
    const calls: Array<{ path: string; method: string; body: unknown }> = [];
    const fetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      calls.push({
        path: new URL(String(input)).pathname,
        method: init?.method ?? 'GET',
        body: init?.body ? JSON.parse(String(init.body)) : null
      });
      if (new URL(String(input)).pathname.endsWith('/validate')) {
        return jsonResponse({ channel_checked: true, channel_reference: '@source' });
      }
      if (new URL(String(input)).pathname.includes('/backfills/')) {
        return jsonResponse({ id: 'resume-recovery-job' });
      }
      if (new URL(String(input)).pathname.endsWith('/replay')) {
        return jsonResponse({ id: 'replay-recovery-job' });
      }
      return jsonResponse({});
    }) satisfies ApiFetch;

    await expect(sourceActions.reviewSuggestion(actionEvent({ suggestion_id: suggestionId, decision: 'reject', admin_note: 'duplicate' }, fetch))).resolves.toEqual({ message: 'Suggestion rejected.' });
    await expect(sourceActions.addSourceByReference(actionEvent({ reference: 'https://t.me/source', telegram_session_id: sessionId, suggestion_id: suggestionId, catchup_message_limit: '750' }, fetch))).resolves.toEqual({ message: 'Telegram source added and suggestion approved.' });
    await expect(sourceActions.addSourceChannel(actionEvent({ platform: 'telegram', platform_id: '-1001234', username: 'source', title: 'Source' }, fetch))).resolves.toEqual({ message: 'Source added without an account; ingestion is off.' });
    await expect(sourceActions.toggleSourceChannel(actionEvent({ channel_id: channelId, paused: 'true' }, fetch))).resolves.toEqual({ message: 'Source paused.' });
    await expect(sourceActions.markSourceChannelDead(actionEvent({ channel_id: channelId, confirmation: channelId }, fetch))).resolves.toEqual({ message: 'Source removed from crawling; checkpoint history was preserved.' });
    await expect(sourceActions.updateSourceChannelIngestion(actionEvent({ channel_id: channelId, catchup_message_limit: '250', live_enabled: 'on' }, fetch))).resolves.toEqual({ message: 'Source ingestion settings updated.' });
    await expect(sourceActions.assignSourceChannel(actionEvent({ channel_id: channelId, telegram_session_id: sessionId, note: 'move source' }, fetch))).resolves.toEqual({ message: 'Source assigned to a Telegram account.' });
    await expect(sourceActions.orphanSourceChannel(actionEvent({ channel_id: channelId, note: 'pause source' }, fetch))).resolves.toEqual({ message: 'Source is now unassigned and ingestion is off.' });
    await expect(sourceActions.validateSourceAccount(actionEvent({ source_channel_id: channelId, telegram_session_id: sessionId, note: 'check access' }, fetch))).resolves.toEqual({ message: 'Source access validated with @source.' });
    await expect(sourceActions.backfillSourceChannel(actionEvent({ channel_id: channelId, message_limit: '5000' }, fetch))).resolves.toEqual({ message: 'Older-message backfill queued for 5,000 messages.' });
    await expect(sourceActions.resumeSourceBackfill(actionEvent({ channel_id: channelId, job_id: 'backfill-id', version: 'version-1', request_id: '44444444-4444-4444-8444-444444444444', reason: 'Telegram account was reconnected.' }, fetch))).resolves.toEqual({ message: 'Failed backfill queued to resume from its durable cursor.', recoveryJobId: 'resume-recovery-job' });
    await expect(sourceActions.replaySourcePost(actionEvent({ channel_id: channelId, post_id: '1234', version: 'version-2', request_id: '55555555-5555-4555-8555-555555555555', reason: 'The provider is healthy again.' }, fetch))).resolves.toEqual({ message: 'Telegram post replay queued.', recoveryJobId: 'replay-recovery-job' });

    expect(calls).toEqual([
      { path: `/api/v1/admin/channel-suggestions/${suggestionId}/reject`, method: 'POST', body: { admin_note: 'duplicate' } },
      { path: '/api/v1/admin/telegram/channels/from-reference', method: 'POST', body: { reference: 'https://t.me/source', telegram_session_id: sessionId, suggestion_id: suggestionId, catchup_message_limit: 750 } },
      { path: '/api/v1/admin/source-channels', method: 'POST', body: { platform: 'telegram', platform_id: '-1001234', username: 'source', title: 'Source', orphaned: true, catchup_message_limit: 5000, catchup_enabled: false, live_enabled: false, engagement_enabled: false } },
      { path: `/api/v1/admin/source-channels/${channelId}/pause`, method: 'POST', body: null },
      { path: `/api/v1/admin/source-channels/${channelId}/mark-dead`, method: 'POST', body: { confirmation: channelId } },
      { path: `/api/v1/admin/telegram/channels/${channelId}`, method: 'PATCH', body: { catchup_enabled: false, live_enabled: true, engagement_enabled: false, catchup_message_limit: 250 } },
      { path: `/api/v1/admin/telegram/channels/${channelId}/assign`, method: 'POST', body: { telegram_session_id: sessionId, note: 'move source' } },
      { path: `/api/v1/admin/telegram/channels/${channelId}/orphan`, method: 'POST', body: { note: 'pause source' } },
      { path: `/api/v1/admin/telegram/sessions/${sessionId}/validate`, method: 'POST', body: { source_channel_id: channelId, note: 'check access' } },
      { path: `/api/v1/admin/source-channels/${channelId}/backfill`, method: 'POST', body: { message_limit: 5000 } },
      { path: `/api/v1/admin/source-channels/${channelId}/backfills/backfill-id/resume`, method: 'POST', body: { request_id: '44444444-4444-4444-8444-444444444444', version: 'version-1', reason: 'Telegram account was reconnected.' } },
      { path: `/api/v1/admin/source-channels/${channelId}/posts/1234/replay`, method: 'POST', body: { request_id: '55555555-5555-4555-8555-555555555555', version: 'version-2', reason: 'The provider is healthy again.' } }
    ]);
  });

  it('maps API failures to Svelte action failures', async () => {
    const result = await sourceActions.addSourceByReference(
      actionEvent(
        { reference: '@source', telegram_session_id: '22222222-2222-4222-8222-222222222222' },
        (async () => jsonResponse({ detail: 'Source already exists.' }, 409)) satisfies ApiFetch
      )
    );

    expect(result).toMatchObject({ status: 409, data: { message: 'Source already exists.', error: true } });
  });

  it('maps malformed forms to failures without making API requests', async () => {
    const fetch = vi.fn(async () => jsonResponse({})) satisfies ApiFetch;
    const channelId = '11111111-1111-4111-8111-111111111111';

    const malformedActions = [
      { result: sourceActions.reviewSuggestion(actionEvent({ decision: 'reject' }, fetch)), message: 'suggestion_id is required.' },
      { result: sourceActions.addSourceByReference(actionEvent({ reference: '@source' }, fetch)), message: 'telegram_session_id is required.' },
      { result: sourceActions.addSourceChannel(actionEvent({ platform: 'telegram', platform_id: '-100' }, fetch)), message: 'title is required.' },
      { result: sourceActions.addSourceChannel(actionEvent({ platform: 'reddit', platform_id: 'r/source', title: 'Source' }, fetch)), message: 'Only Telegram sources can be added until crawler support is available.' },
      { result: sourceActions.toggleSourceChannel(actionEvent({ channel_id: channelId, paused: 'yes' }, fetch)), message: 'paused must be true or false.' },
      { result: sourceActions.markSourceChannelDead(actionEvent({ channel_id: channelId }, fetch)), message: 'confirmation is required.' },
      { result: sourceActions.updateSourceChannelIngestion(actionEvent({}, fetch)), message: 'channel_id is required.' },
      { result: sourceActions.assignSourceChannel(actionEvent({ channel_id: channelId }, fetch)), message: 'telegram_session_id is required.' },
      { result: sourceActions.orphanSourceChannel(actionEvent({}, fetch)), message: 'channel_id is required.' },
      { result: sourceActions.validateSourceAccount(actionEvent({ source_channel_id: channelId }, fetch)), message: 'telegram_session_id is required.' },
      { result: sourceActions.backfillSourceChannel(actionEvent({ message_limit: '5000' }, fetch)), message: 'channel_id is required.' },
      { result: sourceActions.backfillSourceChannel(actionEvent({ channel_id: channelId, message_limit: '50001' }, fetch)), message: 'message_limit must be between 1 and 50000.' },
      { result: sourceActions.resumeSourceBackfill(actionEvent({ channel_id: channelId, job_id: 'backfill-id', version: 'v', reason: 'fixed' }, fetch)), message: 'request_id is required.' },
      { result: sourceActions.resumeSourceBackfill(actionEvent({ channel_id: channelId, version: 'v', request_id: '66666666-6666-4666-8666-666666666666', reason: 'fixed' }, fetch)), message: 'job_id is required.' },
      { result: sourceActions.replaySourcePost(actionEvent({ channel_id: channelId, post_id: '1', version: 'v', reason: 'fixed' }, fetch)), message: 'request_id is required.' },
      { result: sourceActions.replaySourcePost(actionEvent({ channel_id: channelId, post_id: '1', version: 'v', request_id: '77777777-7777-4777-8777-777777777777', reason: 'x' }, fetch)), message: 'reason must be between 3 and 500 characters.' }
    ];

    for (const malformed of malformedActions) {
      await expect(malformed.result).resolves.toMatchObject({ status: 400, data: { message: malformed.message, error: true } });
    }
    expect(fetch).not.toHaveBeenCalled();
  });

  it('forwards submitted request ids unchanged across repeated resume and replay submissions', async () => {
    const requestBodies: unknown[] = [];
    const fetch = vi.fn(async (_input: RequestInfo | URL, init?: RequestInit) => {
      requestBodies.push(init?.body ? JSON.parse(String(init.body)) : null);
      return jsonResponse({ id: 'recovery-job' });
    }) satisfies ApiFetch;
    const resumeValues = {
      channel_id: '11111111-1111-4111-8111-111111111111',
      job_id: 'backfill-id',
      version: 'backfill-version',
      request_id: '88888888-8888-4888-8888-888888888888',
      reason: 'Telegram account was reconnected.'
    };
    const replayValues = {
      channel_id: '11111111-1111-4111-8111-111111111111',
      post_id: '1234',
      version: 'post-version',
      request_id: '99999999-9999-4999-8999-999999999999',
      reason: 'The provider is healthy again.'
    };

    await sourceActions.resumeSourceBackfill(actionEvent(resumeValues, fetch));
    await sourceActions.resumeSourceBackfill(actionEvent(resumeValues, fetch));
    await sourceActions.replaySourcePost(actionEvent(replayValues, fetch));
    await sourceActions.replaySourcePost(actionEvent(replayValues, fetch));

    expect(requestBodies).toEqual([
      { request_id: resumeValues.request_id, version: resumeValues.version, reason: resumeValues.reason },
      { request_id: resumeValues.request_id, version: resumeValues.version, reason: resumeValues.reason },
      { request_id: replayValues.request_id, version: replayValues.version, reason: replayValues.reason },
      { request_id: replayValues.request_id, version: replayValues.version, reason: replayValues.reason }
    ]);
  });
});

function actionEvent(values: Record<string, string>, fetch: ApiFetch) {
  const formData = new FormData();
  for (const [name, value] of Object.entries(values)) formData.set(name, value);
  return {
    fetch,
    request: new Request('http://frontend.test/admin/sources', {
      method: 'POST',
      headers: { cookie: 'memexpert_access_token=token' },
      body: formData
    })
  } as never;
}

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), { status, headers: { 'content-type': 'application/json' } });
}
