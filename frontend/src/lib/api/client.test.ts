import { describe, expect, it, vi } from 'vitest';

import {
  addSourceChannel,
  ApiError,
  createBlockedPerceptualHash,
  createCollection,
  createCollectionInvite,
  deactivateBlockedPerceptualHash,
  deleteBlockedPerceptualHash,
  deleteCollection,
  favoriteMeme,
  fetchAdminDashboard,
  fetchAdminSession,
  fetchCollectionDetail,
  fetchCollections,
  fetchCurrentSession,
  fetchMemeLibrary,
  fetchMemeDetail,
  fetchMemePage,
  fetchMemePopularitySummary,
  fetchTagLanding,
  fetchTagTrendSummaries,
  fetchTemplateTrendSummaries,
  fetchTrendPage,
  pinMeme,
  refreshCurrentSession,
  removeMemeFromCollection,
  removeSavedMeme,
  reportMeme,
  resolveModerationReport,
  reviewChannelSuggestion,
  saveMeme,
  setActiveSaveCollection,
  startTelegramLink,
  unfavoriteMeme,
  unpinMeme,
  updateActiveSaveCollection,
  updateBlockedPerceptualHash,
  updateMemeModeration,
  type ApiFetch
} from './client';
import type { CurrentSessionRead, MemeLibraryRead, PublicMemeSearchPageRead, PublicMemeTrendPageRead } from './types';

const page: PublicMemeSearchPageRead = {
  items: [],
  limit: 12,
  offset: 0,
  total: 0,
  has_more: false
};

const trendPage: PublicMemeTrendPageRead = {
  items: [],
  limit: 12,
  offset: 0,
  total: 0,
  has_more: false
};

