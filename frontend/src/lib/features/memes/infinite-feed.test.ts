import { describe, expect, it } from 'vitest';

import type { PublicMemeCardRead, PublicMemeSearchPageRead, PublicMemeSearchResultRead } from '$lib/api/types';
import { appendUniqueMemeResults, memeFeedKey, nextMemePageOffset, uniqueMemeResults } from './infinite-feed';

describe('infinite meme feed helpers', () => {
  it('appends incoming results in backend order without duplicate meme ids', () => {
    const merged = appendUniqueMemeResults([result('a'), result('b')], [result('b'), result('c'), result('a'), result('d')]);

    expect(merged.map((item) => item.meme.id)).toEqual(['a', 'b', 'c', 'd']);
  });

  it('deduplicates an initial page while keeping the first occurrence', () => {
    const items = uniqueMemeResults([result('a'), result('b'), result('a')]);

    expect(items.map((item) => item.meme.id)).toEqual(['a', 'b']);
  });

  it('advances by backend limit for the next offset', () => {
    const page: PublicMemeSearchPageRead = { items: [result('a')], limit: 12, offset: 24, total: 50, has_more: true };

    expect(nextMemePageOffset(page)).toBe(36);
  });

  it('builds a stable key from shareable URL filters without offset', () => {
    expect(
      memeFeedKey({
        query: ' cats ',
        tags: ['reaction', 'cat'],
        includeNsfw: false,
        mediaType: 'gif',
        language: 'en'
      })
    ).toBe(memeFeedKey({ query: 'cats', tags: ['reaction', 'cat'], includeNsfw: false, mediaType: 'gif', language: 'en' }));
  });
});

function result(id: string): PublicMemeSearchResultRead {
  return { meme: meme(id) };
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
