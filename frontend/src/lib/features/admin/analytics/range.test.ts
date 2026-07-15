import { describe, expect, it } from 'vitest';

import {
  adminAnalyticsHref,
  analyticsRangeForControls,
  analyticsRangeParamsFromUrl,
  dateDurationDays,
  formatUtcDate
} from './range';

describe('admin analytics range helpers', () => {
  it('reads only valid ISO calendar dates from the URL', () => {
    const range = analyticsRangeParamsFromUrl(
      new URL('https://memexpert.test/admin/analytics?start_date=2026-06-01&end_date=2026-06-30')
    );

    expect(range).toEqual({ startDate: '2026-06-01', endDate: '2026-06-30' });
    expect(
      analyticsRangeParamsFromUrl(new URL('https://memexpert.test/admin/analytics?start_date=2026-02-31&end_date=not-a-date'))
    ).toEqual({ startDate: null, endDate: null });
  });

  it('builds shareable links that preserve the resolved UTC range and extras', () => {
    const href = adminAnalyticsHref(
      '/admin/analytics/engagement',
      { startDate: '2026-06-01', endDate: '2026-06-30' },
      { sort: 'niche', offset: 50, query_key: '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef' }
    );

    expect(href).toBe('/admin/analytics/engagement?start_date=2026-06-01&end_date=2026-06-30&sort=niche&offset=50&query_key=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef');
  });

  it('prefers the backend-resolved range in controls and formats UTC labels', () => {
    const controls = analyticsRangeForControls(
      { startDate: '2026-06-01', endDate: '2026-06-30', timezone: 'UTC' },
      { startDate: '2026-01-01', endDate: '2026-01-07' }
    );

    expect(controls).toEqual({ startDate: '2026-06-01', endDate: '2026-06-30' });
    expect(dateDurationDays({ startDate: '2026-06-01', endDate: '2026-06-30' })).toBe(30);
    expect(formatUtcDate('2026-06-01')).toBe('Jun 1, 2026');
  });
});
