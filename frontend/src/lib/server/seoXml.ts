import type {
  ContentKind,
  PublicMemeFileRead,
  SeoCatalogMemePageRead,
  SeoCatalogMemeRead,
  SeoCatalogSummaryRead,
  SeoCatalogTagPageRead,
  SeoCatalogTemplatePageRead
} from '$lib/api/types';

export const XML_CONTENT_TYPE = 'application/xml; charset=utf-8';
export const TEXT_CONTENT_TYPE = 'text/plain; charset=utf-8';
export const MEME_SITEMAP_SHARD_SIZE = 10_000;
export const TAG_SITEMAP_SHARD_SIZE = 50_000;
export const TEMPLATE_SITEMAP_SHARD_SIZE = 50_000;
export const PINTEREST_FEED_LIMIT = 100;

export const STATIC_SITEMAP_ROUTES = ['/', '/search', '/trends', '/trends/compare', '/trends/timeline'] as const;

interface SitemapEntry {
  loc: string;
  lastmod?: string | null;
}

interface SelectedFeedMedia {
  url: string;
  medium: 'image' | 'video';
  mimeType: string;
  fileSize: number | null;
  width: number | null;
  height: number | null;
}

export function xmlHeaders(cacheSeconds = 300): HeadersInit {
  return {
    'cache-control': `public, max-age=${cacheSeconds}`,
    'content-type': XML_CONTENT_TYPE
  };
}

export function textHeaders(cacheSeconds = 300): HeadersInit {
  return {
    'cache-control': `public, max-age=${cacheSeconds}`,
    'content-type': TEXT_CONTENT_TYPE
  };
}

export function xmlResponse(body: string, cacheSeconds?: number): Response {
  return new Response(body, { headers: xmlHeaders(cacheSeconds) });
}

export function textResponse(body: string, cacheSeconds?: number): Response {
  return new Response(body, { headers: textHeaders(cacheSeconds) });
}

export function buildRobotsTxt(origin: string): string {
  return [
    'User-agent: *',
    'Allow: /',
    'Allow: /memes/',
    'Allow: /tags/',
    'Allow: /templates/',
    'Allow: /trends',
    'Allow: /search$',
    'Disallow: /search?q=',
    'Disallow: /search?*q=',
    'Disallow: /*?q=',
    'Disallow: /api/',
    'Disallow: /admin/',
    'Disallow: /auth/',
    'Disallow: /account/',
    'Disallow: /profile/',
    'Disallow: /collection/',
    'Disallow: /collections/',
    'Disallow: /internal/',
    'Disallow: /private/',
    `Sitemap: ${absoluteUrl(origin, '/sitemap.xml')}`,
    ''
  ].join('\n');
}

export function buildSitemapIndex(origin: string, summary: SeoCatalogSummaryRead): string {
  const entries: SitemapEntry[] = [
    { loc: absoluteUrl(origin, '/sitemaps/static.xml'), lastmod: summary.updated_at },
    ...sitemapShardEntries(origin, '/sitemaps/memes', summary.public_safe_meme_count, MEME_SITEMAP_SHARD_SIZE, summary.updated_at),
    ...sitemapShardEntries(origin, '/sitemaps/tags', summary.tag_count, TAG_SITEMAP_SHARD_SIZE, summary.updated_at),
    ...sitemapShardEntries(origin, '/sitemaps/templates', summary.template_count, TEMPLATE_SITEMAP_SHARD_SIZE, summary.updated_at)
  ];

  return xmlDocument([
    '<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ...entries.map((entry) => sitemapIndexEntryXml(entry)),
    '</sitemapindex>'
  ]);
}

export function buildStaticSitemap(origin: string): string {
  return buildUrlset(
    STATIC_SITEMAP_ROUTES.map((path) => ({ loc: absoluteUrl(origin, path) }))
  );
}

export function buildMemeSitemap(origin: string, page: SeoCatalogMemePageRead): string {
  return buildUrlset(
    page.items.map((item) => ({ loc: memePageUrl(origin, item), lastmod: item.updated_at })),
    page.items.map((item) => imageXmlForMeme(origin, item))
  );
}

export function buildTagSitemap(origin: string, page: SeoCatalogTagPageRead): string {
  return buildUrlset(
    page.items.map((item) => ({
      loc: absoluteUrl(origin, `/tags/${encodeURIComponent(item.slug)}`),
      lastmod: item.updated_at
    }))
  );
}

export function buildTemplateSitemap(origin: string, page: SeoCatalogTemplatePageRead): string {
  return buildUrlset(
    page.items.map((item) => ({
      loc: absoluteUrl(origin, `/templates/${encodeURIComponent(item.slug)}`),
      lastmod: item.updated_at
    }))
  );
}

