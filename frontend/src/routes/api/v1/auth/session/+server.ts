import { apiBaseUrl } from '$lib/server/backend';
import { proxyAuthSession } from '$lib/server/authProxy';
import type { RequestHandler } from './$types';

export const GET: RequestHandler = ({ fetch, request }) => {
  return proxyAuthSession({ fetch, request, apiBaseUrl: apiBaseUrl() });
};
