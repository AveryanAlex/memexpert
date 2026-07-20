import { render } from 'svelte/server';
import { describe, expect, it } from 'vitest';

import type {
  PublicMemeCardRead,
  PublicMemeDetailRead,
  PublicMemeLandingRead,
  PublicMemePopularitySummaryRead,
  PublicMemeSearchResultRead,
  PublicTrendMetricsRead
} from '$lib/api/types';
import { buildMemeDetailView, buildRelatedDiscovery, normalizeMemeDisplayText } from '$lib/meme-detail-view';
import MemeDetailPage from '../routes/memes/[id]/+page.svelte';
import TagLandingPage from '../routes/tags/[tag]/+page.svelte';
import TemplateLandingPage from '../routes/templates/[slug]/+page.svelte';

describe('/memes/[id] page', () => {
  it('keeps SEO metadata while suppressing normalized duplicate visible text', () => {
    const captionFallback = buildMemeDetailView(
      memeDetail({
        tags: [],
        seo_title: null,
        seo_description: null,
        caption: '  SAME   Caption  ',
        seo_body_text: 'same caption',
        ocr_text: 'ＳＡＭＥ Caption'
      })
    );

    expect(captionFallback.title).toBe('SAME   Caption');
    expect(captionFallback.metaDescription).toBe('SAME   Caption');
    expect(captionFallback.leadDescription).toBeNull();
    expect(captionFallback.bodyText).toBeNull();
    expect(captionFallback.detectedText).toBeNull();

    const ocrFallback = buildMemeDetailView(
      memeDetail({ tags: [], seo_title: null, seo_description: null, caption: null, seo_body_text: null, ocr_text: 'ship it' })
    );
    expect(ocrFallback.title).toBe('Meme detail');
    expect(ocrFallback.metaDescription).toBe('ship it');
    expect(ocrFallback.leadDescription).toBeNull();
    expect(ocrFallback.detectedText).toBe('ship it');
  });

  it('skips blank titles and uses deterministic locale-independent Unicode normalization', () => {
    const captionTitle = buildMemeDetailView(
      memeDetail({ seo_title: '   ', caption: 'Caption fallback', ocr_text: 'OCR should stay behind About' })
    );
    const noVisibleTitle = buildMemeDetailView(
      memeDetail({ tags: [], seo_title: '\t', caption: '  ', seo_description: null, ocr_text: 'OCR metadata fallback' })
    );
    const tagTitle = buildMemeDetailView(
      memeDetail({ tags: ['classic reaction'], seo_title: '\t', caption: '  ', seo_description: null })
    );

    expect(captionTitle.title).toBe('Caption fallback');
    expect(captionTitle.detectedText).toBe('OCR should stay behind About');
    expect(noVisibleTitle.title).toBe('Meme detail');
    expect(noVisibleTitle.metaDescription).toBe('OCR metadata fallback');
    expect(noVisibleTitle.leadDescription).toBeNull();
    expect(noVisibleTitle.detectedText).toBe('OCR metadata fallback');
    expect(tagTitle.title).toBe('classic reaction');
    expect(normalizeMemeDisplayText('  I  İ  ı  i  ')).toBe('i i̇ ı i');
  });

  it('renders SEO content, media-first actions, progressive context, and related discovery', () => {
    const meme = memeDetail({
      seo_title: 'Launch day reaction meme',
      seo_description: 'A polished caption for sharing this launch reaction.',
      seo_body_text: 'Use this when the deploy finally goes green.',
      ocr_text: 'ship it',
      tags: ['reaction', 'launch'],
      download_url: 'https://cdn.example.test/memes/launch.jpg',
      popularity_score: 42.5
    });
    const related = memeCard('22222222-2222-4222-8222-222222222222', 'Another reaction meme');

    const { body, head } = render(MemeDetailPage, {
      props: {
        data: {
          ...emptyInsightsData(),
          session: null,
          sessionError: null,
          attribution: null,
          meme,
          popularity: popularitySummary(meme.id),
          relatedSource: {
            kind: 'similar',
            page: {
              items: [result(meme, 1, 'qdrant_similarity'), result(related, 2, 'qdrant_similarity')],
              limit: 7,
              offset: 0,
              total: 2,
              has_more: false,
              request_id: 'req_detail'
            }
          },
          unavailableMessage: null
        },
        form: null
      }
    });

    expect(head).toContain('Launch day reaction meme | MemeXpert');
    expect(head).toContain('A polished caption for sharing this launch reaction.');
    expect(body).toContain('https://cdn.example.test/memes/display.jpg');
    expect(body).toContain('Launch day reaction meme');
    expect(body).toContain('Use this when the deploy finally goes green.');
    expect(body).toContain('About this meme');
    expect(body).toContain('<details');
    expect(body).toContain('Text detected in the image');
    expect(body).toContain('ship it');
    expect(body).toContain('#reaction');
    expect(body).toContain('Favorite');
    expect(body).toContain('Save');
    expect(body).toContain('Send');
    expect(body).toContain('34 views');
    expect(body).toContain('Related memes');
    expect(body).not.toContain('Media and file info');
    expect(body).not.toContain('Only fields exposed by the public meme detail API');
    expect(body).not.toContain('image/jpeg');
    expect(body).not.toContain('score 42.5');
    expect(body).not.toContain('source image embedding');
    expect(body).toContain('data-discovery-source="qdrant_similarity"');
    expect(body).toContain('data-discovery-request-id="req_detail"');
    expect(body).toContain('data-discovery-source-meme-id="11111111-1111-4111-8111-111111111111"');
    expect(body).toContain('attribution_request_id=req_detail');
    expect(body).toContain('attribution_source_algorithm=qdrant_similarity');
    expect(body).toContain('Another reaction meme');
  });

  it('renders a consumer-friendly discovery fallback without technical diagnostics', () => {
    const meme = memeDetail({
      tags: [],
      seo_title: null,
      seo_description: null,
      seo_body_text: null,
      caption: 'When there are no tags',
      ocr_text: null,
      download_url: null,
      primary_file: null,
      files: []
    });

    const { body } = render(MemeDetailPage, {
      props: {
        data: {
          ...emptyInsightsData(),
          session: null,
          sessionError: null,
          attribution: null,
          meme,
          popularity: null,
          relatedSource: {
            kind: 'trending',
            items: [result(memeCard('33333333-3333-4333-8333-333333333333', 'Trending fallback meme'), 1, 'legacy_trending')]
          },
          unavailableMessage: null
        },
        form: null
      }
    });

    expect(body).toContain('When there are no tags');
    expect(body).toContain('Related memes');
    expect(body).not.toContain('No public file metadata is available');
    expect(body).not.toContain('Download is unavailable until the catalog exposes a media download URL.');
    expect(body).not.toContain('Popularity analytics are not available for this meme yet.');
    expect(body).not.toContain('similar-memes endpoint and tag fallback were unavailable');
    expect(body).toContain('Trending fallback meme');
  });

  it('keeps the Telegram connection prompt after a guest save', () => {
    const meme = memeDetail();

    const { body } = render(MemeDetailPage, {
      props: {
        data: {
          ...emptyInsightsData(),
          session: null,
          sessionError: null,
          attribution: null,
          meme,
          popularity: null,
          relatedSource: null,
          unavailableMessage: null
        },
        form: { status: 'saved', message: 'Saved to favorites.', showConnectTelegram: true }
      }
    });

    expect(body).toContain('Keep this save beyond this browser.');
    expect(body).toContain('Connect Telegram to keep saves/favorites');
  });

  it('builds related discovery from attributed similar and fallback results', () => {
    const current = memeDetail({ tags: ['reaction'] });
    const related = memeCard('44444444-4444-4444-8444-444444444444', 'Different meme');
    const backfill = memeCard('55555555-5555-4555-8555-555555555555', 'Backfill meme');

    const similarDiscovery = buildRelatedDiscovery(current, {
      kind: 'similar',
      page: { items: [result(related, 1, 'qdrant_similarity'), result(backfill, 2, 'fallback_tag', 'similarity_backfill')], limit: 7, offset: 0, total: 2, has_more: false, request_id: 'req_detail' }
    });
    const tagDiscovery = buildRelatedDiscovery(current, { kind: 'tag', tag: 'reaction', items: [result(current), result(related)] });
    const trendDiscovery = buildRelatedDiscovery(current, { kind: 'trending', items: [result(current), result(related)] });

    expect(similarDiscovery.heading).toBe('Similar memes');
    expect(similarDiscovery.memes).toEqual([related, backfill]);
    expect(similarDiscovery.attributions[backfill.id].source_algorithm).toBe('fallback_tag');
    expect(tagDiscovery.memes).toEqual([related]);
    expect(tagDiscovery.heading).toBe('More from #reaction');
    expect(tagDiscovery.description).toContain('similar-memes endpoint was unavailable');
    expect(trendDiscovery.memes).toEqual([related]);
    expect(trendDiscovery.description).toContain('tag fallback were unavailable');
  });
});

