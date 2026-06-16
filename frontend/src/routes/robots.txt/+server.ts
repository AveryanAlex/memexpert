import type { RequestHandler } from './$types';
import { canonicalPublicOrigin } from '$lib/server/canonicalOrigin';
import { buildRobotsTxt, textResponse } from '$lib/server/seoXml';

export const GET: RequestHandler = () => {
  return textResponse(buildRobotsTxt(canonicalPublicOrigin()), 3_600);
};
