import { describe, expect, it, vi } from 'vitest';

import {
  addSourceChannel,
  addAdminTelegramChannel,
  addAdminTelegramChannelFromReference,
  assignAdminTelegramChannel,
  ApiError,
  backfillAdminSourceChannel,
  cancelAdminTelegramLoginAttempt,
  completeAdminTelegramPhoneCodeLogin,
  completeAdminTelegramPhonePasswordLogin,
  completeAdminTelegramQrLogin,
  createBlockedPerceptualHash,
  createAdminTelegramSession,
  createCollection,
  createCollectionInvite,
  deactivateBlockedPerceptualHash,
  deleteAdminTelegramSession,
  deleteBlockedPerceptualHash,
  deleteCollection,
  favoriteMeme,
  fetchAdminMemeTemplates,
  fetchAdminAnalyticsAudience,
  fetchAdminAnalyticsContent,
  fetchAdminAnalyticsEngagement,
  fetchAdminAnalyticsOverview,
  fetchAdminAnalyticsSearchQueries,
  fetchAdminAnalyticsSearchQueryDetail,
  fetchAdminOverview,
  fetchAdminSourceChannelPosts,
  fetchAdminSeoReviewRows,
  fetchAdminSession,
  fetchAdminTelegramChannelGroups,
  fetchAdminTelegramChannels,
  fetchAdminTelegramSessions,
  fetchCollectionDetail,
  fetchCollections,
  fetchCurrentSession,
  fetchHomeFeed,
  fetchMemeLibrary,
  fetchMemeAnalytics,
  fetchMemeCollectionChoices,
  fetchMemeDetail,
  fetchMemePage,
  fetchMemePopularitySummary,
  fetchMemeSources,
  fetchProfileStats,
  fetchPinterestFeed,
  fetchSeoMemes,
  fetchSeoSummary,
  fetchSeoTags,
  fetchSeoTemplates,
  fetchSimilarMemes,
  fetchTagLanding,
  fetchTagTrendSummaries,
  fetchTemplateTrendSummaries,
  fetchTrendComparison,
  fetchTrendTimeline,
  fetchTrendPage,
  markSourceChannelDead,
  pinMeme,
  recordMemeDetailClick,
  recordMemeDownload,
  recordMemeImpression,
  recordMemeShare,
  recordMemeView,
  regenerateMemeSeoPage,
  removeCollectionMember,
  removeMemeFromCollection,
  removeSavedMeme,
  orphanAdminTelegramChannel,
  reorderPins,
  reportMeme,
  resolveModerationReport,
  revokeCollectionInvite,
  reviewChannelSuggestion,
  saveMeme,
  setActiveSaveCollection,
  startAdminTelegramPhoneLogin,
  startAdminTelegramQrLogin,
  startTelegramLink,
  unfavoriteMeme,
  unpinMeme,
  updateActiveSaveCollection,
  updateAdminTelegramChannel,
  updateAdminTelegramSession,
  updateBlockedPerceptualHash,
  updateCollectionMemberRole,
  updateMemeModeration,
  updateMemeSeoPage,
  updateUserPreferences,
  validateAdminTelegramSession,
  type ApiFetch
} from './client';
import type { MemeActionAttribution } from '$lib/memeActions';
import type { CurrentSessionRead, MemeLibraryRead, PublicMemeSearchPageRead, PublicMemeTrendPageRead } from './types';

const page: PublicMemeSearchPageRead = {
  items: [],
  limit: 12,
  offset: 0,
  total: 0,
  has_more: false,
  request_id: 'req_test'
};

const trendPage: PublicMemeTrendPageRead = {
  items: [],
  limit: 12,
  offset: 0,
  total: 0,
  has_more: false,
  request_id: 'req_trend_test'
};

