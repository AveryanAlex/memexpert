import { apiBaseUrl } from '$lib/server/backend';
import { proxyInteractionBatch } from '$lib/server/interactionBatchProxy';
import type { RequestHandler } from './$types';

export const POST: RequestHandler = ({ fetch, request }) => {
  return proxyInteractionBatch({ fetch, request, apiBaseUrl: apiBaseUrl() });
};
