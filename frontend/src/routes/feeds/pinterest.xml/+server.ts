import { error } from '@sveltejs/kit';
import type { RequestHandler } from './$types';
import { fetchPinterestFeed } from '$lib/api/client';
import { apiBaseUrl } from '$lib/server/backend';
import { canonicalPublicOrigin } from '$lib/server/canonicalOrigin';
import { buildPinterestRss, PINTEREST_FEED_LIMIT, xmlResponse } from '$lib/server/seoXml';

export const GET: RequestHandler = async ({ fetch }) => {
  try {
    const page = await fetchPinterestFeed({
      fetch,
      baseUrl: apiBaseUrl(),
      limit: PINTEREST_FEED_LIMIT,
      offset: 0
    });
    return xmlResponse(buildPinterestRss(canonicalPublicOrigin(), page), 900);
  } catch {
    throw error(502, 'Could not load Pinterest feed.');
  }
};
