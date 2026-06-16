import { error } from '@sveltejs/kit';
import type { RequestHandler } from './$types';
import { fetchSeoSummary } from '$lib/api/client';
import { apiBaseUrl } from '$lib/server/backend';
import { canonicalPublicOrigin } from '$lib/server/canonicalOrigin';
import { buildSitemapIndex, xmlResponse } from '$lib/server/seoXml';

export const GET: RequestHandler = async ({ fetch }) => {
  try {
    const summary = await fetchSeoSummary({ fetch, baseUrl: apiBaseUrl() });
    return xmlResponse(buildSitemapIndex(canonicalPublicOrigin(), summary), 900);
  } catch {
    throw error(502, 'Could not load SEO sitemap summary.');
  }
};