describe('catalog API client', () => {
  it('sends plain text search query and forwards SSR cookies', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const headers = new Headers(init?.headers);

      expect(url.pathname).toBe('/api/v1/memes/search');
      expect(url.searchParams.get('query')).toBe('frog reaction');
      expect(url.searchParams.get('limit')).toBe('12');
      expect(url.searchParams.get('offset')).toBe('24');
      expect(url.searchParams.has('query_vector')).toBe(false);
      expect(headers.get('cookie')).toBe('sid=abc');

      return jsonResponse(page);
    }) satisfies ApiFetch;

    await fetchMemePage({
      fetch: mockFetch,
      baseUrl: 'https://api.memexpert.test',
      query: '  frog reaction  ',
      limit: 12,
      offset: 24,
      cookieHeader: 'sid=abc'
    });

    expect(mockFetch).toHaveBeenCalledOnce();
  });

  it('uses browse for empty queries', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));

      expect(url.pathname).toBe('/api/v1/memes/browse');
      expect(url.searchParams.has('query')).toBe(false);
      expect(url.searchParams.get('limit')).toBe('12');
      expect(url.searchParams.get('offset')).toBe('0');

      return jsonResponse(page);
    }) satisfies ApiFetch;

    await fetchMemePage({
      fetch: mockFetch,
      baseUrl: 'https://api.memexpert.test',
      query: '',
      limit: 12,
      offset: 0
    });

    expect(mockFetch).toHaveBeenCalledOnce();
  });

  it('forwards catalog filters with repeated tag params', async () => {
    const calls: string[] = [];
    const mockFetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));
      calls.push(`${url.pathname}?${url.searchParams.toString()}`);

      expect(url.searchParams.getAll('tags')).toEqual(['reaction', 'cat']);
      expect(url.searchParams.get('include_nsfw')).toBe('true');
      expect(url.searchParams.get('media_type')).toBe('gif');
      expect(url.searchParams.get('language')).toBe('mixed');

      return jsonResponse(page);
    }) satisfies ApiFetch;

    await fetchMemePage({
      fetch: mockFetch,
      baseUrl: 'https://api.memexpert.test',
      query: 'frog',
      tags: [' reaction ', '', 'cat'],
      includeNsfw: true,
      mediaType: 'gif',
      language: 'mixed',
      limit: 12,
      offset: 0
    });
    await fetchMemePage({
      fetch: mockFetch,
      baseUrl: 'https://api.memexpert.test',
      query: '',
      tags: ['reaction', 'cat'],
      includeNsfw: true,
      mediaType: 'gif',
      language: 'mixed',
      limit: 12,
      offset: 12
    });

    expect(calls).toEqual([
      '/api/v1/memes/search?limit=12&offset=0&tags=reaction&tags=cat&include_nsfw=true&media_type=gif&language=mixed&query=frog',
      '/api/v1/memes/browse?limit=12&offset=12&tags=reaction&tags=cat&include_nsfw=true&media_type=gif&language=mixed'
    ]);
  });

  it('requests detail with the public NSFW filter', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));

      expect(url.pathname).toBe('/api/v1/memes/slug/frog-wizard');
      expect(url.searchParams.get('include_nsfw')).toBe('false');

      return jsonResponse(memeDetail({ id: 'meme-123', seo_page_slug: 'frog-wizard' }));
    }) satisfies ApiFetch;

    await fetchMemeDetail({
      fetch: mockFetch,
      baseUrl: 'https://api.memexpert.test',
      memeId: 'frog-wizard'
    });

    expect(mockFetch).toHaveBeenCalledOnce();
  });

  it('keeps UUID detail links on the compatible id route', async () => {
    const memeId = '11111111-1111-4111-8111-111111111111';
    const mockFetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));

      expect(url.pathname).toBe(`/api/v1/memes/${memeId}`);

      return jsonResponse(memeDetail({ id: memeId, seo_page_slug: 'frog-wizard' }));
    }) satisfies ApiFetch;

    await fetchMemeDetail({
      fetch: mockFetch,
      baseUrl: 'https://api.memexpert.test',
      memeId
    });

    expect(mockFetch).toHaveBeenCalledOnce();
  });

  it('requests tag landing pages with pagination', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));

      expect(url.pathname).toBe('/api/v1/memes/tags/reaction');
      expect(url.searchParams.get('limit')).toBe('12');
      expect(url.searchParams.get('offset')).toBe('24');

      return jsonResponse({
        kind: 'tag',
        slug: 'reaction',
        title: 'Reaction memes',
        description: 'Browse reaction memes.',
        page
      });
    }) satisfies ApiFetch;

    await fetchTagLanding({
      fetch: mockFetch,
      baseUrl: 'https://api.memexpert.test',
      slug: 'reaction',
      limit: 12,
      offset: 24
    });

    expect(mockFetch).toHaveBeenCalledOnce();
  });

  it('requests trend ranking pages and aggregate summaries', async () => {
    const calls: string[] = [];
    const mockFetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));
      calls.push(`${url.pathname}?${url.searchParams.toString()}`);
      if (url.pathname === '/api/v1/memes/trends') {
        expect(url.searchParams.get('ranking')).toBe('fastest_rising');
        return jsonResponse(trendPage);
      }
      return jsonResponse([]);
    }) satisfies ApiFetch;

    await fetchTrendPage({
      fetch: mockFetch,
      baseUrl: 'https://api.memexpert.test',
      ranking: 'fastest_rising',
      limit: 12,
      offset: 24
    });
    await fetchTagTrendSummaries({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', limit: 8, offset: 0 });
    await fetchTemplateTrendSummaries({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', limit: 8, offset: 0 });

    expect(calls).toEqual([
      '/api/v1/memes/trends?ranking=fastest_rising&limit=12&offset=24',
      '/api/v1/memes/trends/tags?limit=8&offset=0',
      '/api/v1/memes/trends/templates?limit=8&offset=0'
    ]);
  });

  it('requests per-meme popularity summary through aggregate endpoint', async () => {
    const memeId = '11111111-1111-4111-8111-111111111111';
    const mockFetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));

      expect(url.pathname).toBe(`/api/v1/memes/${memeId}/popularity`);
      expect(url.searchParams.get('include_nsfw')).toBe('false');

      return jsonResponse({ meme_id: memeId, trend: null, sparkline: [] });
    }) satisfies ApiFetch;

    await fetchMemePopularitySummary({
      fetch: mockFetch,
      baseUrl: 'https://api.memexpert.test',
      memeId
    });

    expect(mockFetch).toHaveBeenCalledOnce();
  });

  it('surfaces API not found details', async () => {
    const mockFetch = vi.fn(async () => jsonResponse({ detail: 'Meme was not found.' }, 404)) satisfies ApiFetch;

    await expect(
      fetchMemeDetail({
        fetch: mockFetch,
        baseUrl: 'https://api.memexpert.test',
        memeId: 'missing'
      })
    ).rejects.toEqual(new ApiError(404, 'Meme was not found.'));
  });

  it('posts and deletes favorite actions with credentials', async () => {
    const calls: Array<{ method: string | undefined; path: string; credentials: RequestCredentials | undefined }> = [];
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? new URL(input, 'https://web.memexpert.test') : new URL(String(input));
      calls.push({ method: init?.method, path: url.pathname, credentials: init?.credentials });
      return jsonResponse(init?.method === 'DELETE' ? { removed: true } : { id: 'collection-meme-1' });
    }) satisfies ApiFetch;

    await favoriteMeme({ fetch: mockFetch, memeId: 'meme-123' });
    await unfavoriteMeme({ fetch: mockFetch, memeId: 'meme-123' });

    expect(calls).toEqual([
      { method: 'POST', path: '/api/v1/memes/meme-123/favorite', credentials: 'include' },
      { method: 'DELETE', path: '/api/v1/memes/meme-123/favorite', credentials: 'include' }
    ]);
  });

  it('uses existing save and pin action endpoints', async () => {
    const calls: Array<{ method: string | undefined; path: string }> = [];
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      calls.push({ method: init?.method, path: url.pathname });
      return jsonResponse(init?.method === 'DELETE' ? { removed: false } : { id: 'action-row-1' });
    }) satisfies ApiFetch;

    await saveMeme({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', memeId: 'meme-123' });
    await removeSavedMeme({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', memeId: 'meme-123' });
    await pinMeme({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', memeId: 'meme-123' });
    await unpinMeme({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', memeId: 'meme-123' });

    expect(calls).toEqual([
      { method: 'POST', path: '/api/v1/memes/meme-123/save' },
      { method: 'DELETE', path: '/api/v1/memes/meme-123/save' },
      { method: 'POST', path: '/api/v1/memes/meme-123/pin' },
      { method: 'DELETE', path: '/api/v1/memes/meme-123/pin' }
    ]);
  });

  it('submits meme reports with JSON body and CSRF-compatible request header', async () => {
    const memeId = '11111111-1111-4111-8111-111111111111';
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const headers = new Headers(init?.headers);

      expect(url.pathname).toBe(`/api/v1/memes/${memeId}/report`);
      expect(init?.method).toBe('POST');
      expect(init?.credentials).toBe('include');
      expect(headers.get('content-type')).toBe('application/json');
      expect(headers.get('x-requested-with')).toBe('XMLHttpRequest');
      expect(headers.get('cookie')).toBe('memexpert_access_token=full');
      expect(JSON.parse(String(init?.body))).toEqual({ reason: 'harassment', note: 'targets someone' });

      return jsonResponse({
        id: '22222222-2222-4222-8222-222222222222',
        meme_id: memeId,
        status: 'pending',
        reason: 'harassment',
        note: 'targets someone',
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z'
      });
    }) satisfies ApiFetch;

    const report = await reportMeme({
      fetch: mockFetch,
      baseUrl: 'https://api.memexpert.test',
      cookieHeader: 'memexpert_access_token=full',
      memeId,
      reason: 'harassment',
      note: 'targets someone'
    });

    expect(report.status).toBe('pending');
    expect(mockFetch).toHaveBeenCalledOnce();
  });

  it('loads current session through the web bootstrap endpoint and forwards Set-Cookie hooks', async () => {
    const responses: Response[] = [];
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const headers = new Headers(init?.headers);

      expect(url.pathname).toBe('/api/v1/auth/session');
      expect(headers.get('cookie')).toBe('memexpert_access_token=old');

      return jsonResponse(sessionPayload('guest'), 200, {
        'set-cookie': 'memexpert_access_token=new; HttpOnly; Secure; SameSite=lax; Path=/'
      });
    }) satisfies ApiFetch;

    const session = await fetchCurrentSession({
      fetch: mockFetch,
      baseUrl: 'https://api.memexpert.test',
      cookieHeader: 'memexpert_access_token=old',
      onResponse: (response) => responses.push(response)
    });

    expect(session.user.account_type).toBe('guest');
    expect(responses[0].headers.get('set-cookie')).toContain('memexpert_access_token=new');
  });

  it('starts and refreshes Telegram linking without token payloads', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const method = init?.method ?? 'GET';

      if (url.pathname === '/api/v1/auth/link/telegram') {
        expect(method).toBe('POST');
        return jsonResponse({
          code: 'abc123',
          deep_link_url: 'https://t.me/memexpertbot?start=link_abc123',
          expires_at: '2026-06-12T12:00:00Z',
          expires_in_seconds: 600,
          return_url: 'https://memexpert.test/account/telegram'
        });
      }

      expect(url.pathname).toBe('/api/v1/auth/session/refresh');
      expect(method).toBe('POST');
      return jsonResponse(sessionPayload('full'));
    }) satisfies ApiFetch;

    const link = await startTelegramLink({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test' });
    const refreshed = await refreshCurrentSession({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test' });

    expect(link.deep_link_url).toContain('https://t.me/');
    expect(refreshed.user.account_type).toBe('full');
    expect('access_token' in refreshed).toBe(false);
  });

  it('saves a favorite through cookie-only transport', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const headers = new Headers(init?.headers);

      expect(url.pathname).toBe('/api/v1/memes/11111111-1111-4111-8111-111111111111/favorite');
      expect(init?.method).toBe('POST');
      expect(headers.get('cookie')).toBe('memexpert_access_token=guest');

      return jsonResponse({ id: 'save-1' });
    }) satisfies ApiFetch;

    await favoriteMeme({
      fetch: mockFetch,
      baseUrl: 'https://api.memexpert.test',
      memeId: '11111111-1111-4111-8111-111111111111',
      cookieHeader: 'memexpert_access_token=guest'
    });

    expect(mockFetch).toHaveBeenCalledOnce();
  });

  it('loads profile library data with SSR cookie forwarding', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const headers = new Headers(init?.headers);

      expect(url.pathname).toBe('/api/v1/memes/library');
      expect(headers.get('cookie')).toBe('memexpert_access_token=guest');

      return jsonResponse(libraryPayload());
    }) satisfies ApiFetch;

    const library = await fetchMemeLibrary({
      fetch: mockFetch,
      baseUrl: 'https://api.memexpert.test',
      cookieHeader: 'memexpert_access_token=guest'
    });

    expect(library.favorites[0].viewer_has_favorited).toBe(true);
    expect(library.pinned_memes[0].viewer_has_pinned).toBe(true);
    expect(library.active_save_collection?.title).toBe('Favorites');
    expect(mockFetch).toHaveBeenCalledOnce();
  });

  it('updates active save collection with JSON body and cookies', async () => {
    const collectionId = '44444444-4444-4444-8444-444444444444';
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const headers = new Headers(init?.headers);

      expect(url.pathname).toBe('/api/v1/memes/active-save-collection');
      expect(init?.method).toBe('PUT');
      expect(headers.get('cookie')).toBe('memexpert_access_token=full');
      expect(headers.get('content-type')).toBe('application/json');
      expect(JSON.parse(String(init?.body))).toEqual({ collection_id: collectionId });

      return jsonResponse({ ...sessionPayload('full').user, active_save_collection_id: collectionId });
    }) satisfies ApiFetch;
    const onResponse = vi.fn();

    const user = await updateActiveSaveCollection({
      fetch: mockFetch,
      baseUrl: 'https://api.memexpert.test',
      cookieHeader: 'memexpert_access_token=full',
      onResponse,
      collectionId
    });

    expect(user.active_save_collection_id).toBe(collectionId);
    expect(mockFetch).toHaveBeenCalledOnce();
    expect(onResponse).toHaveBeenCalledOnce();
  });

  it('uses collection list, detail, mutation, active-save, remove, and invite endpoints', async () => {
    const collectionId = '11111111-1111-4111-8111-111111111111';
    const memeId = '22222222-2222-4222-8222-222222222222';
    const calls: Array<{ method: string; path: string; body: unknown }> = [];
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      calls.push({ method: init?.method ?? 'GET', path: url.pathname, body: init?.body ? JSON.parse(String(init.body)) : null });

      if (url.pathname === '/api/v1/collections' && (init?.method ?? 'GET') === 'GET') {
        return jsonResponse({ collections: [], active_save_collection_id: null });
      }
      if (url.pathname === `/api/v1/collections/${collectionId}` && (init?.method ?? 'GET') === 'GET') {
        return jsonResponse({ ...collectionSummary(collectionId), saved_memes: [] });
      }
      if (url.pathname === `/api/v1/collections/${collectionId}/invites`) {
        return jsonResponse({ invite: collectionInvite(collectionId), token: 'invite-token', join_path: '/collection/invite/invite-token' });
      }
      if (url.pathname.endsWith('/active-save')) {
        return jsonResponse({ active_save_collection_id: collectionId });
      }
      if (url.pathname.endsWith(`/memes/${memeId}`)) {
        return jsonResponse(init?.method === 'DELETE' ? { removed: true } : { saved: true });
      }
      if (init?.method === 'DELETE') {
        return jsonResponse({ deleted: true });
      }
      return jsonResponse(collectionSummary(collectionId));
    }) satisfies ApiFetch;

    await fetchCollections({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test' });
    await fetchCollectionDetail({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', collectionId });
    await createCollection({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', body: { title: 'Launch', visibility: 'private' } });
    await setActiveSaveCollection({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', collectionId });
    await removeMemeFromCollection({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', collectionId, memeId });
    await createCollectionInvite({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', collectionId, body: { role: 'viewer', max_uses: 1 } });
    await deleteCollection({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', collectionId });

    expect(calls.map((call) => [call.method, call.path])).toEqual([
      ['GET', '/api/v1/collections'],
      ['GET', `/api/v1/collections/${collectionId}`],
      ['POST', '/api/v1/collections'],
      ['PUT', `/api/v1/collections/${collectionId}/active-save`],
      ['DELETE', `/api/v1/collections/${collectionId}/memes/${memeId}`],
      ['POST', `/api/v1/collections/${collectionId}/invites`],
      ['DELETE', `/api/v1/collections/${collectionId}`]
    ]);
    expect(calls[2].body).toEqual({ title: 'Launch', visibility: 'private' });
    expect(calls[5].body).toEqual({ role: 'viewer', max_uses: 1 });
  });
});

describe('admin API client', () => {
  it('fetches the admin session with SSR cookies', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const headers = new Headers(init?.headers);

      expect(url.pathname).toBe('/api/v1/admin/session');
      expect(headers.get('cookie')).toBe('memexpert_access_token=token');

      return jsonResponse({ user: { id: 'u1', account_type: 'full', email: null, telegram_id: null, google_id: null, is_admin: true } });
    }) satisfies ApiFetch;

    await fetchAdminSession({
      fetch: mockFetch,
      baseUrl: 'https://api.memexpert.test',
      cookieHeader: 'memexpert_access_token=token'
    });

    expect(mockFetch).toHaveBeenCalledOnce();
  });

  it('posts admin writes with cookie and CSRF-compatible request header', async () => {
    const suggestionId = '11111111-1111-4111-8111-111111111111';
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const headers = new Headers(init?.headers);

      expect(url.pathname).toBe(`/api/v1/admin/channel-suggestions/${suggestionId}/approve`);
      expect(init?.method).toBe('POST');
      expect(headers.get('cookie')).toBe('memexpert_access_token=token');
      expect(headers.get('x-requested-with')).toBe('XMLHttpRequest');
      expect(JSON.parse(String(init?.body))).toEqual({ admin_note: 'approved from UI' });

      return jsonResponse({
        id: suggestionId,
        user_id: '22222222-2222-4222-8222-222222222222',
        platform: 'telegram',
        channel_url: 'https://t.me/source',
        status: 'approved',
        admin_note: 'approved from UI',
        reviewed_at: '2026-01-01T00:00:00Z',
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z'
      });
    }) satisfies ApiFetch;

    await reviewChannelSuggestion(
      {
        fetch: mockFetch,
        baseUrl: 'https://api.memexpert.test',
        cookieHeader: 'memexpert_access_token=token',
        body: { admin_note: 'approved from UI' }
      },
      suggestionId,
      'approve'
    );

    expect(mockFetch).toHaveBeenCalledOnce();
  });

  it('adds source channels through the admin route', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));

      expect(url.pathname).toBe('/api/v1/admin/source-channels');
      expect(init?.method).toBe('POST');

      return jsonResponse({
        id: '33333333-3333-4333-8333-333333333333',
        platform: 'telegram',
        platform_id: 'source-id',
        username: 'source',
        title: 'Source',
        subscriber_count: null,
        is_active: true,
        is_paused: false,
        catchup_enabled: true,
        catchup_message_limit: 500,
        session_id: null,
        last_read_post_id: null,
        last_fetched_at: null,
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z'
      });
    }) satisfies ApiFetch;

    await addSourceChannel({
      fetch: mockFetch,
      baseUrl: 'https://api.memexpert.test',
      body: { platform: 'telegram', platform_id: 'source-id', title: 'Source' }
    });

    expect(mockFetch).toHaveBeenCalledOnce();
  });

  it('loads moderation reports and decision history with the admin dashboard', async () => {
    const calls: string[] = [];
    const mockFetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));
      calls.push(`${url.pathname}?${url.searchParams.toString()}`);

      if (url.pathname === '/api/v1/admin/moderation-reports') {
        expect(url.searchParams.get('limit')).toBe('20');
        return jsonResponse([moderationReportPayload()]);
      }
      if (url.pathname === '/api/v1/admin/moderation-decisions') {
        expect(url.searchParams.get('limit')).toBe('20');
        return jsonResponse([moderationDecisionPayload()]);
      }
      return jsonResponse([]);
    }) satisfies ApiFetch;

    const dashboard = await fetchAdminDashboard({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test' });

    expect(dashboard.reports).toHaveLength(1);
    expect(dashboard.decisions).toHaveLength(1);
    expect(calls).toContain('/api/v1/admin/blocked-perceptual-hashes?');
    expect(calls).toContain('/api/v1/admin/moderation-reports?limit=20');
    expect(calls).toContain('/api/v1/admin/moderation-decisions?limit=20');
  });

  it('manages blocked perceptual hashes through admin endpoints', async () => {
    const blockedHashId = '66666666-6666-4666-8666-666666666666';
    const calls: Array<{ method: string | undefined; path: string; body: unknown }> = [];
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      calls.push({
        method: init?.method,
        path: url.pathname,
        body: init?.body ? JSON.parse(String(init.body)) : null
      });
      return jsonResponse(blockedPhashPayload({ id: blockedHashId }));
    }) satisfies ApiFetch;

    await createBlockedPerceptualHash({
      fetch: mockFetch,
      baseUrl: 'https://api.memexpert.test',
      body: { perceptual_hash: 'abcdef1234567890', hash_size: 64, max_hamming_distance: 1, reason: 'spam' }
    });
    await updateBlockedPerceptualHash(
      {
        fetch: mockFetch,
        baseUrl: 'https://api.memexpert.test',
        body: { max_hamming_distance: 2, note: 'updated' }
      },
      blockedHashId
    );
    await deactivateBlockedPerceptualHash(
      { fetch: mockFetch, baseUrl: 'https://api.memexpert.test', body: { note: 'pause' } },
      blockedHashId
    );
    await deleteBlockedPerceptualHash({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test' }, blockedHashId);

    expect(calls).toEqual([
      {
        method: 'POST',
        path: '/api/v1/admin/blocked-perceptual-hashes',
        body: { perceptual_hash: 'abcdef1234567890', hash_size: 64, max_hamming_distance: 1, reason: 'spam' }
      },
      {
        method: 'PATCH',
        path: `/api/v1/admin/blocked-perceptual-hashes/${blockedHashId}`,
        body: { max_hamming_distance: 2, note: 'updated' }
      },
      {
        method: 'POST',
        path: `/api/v1/admin/blocked-perceptual-hashes/${blockedHashId}/deactivate`,
        body: { note: 'pause' }
      },
      {
        method: 'DELETE',
        path: `/api/v1/admin/blocked-perceptual-hashes/${blockedHashId}`,
        body: null
      }
    ]);
  });

  it('resolves moderation reports through an audited admin write', async () => {
    const reportId = '44444444-4444-4444-8444-444444444444';
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const headers = new Headers(init?.headers);

      expect(url.pathname).toBe(`/api/v1/admin/moderation-reports/${reportId}/resolve`);
      expect(init?.method).toBe('POST');
      expect(headers.get('x-requested-with')).toBe('XMLHttpRequest');
      expect(JSON.parse(String(init?.body))).toEqual({ action: 'mark_nsfw', reason: 'nsfw', note: 'confirmed' });

      return jsonResponse({ ...moderationReportPayload(), id: reportId, status: 'resolved' });
    }) satisfies ApiFetch;

    await resolveModerationReport(
      {
        fetch: mockFetch,
        baseUrl: 'https://api.memexpert.test',
        body: { action: 'mark_nsfw', reason: 'nsfw', note: 'confirmed' }
      },
      reportId
    );

    expect(mockFetch).toHaveBeenCalledOnce();
  });

  it('updates direct meme moderation flags with audit reason and note', async () => {
    const memeId = '55555555-5555-4555-8555-555555555555';
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const headers = new Headers(init?.headers);

      expect(url.pathname).toBe(`/api/v1/admin/memes/${memeId}/moderation`);
      expect(init?.method).toBe('PATCH');
      expect(headers.get('x-requested-with')).toBe('XMLHttpRequest');
      expect(JSON.parse(String(init?.body))).toEqual({
        is_public: false,
        is_nsfw: true,
        reason: 'spam',
        note: 'manual override'
      });

      return jsonResponse({ ...adminMemePayload(), id: memeId, is_public: false, is_nsfw: true });
    }) satisfies ApiFetch;

    await updateMemeModeration(
      {
        fetch: mockFetch,
        baseUrl: 'https://api.memexpert.test',
        body: { is_public: false, is_nsfw: true, reason: 'spam', note: 'manual override' }
      },
      memeId
    );

    expect(mockFetch).toHaveBeenCalledOnce();
  });
});