export function buildPinterestRss(origin: string, page: SeoCatalogMemePageRead): string {
  return xmlDocument([
    '<rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">',
    '<channel>',
    '<title>MemeXpert Pinterest Feed</title>',
    '<description>Public safe MemeXpert memes for Pinterest ingestion.</description>',
    `<link>${xmlEscape(absoluteUrl(origin, '/'))}</link>`,
    ...page.items.map((item) => pinterestItemXml(origin, item)),
    '</channel>',
    '</rss>'
  ]);
}

export function sitemapPageCount(total: number, shardSize: number): number {
  return Math.max(1, Math.ceil(Math.max(0, total) / shardSize));
}

export function sitemapOffsetForPage(page: number, shardSize: number): number {
  return (page - 1) * shardSize;
}

export function parseSitemapPage(value: string): number | null {
  if (!/^\d+$/.test(value)) {
    return null;
  }

  const page = Number.parseInt(value, 10);
  return page > 0 ? page : null;
}

export function absoluteUrl(origin: string, path: string): string {
  const trimmedOrigin = origin.replace(/\/+$/, '');
  if (/^https?:\/\//i.test(path)) {
    return path;
  }
  return `${trimmedOrigin}${path.startsWith('/') ? path : `/${path}`}`;
}

export function xmlEscape(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&apos;');
}

export function formatXmlDate(value: string | null | undefined): string | null {
  const date = parseDate(value);
  return date ? date.toISOString() : null;
}

export function formatRssDate(value: string | null | undefined): string | null {
  const date = parseDate(value);
  return date ? date.toUTCString() : null;
}

function sitemapShardEntries(origin: string, basePath: string, total: number, shardSize: number, lastmod: string | null): SitemapEntry[] {
  return Array.from({ length: sitemapPageCount(total, shardSize) }, (_, index) => ({
    loc: absoluteUrl(origin, `${basePath}/${index + 1}.xml`),
    lastmod
  }));
}

function sitemapIndexEntryXml(entry: SitemapEntry): string {
  const lastmod = formatXmlDate(entry.lastmod);
  return [
    '<sitemap>',
    `<loc>${xmlEscape(entry.loc)}</loc>`,
    lastmod ? `<lastmod>${lastmod}</lastmod>` : '',
    '</sitemap>'
  ].filter(Boolean).join('');
}

function buildUrlset(entries: SitemapEntry[], imageXml: Array<string | null> = []): string {
  const hasImages = imageXml.some(Boolean);
  return xmlDocument([
    `<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"${hasImages ? ' xmlns:image="http://www.google.com/schemas/sitemap-image/1.1"' : ''}>`,
    ...entries.map((entry, index) => sitemapUrlEntryXml(entry, imageXml[index] ?? null)),
    '</urlset>'
  ]);
}

function sitemapUrlEntryXml(entry: SitemapEntry, nestedImageXml: string | null): string {
  const lastmod = formatXmlDate(entry.lastmod);
  return [
    '<url>',
    `<loc>${xmlEscape(entry.loc)}</loc>`,
    lastmod ? `<lastmod>${lastmod}</lastmod>` : '',
    nestedImageXml ?? '',
    '</url>'
  ].filter(Boolean).join('');
}

function memePageUrl(origin: string, item: SeoCatalogMemeRead): string {
  const slugOrId = item.seo_slug?.trim() || item.id;
  return absoluteUrl(origin, `/memes/${encodeURIComponent(slugOrId)}`);
}

function imageXmlForMeme(origin: string, item: SeoCatalogMemeRead): string | null {
  const title = cleanText(item.title);
  const altText = cleanText(item.alt_text);
  if (!title || !altText) {
    return null;
  }

  const url = selectImageSitemapUrl(origin, item);
  if (!url) {
    return null;
  }

  return [
    '<image:image>',
    `<image:loc>${xmlEscape(url)}</image:loc>`,
    `<image:title>${xmlEscape(title)}</image:title>`,
    `<image:caption>${xmlEscape(altText)}</image:caption>`,
    '</image:image>'
  ].join('');
}

function pinterestItemXml(origin: string, item: SeoCatalogMemeRead): string {
  const pageUrl = memePageUrl(origin, item);
  const title = cleanText(item.title) ?? 'MemeXpert meme';
  const description = cleanText(item.description) ?? cleanText(item.caption) ?? cleanText(item.alt_text) ?? title;
  const pubDate = formatRssDate(item.created_at) ?? formatRssDate(item.updated_at);
  const media = selectPinterestMedia(origin, item);

  return [
    '<item>',
    `<title>${xmlEscape(title)}</title>`,
    `<description>${xmlEscape(description)}</description>`,
    `<link>${xmlEscape(pageUrl)}</link>`,
    `<guid isPermaLink="true">${xmlEscape(pageUrl)}</guid>`,
    pubDate ? `<pubDate>${pubDate}</pubDate>` : '',
    media ? enclosureXml(media) : '',
    media ? mediaContentXml(media, title, description) : '',
    '</item>'
  ].filter(Boolean).join('');
}

