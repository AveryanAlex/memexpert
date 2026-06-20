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

  it('round trips all shareable filters through search hrefs', () => {
    const state = parseSearchParams(
      new URLSearchParams(
        'q=vault+reaction&tags=Cat&tags=work,deploy&include_nsfw=1&media_type=video&language=ru&scope=collections&collection_ids=team&collection_ids=shared&offset=36'
      )
    );

    const href = buildSearchHref(state);
    const roundTripped = parseSearchParams(new URL(href, 'https://memexpert.test').searchParams);

    expect(roundTripped).toEqual({
      query: 'vault reaction',
      tags: ['cat', 'work', 'deploy'],
      includeNsfw: true,
      mediaType: 'video',
      language: 'ru',
      scope: 'collections',
      collectionIds: ['team', 'shared'],
      offset: 36
    });
  });

  it('switches away from collections without losing other shareable filters', () => {
    const state = parseSearchParams(
      new URLSearchParams(
        'q=frog&tags=reaction&include_nsfw=true&media_type=gif&language=mixed&scope=collections&collection_ids=team&collection_ids=shared&offset=24'
      )
    );

    const href = buildSearchHref(state, { scope: 'all', offset: 0 });

    expect(href).toBe('/search?q=frog&tags=reaction&include_nsfw=true&media_type=gif&language=mixed&scope=all');
    expect(parseSearchParams(new URL(href, 'https://memexpert.test').searchParams)).toEqual({
      query: 'frog',
      tags: ['reaction'],
      includeNsfw: true,
      mediaType: 'gif',
      language: 'mixed',
      scope: 'all',
      collectionIds: [],
      offset: 0
    });
  });

  it('defaults to public search and ignores collection ids outside collection scope', () => {
    const state = parseSearchParams(new URLSearchParams('collection_ids=team&collection_ids=shared'));

    expect(state).toEqual({
      query: '',
      tags: [],
      includeNsfw: false,
      mediaType: null,
      language: null,
      scope: 'public',
      collectionIds: [],
      offset: 0
    });
    expect(buildSearchHref(state)).toBe('/search?include_nsfw=false&scope=public');
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