function memeDetail(overrides: { id: string; seo_page_slug: string | null }) {
  return {
    id: overrides.id,
    media_type: 'image',
    language: 'en',
    is_nsfw: false,
    popularity_score: 1,
    like_count: 0,
    tags: [],
    primary_file: null,
    caption: null,
    seo_page_slug: overrides.seo_page_slug,
    viewer_has_favorited: false,
    viewer_has_saved: false,
    viewer_has_pinned: false,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ocr_text: null,
    seo_title: null,
    seo_description: null,
    seo_alt_text: null,
    seo_body_text: null,
    seo_model_id: null,
    seo_prompt_version: null,
    seo_generated_at: null,
    files: []
  };
}

function collectionSummary(collectionId: string) {
  return {
    collection: {
      id: collectionId,
      owner_id: '22222222-2222-4222-8222-222222222222',
      title: 'Launch',
      description: null,
      kind: 'custom',
      visibility: 'private',
      memberships: [],
      invites: [],
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z'
    },
    viewer_role: 'owner',
    capabilities: {
      can_view: true,
      can_add_memes: true,
      can_remove_memes: true,
      can_rename: true,
      can_delete: true,
      can_create_invites: true,
      can_set_active_save: true
    },
    active_save_collection_id: null
  };
}

function collectionInvite(collectionId: string) {
  return {
    id: '33333333-3333-4333-8333-333333333333',
    collection_id: collectionId,
    created_by_user_id: '22222222-2222-4222-8222-222222222222',
    role: 'viewer',
    channel: 'direct_link',
    label: null,
    status: 'pending',
    max_uses: 1,
    use_count: 0,
    expires_at: null,
    last_used_at: null,
    revoked_at: null,
    recipient_email: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z'
  };
}

