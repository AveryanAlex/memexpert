import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import type {
  CurrentSessionRead,
  MemeResultAttributionRead,
  PublicMemeCardRead,
  PublicMemeOfTheDayRead,
  PublicMemeSearchPageRead,
} from '$lib/api/types';
import HomePage from '../routes/+page.svelte';

describe('/ page', () => {
  it('renders personalized SSR home feed results through the infinite feed without page links', () => {
    const page: PublicMemeSearchPageRead = {
      items: [
        { meme: memeCard('11111111-1111-4111-8111-111111111111', 'SSR cat reaction'), attribution: attribution(1, 'personalized_recommendations', 'qdrant_preference_vector') },
        { meme: memeCard('22222222-2222-4222-8222-222222222222', 'SSR launch mood'), attribution: attribution(2, 'personalized_recommendations', 'qdrant_preference_vector') },
        { meme: videoMemeCard('33333333-3333-4333-8333-333333333333', 'SSR video mood'), attribution: attribution(3, 'personalized_recommendations', 'qdrant_preference_vector') }
      ],
      limit: 3,
      offset: 0,
      total: 8,
      has_more: true,
      request_id: 'req_home'
    };

    const { body } = render(HomePage, {
      props: {
        data: {
          session: fullSession(),
          sessionError: null,
          page,
          query: '',
          offset: 0,
          feedSource: 'home',
          errorMessage: null,
          memeOfTheDay: null,
          memeOfTheDayErrorMessage: null
        }
      }
    });

    expect(body).toContain('Discover');
    expect(body).toContain('Fresh memes, ready to send.');
    expect(body).toContain('Discover more');
    expect(body).toContain('SSR cat reaction');
    expect(body).toContain('SSR launch mood');
    expect(body).toContain('SSR video mood');
    expect(body).toContain('Favorite');
    expect(body).toContain('Save');
    expect(body).toContain('Send');
    expect(body).toContain('Showing 3 of 8');
    expect(body).toContain('Load more');
    expect(body).toContain('role="list"');
    expect(body).toContain('aria-posinset="1"');
    expect(body).toContain('loading="lazy"');
    expect(body).toContain('preload="none"');
    expect(body).toContain('Actions for SSR cat reaction');
    expect(body).not.toContain('Your collections');
    expect(body).not.toContain('Create collection');
    expect(body).not.toContain('Bulk actions');
    expect(body).not.toContain('Algorithm');
    expect(body).not.toContain('Find the right meme fast.');
    expect(body).not.toContain('action="/search"');
    expect(body).not.toContain('Previous');
    expect(body).not.toContain('Next page');
  });

  it('renders a selected Meme of the Day panel on SSR', () => {
    const body = renderHome(homePageWithAttribution('personalized_recommendations', 'qdrant_preference_vector'), fullSession(), {
      memeOfTheDay: memeOfTheDay()
    });

    expect(body).toContain('Meme of the Day');
    expect(body).toContain('Daily pick reaction');
    expect(body).toContain('Daily pick');
    expect(body.indexOf('Meme of the Day')).toBeLessThan(body.indexOf('Daily pick reaction'));
    expect(body.indexOf('Meme of the Day')).toBeLessThan(body.indexOf('Reactions'));
    expect(body).not.toContain('Selected 2026-06-20');
    expect(body).not.toContain('12 candidates');
    expect(body).not.toContain('Algorithm motd-v1');
  });

  it('keeps MOTD attribution in rendered meme links for telemetry handoff', () => {
    const body = renderHome(homePageWithAttribution('personalized_recommendations', 'qdrant_preference_vector'), fullSession(), {
      memeOfTheDay: memeOfTheDay()
    });

    expect(body).toContain('attribution_source_algorithm=motd');
    expect(body).toContain('attribution_surface=web_home');
    expect(body).toContain('attribution_rank=1');
    expect(body).toContain('data-discovery-source="motd"');
    expect(body).toContain('data-discovery-reason="daily_selection"');
    expect(body).toContain('data-discovery-request-id="req_motd"');
    expect(body).toContain('data-discovery-impression-id="imp_motd"');
    expect(body).toContain('data-discovery-source-meme-id="motd-source-meme"');
    expect(body).toContain('data-discovery-score="0.91"');
  });

  it('renders an empty Meme of the Day state when no candidate is selected', () => {
    const body = renderHome(homePageWithAttribution('personalized_recommendations', 'qdrant_preference_vector'), guestSession(), {
      memeOfTheDay: memeOfTheDay({ meme: null, attribution: null, candidate_count: 0, reason: 'no_candidates' })
    });

    expect(body).toContain('Meme of the Day');
    expect(body).toContain('No Meme of the Day yet');
    expect(body).toContain('Check back soon for a fresh pick.');
    expect(body).not.toContain('did not find an eligible public meme');
    expect(body).not.toContain('0 candidates');
  });

  it('renders a separate Meme of the Day error state', () => {
    const body = renderHome(homePageWithAttribution('personalized_recommendations', 'qdrant_preference_vector'), guestSession(), {
      memeOfTheDayErrorMessage: 'Could not load today\'s pick.'
    });

    expect(body).toContain('Meme of the Day');
    expect(body).toContain('Could not load today\'s pick.');
    expect(body).toContain('Retry');
    expect(body).toContain('SSR fallback reaction');
  });

  it('keeps fallback attribution out of consumer-facing home copy', () => {
    const page = homePageWithAttribution('fallback_trending', 'cold_start_no_positive_signals');

    const full = renderHome(page, fullSession());
    const guest = renderHome(page, guestSession());

    expect(full).toContain('Discover more');
    expect(guest).toContain('Discover more');
    expect(full).not.toContain('Trending while we learn your taste');
    expect(guest).not.toContain('Trending for guests');
    expect(full).toContain('data-discovery-source="fallback_trending"');
  });

  it('does not show backend degradation details from attribution', () => {
    const body = renderHome(homePageWithAttribution('fallback_trending', 'qdrant_failure'), fullSession());

    expect(body).not.toContain('Trending fallback');
    expect(body).not.toContain('Recommendations are temporarily degraded');
  });

  it('renders the home feed empty state', () => {
    const page: PublicMemeSearchPageRead = {
      items: [],
      limit: 12,
      offset: 0,
      total: 0,
      has_more: false,
      request_id: 'req_empty'
    };

    const body = renderHome(page, guestSession());

    expect(body).toContain('Discover more');
    expect(body).toContain('No home feed memes yet');
    expect(body).toContain('Try Search or check back soon.');
    expect(body).toContain('Search memes');
  });
});

