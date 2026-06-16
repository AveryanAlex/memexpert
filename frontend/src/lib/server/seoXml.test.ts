import { describe, expect, it } from 'vitest';

import type { PublicMemeFileRead, SeoCatalogMemePageRead, SeoCatalogTagPageRead, SeoCatalogTemplatePageRead } from '$lib/api/types';
import { canonicalPublicOrigin, DEFAULT_PUBLIC_ORIGIN, normalizePublicOrigin } from './canonicalOrigin';
import {
  buildMemeSitemap,
  buildPinterestRss,
  buildRobotsTxt,
  buildSitemapIndex,
  buildStaticSitemap,
  buildTagSitemap,
  buildTemplateSitemap,
  MEME_SITEMAP_SHARD_SIZE,
  sitemapOffsetForPage,
  sitemapPageCount,
  TAG_SITEMAP_SHARD_SIZE,
  TEMPLATE_SITEMAP_SHARD_SIZE
} from './seoXml';

const origin = 'https://memexpert.net';

describe('canonical public origin', () => {
  it('prefers FRONTEND_ORIGIN, then ORIGIN, then the production default', () => {
    expect(canonicalPublicOrigin({ FRONTEND_ORIGIN: 'https://front.example.com///', ORIGIN: 'https://origin.example.com' })).toBe(
      'https://front.example.com'
    );
    expect(canonicalPublicOrigin({ ORIGIN: 'https://origin.example.com/' })).toBe('https://origin.example.com');
    expect(canonicalPublicOrigin({})).toBe(DEFAULT_PUBLIC_ORIGIN);
  });

  it('normalizes only http and https origins', () => {
    expect(normalizePublicOrigin('https://memexpert.net/path/')).toBe('https://memexpert.net');
    expect(normalizePublicOrigin('ftp://memexpert.net')).toBeNull();
    expect(normalizePublicOrigin('not a url')).toBeNull();
  });
});

describe('robots.txt generation', () => {
  it('allows public pages, discourages q spam, blocks private surfaces, and links the sitemap', () => {
    const robots = buildRobotsTxt(origin);

    expect(robots).toContain('Allow: /memes/');
    expect(robots).toContain('Allow: /search$');
    expect(robots).toContain('Disallow: /search?q=');
    expect(robots).toContain('Disallow: /search?*q=');
    expect(robots).toContain('Disallow: /api/');
    expect(robots).toContain('Disallow: /admin/');
    expect(robots).toContain('Disallow: /auth/');
    expect(robots).toContain('Disallow: /account/');
    expect(robots).toContain('Disallow: /profile/');
    expect(robots).toContain('Sitemap: https://memexpert.net/sitemap.xml');
    expect(robots).not.toContain('localhost');
  });
});

