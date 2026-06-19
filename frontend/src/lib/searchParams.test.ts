import { describe, expect, it } from 'vitest';

import { buildSearchHref, normalizeTags, parseSearchParams } from './searchParams';

describe('search route params', () => {
  it('parses shareable search filters from URL params', () => {
    const state = parseSearchParams(
      new URLSearchParams(
        'q=cat+reaction&tags=Cat&tags=wholesome,work&include_nsfw=true&media_type=gif&language=en&scope=collections&collection_ids=team&collection_ids=shared,team&offset=24'
      )
    );

    expect(state).toEqual({
      query: 'cat reaction',
      tags: ['cat', 'wholesome', 'work'],
      includeNsfw: true,
      mediaType: 'gif',
      language: 'en',
      scope: 'collections',
      collectionIds: ['team', 'shared'],
      offset: 24
    });
  });

  it('ignores unsupported enum values and unsafe offsets', () => {
    const state = parseSearchParams(new URLSearchParams('include_nsfw=false&media_type=doc&language=de&scope=friends&offset=-12'));

    expect(state.mediaType).toBeNull();
    expect(state.language).toBeNull();
    expect(state.scope).toBe('public');
    expect(state.includeNsfw).toBe(false);
    expect(state.offset).toBe(0);
  });

  it('builds pagination links with repeated tags and preserved filters', () => {
    const href = buildSearchHref(
      {
        query: 'frog',
        tags: ['reaction', 'cat'],
        includeNsfw: false,
        mediaType: 'image',
        language: 'mixed',
        scope: 'collections',
        collectionIds: ['team', 'shared'],
        offset: 0
      },
      { offset: 12 }
    );

    expect(href).toBe(
      '/search?q=frog&tags=reaction&tags=cat&include_nsfw=false&media_type=image&language=mixed&scope=collections&collection_ids=team&collection_ids=shared&offset=12'
    );
  });

  it('drops collection ids when building non-collection scope links', () => {
    const href = buildSearchHref({
      query: '',
      tags: [],
      includeNsfw: false,
      mediaType: null,
      language: null,
      scope: 'all',
      collectionIds: ['team'],
      offset: 0
    });

    expect(href).toBe('/search?include_nsfw=false&scope=all');
  });

  it('normalizes comma-delimited tag input', () => {
    expect(normalizeTags(['#Cat, cat', ' wholesome '])).toEqual(['cat', 'wholesome']);
  });
});
