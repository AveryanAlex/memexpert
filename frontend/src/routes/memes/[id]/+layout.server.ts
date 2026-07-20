import { redirect } from '@sveltejs/kit';
import type { LayoutServerLoad } from './$types';
import {
  ApiError,
  fetchMemeDetail
} from '$lib/api/client';
import { apiBaseUrl } from '$lib/server/backend';

export const load: LayoutServerLoad = async ({ fetch, params, request }) => {
  const cookieHeader = request.headers.get('cookie') ?? undefined;
  let meme: Awaited<ReturnType<typeof fetchMemeDetail>>;

  try {
    meme = await fetchMemeDetail({
      fetch,
      baseUrl: apiBaseUrl(),
      memeId: params.id,
      cookieHeader
    });
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) {
      return unavailableDetail('This meme is not available. It may be private, removed, or filtered by safety settings.');
    }

    if (error instanceof ApiError && error.status === 422) {
      return unavailableDetail('This meme link is invalid.');
    }

    return unavailableDetail('Could not reach the meme catalog API.');
  }

  if (meme.seo_page_slug && params.id !== meme.seo_page_slug) {
    throw redirect(308, `/memes/${meme.seo_page_slug}${new URL(request.url).search}`);
  }

  return {
    meme,
    unavailableMessage: null
  };
};

function unavailableDetail(unavailableMessage: string) {
  return {
    meme: null,
    unavailableMessage
  };
}
