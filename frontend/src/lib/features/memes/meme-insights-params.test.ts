import { describe, expect, it } from 'vitest';

import {
  memeInsightsHref,
  parseMemeInsightsParams
} from './meme-insights-params';

describe('meme insights URL state', () => {
  it('parses supported source and analytics controls and rejects unsafe input', () => {
    expect(
      parseMemeInsightsParams(
        new URLSearchParams(
          'source_sort=reposts_desc&source_offset=20&source_snapshot=2026-07-20T10%3A00%3A00Z&activity_window=90d'
        )
      )
    ).toEqual({
      sourceSort: 'reposts_desc',
      sourceOffset: 20,
      sourceSnapshot: '2026-07-20T10:00:00.000Z',
      analyticsWindow: '90d'
    });

    expect(
      parseMemeInsightsParams(
        new URLSearchParams('source_sort=secret&source_offset=-5&source_snapshot=never&activity_window=365d')
      )
    ).toEqual({
      sourceSort: 'views_desc',
      sourceOffset: 0,
      sourceSnapshot: null,
      analyticsWindow: '30d'
    });
  });

  it('preserves discovery attribution while changing one insight slice', () => {
    const current = new URLSearchParams(
      'attribution_impression_id=imp-1&source_offset=10&source_snapshot=2026-07-20T10%3A00%3A00.000Z&activity_window=7d'
    );

    expect(
      memeInsightsHref('/memes/launch', current, {
        sourceSort: 'newest',
        sourceOffset: 0,
        sourceSnapshot: null
      })
    ).toBe('/memes/launch?attribution_impression_id=imp-1&activity_window=7d&source_sort=newest#meme-sources-activity');

    expect(memeInsightsHref('/memes/launch', current, { analyticsWindow: '30d' })).toContain(
      'attribution_impression_id=imp-1'
    );
    expect(memeInsightsHref('/memes/launch', current, { analyticsWindow: '30d' })).not.toContain(
      'activity_window'
    );
    expect(memeInsightsHref('/memes/launch', current, { analyticsWindow: '30d' })).toContain(
      '#meme-professional-analytics'
    );
  });
});
