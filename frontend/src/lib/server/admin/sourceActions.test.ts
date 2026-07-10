import { describe, expect, it, vi } from 'vitest';
import type { ApiFetch } from '$lib/api/client';
import { sourceActions } from './sourceActions';

describe('source admin actions', () => {
  it('exports the named handlers used by the source workspace', () => {
    expect(Object.keys(sourceActions)).toEqual([
      'reviewSuggestion',
      'addSourceChannel',
      'toggleSourceChannel',
      'markSourceChannelDead',
      'updateSourceChannelIngestion',
      'assignSourceChannel',
      'orphanSourceChannel',
      'validateSourceAccount'
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
      return jsonResponse({});
    }) satisfies ApiFetch;

    await expect(sourceActions.reviewSuggestion(actionEvent({ suggestion_id: suggestionId, decision: 'reject', admin_note: 'duplicate' }, fetch))).resolves.toEqual({ message: 'Suggestion rejected.' });
    await expect(sourceActions.addSourceChannel(actionEvent({ platform: 'telegram', platform_id: '-1001234', username: 'source', title: 'Source' }, fetch))).resolves.toEqual({ message: 'Source added without an account; ingestion is off.' });
    await expect(sourceActions.toggleSourceChannel(actionEvent({ channel_id: channelId, paused: 'true' }, fetch))).resolves.toEqual({ message: 'Source paused.' });
    await expect(sourceActions.markSourceChannelDead(actionEvent({ channel_id: channelId, confirmation: channelId }, fetch))).resolves.toEqual({ message: 'Source removed from crawling; checkpoint history was preserved.' });
    await expect(sourceActions.updateSourceChannelIngestion(actionEvent({ channel_id: channelId, catchup_message_limit: '250', live_enabled: 'on' }, fetch))).resolves.toEqual({ message: 'Source ingestion settings updated.' });
    await expect(sourceActions.assignSourceChannel(actionEvent({ channel_id: channelId, telegram_session_id: sessionId, note: 'move source' }, fetch))).resolves.toEqual({ message: 'Source assigned to a Telegram account.' });
    await expect(sourceActions.orphanSourceChannel(actionEvent({ channel_id: channelId, note: 'pause source' }, fetch))).resolves.toEqual({ message: 'Source is now unassigned and ingestion is off.' });
    await expect(sourceActions.validateSourceAccount(actionEvent({ source_channel_id: channelId, telegram_session_id: sessionId, note: 'check access' }, fetch))).resolves.toEqual({ message: 'Source access validated with @source.' });

    expect(calls).toEqual([
      { path: `/api/v1/admin/channel-suggestions/${suggestionId}/reject`, method: 'POST', body: { admin_note: 'duplicate' } },
      { path: '/api/v1/admin/source-channels', method: 'POST', body: { platform: 'telegram', platform_id: '-1001234', username: 'source', title: 'Source', orphaned: true, catchup_message_limit: 500, catchup_enabled: false, live_enabled: false, engagement_enabled: false } },
      { path: `/api/v1/admin/source-channels/${channelId}/pause`, method: 'POST', body: null },
      { path: `/api/v1/admin/source-channels/${channelId}/mark-dead`, method: 'POST', body: { confirmation: channelId } },
      { path: `/api/v1/admin/telegram/channels/${channelId}`, method: 'PATCH', body: { catchup_enabled: false, live_enabled: true, engagement_enabled: false, catchup_message_limit: 250 } },
      { path: `/api/v1/admin/telegram/channels/${channelId}/assign`, method: 'POST', body: { telegram_session_id: sessionId, note: 'move source' } },
      { path: `/api/v1/admin/telegram/channels/${channelId}/orphan`, method: 'POST', body: { note: 'pause source' } },
      { path: `/api/v1/admin/telegram/sessions/${sessionId}/validate`, method: 'POST', body: { source_channel_id: channelId, note: 'check access' } }
    ]);
  });

  it('maps API failures to Svelte action failures', async () => {
    const result = await sourceActions.addSourceChannel(
      actionEvent(
        { platform: 'telegram', platform_id: '-1001234', title: 'Source' },
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
      { result: sourceActions.addSourceChannel(actionEvent({ platform: 'telegram', platform_id: '-100' }, fetch)), message: 'title is required.' },
      { result: sourceActions.addSourceChannel(actionEvent({ platform: 'reddit', platform_id: 'r/source', title: 'Source' }, fetch)), message: 'Only Telegram sources can be added until crawler support is available.' },
      { result: sourceActions.toggleSourceChannel(actionEvent({ channel_id: channelId, paused: 'yes' }, fetch)), message: 'paused must be true or false.' },
      { result: sourceActions.markSourceChannelDead(actionEvent({ channel_id: channelId }, fetch)), message: 'confirmation is required.' },
      { result: sourceActions.updateSourceChannelIngestion(actionEvent({}, fetch)), message: 'channel_id is required.' },
      { result: sourceActions.assignSourceChannel(actionEvent({ channel_id: channelId }, fetch)), message: 'telegram_session_id is required.' },
      { result: sourceActions.orphanSourceChannel(actionEvent({}, fetch)), message: 'channel_id is required.' },
      { result: sourceActions.validateSourceAccount(actionEvent({ source_channel_id: channelId }, fetch)), message: 'telegram_session_id is required.' }
    ];

    for (const malformed of malformedActions) {
      await expect(malformed.result).resolves.toMatchObject({ status: 400, data: { message: malformed.message, error: true } });
    }
    expect(fetch).not.toHaveBeenCalled();
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
