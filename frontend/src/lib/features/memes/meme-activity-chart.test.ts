import { describe, expect, it } from 'vitest';

import type { PublicMemeActivityPointRead, PublicMemeObservedSourcePointRead } from '$lib/api/types';
import {
  activityBucketDurationDays,
  canPlotMemeActivity,
  knownObservedTelegramPoints,
  memeActivityChartDatum,
  observedTelegramLineData
} from './meme-activity-chart';

describe('meme activity chart data', () => {
  it('normalizes adaptive week and month totals to comparable signals per day', () => {
    const week = memeActivityChartDatum(activityPoint({
      bucket_start: '2026-06-01T00:00:00Z',
      bucket_end: '2026-06-08T00:00:00Z',
      granularity: 'week',
      recorded_activity: 70,
      source_views: 35,
      memeexpert_views: 14
    }));
    const month = memeActivityChartDatum(activityPoint({
      bucket_start: '2026-06-01T00:00:00Z',
      bucket_end: '2026-07-01T00:00:00Z',
      granularity: 'month',
      recorded_activity: 300,
      source_views: 150,
      memeexpert_views: 60
    }));

    expect(week).toMatchObject({
      durationDays: 7,
      activityPerDay: 10,
      rawActivity: 70,
      granularity: 'week'
    });
    expect(month).toMatchObject({
      durationDays: 30,
      activityPerDay: 10,
      rawActivity: 300,
      granularity: 'month'
    });
    expect(activityBucketDurationDays('2026-06-01T00:00:00Z', '2026-06-01T06:00:00Z')).toBe(1);
    expect(canPlotMemeActivity([week!, { ...month!, rawActivity: 0, activityPerDay: 0 }])).toBe(true);
    expect(canPlotMemeActivity([{ ...week!, rawActivity: 0, activityPerDay: 0 }, { ...month!, rawActivity: 0, activityPerDay: 0 }])).toBe(false);
    expect(canPlotMemeActivity([week!])).toBe(false);
  });

  it('retains null observations in line data while exposing only known tooltip points', () => {
    const observations = [
      observation('2026-06-01T00:00:00Z', 10),
      observation('2026-06-02T00:00:00Z', null),
      observation('2026-06-03T00:00:00Z', 20)
    ];

    const line = observedTelegramLineData(observations, 'views');
    expect(line.map((point) => point.value)).toEqual([10, null, 20]);
    expect(knownObservedTelegramPoints(line).map((point) => point.value)).toEqual([10, 20]);
  });
});

function activityPoint(overrides: Partial<PublicMemeActivityPointRead>): PublicMemeActivityPointRead {
  return {
    bucket_start: '2026-06-01T00:00:00Z',
    bucket_end: '2026-06-02T00:00:00Z',
    granularity: 'day',
    source_views: 0,
    source_reactions: 0,
    source_reposts: 0,
    memeexpert_views: 0,
    memeexpert_sends: 0,
    memeexpert_saves: 0,
    memeexpert_favorites: 0,
    downloads: 0,
    recorded_activity: 0,
    ...overrides
  };
}

function observation(observedAt: string, views: number | null): PublicMemeObservedSourcePointRead {
  const coverage = {
    views: { measured_posts: views === null ? 0 : 1, total_posts: 1, ratio: views === null ? 0 : 1 },
    reactions: { measured_posts: 0, total_posts: 1, ratio: 0 },
    comments: { measured_posts: 0, total_posts: 1, ratio: 0 },
    reposts: { measured_posts: 0, total_posts: 1, ratio: 0 }
  };
  return { observed_at: observedAt, views, reactions: null, comments: null, reposts: null, coverage };
}
