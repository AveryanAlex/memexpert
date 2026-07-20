import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import type {
  PublicMemeAnalyticsRead,
  PublicMemeMetricCoverageRead,
  PublicMemeSourcePageRead,
  PublicMemeSourceRateRead,
  PublicMemeSourceSummaryRead
} from '$lib/api/types';
import MemeSourcesAndActivity from './MemeSourcesAndActivity.svelte';

describe('MemeSourcesAndActivity', () => {
  it('renders honest Telegram attribution and a nested collapsed professional suite', () => {
    const { body } = render(MemeSourcesAndActivity, {
      props: {
        sourcePage: sourcePage(),
        sourceError: null,
        analytics: analytics(),
        analyticsError: null,
        insightsParams: {
          sourceSort: 'views_desc',
          sourceOffset: 0,
          sourceSnapshot: null,
          analyticsWindow: '30d'
        },
        pathname: '/memes/launch',
        search: '?attribution_impression_id=imp-1'
      }
    });

    expect(body).toContain('Sources &amp; activity');
    expect(body).toContain('2 Telegram posts across 1 channel');
    expect(body).toContain('href="https://t.me/source_lab/42"');
    expect(body).toContain('Open Telegram post');
    expect(body).toContain('Unknown');
    expect(body).toContain('Professional analytics');
    expect(body).toContain('Recorded activity · signals per day');
    expect(body).toContain('aria-label="Telegram counter shown in chart"');
    expect(body.match(/type="radio"/g)).toHaveLength(4);
    expect(body).toContain('value="comments"');
    expect(body).toContain('Absolute server-bucketed end states');
    expect(body).toContain('Observed / as of');
    expect(body).toContain('Channel audience context');
    expect(body).toContain('Exposure funnels');
    expect(body).toContain('Telegram inline');
    expect(body).toContain('source_snapshot=2026-07-20T10%3A00%3A00Z');
    expect(body).toContain('attribution_impression_id=imp-1');
    expect(body.match(/<details/g)).toHaveLength(3); // Outer, professional, and chart-data disclosures.
    expect(body).not.toMatch(/<details[^>]*\sopen(?:=|\s|>)/);
  });

  it('keeps independent source and analytics failures visible', () => {
    const { body } = render(MemeSourcesAndActivity, {
      props: {
        sourcePage: null,
        sourceError: 'Sources are unavailable.',
        analytics: null,
        analyticsError: 'Analytics are unavailable.',
        insightsParams: {
          sourceSort: 'views_desc',
          sourceOffset: 0,
          sourceSnapshot: null,
          analyticsWindow: '30d'
        },
        pathname: '/memes/launch',
        search: ''
      }
    });

    expect(body).toContain('Sources are unavailable.');
    expect(body).toContain('Activity analytics below may still be available.');
    expect(body).toContain('Analytics are unavailable.');
    expect(body).toContain('The Telegram post list above may still be available.');
  });

  it('uses honest copy for an out-of-range empty source page', () => {
    const page = sourcePage();
    page.items = [];
    page.limit = 10;
    page.offset = 100;
    page.total = 20;
    page.has_more = false;

    const { body } = render(MemeSourcesAndActivity, {
      props: {
        sourcePage: page,
        sourceError: null,
        analytics: null,
        analyticsError: null,
        insightsParams: {
          sourceSort: 'views_desc',
          sourceOffset: 100,
          sourceSnapshot: page.snapshot_at,
          analyticsWindow: '30d'
        },
        pathname: '/memes/launch',
        search: '?source_offset=100'
      }
    });

    expect(body).toContain('20 posts total');
    expect(body).toContain('No posts on this page');
    expect(body).not.toContain('Showing 101–20');
    expect(body).not.toMatch(/Showing\s+\d+[–-]\d+/);
  });
});

function sourcePage(): PublicMemeSourcePageRead {
  return {
    meme_id: 'meme-1',
    snapshot_at: '2026-07-20T10:00:00Z',
    sort: 'views_desc',
    items: [
      {
        channel_title: 'Source Lab',
        channel_username: 'source_lab',
        channel_url: 'https://t.me/source_lab',
        post_url: 'https://t.me/source_lab/42',
        published_at: '2026-07-18T10:00:00Z',
        available: true,
        captured_at: '2026-07-20T10:00:00Z',
        views: 120,
        reactions: 8,
        comments: null,
        reposts: 4,
        rates: rates(2),
        audience: {
          audience_at_publish: 500,
          current_audience: 540,
          views_per_1000_subscribers: 240,
          interactions_per_1000_subscribers: null
        }
      }
    ],
    summary: sourceSummary(),
    limit: 1,
    offset: 0,
    total: 2,
    has_more: true
  };
}

