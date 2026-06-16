import { error } from '@sveltejs/kit';
import type { RequestHandler } from './$types';
import { fetchSeoMemes } from '$lib/api/client';
import { apiBaseUrl } from '$lib/server/backend';
import { canonicalPublicOrigin } from '$lib/server/canonicalOrigin';
import { buildMemeSitemap, MEME_SITEMAP_SHARD_SIZE, parseSitemapPage, sitemapOffsetForPage, xmlResponse } from '$lib/server/seoXml';

export const GET: RequestHandler = async ({ fetch, params }) => {
  const pageNumber = parseSitemapPage(params.page);
  if (!pageNumber) {
    throw error(404, 'Sitemap page not found.');
  }

  try {
    const page = await fetchSeoMemes({
      fetch,
      baseUrl: apiBaseUrl(),
      limit: MEME_SITEMAP_SHARD_SIZE,
      offset: sitemapOffsetForPage(pageNumber, MEME_SITEMAP_SHARD_SIZE)
    });
    return xmlResponse(buildMemeSitemap(canonicalPublicOrigin(), page), 900);
  } catch {
    throw error(502, 'Could not load meme sitemap.');
  }
};