interface HomeRenderOptions {
  memeOfTheDay?: PublicMemeOfTheDayRead | null;
  memeOfTheDayErrorMessage?: string | null;
}

function renderHome(page: PublicMemeSearchPageRead, session: CurrentSessionRead, options: HomeRenderOptions = {}): string {
  const { body } = render(HomePage, {
    props: {
      data: {
        session,
        sessionError: null,
        page,
        query: '',
        offset: 0,
        feedSource: 'home',
        errorMessage: null,
        memeOfTheDay: options.memeOfTheDay ?? null,
        memeOfTheDayErrorMessage: options.memeOfTheDayErrorMessage ?? null
      }
    }
  });

  return body;
}

function homePageWithAttribution(sourceAlgorithm: string, reason: string): PublicMemeSearchPageRead {
  return {
    items: [
      {
        meme: memeCard('11111111-1111-4111-8111-111111111111', 'SSR fallback reaction'),
        attribution: attribution(1, sourceAlgorithm, reason)
      }
    ],
    limit: 12,
    offset: 0,
    total: 1,
    has_more: false,
    request_id: 'req_home'
  };
}

function memeOfTheDay(overrides: Partial<PublicMemeOfTheDayRead> = {}): PublicMemeOfTheDayRead {
  return {
    meme: memeCard('55555555-5555-4555-8555-555555555555', 'Daily pick reaction'),
    selected_for: '2026-06-20',
    refreshed_at: '2026-06-20T08:00:00Z',
    algorithm_version: 'motd-v1',
    score: 0.91,
    score_components: { freshness: 0.3, quality: 0.61 },
    reason: 'daily_selection',
    candidate_count: 12,
    attribution: motdAttribution(),
    ...overrides
  };
}