describe('sitemap XML generation', () => {
  it('builds a sitemap index with stable one-based shard URLs from summary counts', () => {
    const xml = buildSitemapIndex(origin, {
      public_safe_meme_count: 20_001,
      tag_count: 50_001,
      template_count: 1,
      updated_at: '2026-06-14T09:30:00Z'
    });

    expect(xml).toContain('<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">');
    expect(xml).not.toContain('<urlset');
    expect(xml).toContain('<loc>https://memexpert.net/sitemaps/static.xml</loc>');
    expect(xml).toContain('<loc>https://memexpert.net/sitemaps/memes/1.xml</loc>');
    expect(xml).toContain('<loc>https://memexpert.net/sitemaps/memes/3.xml</loc>');
    expect(xml).toContain('<loc>https://memexpert.net/sitemaps/tags/2.xml</loc>');
    expect(xml).toContain('<loc>https://memexpert.net/sitemaps/templates/1.xml</loc>');
    expect(xml).toContain('<lastmod>2026-06-14T09:30:00.000Z</lastmod>');
    expect(MEME_SITEMAP_SHARD_SIZE).toBe(10_000);
    expect(TAG_SITEMAP_SHARD_SIZE).toBeLessThanOrEqual(50_000);
    expect(TEMPLATE_SITEMAP_SHARD_SIZE).toBeLessThanOrEqual(50_000);
    expect(sitemapPageCount(20_001, MEME_SITEMAP_SHARD_SIZE)).toBe(3);
    expect(sitemapOffsetForPage(3, MEME_SITEMAP_SHARD_SIZE)).toBe(20_000);
  });

  it('builds the static sitemap from public canonical routes only', () => {
    const xml = buildStaticSitemap(origin);

    expect(xml).toContain('<loc>https://memexpert.net/</loc>');
    expect(xml).toContain('<loc>https://memexpert.net/search</loc>');
    expect(xml).toContain('<loc>https://memexpert.net/trends</loc>');
    expect(xml).toContain('<loc>https://memexpert.net/trends/compare</loc>');
    expect(xml).toContain('<loc>https://memexpert.net/trends/timeline</loc>');
    expect(xml).not.toContain('/search?q=');
    expect(xml).not.toContain('/api/');
    expect(xml).not.toContain('/admin/');
    expect(xml).not.toContain('/account/');
    expect(xml).not.toContain('/profile/');
    expect(xml).not.toContain('/collection/');
  });

  it('builds meme sitemap URLs, lastmod, and safe image metadata only when media is present', () => {
    const xml = buildMemeSitemap(origin, {
      items: [
        seoMeme({
          id: '11111111-1111-4111-8111-111111111111',
          seo_slug: 'frog-wizard',
          title: 'Frog & Wizard <meme>',
          alt_text: 'Green frog & magic hat',
          primary_file: file({ display_url: '/media/frog.jpg', mime_type: 'image/jpeg' })
        }),
        seoMeme({
          id: '22222222-2222-4222-8222-222222222222',
          seo_slug: null,
          title: 'Text only meme',
          alt_text: 'Text only meme',
          primary_file: file({ display_url: null, mime_type: 'text/plain' })
        })
      ],
      limit: MEME_SITEMAP_SHARD_SIZE,
      offset: 0,
      total: 2,
      has_more: false
    });

    expect(xml).toContain('xmlns:image="http://www.google.com/schemas/sitemap-image/1.1"');
    expect(xml).toContain('<loc>https://memexpert.net/memes/frog-wizard</loc>');
    expect(xml).toContain('<lastmod>2026-06-14T09:30:00.000Z</lastmod>');
    expect(xml).toContain('<image:loc>https://memexpert.net/media/frog.jpg</image:loc>');
    expect(xml).toContain('<image:title>Frog &amp; Wizard &lt;meme&gt;</image:title>');
    expect(xml).toContain('<image:caption>Green frog &amp; magic hat</image:caption>');
    expect(xml).toContain('<loc>https://memexpert.net/memes/22222222-2222-4222-8222-222222222222</loc>');
    expect(xml).not.toContain('null');
  });

  it('builds tag and template sitemap URLs with lastmod', () => {
    const tagXml = buildTagSitemap(origin, tagPage('reaction'));
    const templateXml = buildTemplateSitemap(origin, templatePage('distracted-boyfriend'));

    expect(tagXml).toContain('<loc>https://memexpert.net/tags/reaction</loc>');
    expect(tagXml).toContain('<lastmod>2026-06-14T09:30:00.000Z</lastmod>');
    expect(templateXml).toContain('<loc>https://memexpert.net/templates/distracted-boyfriend</loc>');
    expect(templateXml).toContain('<lastmod>2026-06-14T09:30:00.000Z</lastmod>');
  });
});

