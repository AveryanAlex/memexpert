import { apiBaseUrl } from '$lib/server/backend';
import { proxyPageView } from '$lib/server/pageViewProxy';
import type { RequestHandler } from './$types';

export const POST: RequestHandler = ({ fetch, request }) => {
  return proxyPageView({ fetch, request, apiBaseUrl: apiBaseUrl() });
};
