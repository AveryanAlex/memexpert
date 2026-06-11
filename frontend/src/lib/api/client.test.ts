import { describe, expect, it, vi } from 'vitest';

import {
  addSourceChannel,
  ApiError,
  fetchAdminSession,
  fetchMemeDetail,
  fetchMemePage,
  fetchTagLanding,
  reviewChannelSuggestion,
  type ApiFetch
} from './client';
import type { PublicMemeSearchPageRead } from './types';

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

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' }
  });
}