function analytics(): PublicMemeAnalyticsRead {
  const coverage = sourceCoverage(2);
  return {
    meme_id: 'meme-1',
    window: '30d',
    start_at: '2026-06-20T10:00:00Z',
    end_at: '2026-07-20T10:00:00Z',
    granularity: 'day',
    history_start_at: '2026-07-18T10:00:00Z',
    history_end_at: '2026-07-20T10:00:00Z',
    refreshed_at: '2026-07-20T10:00:00Z',
    insufficient_history: false,
    summary: {
      totals: counts(42),
      average_recorded_activity_per_day: 1.4,
      current_favorites: 9,
      momentum: { recent_recorded_activity: 30, previous_recorded_activity: 12, change: 18, change_rate: 1.5 },
      peak: { bucket_start: '2026-07-20T00:00:00Z', bucket_end: '2026-07-21T00:00:00Z', granularity: 'day', recorded_activity: 25 }
    },
    activity_points: [
      { ...counts(17), bucket_start: '2026-07-19T00:00:00Z', bucket_end: '2026-07-20T00:00:00Z', granularity: 'day' },
      { ...counts(25), bucket_start: '2026-07-20T00:00:00Z', bucket_end: '2026-07-21T00:00:00Z', granularity: 'day' }
    ],
    observed_source: {
      opening_baseline: { observed_at: '2026-07-18T10:00:00Z', views: 90, reactions: 6, comments: null, reposts: 3, coverage },
      points: [{ observed_at: '2026-07-20T10:00:00Z', views: 120, reactions: 8, comments: null, reposts: 4, coverage }]
    },
    source_performance: sourceSummary(),
    audience_change: { total_channels: 1, current_known_channels: 1, comparable_channels: 1, net_known_subscriber_change: 40 },
    exposure_funnels: {
      web: { recorded_card_impressions: 20, attributed_impressions: 18, matched_detail_clicks: 8, matched_high_intent_actions: 3, detail_click_rate: 8 / 18, high_intent_rate: 3 / 18 },
      telegram_inline: { inline_results_served: 10, attributed_results_served: 9, matched_chosen: 4, matched_sent: 3, chosen_rate: 4 / 9, sent_rate: 3 / 9 }
    }
  };
}

function counts(recordedActivity: number) {
  return {
    source_views: Math.max(recordedActivity - 8, 0),
    source_reactions: 2,
    source_reposts: 1,
    memeexpert_views: 2,
    memeexpert_sends: 1,
    memeexpert_saves: 1,
    memeexpert_favorites: 1,
    downloads: 3,
    recorded_activity: recordedActivity
  };
}

function sourceSummary(): PublicMemeSourceSummaryRead {
  return {
    total_posts: 2,
    available_posts: 2,
    distinct_channels: 1,
    earliest_published_at: '2026-07-18T10:00:00Z',
    latest_published_at: '2026-07-19T10:00:00Z',
    latest_captured_at: '2026-07-20T10:00:00Z',
    totals: { views: 220, reactions: 14, comments: null, reposts: 7 },
    coverage: sourceCoverage(2),
    rates: rates(2),
    audience: {
      current_known_channels: 1,
      total_channels: 1,
      publish_time_eligible_posts: 1,
      total_posts: 2,
      views_per_1000_subscribers: rate(440, 220, 500, 1, 2),
      interactions_per_1000_subscribers: rate(null, null, null, 0, 2)
    }
  };
}

function rates(totalPosts: number) {
  return {
    reactions: rate(0.06, 14, 220, totalPosts, totalPosts),
    comments: rate(null, null, null, 0, totalPosts),
    reposts: rate(0.03, 7, 220, totalPosts, totalPosts),
    interactions: rate(null, null, null, 0, totalPosts)
  };
}

function rate(
  value: number | null,
  numerator: number | null,
  denominator: number | null,
  eligiblePosts: number,
  totalPosts: number
): PublicMemeSourceRateRead {
  return { value, numerator, denominator, eligible_posts: eligiblePosts, total_posts: totalPosts };
}

function sourceCoverage(totalPosts: number) {
  return {
    views: coverage(totalPosts, totalPosts),
    reactions: coverage(totalPosts, totalPosts),
    comments: coverage(0, totalPosts),
    reposts: coverage(totalPosts, totalPosts)
  };
}

function coverage(measuredPosts: number, totalPosts: number): PublicMemeMetricCoverageRead {
  return {
    measured_posts: measuredPosts,
    total_posts: totalPosts,
    ratio: totalPosts === 0 ? 0 : measuredPosts / totalPosts
  };
}
