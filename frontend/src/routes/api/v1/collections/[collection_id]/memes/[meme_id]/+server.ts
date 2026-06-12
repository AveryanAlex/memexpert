import { apiBaseUrl } from '$lib/server/backend';
import { proxyCollectionMemeAction } from '$lib/server/collectionMemeProxy';
import type { RequestHandler } from './$types';

export const POST: RequestHandler = ({ fetch, params, request }) => {
  return proxyCollectionMemeAction({
    fetch,
    request,
    apiBaseUrl: apiBaseUrl(),
    collectionId: params.collection_id,
    memeId: params.meme_id,
    method: 'POST'
  });
};

export const DELETE: RequestHandler = ({ fetch, params, request }) => {
  return proxyCollectionMemeAction({
    fetch,
    request,
    apiBaseUrl: apiBaseUrl(),
    collectionId: params.collection_id,
    memeId: params.meme_id,
    method: 'DELETE'
  });
};
