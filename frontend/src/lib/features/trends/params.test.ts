import { describe, expect, it } from 'vitest';

import { comparisonHref, readComparisonItems, readTimelineGranularity, trendTimelineHref } from './params';

describe('trend URL params', () => {
  it('reads repeated comparison items and clamps empty/noisy params', () => {
    const params = new URLSearchParams();
    for (const item of [' meme:first ', '', 'tag:reaction', 'template:frog', 'meme:2', 'meme:3', 'meme:4', 'meme:5']) {
      params.append('item', item);
    }

    expect(readComparisonItems(params)).toEqual(['meme:first', 'tag:reaction', 'template:frog', 'meme:2', 'meme:3', 'meme:4']);
  });

  it('deduplicates repeated comparison values without changing their order', () => {
    const params = new URLSearchParams('item=tag%3Ax&item=tag%3Ax&item=meme%3Ay');

    expect(readComparisonItems(params)).toEqual(['tag:x', 'meme:y']);
    expect(comparisonHref(['tag:x', 'tag:x', 'meme:y'])).toBe('/trends/compare?item=tag%3Ax&item=meme%3Ay');
  });

  it('builds shareable compare and timeline links', () => {
    expect(comparisonHref(['meme:first', ' ', 'tag:reaction'])).toBe('/trends/compare?item=meme%3Afirst&item=tag%3Areaction');
    expect(comparisonHref([])).toBe('/trends/compare');
    expect(readTimelineGranularity('year')).toBe('year');
    expect(readTimelineGranularity('week')).toBe('month');
    expect(trendTimelineHref('month')).toBe('/trends/timeline?granularity=month');
    expect(trendTimelineHref('year', 12)).toBe('/trends/timeline?granularity=year&offset=12');
  });
});
