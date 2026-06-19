import { apiBaseUrl } from '$lib/server/backend';
import { proxyPinReorder } from '$lib/server/pinReorderProxy';
import type { RequestHandler } from './$types';

export const PUT: RequestHandler = ({ fetch, request }) => {
  return proxyPinReorder({ fetch, request, apiBaseUrl: apiBaseUrl() });
};
