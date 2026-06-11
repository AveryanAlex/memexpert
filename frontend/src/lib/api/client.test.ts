import { describe, expect, it, vi } from 'vitest';

import {
  addSourceChannel,
  ApiError,
  favoriteMeme,
  fetchAdminDashboard,
  fetchAdminSession,
  fetchCurrentSession,
  fetchMemeDetail,
  fetchMemePage,
  fetchTagLanding,
  pinMeme,
  refreshCurrentSession,
  removeSavedMeme,
  resolveModerationReport,
  reviewChannelSuggestion,
  saveMeme,
  startTelegramLink,
  unfavoriteMeme,
  unpinMeme,
  updateMemeModeration,
  type ApiFetch
} from './client';
import type { CurrentSessionRead, PublicMemeSearchPageRead } from './types';

const page: PublicMemeSearchPageRead = {
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
    expect(calls).toContain('/api/v1/admin/moderation-reports?limit=20');
    expect(calls).toContain('/api/v1/admin/moderation-decisions?limit=20');
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

function jsonResponse(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json', ...headers }
  });
}
