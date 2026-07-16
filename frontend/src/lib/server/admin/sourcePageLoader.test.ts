import { describe, expect, it, vi } from 'vitest';
import type { ApiFetch } from '$lib/api/client';
import { load as loadSourcePage } from '../../../routes/admin/sources/+page.server';
import { load as loadSourceDetailPage } from '../../../routes/admin/sources/[channelId]/+page.server';

describe('source admin page loader', () => {
  it('keeps independently loaded source data when one backend projection is temporarily unavailable', async () => {
    const calls: string[] = [];
    const fetch = vi.fn(async (input: RequestInfo | URL) => {
      const pathname = new URL(String(input)).pathname;
      calls.push(pathname);
      if (pathname === '/api/v1/admin/channel-suggestions') {
        return jsonResponse([{ id: 'suggestion-id' }]);
      }
      if (pathname === '/api/v1/admin/source-channels') {
        return jsonResponse({ detail: 'Source catalog is restarting after creation.' }, 503);
      }
      if (pathname === '/api/v1/admin/telegram/sessions') {
        return jsonResponse([{ id: 'ready-account-id' }]);
      }
      return jsonResponse({ detail: `Unexpected path: ${pathname}` }, 404);
    }) satisfies ApiFetch;

    const depends = vi.fn();
    const result = await loadSourcePage({
      depends,
      fetch,
      request: new Request('http://frontend.test/admin/sources', {
        headers: { cookie: 'memexpert_access_token=token' }
      })
    } as never) as {
      sourceAdmin: {
        suggestions: Array<{ id: string }>;
        sourceChannels: unknown[];
        telegramAccounts: Array<{ id: string }>;
      };
      sourceAdminErrors: {
        suggestions: string | null;
        sourceChannels: string | null;
        telegramAccounts: string | null;
      };
      loadError: string | null;
    };

    expect(depends).toHaveBeenCalledWith('app:admin-sources');
    expect(calls.sort()).toEqual([
      '/api/v1/admin/channel-suggestions',
      '/api/v1/admin/source-channels',
      '/api/v1/admin/telegram/sessions'
    ]);
    expect(result.sourceAdmin).toEqual({
      suggestions: [{ id: 'suggestion-id' }],
      sourceChannels: [],
      telegramAccounts: [{ id: 'ready-account-id' }]
    });
    expect(result.sourceAdminErrors).toEqual({
      suggestions: null,
      sourceChannels: 'Source catalog is restarting after creation.',
      telegramAccounts: null
    });
    expect(result.loadError).toBe('Source catalog is restarting after creation.');
  });

  it('keeps source management data when the fetched-message ledger is temporarily unavailable', async () => {
    const fetch = vi.fn(async (input: RequestInfo | URL) => {
      const pathname = new URL(String(input)).pathname;
      if (pathname === '/api/v1/admin/source-channels') {
        return jsonResponse([{ id: 'source-id' }]);
      }
      if (pathname === '/api/v1/admin/source-channels/source-id/posts') {
        return jsonResponse({ detail: 'Fetched-message ledger is restarting.' }, 503);
      }
      if (pathname === '/api/v1/admin/source-channels/source-id/backfills') {
        return jsonResponse({ items: [] });
      }
      if (pathname === '/api/v1/admin/telegram/sessions') {
        return jsonResponse([{ id: 'ready-account-id' }]);
      }
      return jsonResponse({ detail: `Unexpected path: ${pathname}` }, 404);
    }) satisfies ApiFetch;

    const result = await loadSourceDetailPage({
      fetch,
      params: { channelId: 'source-id' },
      request: new Request('http://frontend.test/admin/sources/source-id', {
        headers: { cookie: 'memexpert_access_token=token' }
      }),
      url: new URL('http://frontend.test/admin/sources/source-id')
    } as never) as {
      source: { id: string } | null;
      postPage: unknown | null;
      postLoadError: string | null;
      telegramAccounts: Array<{ id: string }>;
      loadError: string | null;
    };

    expect(result.source).toEqual({ id: 'source-id' });
    expect(result.postPage).toBeNull();
    expect(result.postLoadError).toBe('Fetched-message ledger is restarting.');
    expect(result.telegramAccounts).toEqual([{ id: 'ready-account-id' }]);
    expect(result.loadError).toBeNull();
  });
});

function jsonResponse(payload: unknown, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { 'content-type': 'application/json' }
  });
}
