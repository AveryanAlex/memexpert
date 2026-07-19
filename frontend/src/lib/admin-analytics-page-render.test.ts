import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import type {
  AdminAnalyticsAudienceRead,
  AdminAnalyticsContentRead,
  AdminAnalyticsEngagementRead,
  AdminAnalyticsOverviewRead,
  AdminAnalyticsSearchQueryPageRead
} from '$lib/api/types';
import AudienceAnalytics from '$lib/features/admin/analytics/AudienceAnalytics.svelte';
import ContentAnalytics from '$lib/features/admin/analytics/ContentAnalytics.svelte';
import EngagementAnalytics from '$lib/features/admin/analytics/EngagementAnalytics.svelte';
import OverviewAnalytics from '$lib/features/admin/analytics/OverviewAnalytics.svelte';

const requestedRange = { startDate: '2026-06-01', endDate: '2026-06-30' };

describe('admin analytics workspaces', () => {
  it('renders the overview with shared UTC controls, section navigation, funnel, and source signals', () => {
    const { body } = render(OverviewAnalytics, { props: { dashboard: overview(), requestedRange, loadError: null } });

    expect(body).toContain('Analytics overview');
    expect(body).toContain('Engagement');
    expect(body).toContain('Content &amp; sources');
    expect(body).toContain('name="start_date"');
    expect(body).toContain('value="2026-06-01"');
    expect(body).toContain('Catalog memes');
    expect(body).toContain('From search to saved media');
    expect(body).toContain('Source activity');
  });

  it('renders query sort modes, raw query outcomes, and retained range links', () => {
    const { body } = render(EngagementAnalytics, {
      props: {
        dashboard: engagement(),
        searchQueries: searchQueries(),
        queryDetail: null,
        selectedQueryKey: null,
        offset: 0,
        sort: 'niche',
        requestedRange,
        loadError: null
      }
    });

    expect(body).toContain('Search query explorer');
    expect(body).toContain('Popular');
    expect(body).toContain('Niche');
    expect(body).toContain('No-result');
    expect(body).toContain('Downloads');
    expect(body).toContain('frog reaction');
    expect(body).toContain('View outcomes');
    expect(body).toContain('sort=niche');
    expect(body).toContain('start_date=2026-06-01');
    expect(body).toContain('query_key=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef');
    expect(body).toMatch(/href="[^\"]*sort=niche[^\"]*"[^>]*aria-current="page"/);
    expect(body).toContain('focus-visible:outline-accent');
    expect(body).not.toContain('query=frog+reaction');
  });

  it('renders mature cohort states and content/source breakdowns', () => {
    const audienceBody = render(AudienceAnalytics, { props: { dashboard: audience(), requestedRange, loadError: null } }).body;
    const contentBody = render(ContentAnalytics, { props: { dashboard: content(), requestedRange, loadError: null } }).body;

    expect(audienceBody).toContain('Mature account cohorts');
    expect(audienceBody).toContain('D30 retention');
    expect(audienceBody).toContain('Not mature yet');
    expect(contentBody).toContain('Content &amp; sources');
    expect(contentBody).toContain('Catalog growth');
    expect(contentBody).toContain('Media types');
    expect(contentBody).toContain('Source health');
  });
});

function range() {
  return {
    start_date: '2026-06-01',
    end_date: '2026-06-30',
    comparison_start_date: '2026-05-02',
    comparison_end_date: '2026-05-31',
    timezone: 'UTC' as const,
    bucket: 'day' as const
  };
}

function metric(value = 12) {
  return { value, previous_value: 10, change: value - 10, change_percent: (value - 10) * 10 };
}

function overview(): AdminAnalyticsOverviewRead {
  return {
    range: range(),
    metrics: {
      catalog_memes: metric(500),
      new_memes: metric(20),
      page_views: metric(100),
      active_users: metric(30),
      interactions: metric(80),
      downloads: metric(7),
      guest_to_full_conversions: metric(2)
    },
    activity: [
      { date: '2026-06-01', page_views: 3, active_users: 2, interactions: 4, searches: 2, downloads: 1, new_memes: 1 },
      { date: '2026-06-02', page_views: 5, active_users: 3, interactions: 6, searches: 3, downloads: 2, new_memes: 2 }
    ],
    discovery_funnel: { searches: 10, searches_with_results: 8, searches_without_results: 2, detail_clicks: 5, downloads: 2 },
    surface_mix: [{ surface: 'web_search', count: 8 }, { surface: 'telegram_inline', count: 3 }],
    source_activity: { sources: 5, new_sources: 1, source_views: 100, source_reactions: 12, source_reposts: 3 }
  };
}

function engagement(): AdminAnalyticsEngagementRead {
  return {
    range: range(),
    metrics: {
      interactions: metric(), searches: metric(), zero_result_searches: metric(), zero_result_rate: metric(5), average_search_latency_ms: metric(101), detail_clicks: metric(), downloads: metric(), sends: metric(), saves: metric(), shares: metric()
    },
    activity: [
      { date: '2026-06-01', interactions: 2, searches: 2, zero_result_searches: 0, detail_clicks: 1, downloads: 1, sends: 0, saves: 0, shares: 0 },
      { date: '2026-06-02', interactions: 4, searches: 3, zero_result_searches: 1, detail_clicks: 2, downloads: 1, sends: 1, saves: 1, shares: 0 }
    ],
    interactions_by_type: [],
    surface_mix: [{ surface: 'web_search', count: 4 }],
    top_search_queries: [queryItem()]
  };
}

function queryItem() {
  return { query_key: '0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef', query: 'frog reaction', searches: 9, zero_result_searches: 1, zero_result_rate: 11.1, average_latency_ms: 45, detail_clicks: 3, downloads: 2 };
}

function searchQueries(): AdminAnalyticsSearchQueryPageRead {
  return { range: range(), items: [queryItem()], total: 1, limit: 50, offset: 0 };
}

function audience(): AdminAnalyticsAudienceRead {
  return {
    range: range(),
    metrics: { new_guests: metric(), new_full_accounts: metric(), active_users: metric(), active_guests: metric(), active_full_accounts: metric(), guest_to_full_conversions: metric(), guest_to_full_conversion_rate: metric(4) },
    activity: [],
    surface_mix: [],
    retention_cohorts: [{ cohort_date: '2026-06-30', cohort_size: 3, d1: null, d7: null, d30: null }]
  };
}

function content(): AdminAnalyticsContentRead {
  return {
    range: range(),
    metrics: { catalog_memes: metric(), new_memes: metric(), public_memes: metric(), private_memes: metric(), nsfw_memes: metric(), seo_pages: metric(), active_sources: metric(), new_sources: metric(), source_views: metric(), source_reactions: metric(), source_reposts: metric() },
    catalog_growth: [{ date: '2026-06-01', new_memes: 2 }, { date: '2026-06-02', new_memes: 3 }],
    media_types: [{ key: 'image', count: 4 }],
    languages: [{ key: 'en', count: 3 }],
    visibility: [{ key: 'public', count: 4 }],
    processing: [{ key: 'ready', count: 4 }],
    source_health: [{ key: 'fresh', count: 2 }],
    source_engagement: [
      { date: '2026-06-01', source_views: 5, source_reactions: 1, source_reposts: 0 },
      { date: '2026-06-02', source_views: 8, source_reactions: 2, source_reposts: 1 }
    ]
  };
}