function sessionPayload(accountType: 'full' | 'guest'): CurrentSessionRead {
  return {
    user: {
      id: '22222222-2222-4222-8222-222222222222',
      account_type: accountType,
      telegram_id: accountType === 'full' ? 123 : null,
      google_id: null,
      email: null,
      email_verified_at: null,
      language: 'any',
      nsfw_enabled: false,
      token_nonce: 0,
      status: 'active',
      guest_expires_at: accountType === 'guest' ? '2026-07-12T00:00:00Z' : null,
      active_save_collection_id: null,
      is_admin: accountType === 'full',
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z'
    },
    linked_providers: {
      email: null,
      email_verified_at: null,
      has_password: false,
      google_linked: false,
      telegram_linked: accountType === 'full'
    }
  };
}

function libraryPayload(): MemeLibraryRead {
  const favorite = memeCard('11111111-1111-4111-8111-111111111111', {
    viewer_has_favorited: true,
    viewer_has_saved: true,
    viewer_has_pinned: false
  });
  const pinned = memeCard('22222222-2222-4222-8222-222222222222', {
    viewer_has_favorited: false,
    viewer_has_saved: false,
    viewer_has_pinned: true
  });
  const collection = {
    id: '33333333-3333-4333-8333-333333333333',
    owner_id: '22222222-2222-4222-8222-222222222222',
    title: 'Favorites',
    description: null,
    kind: 'favorites' as const,
    visibility: 'private' as const,
    role: 'owner' as const,
    can_write: true,
    saved_meme_count: 1,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z'
  };

  return {
    favorites: [favorite],
    pinned_memes: [pinned],
    collections: [collection],
    active_save_collection: collection
  };
}

