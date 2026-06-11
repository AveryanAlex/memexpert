import { env } from '$env/dynamic/private';
import { redirect } from '@sveltejs/kit';
import type { PageServerLoad } from './$types';
import { ApiError, fetchMemeDetail } from '$lib/api/client';

export const load: PageServerLoad = async ({ fetch, params, request }) => {
  let meme: Awaited<ReturnType<typeof fetchMemeDetail>>;
  try {
    meme = await fetchMemeDetail({
      fetch,
      baseUrl: apiBaseUrl(),
      memeId: params.id,
      cookieHeader: request.headers.get('cookie') ?? undefined
    });
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

  if (meme.seo_page_slug && params.id !== meme.seo_page_slug) {
    throw redirect(308, `/memes/${meme.seo_page_slug}`);
  }

  return { meme, unavailableMessage: null };
};

function apiBaseUrl(): string {
  return env.API_BASE_URL || 'http://localhost:8000';
}
