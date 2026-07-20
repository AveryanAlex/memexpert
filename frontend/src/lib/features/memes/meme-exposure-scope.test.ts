import { describe, expect, it } from 'vitest';

import { createMemeExposureScope, hasQualifyingMemeExposure } from './meme-exposure-scope';

describe('page-scoped meme exposures', () => {
  it('keeps generated placement IDs stable and distinct', () => {
    const scope = createMemeExposureScope('/search');

    const first = scope.resolveExposureId(null, 'results:1:meme-a');
    expect(first).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-7[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    expect(scope.resolveExposureId(null, 'results:1:meme-a')).toBe(first);
    expect(scope.resolveExposureId(null, 'results:2:meme-a')).not.toBe(first);
    expect(scope.resolveExposureId(' backend-token ', 'results:3:meme-a')).toBe(' backend-token ');
  });

  it('deduplicates a remounted card through the shared page scope and resets after navigation', () => {
    const scope = createMemeExposureScope('/search');
    const ssrExposureId = scope.resolveExposureId(null, 'results:1:meme-a');
    scope.beginClientVisit('browser-load-a');
    const exposureId = scope.resolveExposureId(null, 'results:1:meme-a');

    expect(exposureId).not.toBe(ssrExposureId);

    expect(scope.claim(exposureId)).toBe(true);
    expect(scope.hasRecorded(exposureId)).toBe(true);
    // A remounted card resolves the same placement and cannot claim it twice.
    expect(scope.claim(scope.resolveExposureId(null, 'results:1:meme-a'))).toBe(false);

    scope.syncPage('/memes/meme-a');
    expect(scope.hasRecorded(exposureId)).toBe(false);
    scope.syncPage('/search');
    const revisitExposureId = scope.resolveExposureId(null, 'results:1:meme-a');
    expect(revisitExposureId).not.toBe(exposureId);
    expect(scope.resolveExposureId('backend-token', 'results:1:meme-a')).toBe('backend-token');
    expect(scope.claim(revisitExposureId)).toBe(true);
  });

  it('requires an actual 25 percent intersection', () => {
    expect(hasQualifyingMemeExposure([{ isIntersecting: true, intersectionRatio: 0.249 }])).toBe(false);
    expect(hasQualifyingMemeExposure([{ isIntersecting: false, intersectionRatio: 0.9 }])).toBe(false);
    expect(hasQualifyingMemeExposure([{ isIntersecting: true, intersectionRatio: 0.25 }])).toBe(true);
  });
});
