import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import type { PublicMemeCardRead, PublicTrendTimelinePageRead } from '$lib/api/types';
import TrendTimelinePage from '../routes/trends/timeline/+page.svelte';

describe('/trends/timeline page', () => {
  it('renders granularity navigation and the honest empty state', () => {
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
    expect(body).toContain('No timeline data yet');
    expect(body).toContain('real snapshots have been captured');
    expect(body).not.toContain('Next periods');
  });

  it('renders a real snapshot period and pagination state', () => {
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
    expect(body).toContain('1 memes · 2 real snapshots');
    expect(body).toContain('Timeline reaction');
    expect(body).toContain('Popularity');
    expect(body).toContain('17.5');
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
