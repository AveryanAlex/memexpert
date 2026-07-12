import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import type { PublicMemeCardRead, PublicTrendComparisonRead, PublicTrendMetricsRead } from '$lib/api/types';
import TrendComparePage from '../routes/trends/compare/+page.svelte';

describe('/trends/compare page', () => {
  it('renders labeled comparison rows, selected chips, and an accessible text fallback', () => {
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

    expect(body).toContain('Compare what is catching on.');
    expect(body).toContain('Choose what to compare');
    expect(body).toContain('Item type');
    expect(body).toContain('Name or identifier');
    expect(body).toContain('Selected items');
    expect(body).toContain('meme:launch-reaction');
    expect(body).toContain('tag:reaction');
    expect(body).toContain('template:current-only-template');
    expect(body).toContain('name="item" value="meme:launch-reaction"');
    expect(body).toContain('name="item" value="tag:reaction"');
    expect(body).toContain('name="item" value="template:current-only-template"');
    expect(body).toContain('Recorded activity comparison');
    expect(body).toContain('Recorded activity adds original-source views, reactions, and reposts');
    expect(body).toContain('Recorded activity details for the comparison');
    expect(body).toContain('Recorded activity');
    expect(body).toContain('Jan 1, 2026');
    expect(body).toContain('23 signals');
    expect(body).toContain('22 signals');
    expect(body).toContain('Some picks will join the chart once they have two recorded activity moments.');
    expect(body).toContain('<noscript>');
    expect(body).toContain('Without JavaScript, enter comparison items from a shared link.');
    expect(body).toContain('Comparison item');
    expect(body).toMatch(/<noscript>[\s\S]*name="item"/);
    expect(body).toContain('Launch reaction meme');
    expect(body).toContain('Meme · Launch reaction meme');
    expect(body).toContain('Reaction memes');
    expect(body).toContain('Current Only Template memes');
    expect(body).not.toContain('Use specs like');
    expect(body).not.toContain('Meme series use source-delta');
    expect(body).not.toContain('15.0');
    expect(body).not.toContain('Aggregate history points');
    expect(body).not.toContain('Current-window aggregate fallback');
    expect(body).not.toContain('Insufficient history');
    expect(body).not.toContain('No historical aggregate snapshot points exist; using the current public trend window only.');
  });

  it('renders a friendly empty starter state without typed URL instructions', () => {
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

    expect(body).toContain('Pick a few things to compare');
    expect(body).toContain('Start with a meme, tag, or template you want to explore.');
    expect(body).not.toContain('Start with URL params');
    expect(body).not.toContain('/trends/compare?item=tag:reaction');
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
        meme: memeCard(),
        trend: trendMetrics(),
        insufficient_history: false,
        no_data_reason: null,
        points: [
          {
            observed_at: '2026-01-01T00:00:00Z',
            value: 10,
            metric: 'popularity_score',
            label: 'Popularity score',
            source_views: 4,
            source_reactions: 1,
            source_reposts: 0,
            platform_views: 3,
            platform_sends: 1,
            platform_saves: 0,
            platform_likes: 1
          },
          {
            observed_at: '2026-01-02T00:00:00Z',
            value: 15,
            metric: 'popularity_score',
            label: 'Popularity score',
            source_views: 8,
            source_reactions: 2,
            source_reposts: 1,
            platform_views: 6,
            platform_sends: 2,
            platform_saves: 1,
            platform_likes: 3
          }
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

function memeCard(): PublicMemeCardRead {
  return {
    id: '11111111-1111-4111-8111-111111111111',
    media_type: 'image',
    language: 'en',
    is_nsfw: false,
    popularity_score: 15,
    like_count: 2,
    tags: ['reaction'],
    primary_file: null,
    caption: 'Launch reaction meme',
    seo_page_slug: 'launch-reaction',
    viewer_has_favorited: false,
    viewer_has_saved: false,
    viewer_has_pinned: false,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-02T00:00:00Z'
  };
}
