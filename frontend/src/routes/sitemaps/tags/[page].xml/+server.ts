import { error } from '@sveltejs/kit';
import type { RequestHandler } from './$types';
import { fetchSeoTags } from '$lib/api/client';
import { apiBaseUrl } from '$lib/server/backend';
import { canonicalPublicOrigin } from '$lib/server/canonicalOrigin';
import { buildTagSitemap, parseSitemapPage, sitemapOffsetForPage, TAG_SITEMAP_SHARD_SIZE, xmlResponse } from '$lib/server/seoXml';

export const GET: RequestHandler = async ({ fetch, params }) => {
  const pageNumber = parseSitemapPage(params.page);
  if (!pageNumber) {
    throw error(404, 'Sitemap page not found.');
  }

  try {
    const page = await fetchSeoTags({
      fetch,
      baseUrl: apiBaseUrl(),
      limit: TAG_SITEMAP_SHARD_SIZE,
      offset: sitemapOffsetForPage(pageNumber, TAG_SITEMAP_SHARD_SIZE)
    });
    return xmlResponse(buildTagSitemap(canonicalPublicOrigin(), page), 900);
  } catch {
    throw error(502, 'Could not load tag sitemap.');
  }
};
