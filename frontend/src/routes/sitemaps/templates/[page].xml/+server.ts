import { error } from '@sveltejs/kit';
import type { RequestHandler } from './$types';
import { fetchSeoTemplates } from '$lib/api/client';
import { apiBaseUrl } from '$lib/server/backend';
import { canonicalPublicOrigin } from '$lib/server/canonicalOrigin';
import { buildTemplateSitemap, parseSitemapPage, sitemapOffsetForPage, TEMPLATE_SITEMAP_SHARD_SIZE, xmlResponse } from '$lib/server/seoXml';

export const GET: RequestHandler = async ({ fetch, params }) => {
  const pageNumber = parseSitemapPage(params.page);
  if (!pageNumber) {
    throw error(404, 'Sitemap page not found.');
  }

  try {
    const page = await fetchSeoTemplates({
      fetch,
      baseUrl: apiBaseUrl(),
      limit: TEMPLATE_SITEMAP_SHARD_SIZE,
      offset: sitemapOffsetForPage(pageNumber, TEMPLATE_SITEMAP_SHARD_SIZE)
    });
    return xmlResponse(buildTemplateSitemap(canonicalPublicOrigin(), page), 900);
  } catch {
    throw error(502, 'Could not load template sitemap.');
  }
};
