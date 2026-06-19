import { describe, expect, it } from 'vitest';

import type { PublicMemeCardRead, PublicMemeSearchPageRead, PublicMemeSearchResultRead } from '$lib/api/types';
import {
  INFINITE_FEED_OBSERVER_ROOT_MARGIN,
  appendUniqueMemeResults,
  canLoadNextMemePage,
  memeFeedKey,
  nextMemePageOffset,
  uniqueMemeResults
} from './infinite-feed';

describe('infinite meme feed helpers', () => {
  it('appends incoming results in backend order without duplicate meme ids', () => {
    const merged = appendUniqueMemeResults([result('a'), result('b')], [result('b'), result('c'), result('a'), result('d')]);

    expect(merged.map((item) => item.meme.id)).toEqual(['a', 'b', 'c', 'd']);
  });

  it('deduplicates an initial page while keeping the first occurrence', () => {
    const items = uniqueMemeResults([result('a'), result('b'), result('a')]);

    expect(items.map((item) => item.meme.id)).toEqual(['a', 'b']);
  });

  it('does not reorder existing items when an incoming page is entirely duplicate', () => {
    const existing = [result('rank-1'), result('rank-2'), result('rank-3')];
    const merged = appendUniqueMemeResults(existing, [result('rank-2'), result('rank-1')]);

    expect(merged).toHaveLength(3);
    expect(merged.map((item) => item.meme.id)).toEqual(['rank-1', 'rank-2', 'rank-3']);
  });

  it('advances by backend limit for the next offset', () => {
    const page: PublicMemeSearchPageRead = { items: [result('a')], limit: 12, offset: 24, total: 50, has_more: true, request_id: 'req_test' };

    expect(nextMemePageOffset(page)).toBe(36);
  });

  it('builds a stable key from shareable URL filters without offset', () => {
    expect(
      memeFeedKey({
        query: ' cats ',
        tags: ['reaction', 'cat'],
        includeNsfw: false,
        mediaType: 'gif',
        language: 'en',
        scope: 'collections',
        collectionIds: ['team']
      })
    ).toBe(
      memeFeedKey({
        query: 'cats',
        tags: ['reaction', 'cat'],
        includeNsfw: false,
        mediaType: 'gif',
        language: 'en',
        scope: 'collections',
        collectionIds: ['team']
      })
    );

    expect(memeFeedKey({ query: 'cats', scope: 'collections', collectionIds: ['team'] })).not.toBe(
      memeFeedKey({ query: 'cats', scope: 'collections', collectionIds: ['shared'] })
    );
  });

  it('only allows observer or Load more fetching in a stable ready state', () => {
    expect(canLoadNextMemePage({ hasMore: true, loading: false, errorMessage: null, itemCount: 3 })).toBe(true);
    expect(canLoadNextMemePage({ hasMore: false, loading: false, errorMessage: null, itemCount: 3 })).toBe(false);
    expect(canLoadNextMemePage({ hasMore: true, loading: true, errorMessage: null, itemCount: 3 })).toBe(false);
    expect(canLoadNextMemePage({ hasMore: true, loading: false, errorMessage: 'Network failed', itemCount: 3 })).toBe(false);
    expect(canLoadNextMemePage({ hasMore: true, loading: false, errorMessage: null, itemCount: 0 })).toBe(false);
  });

  it('uses a fixed early root margin for stable IntersectionObserver prefetching', () => {
    expect(INFINITE_FEED_OBSERVER_ROOT_MARGIN).toBe('420px 0px');
  });
});

function result(id: string): PublicMemeSearchResultRead {
  return {
    meme: meme(id),
    attribution: {
      request_id: 'req_test',
      impression_id: `imp_${id}`,
      surface: 'test',
      source_algorithm: 'hybrid_search',
      rank: null,
      query: null,
      filters: { language: null, media_type: null, include_nsfw: false, tags: [], scope: 'public', collection_ids: [] },
      collection_scope: 'public',
      collection_ids: [],
      source_meme_id: null,
      algorithm_version: 'test',
      score: null,
      score_components: {},
      reason: null
    }
  };
}

function meme(id: string): PublicMemeCardRead {
  return {
    id,
    media_type: 'image',
    language: 'en',
    is_nsfw: false,
    popularity_score: 1,
    like_count: 0,
    tags: [],
    primary_file: null,
    caption: id,
    seo_page_slug: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    viewer_has_favorited: false,
    viewer_has_saved: false,
    viewer_has_pinned: false
  };
}
