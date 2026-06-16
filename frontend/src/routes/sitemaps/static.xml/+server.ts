import type { RequestHandler } from './$types';
import { canonicalPublicOrigin } from '$lib/server/canonicalOrigin';
import { buildStaticSitemap, xmlResponse } from '$lib/server/seoXml';

export const GET: RequestHandler = () => {
  return xmlResponse(buildStaticSitemap(canonicalPublicOrigin()), 3_600);
};
