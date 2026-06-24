import { apiBaseUrl } from '$lib/server/backend';
import { proxyTelegramLinkStart } from '$lib/server/authProxy';
import type { RequestHandler } from './$types';

export const POST: RequestHandler = ({ fetch, request }) => {
  return proxyTelegramLinkStart({ fetch, request, apiBaseUrl: apiBaseUrl() });
};