describe('catalog API client', () => {
  it('requests bounded admin analytics dashboards, query sort modes, and query outcomes', async () => {
    const requests: string[] = [];
    const mockFetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));
      requests.push(`${url.pathname}?${url.searchParams.toString()}`);
      return jsonResponse({});
    }) satisfies ApiFetch;
    const baseRequest = {
      fetch: mockFetch,
      baseUrl: 'https://api.memexpert.test',
      startDate: '2026-06-01',
      endDate: '2026-06-30',
      cookieHeader: 'sid=admin'
    };

    await fetchAdminAnalyticsOverview(baseRequest);
    await fetchAdminAnalyticsEngagement(baseRequest);
    await fetchAdminAnalyticsAudience(baseRequest);
    await fetchAdminAnalyticsContent(baseRequest);
    await fetchAdminAnalyticsSearchQueries({ ...baseRequest, limit: 50, offset: 100, sort: 'niche' });
    await fetchAdminAnalyticsSearchQueryDetail({ ...baseRequest, queryKey: '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef' });

    expect(requests).toEqual([
      '/api/v1/admin/analytics/overview?start_date=2026-06-01&end_date=2026-06-30',
      '/api/v1/admin/analytics/engagement?start_date=2026-06-01&end_date=2026-06-30',
      '/api/v1/admin/analytics/audience?start_date=2026-06-01&end_date=2026-06-30',
      '/api/v1/admin/analytics/content?start_date=2026-06-01&end_date=2026-06-30',
      '/api/v1/admin/analytics/search-queries?start_date=2026-06-01&end_date=2026-06-30&limit=50&offset=100&sort=niche',
      '/api/v1/admin/analytics/search-queries/detail?start_date=2026-06-01&end_date=2026-06-30&query_key=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef'
    ]);
  });

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

  it('uses home feed endpoint with cookies and pagination for home no-query pages', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const headers = new Headers(init?.headers);

      expect(url.pathname).toBe('/api/v1/memes/home-feed');
      expect(url.searchParams.has('query')).toBe(false);
      expect(url.searchParams.get('limit')).toBe('12');
      expect(url.searchParams.get('offset')).toBe('24');
      expect(url.searchParams.has('scope')).toBe(false);
      expect(url.searchParams.has('collection_ids')).toBe(false);
      expect(headers.get('cookie')).toBe('memexpert_access_token=guest');

      return jsonResponse(page);
    }) satisfies ApiFetch;

    await fetchHomeFeed({
      fetch: mockFetch,
      baseUrl: 'https://api.memexpert.test',
      limit: 12,
      offset: 24,
      cookieHeader: 'memexpert_access_token=guest'
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
      expect(url.searchParams.get('scope')).toBe('collections');
      expect(url.searchParams.getAll('collection_ids')).toEqual(['team', 'shared']);

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
      scope: 'collections',
      collectionIds: ['team', 'shared'],
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
      scope: 'collections',
      collectionIds: ['team', 'shared'],
      limit: 12,
      offset: 12
    });

    expect(calls).toEqual([
      '/api/v1/memes/search?limit=12&offset=0&tags=reaction&tags=cat&include_nsfw=true&media_type=gif&language=mixed&scope=collections&collection_ids=team&collection_ids=shared&query=frog',
      '/api/v1/memes/browse?limit=12&offset=12&tags=reaction&tags=cat&include_nsfw=true&media_type=gif&language=mixed&scope=collections&collection_ids=team&collection_ids=shared'
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

  it('requests stable source pages and selectable professional analytics', async () => {
    const calls: string[] = [];
    const mockFetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));
      calls.push(`${url.pathname}?${url.searchParams.toString()}`);
      return jsonResponse({});
    }) satisfies ApiFetch;

    await fetchMemeSources({
      fetch: mockFetch,
      baseUrl: 'https://api.memexpert.test',
      memeId: 'meme-123',
      sort: 'interaction_rate_desc',
      limit: 10,
      offset: 20,
      snapshotAt: '2026-07-20T10:00:00.000Z',
      cookieHeader: 'sid=viewer'
    });
    await fetchMemeAnalytics({
      fetch: mockFetch,
      baseUrl: 'https://api.memexpert.test',
      memeId: 'meme-123',
      window: '90d'
    });

    expect(calls).toEqual([
      '/api/v1/memes/meme-123/sources?include_nsfw=false&sort=interaction_rate_desc&limit=10&offset=20&snapshot_at=2026-07-20T10%3A00%3A00.000Z',
      '/api/v1/memes/meme-123/analytics?include_nsfw=false&window=90d'
    ]);
  });

  it('requests similar memes through the canonical id endpoint', async () => {
    const memeId = '11111111-1111-4111-8111-111111111111';
    const mockFetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));

      expect(url.pathname).toBe(`/api/v1/memes/${memeId}/similar`);
      expect(url.searchParams.get('include_nsfw')).toBe('false');
      expect(url.searchParams.get('limit')).toBe('12');
      expect(url.searchParams.get('offset')).toBe('24');

      return jsonResponse(page);
    }) satisfies ApiFetch;

    await fetchSimilarMemes({
      fetch: mockFetch,
      baseUrl: 'https://api.memexpert.test',
      memeId,
      limit: 12,
      offset: 24
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

  it('requests public SEO summary, sitemap pages, and Pinterest feed pages', async () => {
    const calls: string[] = [];
    const mockFetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));
      calls.push(`${url.pathname}?${url.searchParams.toString()}`);

      if (url.pathname === '/api/v1/seo/summary') {
        return jsonResponse({ public_safe_meme_count: 12, tag_count: 3, template_count: 2, updated_at: '2026-06-14T09:30:00Z' });
      }

      if (url.pathname === '/api/v1/seo/tags') {
        return jsonResponse({ items: [], limit: 50000, offset: 0, total: 0, has_more: false });
      }

      if (url.pathname === '/api/v1/seo/templates') {
        return jsonResponse({ items: [], limit: 50000, offset: 0, total: 0, has_more: false });
      }

      return jsonResponse({ items: [], limit: 10000, offset: 10000, total: 0, has_more: false });
    }) satisfies ApiFetch;

    await fetchSeoSummary({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test' });
    await fetchSeoMemes({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', limit: 10000, offset: 10000 });
    await fetchSeoTags({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', limit: 50000, offset: 0 });
    await fetchSeoTemplates({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', limit: 50000, offset: 0 });
    await fetchPinterestFeed({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', limit: 100, offset: 0 });

    expect(calls).toEqual([
      '/api/v1/seo/summary?',
      '/api/v1/seo/memes?limit=10000&offset=10000',
      '/api/v1/seo/tags?limit=50000&offset=0',
      '/api/v1/seo/templates?limit=50000&offset=0',
      '/api/v1/seo/pinterest-feed?limit=100&offset=0'
    ]);
  });

  it('requests trend ranking pages, aggregate summaries, comparison, and timeline', async () => {
    const calls: string[] = [];
    const mockFetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));
      calls.push(`${url.pathname}?${url.searchParams.toString()}`);
      if (url.pathname === '/api/v1/memes/trends') {
        expect(url.searchParams.get('ranking')).toBe('fastest_rising');
        return jsonResponse(trendPage);
      }
      if (url.pathname === '/api/v1/memes/trends/compare') {
        expect(url.searchParams.getAll('item')).toEqual(['meme:launch-reaction', 'tag:reaction']);
        return jsonResponse({ items: [], requested_items: ['meme:launch-reaction', 'tag:reaction'], max_items: 6 });
      }
      if (url.pathname === '/api/v1/memes/trends/timeline') {
        expect(url.searchParams.get('granularity')).toBe('year');
        expect(url.searchParams.get('limit')).toBe('12');
        expect(url.searchParams.get('offset')).toBe('24');
        return jsonResponse({ granularity: 'year', periods: [], limit: 12, offset: 24, total: 0, has_more: false });
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
    await fetchTrendComparison({
      fetch: mockFetch,
      baseUrl: 'https://api.memexpert.test',
      items: [' meme:launch-reaction ', '', 'tag:reaction']
    });
    await fetchTrendTimeline({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', granularity: 'year', limit: 12, offset: 24 });

    expect(calls).toEqual([
      '/api/v1/memes/trends?ranking=fastest_rising&limit=12&offset=24',
      '/api/v1/memes/trends/tags?limit=8&offset=0',
      '/api/v1/memes/trends/templates?limit=8&offset=0',
      '/api/v1/memes/trends/compare?item=meme%3Alaunch-reaction&item=tag%3Areaction',
      '/api/v1/memes/trends/timeline?granularity=year&limit=12&offset=24'
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

  it('posts and deletes favorite actions with credentials and CSRF-compatible request header', async () => {
    const calls: Array<{
      method: string | undefined;
      path: string;
      credentials: RequestCredentials | undefined;
      requestedWith: string | null;
    }> = [];
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? new URL(input, 'https://web.memexpert.test') : new URL(String(input));
      const headers = new Headers(init?.headers);
      calls.push({
        method: init?.method,
        path: url.pathname,
        credentials: init?.credentials,
        requestedWith: headers.get('x-requested-with')
      });
      return jsonResponse(
        init?.method === 'DELETE'
          ? { favorited: false, changed: true, like_count: 7 }
          : { favorited: true, changed: true, like_count: 8 }
      );
    }) satisfies ApiFetch;

    await expect(favoriteMeme({ fetch: mockFetch, memeId: 'meme-123' })).resolves.toEqual({
      favorited: true,
      changed: true,
      like_count: 8
    });
    await expect(unfavoriteMeme({ fetch: mockFetch, memeId: 'meme-123' })).resolves.toEqual({
      favorited: false,
      changed: true,
      like_count: 7
    });

    expect(calls).toEqual([
      { method: 'POST', path: '/api/v1/memes/meme-123/favorite', credentials: 'include', requestedWith: 'XMLHttpRequest' },
      { method: 'DELETE', path: '/api/v1/memes/meme-123/favorite', credentials: 'include', requestedWith: 'XMLHttpRequest' }
    ]);
  });

  it('uses existing save and pin action endpoints with CSRF-compatible request header', async () => {
    const calls: Array<{ method: string | undefined; path: string; requestedWith: string | null }> = [];
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const headers = new Headers(init?.headers);
      calls.push({ method: init?.method, path: url.pathname, requestedWith: headers.get('x-requested-with') });
      return jsonResponse(init?.method === 'DELETE' ? { removed: false } : { id: 'action-row-1' });
    }) satisfies ApiFetch;

    await saveMeme({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', memeId: 'meme-123' });
    await removeSavedMeme({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', memeId: 'meme-123' });
    await pinMeme({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', memeId: 'meme-123' });
    await unpinMeme({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', memeId: 'meme-123' });

    expect(calls).toEqual([
      { method: 'POST', path: '/api/v1/memes/meme-123/save', requestedWith: 'XMLHttpRequest' },
      { method: 'DELETE', path: '/api/v1/memes/meme-123/save', requestedWith: 'XMLHttpRequest' },
      { method: 'POST', path: '/api/v1/memes/meme-123/pin', requestedWith: 'XMLHttpRequest' },
      { method: 'DELETE', path: '/api/v1/memes/meme-123/pin', requestedWith: 'XMLHttpRequest' }
    ]);
  });

  it('posts action attribution bodies for mutations and telemetry-only actions', async () => {
    const paths: string[] = [];
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const headers = new Headers(init?.headers);
      paths.push(url.pathname);

      expect(init?.method).toBe('POST');
      expect(headers.get('content-type')).toBe('application/json');
      expect(JSON.parse(String(init?.body))).toEqual({ attribution: actionAttribution() });

      return jsonResponse(isTelemetryActionPath(url.pathname) ? { ok: true } : { id: 'action-row-1' });
    }) satisfies ApiFetch;

    await favoriteMeme({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', memeId: 'meme-123', body: { attribution: actionAttribution() } });
    await saveMeme({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', memeId: 'meme-123', body: { attribution: actionAttribution() } });
    await pinMeme({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', memeId: 'meme-123', body: { attribution: actionAttribution() } });
    await recordMemeImpression({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', memeId: 'meme-123', body: { attribution: actionAttribution() } });
    await recordMemeView({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', memeId: 'meme-123', body: { attribution: actionAttribution() } });
    await recordMemeDetailClick({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', memeId: 'meme-123', body: { attribution: actionAttribution() } });
    await recordMemeShare({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', memeId: 'meme-123', body: { attribution: actionAttribution() } });
    await recordMemeDownload({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', memeId: 'meme-123', body: { attribution: actionAttribution() } });

    expect(paths).toEqual([
      '/api/v1/memes/meme-123/favorite',
      '/api/v1/memes/meme-123/save',
      '/api/v1/memes/meme-123/pin',
      '/api/v1/memes/meme-123/impression',
      '/api/v1/memes/meme-123/view',
      '/api/v1/memes/meme-123/detail-click',
      '/api/v1/memes/meme-123/share',
      '/api/v1/memes/meme-123/download'
    ]);
  });

  it('passes keepalive through best-effort meme telemetry calls', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input), 'https://web.memexpert.test');

      expect(url.pathname).toBe('/api/v1/memes/meme-123/impression');
      expect(init?.method).toBe('POST');
      expect(init?.keepalive).toBe(true);

      return jsonResponse({ ok: true });
    }) satisfies ApiFetch;

    await recordMemeImpression({ fetch: mockFetch, memeId: 'meme-123', keepalive: true });

    expect(mockFetch).toHaveBeenCalledOnce();
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

  it('adds action attribution to meme report payloads when provided', async () => {
    const memeId = '11111111-1111-4111-8111-111111111111';
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));

      expect(url.pathname).toBe(`/api/v1/memes/${memeId}/report`);
      expect(JSON.parse(String(init?.body))).toEqual({
        reason: 'spam',
        note: null,
        attribution: actionAttribution()
      });

      return jsonResponse({
        id: '22222222-2222-4222-8222-222222222222',
        meme_id: memeId,
        status: 'pending',
        reason: 'spam',
        note: null,
        created_at: '2026-01-01T00:00:00Z',
        updated_at: '2026-01-01T00:00:00Z'
      });
    }) satisfies ApiFetch;

    await reportMeme({
      fetch: mockFetch,
      baseUrl: 'https://api.memexpert.test',
      memeId,
      reason: 'spam',
      body: { attribution: actionAttribution() }
    });

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

  it('starts Telegram linking without token payloads', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const method = init?.method ?? 'GET';
      const headers = new Headers(init?.headers);

      expect(url.pathname).toBe('/api/v1/auth/link/telegram');
      expect(method).toBe('POST');
      expect(headers.get('x-requested-with')).toBe('XMLHttpRequest');
      return jsonResponse({
        code: 'abc123',
        deep_link_url: 'https://t.me/memexpertbot?start=link_abc123',
        expires_at: '2026-06-12T12:00:00Z',
        expires_in_seconds: 600,
        return_url: 'https://memexpert.test/account/telegram/complete'
      });
    }) satisfies ApiFetch;

    const link = await startTelegramLink({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test' });

    expect(link.deep_link_url).toContain('https://t.me/');
    expect(link.return_url).toBe('https://memexpert.test/account/telegram/complete');
    expect(mockFetch).toHaveBeenCalledOnce();
  });

  it('saves a favorite through cookie-only transport', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const headers = new Headers(init?.headers);

      expect(url.pathname).toBe('/api/v1/memes/11111111-1111-4111-8111-111111111111/favorite');
      expect(init?.method).toBe('POST');
      expect(headers.get('cookie')).toBe('memexpert_access_token=guest');
      expect(headers.get('x-requested-with')).toBe('XMLHttpRequest');

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

  it('loads profile interaction stats with SSR cookie forwarding', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const headers = new Headers(init?.headers);

      expect(url.pathname).toBe('/api/v1/auth/profile-stats');
      expect(headers.get('cookie')).toBe('memexpert_access_token=guest');

      return jsonResponse({
        viewed: 12,
        sent: 3,
        saved: 4,
        downloaded: 2,
        days_active: 5,
        top_tags: [{ tag: 'frog', count: 7 }],
        top_templates: [
          {
            template_id: '44444444-4444-4444-8444-444444444444',
            slug: 'frog-template',
            name: 'Frog Template',
            count: 5
          }
        ],
        metadata: { notes: ['Top tags require analytics events with payload.refs.meme_id and tagged meme rows.'] }
      });
    }) satisfies ApiFetch;

    const stats = await fetchProfileStats({
      fetch: mockFetch,
      baseUrl: 'https://api.memexpert.test',
      cookieHeader: 'memexpert_access_token=guest'
    });

    expect(stats.viewed).toBe(12);
    expect(stats.top_tags[0]).toEqual({ tag: 'frog', count: 7 });
    expect(stats.top_templates[0]?.slug).toBe('frog-template');
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

  it('updates user preferences through cookie-only JSON transport', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = typeof input === 'string' ? new URL(input, 'https://web.memexpert.test') : new URL(String(input));
      const headers = new Headers(init?.headers);

      expect(url.pathname).toBe('/api/v1/auth/preferences');
      expect(init?.method).toBe('PATCH');
      expect(init?.credentials).toBe('include');
      expect(headers.get('cookie')).toBe('memexpert_access_token=full');
      expect(headers.get('content-type')).toBe('application/json');
      expect(headers.get('x-requested-with')).toBe('XMLHttpRequest');
      expect(JSON.parse(String(init?.body))).toEqual({ nsfw_enabled: true, language: 'en' });

      return jsonResponse({ ...sessionPayload('full').user, nsfw_enabled: true, language: 'en' });
    }) satisfies ApiFetch;

    const user = await updateUserPreferences({
      fetch: mockFetch,
      cookieHeader: 'memexpert_access_token=full',
      body: { nsfw_enabled: true, language: 'en' }
    });

    expect(user.nsfw_enabled).toBe(true);
    expect(mockFetch).toHaveBeenCalledOnce();
  });

  it('uses collection list, detail, mutation, active-save, remove, and invite endpoints', async () => {
    const collectionId = '11111111-1111-4111-8111-111111111111';
    const memeId = '22222222-2222-4222-8222-222222222222';
    const inviteId = '33333333-3333-4333-8333-333333333333';
    const memberUserId = '44444444-4444-4444-8444-444444444444';
    const calls: Array<{ method: string; path: string; body: unknown }> = [];
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      calls.push({ method: init?.method ?? 'GET', path: url.pathname, body: init?.body ? JSON.parse(String(init.body)) : null });

      if (url.pathname === '/api/v1/collections' && (init?.method ?? 'GET') === 'GET') {
        return jsonResponse({ collections: [], active_save_collection_id: null });
      }
      if (url.pathname === `/api/v1/collections/meme-choices/${memeId}`) {
        return jsonResponse({ collections: [] });
      }
      if (url.pathname === `/api/v1/collections/${collectionId}` && (init?.method ?? 'GET') === 'GET') {
        return jsonResponse({ ...collectionSummary(collectionId), saved_memes: [] });
      }
      if (url.pathname === `/api/v1/collections/${collectionId}/invites`) {
        return jsonResponse({ invite: collectionInvite(collectionId), token: 'invite-token', join_path: '/collection/invite/invite-token' });
      }
      if (url.pathname === `/api/v1/collections/${collectionId}/invites/${inviteId}`) {
        return jsonResponse({ ...collectionInvite(collectionId), id: inviteId, status: 'revoked' });
      }
      if (url.pathname === `/api/v1/collections/${collectionId}/members/${memberUserId}`) {
        return jsonResponse(init?.method === 'DELETE' ? { removed: true } : { collection_id: collectionId, user_id: memberUserId, role: 'viewer', joined_at: '2026-01-02T00:00:00Z' });
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
    await fetchMemeCollectionChoices({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', memeId });
    await fetchCollectionDetail({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', collectionId });
    await createCollection({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', body: { title: 'Launch', visibility: 'private' } });
    await setActiveSaveCollection({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', collectionId });
    await removeMemeFromCollection({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', collectionId, memeId });
    await createCollectionInvite({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', collectionId, body: { role: 'viewer', max_uses: 1 } });
    await revokeCollectionInvite({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', collectionId, inviteId });
    await updateCollectionMemberRole({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', collectionId, memberUserId, body: { role: 'viewer' } });
    await removeCollectionMember({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', collectionId, memberUserId });
    await deleteCollection({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', collectionId });

    expect(calls.map((call) => [call.method, call.path])).toEqual([
      ['GET', '/api/v1/collections'],
      ['GET', `/api/v1/collections/meme-choices/${memeId}`],
      ['GET', `/api/v1/collections/${collectionId}`],
      ['POST', '/api/v1/collections'],
      ['PUT', `/api/v1/collections/${collectionId}/active-save`],
      ['DELETE', `/api/v1/collections/${collectionId}/memes/${memeId}`],
      ['POST', `/api/v1/collections/${collectionId}/invites`],
      ['DELETE', `/api/v1/collections/${collectionId}/invites/${inviteId}`],
      ['PATCH', `/api/v1/collections/${collectionId}/members/${memberUserId}`],
      ['DELETE', `/api/v1/collections/${collectionId}/members/${memberUserId}`],
      ['DELETE', `/api/v1/collections/${collectionId}`]
    ]);
    expect(calls[3].body).toEqual({ title: 'Launch', visibility: 'private' });
    expect(calls[6].body).toEqual({ role: 'viewer', max_uses: 1 });
    expect(calls[8].body).toEqual({ role: 'viewer' });
  });

  it('reorders pins with the full ordered id list', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const headers = new Headers(init?.headers);

      expect(url.pathname).toBe('/api/v1/memes/pins/reorder');
      expect(init?.method).toBe('PUT');
      expect(headers.get('cookie')).toBe('memexpert_access_token=full');
      expect(headers.get('content-type')).toBe('application/json');
      expect(JSON.parse(String(init?.body))).toEqual({ meme_ids: ['meme-2', 'meme-1'] });

      return jsonResponse([
        { user_id: 'user-id', meme_id: 'meme-2', position: 1, pinned_at: '2026-01-02T00:00:00Z' },
        { user_id: 'user-id', meme_id: 'meme-1', position: 2, pinned_at: '2026-01-01T00:00:00Z' }
      ]);
    }) satisfies ApiFetch;

    const pins = await reorderPins({
      fetch: mockFetch,
      baseUrl: 'https://api.memexpert.test',
      cookieHeader: 'memexpert_access_token=full',
      body: { meme_ids: ['meme-2', 'meme-1'] }
    });

    expect(pins.map((pin) => pin.meme_id)).toEqual(['meme-2', 'meme-1']);
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
        latest_post_at: null,
        observed_post_count: 0,
        meme_count: 0,
        is_active: true,
        is_paused: false,
        catchup_enabled: true,
        live_enabled: true,
        engagement_enabled: true,
        catchup_message_limit: 5000,
        telegram_session_id: null,
        telegram_session_name: null,
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

  it('marks source channels dead with exact confirmation JSON', async () => {
    const channelId = '33333333-3333-4333-8333-333333333333';
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const headers = new Headers(init?.headers);

      expect(url.pathname).toBe(`/api/v1/admin/source-channels/${channelId}/mark-dead`);
      expect(init?.method).toBe('POST');
      expect(headers.get('content-type')).toBe('application/json');
      expect(headers.get('x-requested-with')).toBe('XMLHttpRequest');
      expect(JSON.parse(String(init?.body))).toEqual({ confirmation: channelId });

      return jsonResponse({ id: channelId, is_active: false, is_paused: false });
    }) satisfies ApiFetch;

    await markSourceChannelDead({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test' }, channelId, channelId);

    expect(mockFetch).toHaveBeenCalledOnce();
  });

  it('loads source post status and queues bounded historical backfill', async () => {
    const channelId = '33333333-3333-4333-8333-333333333333';
    const calls: Array<{ method: string; path: string; search: string; body: unknown }> = [];
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      calls.push({
        method: init?.method ?? 'GET',
        path: url.pathname,
        search: url.searchParams.toString(),
        body: init?.body ? JSON.parse(String(init.body)) : null
      });
      if ((init?.method ?? 'GET') === 'GET') {
        return jsonResponse({
          source_channel_id: channelId,
          snapshot_at: '2026-07-13T12:00:00Z',
          summary: { observed_count: 1, indexed_count: 1, partially_indexed_count: 0, processing_count: 0, failed_count: 0, not_indexable_count: 0, metadata_captured_count: 1, metadata_missing_count: 0 },
          items: [{
            id: 'source-post-id',
            post_id: '184',
            telegram_url: 'https://t.me/daily_memes/184',
            published_at: '2026-07-13T09:30:00Z',
            observed_at: '2026-07-13T09:31:00Z',
            media_type: 'image',
            metadata_state: 'captured',
            text_excerpt: 'Unicode caption: Привет 👋',
            media_group_id: '9007199254740993',
            reply_to_post_id: '183',
            telegram_edited_at: '2026-07-13T09:32:00Z',
            metadata_first_observed_at: '2026-07-13T09:31:00Z',
            metadata_last_observed_at: '2026-07-13T09:33:00Z',
            is_deleted: false,
            deletion_observed_at: null,
            fetch_status: 'accepted',
            fetch_detail: null,
            ingest_outcome: 'ingested',
            ingest_status: 'materialized',
            meme_id: null,
            meme_file_id: null,
            pipeline_stage: null,
            pipeline_status: null,
            pipeline_error: null,
            qdrant_status: 'synced',
            meilisearch_status: 'synced',
            index_status: 'indexed',
            is_retryable: false,
            version: 'source-post-version',
            capabilities: [],
            blocked_reason: null
          }],
          total: 1,
          limit: 50,
          offset: 100
        });
      }
      return jsonResponse(telegramChannelPayload(channelId, 'session-id'));
    }) satisfies ApiFetch;

    const postPage = await fetchAdminSourceChannelPosts(
      { fetch: mockFetch, baseUrl: 'https://api.memexpert.test' },
      channelId,
      { limit: 50, offset: 100, snapshotAt: '2026-07-13T12:00:00Z' }
    );
    expect(postPage.summary.metadata_captured_count).toBe(1);
    expect(postPage.summary.metadata_missing_count).toBe(0);
    expect(postPage.items[0]).toMatchObject({
      metadata_state: 'captured',
      text_excerpt: 'Unicode caption: Привет 👋',
      media_group_id: '9007199254740993',
      reply_to_post_id: '183',
      is_deleted: false
    });
    await backfillAdminSourceChannel(
      { fetch: mockFetch, baseUrl: 'https://api.memexpert.test', body: { message_limit: 5000 } },
      channelId
    );

    expect(calls).toEqual([
      { method: 'GET', path: `/api/v1/admin/source-channels/${channelId}/posts`, search: 'limit=50&offset=100&snapshot_at=2026-07-13T12%3A00%3A00Z', body: null },
      { method: 'POST', path: `/api/v1/admin/source-channels/${channelId}/backfill`, search: '', body: { message_limit: 5000 } }
    ]);
  });

  it('uses DB-backed Telegram session and channel admin endpoints', async () => {
    const sessionId = '11111111-1111-4111-8111-111111111111';
    const channelId = '22222222-2222-4222-8222-222222222222';
    const calls: Array<{ method: string; path: string; search: string; body: unknown; requestedWith: string | null }> = [];
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const headers = new Headers(init?.headers);
      calls.push({
        method: init?.method ?? 'GET',
        path: url.pathname,
        search: url.searchParams.toString(),
        body: init?.body ? JSON.parse(String(init.body)) : null,
        requestedWith: headers.get('x-requested-with')
      });

      if (url.pathname === '/api/v1/admin/telegram/sessions' && (init?.method ?? 'GET') === 'GET') {
        return jsonResponse([telegramSessionPayload(sessionId)]);
      }
      if (url.pathname === '/api/v1/admin/telegram/sessions' && init?.method === 'POST') {
        return jsonResponse(telegramSessionPayload(sessionId), 201);
      }
      if (url.pathname.endsWith('/login-attempts/qr')) {
        return jsonResponse({ attempt_id: 'qr-attempt', qr_url: 'tg://login?token=qr', expires_at: '2026-01-01T00:10:00Z', message: 'qr started' });
      }
      if (url.pathname.endsWith('/login-attempts/phone')) {
        return jsonResponse({ attempt_id: 'phone-attempt', phone_number_hint: 'ending-1234', expires_at: '2026-01-01T00:10:00Z', message: 'code sent' });
      }
      if (url.pathname.endsWith('/qr/complete')) {
        return jsonResponse({ status: 'completed', telegram_session: telegramSessionPayload(sessionId), password_required: false, message: 'login complete' });
      }
      if (url.pathname.endsWith('/phone/code') || url.pathname.endsWith('/password')) {
        return jsonResponse({ telegram_session: telegramSessionPayload(sessionId), password_required: false, message: 'login complete' });
      }
      if (url.pathname.endsWith('/login-attempts/qr-attempt') && init?.method === 'DELETE') {
        return jsonResponse({ attempt_id: 'qr-attempt', status: 'cancelled', message: 'cancelled' });
      }
      if (url.pathname.endsWith('/validate')) {
        return jsonResponse({ telegram_session: telegramSessionPayload(sessionId), channel_checked: true, channel_reference: '@source' });
      }
      if (url.pathname === `/api/v1/admin/telegram/sessions/${sessionId}` && init?.method === 'DELETE') {
        return jsonResponse({ action: 'delete', telegram_session_id: sessionId, orphaned_source_channel_count: 1, message: 'deleted' });
      }
      if (url.pathname === `/api/v1/admin/telegram/sessions/${sessionId}`) {
        return jsonResponse(telegramSessionPayload(sessionId));
      }
      if (url.pathname === '/api/v1/admin/telegram/channels/grouped') {
        return jsonResponse([{ telegram_session: telegramSessionPayload(sessionId), is_orphaned: false, channels: [telegramChannelPayload(channelId, sessionId)] }]);
      }
      if (url.pathname === '/api/v1/admin/telegram/channels' && (init?.method ?? 'GET') === 'GET') {
        expect(url.searchParams.get('telegram_session_id')).toBe(sessionId);
        expect(url.searchParams.get('orphaned')).toBe('false');
        return jsonResponse([telegramChannelPayload(channelId, sessionId)]);
      }
      return jsonResponse(telegramChannelPayload(channelId, sessionId));
    }) satisfies ApiFetch;

    await fetchAdminTelegramSessions({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', cookieHeader: 'memexpert_access_token=token' });
    await createAdminTelegramSession({
      fetch: mockFetch,
      baseUrl: 'https://api.memexpert.test',
      body: { enabled: true, live_enabled: true, catchup_enabled: true, engagement_enabled: true, max_requests_per_second: 1 }
    });
    await updateAdminTelegramSession(
      { fetch: mockFetch, baseUrl: 'https://api.memexpert.test', body: { enabled: false, status: 'quarantined', max_requests_per_second: 0.5, clear_error: true, note: 'pause' } },
      sessionId
    );
    await startAdminTelegramQrLogin({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', body: { telegram_session_id: sessionId, note: 'start qr' } });
    await completeAdminTelegramQrLogin({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', body: { note: 'qr done' } }, 'qr-attempt');
    await startAdminTelegramPhoneLogin({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', body: { telegram_session_id: sessionId, phone_number: '+15551234567', note: 'send code' } });
    await completeAdminTelegramPhoneCodeLogin({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', body: { code: '12345', note: 'code done' } }, 'phone-attempt');
    await completeAdminTelegramPhonePasswordLogin({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', body: { password: '2fa-password', note: 'password done' } }, 'phone-attempt');
    await cancelAdminTelegramLoginAttempt({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test' }, 'qr-attempt');
    await validateAdminTelegramSession({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', body: { source_channel_id: channelId, note: 'check' } }, sessionId);
    await deleteAdminTelegramSession({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', body: { confirmation: sessionId, note: 'remove' } }, sessionId);
    await fetchAdminTelegramChannels({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', telegramSessionId: sessionId, orphaned: false });
    await fetchAdminTelegramChannelGroups({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test' });
    await addAdminTelegramChannel({
      fetch: mockFetch,
      baseUrl: 'https://api.memexpert.test',
      body: { platform: 'telegram', platform_id: '-1001', title: 'Source', telegram_session_id: sessionId, catchup_enabled: true, live_enabled: true, engagement_enabled: true, catchup_message_limit: 5000 }
    });
    await addAdminTelegramChannelFromReference({
      fetch: mockFetch,
      baseUrl: 'https://api.memexpert.test',
      body: { reference: '@source', telegram_session_id: sessionId, suggestion_id: 'suggestion-id', catchup_message_limit: 5000 }
    });
    await updateAdminTelegramChannel({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', body: { catchup_enabled: false, live_enabled: true, engagement_enabled: false, catchup_message_limit: 250 } }, channelId);
    await assignAdminTelegramChannel({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', body: { telegram_session_id: sessionId, note: 'move' } }, channelId);
    await orphanAdminTelegramChannel({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test', body: { note: 'orphan' } }, channelId);

    expect(calls.map((call) => [call.method, call.path, call.search])).toEqual([
      ['GET', '/api/v1/admin/telegram/sessions', ''],
      ['POST', '/api/v1/admin/telegram/sessions', ''],
      ['PATCH', `/api/v1/admin/telegram/sessions/${sessionId}`, ''],
      ['POST', '/api/v1/admin/telegram/login-attempts/qr', ''],
      ['POST', '/api/v1/admin/telegram/login-attempts/qr-attempt/qr/complete', ''],
      ['POST', '/api/v1/admin/telegram/login-attempts/phone', ''],
      ['POST', '/api/v1/admin/telegram/login-attempts/phone-attempt/phone/code', ''],
      ['POST', '/api/v1/admin/telegram/login-attempts/phone-attempt/password', ''],
      ['DELETE', '/api/v1/admin/telegram/login-attempts/qr-attempt', ''],
      ['POST', `/api/v1/admin/telegram/sessions/${sessionId}/validate`, ''],
      ['DELETE', `/api/v1/admin/telegram/sessions/${sessionId}`, ''],
      ['GET', '/api/v1/admin/telegram/channels', `telegram_session_id=${sessionId}&orphaned=false`],
      ['GET', '/api/v1/admin/telegram/channels/grouped', ''],
      ['POST', '/api/v1/admin/telegram/channels', ''],
      ['POST', '/api/v1/admin/telegram/channels/from-reference', ''],
      ['PATCH', `/api/v1/admin/telegram/channels/${channelId}`, ''],
      ['POST', `/api/v1/admin/telegram/channels/${channelId}/assign`, ''],
      ['POST', `/api/v1/admin/telegram/channels/${channelId}/orphan`, '']
    ]);
    expect(calls[0].requestedWith).toBe(null);
    expect([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 13, 14, 15, 16, 17].every((index) => calls[index]?.requestedWith === 'XMLHttpRequest')).toBe(true);
    expect(calls[11].requestedWith).toBe(null);
    expect(calls[12].requestedWith).toBe(null);
    expect(calls[1].body).toMatchObject({ enabled: true });
    expect(calls[1].body).not.toHaveProperty('name');
    expect(calls[1].body).not.toHaveProperty('string_session');
    expect(calls[3].body).toEqual({ telegram_session_id: sessionId, note: 'start qr' });
    expect(calls[4].body).toEqual({ note: 'qr done' });
    expect(calls[5].body).toEqual({ telegram_session_id: sessionId, phone_number: '+15551234567', note: 'send code' });
    expect(calls[6].body).toEqual({ code: '12345', note: 'code done' });
    expect(calls[7].body).toEqual({ password: '2fa-password', note: 'password done' });
    expect(calls[10].body).toEqual({ confirmation: sessionId, note: 'remove' });
    expect(calls[13].body).toMatchObject({ platform: 'telegram', telegram_session_id: sessionId });
    expect(calls[14].body).toEqual({ reference: '@source', telegram_session_id: sessionId, suggestion_id: 'suggestion-id', catchup_message_limit: 5000 });
    expect(calls[16].body).toEqual({ telegram_session_id: sessionId, note: 'move' });
    expect(calls[17].body).toEqual({ note: 'orphan' });
  });

  it('loads focused SEO review and template lists without the legacy dashboard fetch', async () => {
    const calls: string[] = [];
    const mockFetch = vi.fn(async (input: RequestInfo | URL) => {
      const url = new URL(String(input));
      calls.push(`${url.pathname}?${url.searchParams.toString()}`);

      if (url.pathname === '/api/v1/admin/seo-pages') {
        expect(url.searchParams.get('limit')).toBe('35');
        return jsonResponse([seoReviewPayload()]);
      }
      return jsonResponse([]);
    }) satisfies ApiFetch;

    const [reviews, templates] = await Promise.all([
      fetchAdminSeoReviewRows({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test' }, { limit: 35, offset: 70 }),
      fetchAdminMemeTemplates({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test' })
    ]);

    expect(reviews).toHaveLength(1);
    expect(templates).toEqual([]);
    expect(calls).toEqual(['/api/v1/admin/seo-pages?limit=35&offset=70', '/api/v1/admin/meme-templates?']);
  });

  it('loads the bounded admin overview from its single endpoint', async () => {
    const mockFetch = vi.fn(async (input: RequestInfo | URL) => {
      expect(new URL(String(input)).pathname).toBe('/api/v1/admin/overview');
      return jsonResponse({
        open_report_count: 1,
        pending_suggestion_count: 2,
        source_attention_count: 3,
        orphaned_source_count: 4,
        stale_source_count: 5,
        waiting_source_count: 6,
        healthy_source_count: 7,
        telegram_account_attention_count: 8,
        ready_telegram_account_count: 9,
        missing_seo_count: 10,
        uncurated_template_count: 11
      });
    }) satisfies ApiFetch;

    const overview = await fetchAdminOverview({ fetch: mockFetch, baseUrl: 'https://api.memexpert.test' });

    expect(overview.source_attention_count).toBe(3);
    expect(overview.ready_telegram_account_count).toBe(9);
    expect(mockFetch).toHaveBeenCalledOnce();
  });

  it('edits and regenerates meme SEO pages through admin endpoints', async () => {
    const memeId = '77777777-7777-4777-8777-777777777777';
    const calls: Array<{ method: string | undefined; path: string; body: unknown }> = [];
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const headers = new Headers(init?.headers);
      calls.push({
        method: init?.method,
        path: url.pathname,
        body: init?.body ? JSON.parse(String(init.body)) : null
      });

      expect(headers.get('content-type')).toBe('application/json');
      expect(headers.get('x-requested-with')).toBe('XMLHttpRequest');

      return jsonResponse(seoPagePayload(memeId));
    }) satisfies ApiFetch;

    await updateMemeSeoPage(
      {
        fetch: mockFetch,
        baseUrl: 'https://api.memexpert.test',
        body: {
          slug: 'launch-reaction',
          page_title: 'Launch Reaction Meme',
          meta_description: 'A launch reaction meme for search results.',
          alt_text: 'A reaction meme about launch day.',
          caption: 'Launch day mood',
          body_text: 'Longer SEO body copy.',
          tags: 'launch, reaction'
        }
      },
      memeId
    );
    await regenerateMemeSeoPage(
      { fetch: mockFetch, baseUrl: 'https://api.memexpert.test', body: { confirmation: memeId } },
      memeId
    );

    expect(calls).toEqual([
      {
        method: 'PATCH',
        path: `/api/v1/admin/memes/${memeId}/seo-page`,
        body: {
          slug: 'launch-reaction',
          page_title: 'Launch Reaction Meme',
          meta_description: 'A launch reaction meme for search results.',
          alt_text: 'A reaction meme about launch day.',
          caption: 'Launch day mood',
          body_text: 'Longer SEO body copy.',
          tags: 'launch, reaction'
        }
      },
      {
        method: 'POST',
        path: `/api/v1/admin/memes/${memeId}/seo-page/regenerate`,
        body: { confirmation: memeId }
      }
    ]);
  });

  it('turns FastAPI validation details into concise field messages while preserving string details', async () => {
    const memeId = '77777777-7777-4777-8777-777777777777';
    const responses = [
      jsonResponse(
        {
          detail: [
            { loc: ['body', 'slug'], msg: 'Field required' },
            { loc: ['query', 'limit'], msg: 'Input should be less than or equal to 100' },
            { loc: ['body', 'meta_description'], msg: 'String should have at least 1 character' },
            { loc: ['body', 'alt_text'], msg: 'String should have at least 1 character' }
          ]
        },
        422
      ),
      jsonResponse({ detail: 'SEO page slug already exists.' }, 409)
    ];
    const fetch = vi.fn(async () => responses.shift() ?? jsonResponse({})) satisfies ApiFetch;
    const request = {
      fetch,
      baseUrl: 'https://api.memexpert.test',
      body: { slug: 'launch-reaction', page_title: 'Launch reaction', meta_description: 'Description', alt_text: 'Alt text' }
    };

    await expect(updateMemeSeoPage(request, memeId)).rejects.toMatchObject({
      status: 422,
      message:
        'slug: Field required; limit: Input should be less than or equal to 100; meta_description: String should have at least 1 character; +1 more'
    });
    await expect(updateMemeSeoPage(request, memeId)).rejects.toMatchObject({
      status: 409,
      message: 'SEO page slug already exists.'
    });
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

  it('updates direct meme visibility policy with audit reason and note', async () => {
    const memeId = '55555555-5555-4555-8555-555555555555';
    const mockFetch = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = new URL(String(input));
      const headers = new Headers(init?.headers);

      expect(url.pathname).toBe(`/api/v1/admin/memes/${memeId}/moderation`);
      expect(init?.method).toBe('PATCH');
      expect(headers.get('x-requested-with')).toBe('XMLHttpRequest');
      expect(JSON.parse(String(init?.body))).toEqual({
        visibility_mode: 'force_private',
        is_nsfw: true,
        reason: 'spam',
        note: 'manual override'
      });

      return jsonResponse({
        ...adminMemePayload(),
        id: memeId,
        visibility_mode: 'force_private',
        is_public: false,
        is_nsfw: true
      });
    }) satisfies ApiFetch;

    await updateMemeModeration(
      {
        fetch: mockFetch,
        baseUrl: 'https://api.memexpert.test',
        body: { visibility_mode: 'force_private', is_nsfw: true, reason: 'spam', note: 'manual override' }
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
      can_revoke_invites: true,
      can_manage_members: true,
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

function actionAttribution(): MemeActionAttribution {
  return {
    request_id: 'req-action-1',
    impression_id: 'imp-action-1',
    surface: 'public_api_meme_similar',
    source_algorithm: 'qdrant_similarity',
    rank: 2,
    query: 'frog',
    filters: { language: 'en', media_type: 'image', include_nsfw: false, tags: ['frog'], scope: 'public', collection_ids: [] },
    collection_scope: 'public',
    collection_ids: ['collection-1'],
    source_meme_id: '11111111-1111-4111-8111-111111111111',
    algorithm_version: 'similar-v1',
    score: 0.92,
    score_components: { total: 0.92 },
    reason: 'similarity_match'
  };
}

function isTelemetryActionPath(pathname: string): boolean {
  return ['/detail-click', '/download', '/impression', '/share', '/view'].some((suffix) => pathname.endsWith(suffix));
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
    visibility_mode: 'auto',
    is_public: true,
    popularity_score: 1,
    like_count: 0,
    tags: [],
    primary_file: null,
    template_id: null,
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
    previous_visibility_mode: 'auto',
    previous_is_nsfw: false,
    new_is_public: true,
    new_visibility_mode: 'auto',
    new_is_nsfw: true,
    created_at: '2026-01-01T00:00:00Z'
  };
}

function seoPagePayload(memeId = '33333333-3333-4333-8333-333333333333') {
  return {
    meme_id: memeId,
    slug: 'launch-reaction',
    page_title: 'Launch Reaction Meme',
    meta_description: 'A launch reaction meme for search results.',
    alt_text: 'A reaction meme about launch day.',
    caption: 'Launch day mood',
    body_text: 'Longer SEO body copy.',
    tags: ['launch', 'reaction'],
    model_id: 'admin-manual',
    prompt_version: 'admin-manual',
    generated_at: '2026-01-01T00:00:00Z',
    edited_at: '2026-01-02T00:00:00Z'
  };
}

function seoReviewPayload() {
  return {
    meme: adminMemePayload(),
    seo_page: seoPagePayload(),
    status: 'edited'
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

function telegramSessionPayload(id: string) {
  return {
    id,
    name: 'primary',
    display_name: 'Primary ingest',
    owned_channel_count: 1,
    status: 'active',
    enabled: true,
    flood_wait_until: null,
    live_listener_started_at: '2026-01-01T00:00:00Z',
    last_heartbeat_at: '2026-01-01T00:05:00Z',
    last_error_class: null,
    last_error_text: null,
    quarantined_at: null,
    live_enabled: true,
    catchup_enabled: true,
    engagement_enabled: true,
    max_requests_per_second: 1,
    account_user_id: 123,
    account_username: 'primary_user',
    account_phone_hint: '+1***1234',
    has_string_session: true,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z'
  };
}

function telegramChannelPayload(id: string, sessionId: string | null) {
  return {
    id,
    platform: 'telegram',
    platform_id: '-1001',
    username: 'source',
    title: 'Source',
    subscriber_count: 1000,
    is_active: true,
    is_paused: false,
    catchup_enabled: sessionId !== null,
    live_enabled: sessionId !== null,
    engagement_enabled: sessionId !== null,
    catchup_message_limit: 5000,
    telegram_session_id: sessionId,
    telegram_session_name: sessionId === null ? null : 'primary',
    is_orphaned: sessionId === null,
    is_indexable: sessionId !== null,
    last_read_post_id: null,
    oldest_observed_post_id: null,
    initial_catchup_completed: false,
    history_exhausted: false,
    backfill_status: 'idle',
    backfill_requested_count: 0,
    backfill_scanned_count: 0,
    backfill_error: null,
    last_fetched_at: null,
    operational_status: 'active',
    freshness_status: 'never_fetched',
    seconds_since_last_fetch: null,
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
