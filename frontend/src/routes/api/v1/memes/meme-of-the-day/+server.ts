import { json } from '@sveltejs/kit';

import { ApiError, fetchMemeOfTheDay } from '$lib/api/client';
import { apiBaseUrl, forwardBackendAccessCookie } from '$lib/server/backend';
import type { RequestHandler } from './$types';

export const GET: RequestHandler = async ({ cookies, fetch, request }) => {
  try {
    const memeOfTheDay = await fetchMemeOfTheDay({
      fetch,
      baseUrl: apiBaseUrl(),
      cookieHeader: request.headers.get('cookie') ?? undefined,
      onResponse: (response) => {
        forwardBackendAccessCookie(response, cookies);
      }
    });

    return json(memeOfTheDay);
  } catch (error) {
    const status = error instanceof ApiError ? error.status : 502;
    const detail = error instanceof Error ? error.message : 'Could not reach the meme catalog API.';
    return json({ detail }, { status });
  }
};