describe('Pinterest RSS generation', () => {
  it('builds valid empty RSS with the media namespace', () => {
    const xml = buildPinterestRss(origin, { items: [], limit: 100, offset: 0, total: 0, has_more: false });

    expect(xml).toContain('<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">');
    expect(xml).toContain('<channel>');
    expect(xml).not.toContain('<item>');
  });

  it('builds RSS items with absolute page/media URLs and enclosure/media fields', () => {
    const xml = buildPinterestRss(origin, {
      items: [
        seoMeme({
          id: '11111111-1111-4111-8111-111111111111',
          seo_slug: 'frog-wizard',
          title: 'Frog & Wizard',
          description: 'A frog in a wizard hat.',
          primary_file: file({
            display_url: '/media/frog.jpg',
            web_video_url: '/media/frog.mp4',
            mime_type: 'video/mp4',
            file_size_bytes: 2048
          })
        })
      ],
      limit: 100,
      offset: 0,
      total: 1,
      has_more: false
    });

    expect(xml).toContain('<title>Frog &amp; Wizard</title>');
    expect(xml).toContain('<description>A frog in a wizard hat.</description>');
    expect(xml).toContain('<link>https://memexpert.net/memes/frog-wizard</link>');
    expect(xml).toContain('<guid isPermaLink="true">https://memexpert.net/memes/frog-wizard</guid>');
    expect(xml).toContain('<pubDate>Sun, 14 Jun 2026 09:30:00 GMT</pubDate>');
    expect(xml).toContain('<enclosure url="https://memexpert.net/media/frog.mp4" length="2048" type="video/mp4" />');
    expect(xml).toContain('<media:content url="https://memexpert.net/media/frog.mp4" medium="video" type="video/mp4" fileSize="2048" width="640" height="480">');
  });
});

function tagPage(slug: string): SeoCatalogTagPageRead {
  return {
    items: [{ slug, title: `${slug} memes`, description: null, meme_count: 1, updated_at: '2026-06-14T09:30:00Z' }],
    limit: TAG_SITEMAP_SHARD_SIZE,
    offset: 0,
    total: 1,
    has_more: false
  };
}

function templatePage(slug: string): SeoCatalogTemplatePageRead {
  return {
    items: [{ slug, name: 'Distracted Boyfriend', title: 'Distracted Boyfriend memes', description: null, meme_count: 1, updated_at: '2026-06-14T09:30:00Z' }],
    limit: TEMPLATE_SITEMAP_SHARD_SIZE,
    offset: 0,
    total: 1,
    has_more: false
  };
}

function seoMeme(overrides: Partial<SeoCatalogMemePageRead['items'][number]> = {}): SeoCatalogMemePageRead['items'][number] {
  return {
    id: '11111111-1111-4111-8111-111111111111',
    seo_slug: 'frog-wizard',
    title: 'Frog wizard',
    description: null,
    alt_text: 'Frog wizard',
    caption: null,
    tags: ['reaction'],
    media_type: 'image',
    language: 'en',
    popularity_score: 1,
    like_count: 2,
    template: null,
    primary_file: null,
    files: [],
    created_at: '2026-06-14T09:30:00Z',
    updated_at: '2026-06-14T09:30:00Z',
    ...overrides
  };
}

type FileOverrides = Partial<NonNullable<PublicMemeFileRead['render']>> & Partial<Omit<PublicMemeFileRead, 'render'>>;

function file(overrides: FileOverrides = {}): PublicMemeFileRead {
  return {
    id: 'file-1',
    mime_type: overrides.mime_type ?? 'image/jpeg',
    width: overrides.width ?? 640,
    height: overrides.height ?? 480,
    file_size_bytes: overrides.file_size_bytes ?? 1024,
    blur_hash: null,
    quality_score: 1,
    render: {
      thumbnail_url: overrides.thumbnail_url ?? null,
      preview_url: overrides.preview_url ?? null,
      display_url: overrides.display_url ?? null,
      original_url: overrides.original_url ?? null,
      download_url: overrides.download_url ?? null,
      web_video_url: overrides.web_video_url ?? null,
      width: overrides.width ?? 640,
      height: overrides.height ?? 480,
      blur_hash: null
    },
    render_url: overrides.render_url ?? null,
    download_url: overrides.download_url ?? null
  };
}