function emptyInsightsData() {
  return {
    analytics: null,
    analyticsError: null,
    insightsParams: {
      sourceSort: 'views_desc' as const,
      sourceOffset: 0,
      sourceSnapshot: null,
      analyticsWindow: '30d' as const
    },
    insightsUrl: { pathname: '/memes/launch-reaction', search: '' },
    sourceError: null,
    sourcePage: null
  };
}

describe('tag and template discovery pages', () => {
  it('puts tag and template galleries ahead of aggregate popularity details', () => {
    const tagMeme = memeCard('66666666-6666-4666-8666-666666666666', 'Gallery-first tag meme');
    const templateMeme = memeCard('77777777-7777-4777-8777-777777777777', 'Gallery-first template meme');
    const tagLanding = landing('tag', 'reaction', 'Reaction memes', tagMeme);
    const templateLanding = landing('template', 'distracted-boyfriend', 'Distracted boyfriend memes', templateMeme);

    const { body: tagBody } = render(TagLandingPage, {
      props: { data: { session: null, sessionError: null, landing: tagLanding, offset: 0, errorMessage: null } }
    });
    const { body: templateBody } = render(TemplateLandingPage, {
      props: { data: { session: null, sessionError: null, landing: templateLanding, offset: 0, errorMessage: null } }
    });

    expect(tagBody).toContain('Gallery-first tag meme');
    expect(tagBody).toContain('aria-label="Tagged memes"');
    expect(tagBody).toContain('About this tag');
    expect(tagBody).toContain("1 memes help shape this tag's recent popularity.");
    expect(tagBody).toContain('34 views');
    expect(tagBody).toContain('href="/search"');
    expect(tagBody.indexOf('Gallery-first tag meme')).toBeLessThan(tagBody.indexOf('About this tag'));
    expect(tagBody).not.toContain('No materialized trend data');

    expect(templateBody).toContain('Gallery-first template meme');
    expect(templateBody).toContain('aria-label="Template memes"');
    expect(templateBody).toContain('About this template');
    expect(templateBody).toContain("1 memes help shape this template's recent popularity.");
    expect(templateBody).toContain('34 views');
    expect(templateBody).toContain('href="/search"');
    expect(templateBody.indexOf('Gallery-first template meme')).toBeLessThan(templateBody.indexOf('About this template'));
    expect(templateBody).not.toContain('No materialized trend data');
  });

  it('preserves offset pagination links for both taxonomy routes', () => {
    const tagLanding = landing(
      'tag',
      'reaction',
      'Reaction memes',
      memeCard('88888888-8888-4888-8888-888888888888', 'Paginated tag meme')
    );
    const templateLanding = landing(
      'template',
      'distracted-boyfriend',
      'Distracted boyfriend memes',
      memeCard('99999999-9999-4999-8999-999999999999', 'Paginated template meme')
    );

    for (const landingPage of [tagLanding, templateLanding]) {
      landingPage.page = { ...landingPage.page, limit: 20, offset: 40, total: 61, has_more: true };
    }

    const { body: tagBody } = render(TagLandingPage, {
      props: { data: { session: null, sessionError: null, landing: tagLanding, offset: 40, errorMessage: null } }
    });
    const { body: templateBody } = render(TemplateLandingPage, {
      props: { data: { session: null, sessionError: null, landing: templateLanding, offset: 40, errorMessage: null } }
    });

    for (const body of [tagBody, templateBody]) {
      expect(body).toContain('Showing 41-41 of 61');
      expect(body).toContain('href="?offset=20"');
      expect(body).toContain('href="?offset=60"');
      expect(body.indexOf('Previous')).toBeLessThan(body.indexOf('Next page'));
    }
  });
});

