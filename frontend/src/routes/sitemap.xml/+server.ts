import { error } from '@sveltejs/kit';
import type { RequestHandler } from './$types';
import { apiBaseUrl } from '$lib/server/backend';
import { canonicalPublicOrigin } from '$lib/server/canonicalOrigin';
import { fetchCachedSeoSummary } from '$lib/server/seoSummary';
import { buildSitemapIndex, xmlResponse } from '$lib/server/seoXml';

export const GET: RequestHandler = async ({ fetch }) => {
  try {
    const summary = await fetchCachedSeoSummary(fetch, apiBaseUrl());
    return xmlResponse(buildSitemapIndex(canonicalPublicOrigin(), summary), 900);
  } catch {
    throw error(502, 'Could not load SEO sitemap summary.');
  }
};