function motdAttribution(): MemeResultAttributionRead {
  return {
    ...attribution(1, 'motd', 'daily_selection'),
    request_id: 'req_motd',
    impression_id: 'imp_motd',
    surface: 'web_home',
    source_meme_id: 'motd-source-meme',
    algorithm_version: 'motd-v1',
    score: 0.91,
    score_components: { freshness: 0.3, quality: 0.61 }
  };
}

function attribution(rank: number, sourceAlgorithm: string, reason: string): MemeResultAttributionRead {
  return {
    request_id: 'req_home',
    impression_id: `imp_${rank}`,
    surface: 'test',
    source_algorithm: sourceAlgorithm,
    rank,
    query: null,
    filters: { language: null, media_type: null, include_nsfw: false, tags: [], scope: 'public', collection_ids: [] },
    collection_scope: 'public',
    collection_ids: [],
    source_meme_id: null,
    algorithm_version: 'test',
    score: null,
    score_components: {},
    reason
  };
}

function fullSession(): CurrentSessionRead {
  return {
    user: {
      id: '33333333-3333-4333-8333-333333333333',
      account_type: 'full',
      telegram_id: null,
      google_id: null,
      email: 'user@example.com',
      email_verified_at: null,
      language: 'en',
      nsfw_enabled: false,
      token_nonce: 1,
      status: 'active',
      guest_expires_at: null,
      active_save_collection_id: '44444444-4444-4444-8444-444444444444',
      is_admin: false,
      created_at: '2026-01-01T00:00:00Z',
      updated_at: '2026-01-01T00:00:00Z'
    },
    linked_providers: {
      email: 'user@example.com',
      email_verified_at: null,
      has_password: false,
      google_linked: false,
      telegram_linked: false
    }
  };
}

function guestSession(): CurrentSessionRead {
  return {
    user: {
      ...fullSession().user,
      account_type: 'guest',
      telegram_id: null,
      email: null,
      guest_expires_at: '2026-07-12T00:00:00Z',
      active_save_collection_id: null,
      is_admin: false
    },
    linked_providers: {
      email: null,
      email_verified_at: null,
      has_password: false,
      google_linked: false,
      telegram_linked: false
    }
  };
}

function memeCard(id: string, caption: string): PublicMemeCardRead {
  return {
    id,
    media_type: 'image',
    language: 'en',
    is_nsfw: false,
    popularity_score: 10,
    like_count: 4,
    tags: ['reaction'],
    primary_file: {
      id: `${id}-file`,
      mime_type: 'image/jpeg',
      width: 640,
      height: 900,
      file_size_bytes: 1234,
      blur_hash: null,
      quality_score: 1,
      render: {
        thumbnail_url: '/thumb.jpg',
        preview_url: '/preview.jpg',
        display_url: '/display.jpg',
        original_url: '/original.jpg',
        download_url: '/download.jpg',
        web_video_url: null,
        width: 640,
        height: 900,
        blur_hash: null
      }
    },
    caption,
    seo_page_slug: null,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    viewer_has_favorited: false,
    viewer_has_saved: false,
    viewer_has_pinned: false
  };
}

function videoMemeCard(id: string, caption: string): PublicMemeCardRead {
  return {
    ...memeCard(id, caption),
    media_type: 'video',
    primary_file: {
      id: `${id}-file`,
      mime_type: 'video/mp4',
      width: null,
      height: null,
      file_size_bytes: 1234,
      blur_hash: null,
      quality_score: 1,
      render: {
        thumbnail_url: '/video-poster.jpg',
        preview_url: null,
        display_url: null,
        original_url: null,
        download_url: '/video-download.mp4',
        web_video_url: '/video.mp4',
        width: null,
        height: null,
        blur_hash: null
      }
    }
  };
}
