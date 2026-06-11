import { env } from '$env/dynamic/private';
import { proxyMemeAction } from '$lib/server/memeActionProxy';
import type { RequestHandler } from './$types';

export const POST: RequestHandler = ({ fetch, params, request }) => {
  return proxyMemeAction({ fetch, request, apiBaseUrl: apiBaseUrl(), memeId: params.meme_id, action: 'favorite', method: 'POST' });
};

export const DELETE: RequestHandler = ({ fetch, params, request }) => {
  return proxyMemeAction({ fetch, request, apiBaseUrl: apiBaseUrl(), memeId: params.meme_id, action: 'favorite', method: 'DELETE' });
};

function apiBaseUrl(): string {
  return env.API_BASE_URL || 'http://localhost:8000';
}
