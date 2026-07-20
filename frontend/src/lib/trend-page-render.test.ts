import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import type {
  MemeResultAttributionRead,
  PublicMemeCardRead,
  PublicTrendMetricsRead
} from '$lib/api/types';
import TrendPage from '../routes/trends/+page.svelte';

describe('/trends page', () => {
  it('renders ranked cards through the shared hydration-stable masonry list', () => {
    const { body } = render(TrendPage, {
      props: {
        data: {
          session: null,
          sessionError: null,
          page: {
            items: [
              {
                meme: memeCard(),
                trend: trendMetrics(),
                attribution: attribution()
              }
            ],
            limit: 12,
            offset: 0,
            total: 2,
            has_more: true,
            request_id: 'trend-request'
          },
          tagSummaries: [],
          templateSummaries: [],
          ranking: 'trending',
          offset: 0,
          errorMessage: null
        }
      }
    });

    expect(body).toContain('aria-label="Trend ranked memes"');
    expect(body).toContain('data-layout="masonry"');
    expect(body).toContain('data-masonry-state="pending"');
    expect(body).toContain('role="listitem"');
    expect(body).toContain('aria-posinset="1"');
    expect(body).toContain('aria-setsize="2"');
    expect(body).toContain('data-exposure-id="trend-impression"');
    expect(body).toContain('Ranked trend meme');
    expect(body).toContain('#1');
    expect(body).not.toContain('aria-label="Enlarge Ranked trend meme"');
  });
});

function attribution(): MemeResultAttributionRead {
  return {
    request_id: 'trend-request',
    impression_id: 'trend-impression',
    surface: 'web_trends',
    source_algorithm: 'trending',
    rank: 1,
    query: null,
    filters: {
      language: null,
      media_type: null,
      include_nsfw: false,
      tags: [],
      scope: 'public',
      collection_ids: []
    },
    collection_scope: 'public',
    collection_ids: [],
    source_meme_id: null,
    algorithm_version: 'test',
    score: 22,
    score_components: { activity: 22 },
    reason: 'trending'
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
    caption: 'Ranked trend meme',
    seo_page_slug: 'ranked-trend-meme',
    viewer_has_favorited: false,
    viewer_has_saved: false,
    viewer_has_pinned: false,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-02T00:00:00Z'
  };
}