function result(
  meme: PublicMemeCardRead,
  rank = 1,
  sourceAlgorithm = 'fallback_tag',
  reason = sourceAlgorithm
): PublicMemeSearchResultRead {
  return {
    meme,
    attribution: {
      request_id: 'req_detail',
      impression_id: `imp_${rank}`,
      surface: 'public_api_meme_similar',
      source_algorithm: sourceAlgorithm,
      rank,
      query: null,
      filters: { language: null, media_type: null, include_nsfw: false, tags: [], scope: 'public', collection_ids: [] },
      collection_scope: 'public',
      collection_ids: [],
      source_meme_id: '11111111-1111-4111-8111-111111111111',
      algorithm_version: 'test',
      score: 0.9,
      score_components: { total: 0.9 },
      reason
    }
  };
}

function memeDetail(overrides: Partial<PublicMemeDetailRead> = {}): PublicMemeDetailRead {
  return {
    id: '11111111-1111-4111-8111-111111111111',
    media_type: 'image',
    language: 'en',
    is_nsfw: false,
    popularity_score: 9.25,
    like_count: 4,
    tags: ['reaction'],
    primary_file: {
      id: 'file-1',
      mime_type: 'image/jpeg',
      width: 640,
      height: 480,
      file_size_bytes: 123456,
      blur_hash: null,
      quality_score: 0.95,
      render: {
        thumbnail_url: 'https://cdn.example.test/memes/thumb.jpg',
        preview_url: 'https://cdn.example.test/memes/preview.jpg',
        display_url: 'https://cdn.example.test/memes/display.jpg',
        original_url: 'https://cdn.example.test/memes/original.jpg',
        download_url: 'https://cdn.example.test/memes/download.jpg',
        web_video_url: null,
        width: 640,
        height: 480,
        blur_hash: null
      },
      render_url: 'https://cdn.example.test/memes/display.jpg',
      download_url: 'https://cdn.example.test/memes/download.jpg'
    },
    caption: 'Fallback caption',
    seo_page_slug: 'launch-day-reaction',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-02T00:00:00Z',
    render_url: 'https://cdn.example.test/memes/display.jpg',
    download_url: null,
    viewer_has_favorited: false,
    viewer_has_saved: false,
    viewer_has_pinned: false,
    ocr_text: null,
    seo_title: null,
    seo_description: null,
    seo_alt_text: null,
    seo_body_text: null,
    seo_model_id: null,
    seo_prompt_version: null,
    seo_generated_at: null,
    files: [],
    ...overrides
  };
}

