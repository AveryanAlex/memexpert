import { apiBaseUrl } from '$lib/server/backend';
import { proxyMemeAction } from '$lib/server/memeActionProxy';
import type { RequestHandler } from './$types';

export const POST: RequestHandler = ({ fetch, params, request }) => {
  return proxyMemeAction({
    fetch,
    request,
    apiBaseUrl: apiBaseUrl(),
    memeId: params.meme_id,
    action: 'view',
    method: 'POST'
  });
};