function memeCard(id: string, flags: Partial<MemeLibraryRead['favorites'][number]>): MemeLibraryRead['favorites'][number] {
  return {
    id,
    media_type: 'image',
    language: 'en',
    is_nsfw: false,
    popularity_score: 1,
    like_count: 3,
    tags: ['reaction'],
    primary_file: null,
    caption: 'Reaction meme',
    seo_page_slug: null,
    viewer_has_favorited: false,
    viewer_has_saved: false,
    viewer_has_pinned: false,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    ...flags
  };
}

function adminMemePayload() {
  return {
    id: '33333333-3333-4333-8333-333333333333',
    media_type: 'image',
    language: 'en',
    is_nsfw: false,
    is_public: true,
    popularity_score: 1,
    like_count: 0,
    tags: [],
    template_id: null,
    author_user_id: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z'
  };
}

function moderationReportPayload() {
  return {
    id: '44444444-4444-4444-8444-444444444444',
    meme_id: '33333333-3333-4333-8333-333333333333',
    reporter_user_id: '22222222-2222-4222-8222-222222222222',
    status: 'pending',
    reason: 'nsfw',
    note: 'reported from UI',
    resolved_by_admin_user_id: null,
    resolved_at: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    meme: adminMemePayload()
  };
}

function moderationDecisionPayload() {
  return {
    id: '66666666-6666-4666-8666-666666666666',
    meme_id: '33333333-3333-4333-8333-333333333333',
    report_id: '44444444-4444-4444-8444-444444444444',
    admin_user_id: '11111111-1111-4111-8111-111111111111',
    action: 'mark_nsfw',
    reason: 'nsfw',
    note: 'confirmed',
    previous_is_public: true,
    previous_is_nsfw: false,
    new_is_public: true,
    new_is_nsfw: true,
    created_at: '2026-01-01T00:00:00Z'
  };
}

function blockedPhashPayload(overrides: { id: string }) {
  return {
    id: overrides.id,
    perceptual_hash: 'abcdef1234567890',
    hash_algorithm: 'phash',
    hash_size: 64,
    max_hamming_distance: 1,
    reason: 'spam',
    note: null,
    is_active: true,
    created_by_admin_user_id: '11111111-1111-4111-8111-111111111111',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z'
  };
}

function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json', ...headers }
  });
}
