import { describe, expect, it } from 'vitest';

import { buildSearchHref, normalizeTags, parseSearchParams } from './searchParams';

describe('search route params', () => {
  it('parses shareable search filters from URL params', () => {
    const state = parseSearchParams(
      new URLSearchParams('q=cat+reaction&tags=Cat&tags=wholesome,work&include_nsfw=true&media_type=gif&language=en&offset=24')
    );

    expect(state).toEqual({
      query: 'cat reaction',
      tags: ['cat', 'wholesome', 'work'],
      includeNsfw: true,
      mediaType: 'gif',
      language: 'en',
      offset: 24
    });
  });

  it('ignores unsupported enum values and unsafe offsets', () => {
    const state = parseSearchParams(new URLSearchParams('include_nsfw=false&media_type=doc&language=de&offset=-12'));

    expect(state.mediaType).toBeNull();
    expect(state.language).toBeNull();
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
        offset: 0
      },
      { offset: 12 }
    );

    expect(href).toBe('/search?q=frog&tags=reaction&tags=cat&include_nsfw=false&media_type=image&language=mixed&offset=12');
  });

  it('normalizes comma-delimited tag input', () => {
    expect(normalizeTags(['#Cat, cat', ' wholesome '])).toEqual(['cat', 'wholesome']);
  });
});
