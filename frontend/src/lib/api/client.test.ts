import { describe, expect, it, vi } from 'vitest';

import { ApiError, fetchMemeDetail, fetchMemePage, fetchTagLanding, type ApiFetch } from './client';
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
