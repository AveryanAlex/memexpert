import { apiBaseUrl } from '$lib/server/backend';
import { proxyMemePage } from '$lib/server/memePageProxy';
import type { RequestHandler } from './$types';

export const GET: RequestHandler = ({ cookies, fetch, request }) => {
  return proxyMemePage({ fetch, request, cookies, apiBaseUrl: apiBaseUrl(), mode: 'search' });
};
