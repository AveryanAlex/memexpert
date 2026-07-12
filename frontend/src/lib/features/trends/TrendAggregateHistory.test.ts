import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import type { PublicTrendAggregatePointRead, PublicTrendMetricsRead, PublicTrendSummaryRead } from '$lib/api/types';
import TrendAggregateHistory from './TrendAggregateHistory.svelte';

describe('TrendAggregateHistory', () => {
  it('plots the documented recorded-activity total and provides the same value in its table', () => {
    const { body } = render(TrendAggregateHistory, {
      props: {
        summary: summaryPayload({
          points: [
            aggregatePoint({ observed_at: '2026-01-01T00:00:00Z', value: 100, source_views: 10, platform_likes: 3 }),
            aggregatePoint({ observed_at: '2026-01-02T00:00:00Z', value: 150, source_views: 20, platform_likes: 7 })
          ]
        })
      }
    });

    expect(body).toContain('Recorded activity over time');
    expect(body).toContain('Reaction memes recorded activity over time');
    expect(body).toContain('Recorded activity details for Reaction memes');
    expect(body).toContain('Recorded activity adds original-source views, reactions, and reposts');
    expect(body).toContain('Jan 1, 2026');
    expect(body).toContain('Original sources');
    expect(body).toContain('MemeExpert');
    expect(body).toContain('26');
    expect(body).toContain('40');
    expect(body).not.toContain('Aggregate popularity score');
    expect(body).not.toContain('150.0');
    expect(body).not.toContain('Source views');
    expect(body).not.toContain('history points');
  });

  it('renders a friendly one-point state without a fake line', () => {
    const { body } = render(TrendAggregateHistory, {
      props: {
        summary: summaryPayload({
          points: [aggregatePoint({ observed_at: '2026-01-02T00:00:00Z', value: 25 })],
          insufficient_history: true
        })
      }
    });

    expect(body).toContain('A new trend is taking shape');
    expect(body).toContain('Come back soon to see how it changes.');
    expect(body).toContain('Recorded activity details for Reaction memes');
    expect(body).not.toContain('Insufficient aggregate history');
    expect(body).not.toContain('recorded activity over time line chart');
  });

  it('renders a friendly empty state when activity has not appeared yet', () => {
    const { body } = render(TrendAggregateHistory, {
      props: {
        summary: summaryPayload({
          points: [],
          insufficient_history: true,
          current_only_reason: 'No historical aggregate snapshot points exist; using the current public trend window only.'
        })
      }
    });

    expect(body).toContain('Nothing to chart yet');
    expect(body).toContain('Activity will appear here as this collection of memes catches on.');
    expect(body).not.toContain('No historical aggregate snapshot points exist; using the current public trend window only.');
    expect(body).not.toContain('current window only');
    expect(body).not.toContain('<table');
  });
});

function summaryPayload(overrides: Partial<PublicTrendSummaryRead> = {}): PublicTrendSummaryRead {
  return {
    kind: 'tag',
    slug: 'reaction',
    title: 'Reaction memes',
    description: 'Aggregate public trend activity for reaction memes.',
    meme_count: 2,
    trend: trendMetrics(),
    points: [],
    insufficient_history: false,
    no_data_reason: null,
    current_only_reason: null,
    ...overrides
  };
}

function aggregatePoint(overrides: Partial<PublicTrendAggregatePointRead> = {}): PublicTrendAggregatePointRead {
  return {
    observed_at: '2026-01-01T00:00:00Z',
    value: 100,
    metric: 'aggregate_popularity_score',
    label: 'Aggregate popularity score',
    meme_count: 2,
    snapshot_count: 4,
    source_views: 10,
    source_reactions: 3,
    source_reposts: 1,
    platform_views: 6,
    platform_sends: 2,
    platform_saves: 1,
    platform_likes: 3,
    ...overrides
  };
}

function trendMetrics(): PublicTrendMetricsRead {
  return {
    recent: { views: 4, sends: 1, likes: 2, saves: 0, downloads: 0 },
    previous: { views: 2, sends: 0, likes: 1, saves: 0, downloads: 0 },
    latest_snapshot_at: '2026-01-02T00:00:00Z',
    latest_source_views: 20,
    latest_source_reactions: 3,
    latest_source_reposts: 1,
    latest_platform_views: 6,
    latest_platform_sends: 1,
    latest_platform_saves: 0,
    latest_platform_likes: 2,
    latest_popularity_score: 15,
    engagement_24h: 7,
    trending_score: 22,
    refreshed_at: '2026-01-02T00:00:00Z'
  };
}
