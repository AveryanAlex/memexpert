import { apiBaseUrl } from '$lib/server/backend';
import { proxySimilarMemes } from '$lib/server/similarMemeProxy';
import type { RequestHandler } from './$types';

export const GET: RequestHandler = ({ cookies, fetch, params, request }) => {
  return proxySimilarMemes({ fetch, request, cookies, apiBaseUrl: apiBaseUrl(), memeId: params.meme_id });
};