function memeCard(id: string, caption: string): PublicMemeCardRead {
  return {
    id,
    media_type: 'image',
    language: 'en',
    is_nsfw: false,
    popularity_score: 1,
    like_count: 2,
    tags: ['reaction'],
    primary_file: null,
    caption,
    seo_page_slug: null,
    viewer_has_favorited: false,
    viewer_has_saved: false,
    viewer_has_pinned: false,
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z'
  };
}

function popularitySummary(memeId: string): PublicMemePopularitySummaryRead {
  return {
    meme_id: memeId,
    trend: trendMetrics(),
    sparkline: [
      {
        captured_at: '2026-01-01T00:00:00Z',
        source_views: 10,
        source_reactions: 1,
        source_reposts: 0,
        platform_views: 20,
        platform_sends: 2,
        platform_saves: 3,
        platform_likes: 4,
        popularity_score: 5
      },
      {
        captured_at: '2026-01-02T00:00:00Z',
        source_views: 12,
        source_reactions: 2,
        source_reposts: 1,
        platform_views: 34,
        platform_sends: 3,
        platform_saves: 4,
        platform_likes: 5,
        popularity_score: 7
      }
    ]
  };
}

function trendMetrics(): PublicTrendMetricsRead {
  return {
    recent: { views: 34, sends: 3, likes: 5, saves: 4, downloads: 2 },
    previous: { views: 20, sends: 2, likes: 4, saves: 3, downloads: 1 },
    latest_snapshot_at: '2026-01-02T00:00:00Z',
    latest_source_views: 12,
    latest_source_reactions: 2,
    latest_source_reposts: 1,
    latest_platform_views: 34,
    latest_platform_sends: 3,
    latest_platform_saves: 4,
    latest_platform_likes: 5,
    latest_popularity_score: 7,
    engagement_24h: 12,
    trending_score: 8.5,
    refreshed_at: '2026-01-02T00:00:00Z'
  };
}

function landing(
  kind: 'tag' | 'template',
  slug: string,
  title: string,
  meme: PublicMemeCardRead
): PublicMemeLandingRead {
  return {
    kind,
    slug,
    title,
    description: `Browse ${title.toLowerCase()}.`,
    page: {
      items: [result(meme)],
      limit: 20,
      offset: 0,
      total: 1,
      has_more: false,
      request_id: `req_${kind}`
    },
    trend_summary: {
      kind,
      slug,
      title,
      description: null,
      meme_count: 1,
      trend: trendMetrics(),
      points: []
    }
  };
}
