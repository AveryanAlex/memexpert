import { apiBaseUrl } from '$lib/server/backend';
import { proxyMemeCollectionChoices } from '$lib/server/memeCollectionChoicesProxy';
import type { RequestHandler } from './$types';

export const GET: RequestHandler = ({ fetch, params, request }) => {
  return proxyMemeCollectionChoices({
    fetch,
    request,
    apiBaseUrl: apiBaseUrl(),
    memeId: params.meme_id
  });
};
