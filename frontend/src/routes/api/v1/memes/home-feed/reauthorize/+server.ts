import { apiBaseUrl } from '$lib/server/backend';
import { proxyHomeFeedReauthorization } from '$lib/server/homeFeedReauthorizationProxy';
import type { RequestHandler } from './$types';

export const POST: RequestHandler = async ({ fetch, request }) => {
  return proxyHomeFeedReauthorization({
    fetch,
    request,
    apiBaseUrl: apiBaseUrl()
  });
};