function enclosureXml(media: SelectedFeedMedia): string {
  return `<enclosure url="${xmlEscape(media.url)}" length="${media.fileSize ?? 0}" type="${xmlEscape(media.mimeType)}" />`;
}

function mediaContentXml(media: SelectedFeedMedia, title: string, description: string): string {
  const attrs = [
    `url="${xmlEscape(media.url)}"`,
    `medium="${media.medium}"`,
    `type="${xmlEscape(media.mimeType)}"`,
    media.fileSize ? `fileSize="${media.fileSize}"` : '',
    media.width ? `width="${media.width}"` : '',
    media.height ? `height="${media.height}"` : ''
  ].filter(Boolean).join(' ');

  return [
    `<media:content ${attrs}>`,
    `<media:title>${xmlEscape(title)}</media:title>`,
    `<media:description>${xmlEscape(description)}</media:description>`,
    '</media:content>'
  ].join('');
}

function selectImageSitemapUrl(origin: string, item: SeoCatalogMemeRead): string | null {
  for (const file of candidateFiles(item)) {
    const url = imageUrlFromFile(file, origin);
    if (url) {
      return url;
    }
  }
  return null;
}

function selectPinterestMedia(origin: string, item: SeoCatalogMemeRead): SelectedFeedMedia | null {
  for (const file of candidateFiles(item)) {
    const render = file.render;
    const videoUrl = toSafeAbsoluteHttpUrl(render?.web_video_url, origin);
    if (videoUrl) {
      return {
        url: videoUrl,
        medium: 'video',
        mimeType: mediaMimeType(file.mime_type, item.media_type, 'video'),
        fileSize: positiveInteger(file.file_size_bytes),
        ...mediaDimensions(file)
      };
    }

    const imageUrl = imageUrlFromFile(file, origin);
    if (imageUrl) {
      return {
        url: imageUrl,
        medium: 'image',
        mimeType: mediaMimeType(file.mime_type, item.media_type, 'image'),
        fileSize: positiveInteger(file.file_size_bytes),
        ...mediaDimensions(file)
      };
    }
  }

  return null;
}

function imageUrlFromFile(file: PublicMemeFileRead, origin: string): string | null {
  const render = file.render;
  const candidates = file.mime_type?.startsWith('image/')
    ? [render?.display_url, render?.preview_url, render?.thumbnail_url, render?.original_url, file.render_url]
    : [render?.display_url, render?.preview_url, render?.thumbnail_url];

  for (const candidate of candidates) {
    const url = toSafeAbsoluteHttpUrl(candidate, origin);
    if (url) {
      return url;
    }
  }

  return null;
}

function candidateFiles(item: SeoCatalogMemeRead): PublicMemeFileRead[] {
  const files = item.primary_file ? [item.primary_file, ...item.files] : item.files;
  const seen = new Set<string>();
  return files.filter((file) => {
    if (seen.has(file.id)) {
      return false;
    }
    seen.add(file.id);
    return true;
  });
}

function mediaMimeType(mimeType: string | null, kind: ContentKind, medium: 'image' | 'video'): string {
  if (mimeType?.startsWith(`${medium}/`)) {
    return mimeType;
  }

  if (kind === 'gif') {
    return 'image/gif';
  }
  if (medium === 'video') {
    return 'video/mp4';
  }
  return 'image/jpeg';
}

function mediaDimensions(file: PublicMemeFileRead): { width: number | null; height: number | null } {
  return {
    width: positiveInteger(file.render?.width) ?? positiveInteger(file.width),
    height: positiveInteger(file.render?.height) ?? positiveInteger(file.height)
  };
}

function positiveInteger(value: number | null | undefined): number | null {
  return Number.isFinite(value) && value && value > 0 ? Math.trunc(value) : null;
}

function toSafeAbsoluteHttpUrl(value: string | null | undefined, origin: string): string | null {
  const trimmed = cleanText(value);
  if (!trimmed) {
    return null;
  }

  try {
    const url = new URL(trimmed, origin);
    return url.protocol === 'http:' || url.protocol === 'https:' ? url.href : null;
  } catch {
    return null;
  }
}

function parseDate(value: string | null | undefined): Date | null {
  if (!value) {
    return null;
  }

  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function cleanText(value: string | null | undefined): string | null {
  const cleaned = value?.trim();
  return cleaned || null;
}

function xmlDocument(lines: string[]): string {
  return `<?xml version="1.0" encoding="UTF-8"?>\n${lines.join('\n')}\n`;
}
