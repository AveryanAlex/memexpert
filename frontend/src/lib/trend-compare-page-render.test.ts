import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import type { PublicTrendComparisonRead, PublicTrendMetricsRead } from '$lib/api/types';
import TrendComparePage from '../routes/trends/compare/+page.svelte';

describe('/trends/compare page', () => {
  it('renders shareable item inputs, aggregate history copy, and current-only notice', () => {
    const { body } = render(TrendComparePage, {
      props: {
        data: {
          session: null,
          sessionError: null,
          items: ['meme:launch-reaction', 'tag:reaction', 'template:current-only-template'],
          comparison: comparisonPayload(),
          errorMessage: null
        }
      }
    });

    expect(body).toContain('Compare public trends.');
    expect(body).toContain('Meme series use per-meme popularity snapshots');
    expect(body).toContain('meme:launch-reaction');
    expect(body).toContain('tag:reaction');
    expect(body).toContain('template:current-only-template');
    expect(body).toContain('Trend comparison line chart');
    expect(body).toContain('Launch reaction meme');
    expect(body).toContain('Reaction memes');
    expect(body).toContain('Current Only Template memes');
    expect(body).toContain('15.0');
    expect(body).toContain('Aggregate history points');
    expect(body).toContain('Current-window aggregate fallback');
    expect(body).toContain('No historical aggregate snapshot points exist; using the current public trend window only.');
    expect(body).not.toContain('Tags and templates do not have historical snapshot series yet');
  });

  it('renders the empty starter state without fabricated examples', () => {
    const { body } = render(TrendComparePage, {
      props: {
        data: {
          session: null,
          sessionError: null,
          items: [],
          comparison: { items: [], requested_items: [], max_items: 6 },
          errorMessage: null
        }
      }
    });

    expect(body).toContain('Start with URL params');
    expect(body).toContain('/trends/compare?item=tag:reaction');
  });
});

function comparisonPayload(): PublicTrendComparisonRead {
  return {
    max_items: 6,
    requested_items: ['meme:launch-reaction', 'tag:reaction'],
    items: [
      {
        kind: 'meme',
        value: '11111111-1111-4111-8111-111111111111',
        title: 'Launch reaction meme',
        description: 'Popularity score from real captured meme snapshots.',
        meme: null,
        trend: trendMetrics(),
        insufficient_history: false,
        no_data_reason: null,
        points: [
          { observed_at: '2026-01-01T00:00:00Z', value: 10, metric: 'popularity_score', label: 'Popularity score' },
          { observed_at: '2026-01-02T00:00:00Z', value: 15, metric: 'popularity_score', label: 'Popularity score' }
        ]
      },
      {
        kind: 'tag',
        value: 'reaction',
        title: 'Reaction memes',
        description: 'Aggregate public trend activity for reaction memes.',
        meme: null,
        trend: trendMetrics(),
        insufficient_history: false,
        no_data_reason: null,
        current_only_reason: null,
        points: [
          aggregatePoint({ observed_at: '2026-01-01T00:00:00Z', value: 10 }),
          aggregatePoint({ observed_at: '2026-01-02T00:00:00Z', value: 22, source_views: 15 })
        ]
      },
      {
        kind: 'template',
        value: 'current-only-template',
        title: 'Current Only Template memes',
        description: 'Current-window aggregate only.',
        meme: null,
        trend: trendMetrics(),
        insufficient_history: true,
        no_data_reason: null,
        current_only_reason: 'No historical aggregate snapshot points exist; using the current public trend window only.',
        points: [
          {
            observed_at: '2026-01-02T00:00:00Z',
            value: 22,
            metric: 'trending_score',
            label: 'Current public trend window',
            meme_count: 1,
            snapshot_count: 0,
            source_views: 0,
            source_reactions: 0,
            source_reposts: 0,
            platform_views: 0,
            platform_sends: 0,
            platform_saves: 0,
            platform_likes: 0
          }
        ]
      }
    ]
  };
}

function aggregatePoint(overrides: Partial<PublicTrendComparisonRead['items'][number]['points'][number]> = {}): PublicTrendComparisonRead['items'][number]['points'][number] {
  return {
    observed_at: '2026-01-01T00:00:00Z',
    value: 10,
    metric: 'aggregate_popularity_score',
    label: 'Aggregate popularity score',
    meme_count: 1,
    snapshot_count: 1,
    source_views: 10,
    source_reactions: 2,
    source_reposts: 1,
    platform_views: 6,
    platform_sends: 1,
    platform_saves: 0,
    platform_likes: 2,
    ...overrides
  };
}

function trendMetrics(): PublicTrendMetricsRead {
  return {
    recent: { views: 4, sends: 1, likes: 2, saves: 0, downloads: 0 },
    previous: { views: 2, sends: 0, likes: 1, saves: 0, downloads: 0 },
    latest_snapshot_at: '2026-01-02T00:00:00Z',
    latest_source_views: 10,
    latest_source_reactions: 2,
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
