import { proxyMemeAction } from '$lib/server/memeActionProxy';
import { apiBaseUrl } from '$lib/server/backend';
import type { RequestHandler } from './$types';

export const POST: RequestHandler = ({ fetch, params, request }) => {
  return proxyMemeAction({ fetch, request, apiBaseUrl: apiBaseUrl(), memeId: params.meme_id, action: 'save', method: 'POST' });
};

export const DELETE: RequestHandler = ({ fetch, params, request }) => {
  return proxyMemeAction({ fetch, request, apiBaseUrl: apiBaseUrl(), memeId: params.meme_id, action: 'save', method: 'DELETE' });
};
