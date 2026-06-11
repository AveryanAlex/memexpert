import { env } from '$env/dynamic/private';
import type { PageServerLoad } from './$types';
import { ApiError, fetchMemeDetail } from '$lib/api/client';

export const load: PageServerLoad = async ({ fetch, params, request }) => {
  try {
    const meme = await fetchMemeDetail({
      fetch,
      baseUrl: apiBaseUrl(),
      memeId: params.id,
      cookieHeader: request.headers.get('cookie') ?? undefined
    });

    return { meme, unavailableMessage: null };
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      return {
        meme: null,
        unavailableMessage: 'This meme is not available. It may be private, removed, or filtered by safety settings.'
      };
    }

    if (error instanceof ApiError && error.status === 422) {
      return {
        meme: null,
        unavailableMessage: 'This meme link is invalid.'
      };
    }

    return {
      meme: null,
      unavailableMessage: 'Could not reach the meme catalog API.'
    };
  }
};

function apiBaseUrl(): string {
  return env.API_BASE_URL || 'http://localhost:8000';
}
