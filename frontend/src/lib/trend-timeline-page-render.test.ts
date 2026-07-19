import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import type { PublicMemeCardRead, PublicTrendTimelinePageRead } from '$lib/api/types';
import TrendTimelinePage from '../routes/trends/timeline/+page.svelte';

describe('/trends/timeline page', () => {
  it('renders month and year browsing controls with a welcoming empty state', () => {
    const { body } = render(TrendTimelinePage, {
      props: {
        data: {
          session: null,
          sessionError: null,
          timeline: timelinePayload({ periods: [], total: 0, has_more: false }),
          granularity: 'month',
          offset: 0,
          errorMessage: null
        }
      }
    });

    expect(body).toContain('Meme timeline.');
    expect(body).toContain('href="/trends/timeline?granularity=month"');
    expect(body).toContain('href="/trends/timeline?granularity=year"');
    expect(body).toMatch(/href="\/trends\/timeline\?granularity=month"[^>]*aria-current="page"/);
    expect(body).not.toMatch(/href="\/trends\/timeline\?granularity=year"[^>]*aria-current="page"/);
    expect(body).toContain('No moments to revisit yet');
    expect(body).toContain('Come back soon to look back at emerging favorites.');
    expect(body).not.toContain('source deltas or platform events');
    expect(body).not.toContain('materialized');
    expect(body).not.toContain('Next periods');
  });

  it('renders a period as a visual look back with friendly activity and pagination', () => {
    const { body } = render(TrendTimelinePage, {
      props: {
        data: {
          session: null,
          sessionError: null,
          timeline: timelinePayload({
            periods: [
              {
                period: '2026-01',
                period_start: '2026-01-01T00:00:00Z',
                meme_count: 1,
                snapshot_count: 2,
                top_memes: [
                  {
                    meme: memeCard(),
                    popularity_score: 17.5,
                    snapshot_count: 2,
                    first_captured_at: '2026-01-01T00:00:00Z',
                    last_captured_at: '2026-01-02T00:00:00Z',
                    source_views: 30,
                    source_reactions: 4,
                    source_reposts: 1,
                    platform_views: 12,
                    platform_sends: 3,
                    platform_saves: 1,
                    platform_likes: 5
                  }
                ]
              }
            ],
            has_more: true,
            total: 2
          }),
          granularity: 'month',
          offset: 0,
          errorMessage: null
        }
      }
    });

    expect(body).toContain('January 2026');
    expect(body).toContain('1 top meme to revisit');
    expect(body).toContain('Timeline reaction');
    expect(body).toContain('Recorded activity adds original-source views, reactions, and reposts');
    expect(body).toContain('Recorded activity');
    expect(body).toContain('56 signals');
    expect(body).toContain('Original sources: 35 · MemeExpert: 21');
    expect(body).not.toContain('5 favorites');
    expect(body).not.toContain('3 sends');
    expect(body).not.toContain('Popularity');
    expect(body).not.toContain('17.5');
    expect(body).not.toContain('Source views');
    expect(body).not.toContain('Snapshots');
    expect(body).toContain('href="/trends/timeline?granularity=month&amp;offset=12"');
    expect(body).toContain('Next periods');
  });
});

function timelinePayload(overrides: Partial<PublicTrendTimelinePageRead> = {}): PublicTrendTimelinePageRead {
  return {
    granularity: 'month',
    periods: [],
    limit: 12,
    offset: 0,
    total: 0,
    has_more: false,
    ...overrides
  };
}

function memeCard(): PublicMemeCardRead {
  return {
    id: '11111111-1111-4111-8111-111111111111',
    media_type: 'image',
    language: 'en',
    is_nsfw: false,
    popularity_score: 17.5,
    like_count: 5,
    tags: ['reaction'],
    primary_file: null,
    caption: 'Timeline reaction',
    seo_page_slug: 'timeline-reaction',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-02T00:00:00Z',
    viewer_has_favorited: false,
    viewer_has_saved: false,
    viewer_has_pinned: false
  };
}
